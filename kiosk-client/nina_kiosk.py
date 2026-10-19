#!/usr/bin/env python3
"""Nina kiosk client: Nina as a boot-time service on the kitchen kiosk instead of a browser tab.

It does what the web page does (live/static/index.html) without a browser: captures the microphone at 16 kHz, streams it to the Nina voice server over one
WebSocket, and plays the 24 kHz speech the server sends back. The server decides when Nina is asleep or awake (the wake word is still matched on the server for now),
so this client just keeps streaming. It reconnects forever with backoff, drops the first seconds of every microphone start (the capture device's resume burst), and
has no UI. Run it as a systemd user service (nina-kiosk.service).

Protocol (same as the web page):  client -> server: binary frames of 16 kHz mono int16 (640 samples), JSON {t: cfg|wake|sleep|stop|text}.
                                  server -> client: binary = <u32 LE sample rate> + int16 mono speech; JSON {t: hello|mgr|cancel|duck|earcon|assistant|stt|...}.
Config: ~/.config/nina-kiosk/config.json (see config.example.json); the service token is read from a file and never logged.
    nina_kiosk.py [--config PATH] [--devices] [--debug]
"""
import argparse
import asyncio
import collections
import hashlib
import json
import logging
import os
import signal
import ssl
import struct
import sys
import time

import numpy as np

log = logging.getLogger("nina-kiosk")
MIC_RATE = 16000
FRAME = 640                      # samples per mic frame (40 ms), what the web page sends
DEFAULTS = {
    "server": "https://nina.local:8443",
    "token_file": "~/.config/nina-kiosk/service_token",
    "cert_sha256": "",                    # pin the server's self-signed certificate (hex sha256 of the DER); empty = do not verify (LAN only)
    "wake": "hey_nina",
    "input_device": None,                 # PortAudio device name or index; None = default (use the PipeWire echo-cancel source, see pipewire-echo-cancel.conf)
    "output_device": None,
    "warmup_s": 2.5,                      # microphone audio dropped after every (re)start: the device's resume burst is fully clipped
    "mic_gain": 1.0,
    "volume": 1.0,
    "duck_gain": 0.12,
    "reconnect_min_s": 2.0,
    "reconnect_max_s": 30.0,
}


def load_config(path):
    cfg = dict(DEFAULTS)
    p = os.path.expanduser(path)
    if os.path.exists(p):
        cfg.update({k: v for k, v in json.load(open(p)).items() if k in DEFAULTS})
    return cfg


def read_token(path):
    try:
        return open(os.path.expanduser(path)).read().strip()
    except OSError:
        return ""


def ws_url(server):
    s = server.rstrip("/")
    s = ("wss://" + s[len("https://"):]) if s.startswith("https://") else ("ws://" + s[len("http://"):]) if s.startswith("http://") else s
    return s + "/ws"


def backoff(attempt, lo, hi):
    """Seconds to wait before reconnect attempt number `attempt` (0-based): lo, 2*lo, 4*lo ... capped at hi."""
    return min(hi, lo * (2 ** max(0, attempt)))


class Framer:
    """Re-cuts whatever block sizes the audio callback delivers into exact 640-sample int16 frames."""
    def __init__(self, n=FRAME):
        self.n, self.buf = n, np.zeros(0, np.int16)

    def feed(self, block: np.ndarray):
        self.buf = np.concatenate([self.buf, block.astype(np.int16, copy=False)])
        while len(self.buf) >= self.n:
            out, self.buf = self.buf[:self.n], self.buf[self.n:]
            yield out.tobytes()


class Warmup:
    """Drops the first `seconds` of microphone audio after a start (counted in samples, not wall time, so a stalled callback cannot shorten it)."""
    def __init__(self, seconds, rate=MIC_RATE):
        self.left = int(seconds * rate)

    def restart(self, seconds, rate=MIC_RATE):
        self.left = int(seconds * rate)

    def __call__(self, block: np.ndarray):
        if self.left <= 0:
            return block
        k = min(self.left, len(block))
        self.left -= k
        return block[k:]


class Playback:
    """Speech from the server: a sample queue the audio callback drains. cancel() empties it (interruption), duck() lowers the level while the user talks over Nina."""
    def __init__(self, out_rate=24000, volume=1.0, duck_gain=0.12):
        self.rate, self.volume, self.duck_gain = out_rate, volume, duck_gain
        self.q = collections.deque()
        self.n = 0
        self.ducked = False

    def push(self, rate, pcm: bytes):
        a = np.frombuffer(pcm[:len(pcm) // 2 * 2], np.int16)
        if rate != self.rate and len(a):
            x = np.arange(int(len(a) * self.rate / rate)) * (rate / self.rate)
            a = np.interp(x, np.arange(len(a)), a.astype(np.float32)).astype(np.int16)
        if len(a):
            self.q.append(a)
            self.n += len(a)

    def tone(self, freqs, each=0.09, level=0.18):
        for f in freqs:
            t = np.arange(int(each * self.rate)) / self.rate
            env = np.minimum(1, np.minimum(t, each - t) / 0.01)
            self.push(self.rate, (np.sin(2 * np.pi * f * t) * env * level * 32767).astype(np.int16).tobytes())

    def cancel(self):
        self.q.clear()
        self.n = 0
        self.ducked = False

    def duck(self, on):
        self.ducked = bool(on)

    def pending_s(self):
        return self.n / self.rate

    def fill(self, frames: int) -> np.ndarray:
        """Exactly `frames` int16 samples: queued speech, then silence."""
        out = np.zeros(frames, np.int16)
        i = 0
        while i < frames and self.q:
            a = self.q[0]
            k = min(len(a), frames - i)
            out[i:i + k] = a[:k]
            i += k
            if k == len(a):
                self.q.popleft()
            else:
                self.q[0] = a[k:]
        self.n = max(0, self.n - i)
        g = self.volume * (self.duck_gain if self.ducked else 1.0)
        return out if g == 1.0 else np.clip(out.astype(np.float32) * g, -32768, 32767).astype(np.int16)


EARCONS = {"wake": (660, 880), "sleep": (660, 440)}


class Client:
    def __init__(self, cfg):
        self.cfg = cfg
        self.play = Playback(24000, cfg["volume"], cfg["duck_gain"])
        self.warm = Warmup(cfg["warmup_s"])
        self.framer = Framer()
        self.q = None                     # asyncio.Queue of mic frames, created in run()
        self.loop = None
        self.connected = False
        self.stopping = False
        self.muted = False
        self.dropped = 0

    # ---- audio threads (PortAudio callbacks): hand everything to the event loop, never block
    def _mic_cb(self, indata, frames, t, status):
        if status:
            log.debug("mic status %s", status)
        a = np.frombuffer(indata, np.int16)
        if self.cfg["mic_gain"] != 1.0:
            a = np.clip(a.astype(np.float32) * self.cfg["mic_gain"], -32768, 32767).astype(np.int16)
        a = self.warm(a)
        if not len(a) or self.muted or not self.connected:
            return
        for f in self.framer.feed(a):
            self.loop.call_soon_threadsafe(self._offer, f)

    def _offer(self, frame):
        if self.q.qsize() > 100:          # 4 s behind: the link is stalled; drop rather than lag behind real time
            self.dropped += 1
            return
        self.q.put_nowait(frame)

    def _spk_cb(self, outdata, frames, t, status):
        outdata[:] = self.play.fill(frames).tobytes()

    # ---- server messages
    def on_json(self, m):
        t = m.get("t")
        if t == "hello":
            log.info("connected: tts=%s stt=%s", m.get("tts"), m.get("stt"))
        elif t == "mgr":
            log.info("session %s (wake: %s)", m.get("state"), m.get("wake"))
        elif t == "cancel":
            self.play.cancel()
        elif t == "duck":
            self.play.duck(m.get("on"))
        elif t == "earcon" and m.get("kind") in EARCONS:
            self.play.tone(EARCONS[m["kind"]])
        elif t in ("assistant", "stt"):
            log.debug("%s: %s", t, (m.get("text") or "")[:80])          # transcripts only at debug level

    async def session(self, http, url, headers, sslctx):
        async with http.ws_connect(url, headers=headers, ssl=sslctx, heartbeat=20, max_msg_size=1 << 22) as ws:
            self.connected = True
            self.warm.restart(self.cfg["warmup_s"])
            self.framer = Framer()
            while not self.q.empty():
                self.q.get_nowait()
            if self.cfg["wake"]:
                await ws.send_str(json.dumps({"t": "cfg", "wake": self.cfg["wake"]}))

            async def pump_mic():
                while True:
                    await ws.send_bytes(await self.q.get())

            task = asyncio.ensure_future(pump_mic())
            try:
                async for msg in ws:
                    if msg.type == 2:                                    # BINARY: <u32 rate> + int16 speech
                        if len(msg.data) > 4:
                            self.play.push(struct.unpack("<I", msg.data[:4])[0], msg.data[4:])
                    elif msg.type == 1:                                  # TEXT
                        try:
                            self.on_json(json.loads(msg.data))
                        except ValueError:
                            pass
                    else:
                        break
            finally:
                task.cancel()
                self.connected = False
                self.play.cancel()

    async def run(self):
        import aiohttp
        import sounddevice as sd
        self.loop = asyncio.get_running_loop()
        self.q = asyncio.Queue()
        url = ws_url(self.cfg["server"])
        token = read_token(self.cfg["token_file"])
        if not token:
            log.error("no service token in %s: the server will refuse the connection", self.cfg["token_file"])
        headers = {"X-Nina-Service": token}
        sslctx = False
        if url.startswith("wss://"):
            sslctx = aiohttp.Fingerprint(bytes.fromhex(self.cfg["cert_sha256"])) if self.cfg["cert_sha256"] else False
        inp = sd.RawInputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=320, device=self.cfg["input_device"], callback=self._mic_cb)
        out = sd.RawOutputStream(samplerate=self.play.rate, channels=1, dtype="int16", blocksize=480, device=self.cfg["output_device"], callback=self._spk_cb)
        attempt = 0
        with inp, out:
            log.info("audio running (mic %s, speaker %s); server %s", inp.device, out.device, url)
            async with aiohttp.ClientSession() as http:
                while not self.stopping:
                    t0 = time.monotonic()
                    try:
                        await self.session(http, url, headers, sslctx)
                        log.warning("server closed the connection")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:                                # noqa: BLE001 - any failure: wait and reconnect
                        log.warning("connection failed: %s", type(e).__name__ + (": " + str(e)[:80] if str(e) else ""))
                    attempt = 0 if time.monotonic() - t0 > 60 else attempt + 1       # a long-lived session resets the backoff
                    if self.stopping:
                        break
                    await asyncio.sleep(backoff(attempt - 1, self.cfg["reconnect_min_s"], self.cfg["reconnect_max_s"]))

    def stop(self, *_):
        self.stopping = True
        for t in asyncio.all_tasks(self.loop):
            t.cancel()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="~/.config/nina-kiosk/config.json")
    ap.add_argument("--devices", action="store_true", help="list audio devices and exit")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if a.devices:
        import sounddevice as sd
        print(sd.query_devices())
        return 0
    cfg = load_config(a.config)
    c = Client(cfg)

    async def go():
        c.loop = asyncio.get_running_loop()
        for s in (signal.SIGTERM, signal.SIGINT):
            c.loop.add_signal_handler(s, c.stop)
        try:
            await c.run()
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    return 0


if __name__ == "__main__":
    sys.exit(main())
