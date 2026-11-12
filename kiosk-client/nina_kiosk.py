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
    "chrome_debug": "http://127.0.0.1:9222",  # Chrome remote-debugging endpoint on this machine: draws the live listening/speaking waveform over the page. Empty = no indicator
    "mic_stall_s": 15.0,                  # no microphone callbacks for this long (PipeWire restarted, device unplugged): exit so systemd restarts us with fresh audio streams
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
        self.alarm_active = False
        self.alarm_pos = 0
        self.alarm_pcm = np.zeros(int(1.5 * self.rate), np.int16)
        for i, hz in enumerate((880, 660, 880)):
            t = np.arange(int(0.22 * self.rate)) / self.rate
            env = np.minimum(1, np.minimum(t, 0.22 - t) / 0.02)
            start = int(i * 0.25 * self.rate)
            self.alarm_pcm[start:start + len(t)] = (np.sin(2 * np.pi * hz * t) * env * 0.2 * 32767).astype(np.int16)

    def alarm(self, active):
        active = bool(active)
        if active and not self.alarm_active:
            self.alarm_pos = 0
        self.alarm_active = active

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
        if self.alarm_active:
            indices = (np.arange(frames) + self.alarm_pos) % len(self.alarm_pcm)
            self.alarm_pos = (self.alarm_pos + frames) % len(self.alarm_pcm)
            out = np.clip(out.astype(np.int32) + self.alarm_pcm[indices], -32768, 32767).astype(np.int16)
        g = self.volume * (self.duck_gain if self.ducked else 1.0)
        return out if g == 1.0 else np.clip(out.astype(np.float32) * g, -32768, 32767).astype(np.int16)



# ---------------------------------------------------------------- "Nina is listening / speaking" waveform over whatever page Chrome is showing
# The overlay is a canvas injected through Chrome's local DevTools endpoint. The client pushes the live audio level (microphone while she listens, her own
# speech while she talks) ~30 times a second and the page draws it: blue = listening, orange = speaking. It removes itself if the pushes stop.
OVERLAY_ID = "nina-wave-overlay"
WAVE_JS = r"""(function(){
var id='__ID__';if(window.__ninaWave&&window.__ninaWave.c&&window.__ninaWave.c.isConnected)return;
var c=document.createElement('canvas');c.id=id;
c.style.cssText='position:fixed;left:0;right:0;bottom:0;width:100vw;height:300px;z-index:2147483647;pointer-events:none;opacity:0;transition:opacity .3s';
document.documentElement.appendChild(c);requestAnimationFrame(function(){c.style.opacity='1';});
var x=c.getContext('2d'),dpr=window.devicePixelRatio||1,w=0,h=0;
function size(){w=c.clientWidth;h=c.clientHeight;c.width=w*dpr;c.height=h*dpr;x.setTransform(dpr,0,0,dpr,0,0);}size();window.addEventListener('resize',size);
var COL={listen:[58,160,255],speak:[255,159,67]};
var S=window.__ninaWave={c:c,mode:'listen',target:0,lvl:0,col:COL.listen.slice(),ph:0,t:0,last:performance.now(),
  push:function(m,l){S.mode=m;S.target=l;S.last=performance.now();}};
var hist=[];for(var i=0;i<256;i++)hist.push(0);var hacc=0;
function frame(t){
  if(!c.isConnected||window.__ninaWave!==S)return;
  if(t-S.last>5000){c.remove();window.__ninaWave=null;return;}
  if(t-S.t<32){requestAnimationFrame(frame);return;}                           // capped at ~30 fps: a weak GPU/CPU does not need to draw more than the level is pushed                 // the client stopped pushing: never leave the glow stuck on screen
  var dt=Math.min(.05,(t-S.t)/1000||0);S.t=t;
  var tgt=t-S.last>1200?0:S.target;S.lvl+=(tgt-S.lvl)*Math.min(1,dt*(tgt>S.lvl?22:7));      // fast attack, slower release: follows the voice, not the jitter
  var k=Math.min(1,dt*6);for(var i=0;i<3;i++)S.col[i]+=(COL[S.mode][i]-S.col[i])*k;
  S.ph+=dt*(1.2+S.lvl*5);hacc+=dt;if(hacc>.03){hacc=0;hist.shift();hist.push(S.lvl);}
  x.clearRect(0,0,w,h);x.globalCompositeOperation='lighter';
  var mid=h/2,r=Math.round(S.col[0]),g=Math.round(S.col[1]),b=Math.round(S.col[2]),n=hist.length;
  for(var L=0;L<5;L++){
    x.beginPath();
    for(var px=0;px<=w;px+=6){
      var u=px/w,env=Math.pow(Math.sin(Math.PI*u),.8);
      var hv=hist[Math.min(n-1,Math.floor(u*(n-1)))];                           // the recent past of the real signal shapes the line left to right
      var amp=Math.min(1.1,.06+1.5*(.5*S.lvl+.5*hv));
      var v=Math.sin(u*10+S.ph*(1+L*.35)+L*1.7)*.55+Math.sin(u*19-S.ph*(1.3+L*.2)+L)*.3+Math.sin(u*33+S.ph*2.1+L*2.3)*.15;
      var y=mid+v*env*amp*(mid-12)*(L?.5+L*.12:1);
      px?x.lineTo(px,y):x.moveTo(px,y);
    }
    x.lineWidth=L?1.5:3;x.strokeStyle='rgba('+r+','+g+','+b+','+(L?.5:.95)+')';
    x.shadowColor='rgba('+r+','+g+','+b+',.9)';x.shadowBlur=L?0:6;x.stroke();                      // glow only on the main line, and small: shadowBlur is the expensive part to draw
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
})()""".replace("__ID__", OVERLAY_ID)
OVERLAY_OFF = "(function(){var W=window.__ninaWave;if(W&&W.c)W.c.remove();window.__ninaWave=null;var e=document.getElementById('%s');if(e)e.remove();})()" % OVERLAY_ID


def push_js(mode, level):
    return "window.__ninaWave&&window.__ninaWave.push('%s',%.3f)" % ("speak" if mode == "speak" else "listen", max(0.0, min(1.0, float(level))))


def level_of(pcm):
    """0..1 loudness of an int16 block: -55 dBFS and below is 0, -10 dBFS and above is 1 (speech through the echo-cancelled mic sits around -40..-20)."""
    a = np.asarray(pcm, dtype=np.float32)
    if not a.size:
        return 0.0
    rms = float(np.sqrt(np.mean(a * a))) / 32768.0
    return max(0.0, min(1.0, (20.0 * np.log10(rms + 1e-6) + 55.0) / 45.0))


def indicator_wanted(state):
    """The waveform shows while Nina is awake and taking a request (session state ACTIVE); idle, dormant and suspended hide it."""
    return str(state or "").lower() == "active"


class Indicator:
    """Keeps one DevTools connection per page tab in the kiosk Chrome and pushes the live level into the overlay. Best effort: failures are logged once, never touch audio."""
    PUSH_S = 1 / 30
    INJECT_EVERY_S = 2.0                                           # re-inject periodically: a page reload loses the overlay

    def __init__(self, endpoint):
        self.endpoint = (endpoint or "").rstrip("/")
        self.mode = None                                           # None (hidden), "listen" or "speak"
        self.level = 0.0
        self.http = None
        self._task = None
        self._warned = False
        self._conns = {}                                           # tab id -> {ws, reader, injected_at}

    def start(self, http):
        if self.endpoint:
            self.http = http
            self._task = asyncio.ensure_future(self._run())

    def set(self, mode, level=None):
        self.mode = "listen" if mode is True else (mode if mode in ("listen", "speak") else None)
        if level is not None:
            self.level = level

    async def _tabs(self):
        import aiohttp
        async with self.http.get(self.endpoint + "/json/list", timeout=aiohttp.ClientTimeout(total=2)) as r:
            return {t["id"]: t["webSocketDebuggerUrl"] for t in await r.json(content_type=None) if t.get("type") == "page" and t.get("webSocketDebuggerUrl")}

    async def _drain(self, ws):
        try:
            async for _ in ws:                                     # responses are not needed, but unread messages would pile up
                pass
        except Exception:                                          # noqa: BLE001
            pass

    async def _drop(self, tid):
        c = self._conns.pop(tid, None)
        if c:
            c["reader"].cancel()
            try:
                await c["ws"].close()
            except Exception:                                      # noqa: BLE001
                pass

    async def _send(self, tid, url, expression):
        c = self._conns.get(tid)
        if c is None or c["ws"].closed:
            await self._drop(tid)
            ws = await self.http.ws_connect(url, max_msg_size=1 << 22, timeout=aiohttp_ws_timeout(2))
            c = self._conns[tid] = {"ws": ws, "reader": asyncio.ensure_future(self._drain(ws)), "injected_at": 0.0, "n": 0}
        c["n"] += 1
        await c["ws"].send_str(json.dumps({"id": c["n"], "method": "Runtime.evaluate", "params": {"expression": expression}}))
        return c

    async def _run(self):
        tabs, tabs_at, hidden = {}, 0.0, True
        while True:
            try:
                await asyncio.sleep(self.PUSH_S if self.mode else 0.25)
                now = time.monotonic()
                if now - tabs_at > 1.0 and (self.mode or not hidden or self._conns):
                    tabs, tabs_at = await self._tabs(), now
                    for tid in [t for t in self._conns if t not in tabs]:
                        await self._drop(tid)
                if self.mode:
                    hidden = False
                    for tid, url in tabs.items():
                        c = self._conns.get(tid)
                        if c is None or now - c["injected_at"] > self.INJECT_EVERY_S:
                            c = await self._send(tid, url, WAVE_JS)
                            c["injected_at"] = now
                        await self._send(tid, url, push_js(self.mode, self.level))
                elif not hidden:
                    for tid, url in tabs.items():
                        await self._send(tid, url, OVERLAY_OFF)
                    await asyncio.sleep(0.1)
                    for tid in list(self._conns):
                        await self._drop(tid)
                    hidden = True
                self._warned = False
            except asyncio.CancelledError:
                for tid in list(self._conns):
                    await self._drop(tid)
                raise
            except Exception as e:                                 # noqa: BLE001  the indicator must never affect audio
                if not self._warned:
                    log.warning("indicator unavailable: %s", type(e).__name__ + (": " + str(e)[:80] if str(e) else ""))
                    self._warned = True
                for tid in list(self._conns):
                    await self._drop(tid)
                await asyncio.sleep(1.0)

    def stop(self):
        if self._task:
            self._task.cancel()


def aiohttp_ws_timeout(s):
    import aiohttp
    return aiohttp.ClientWSTimeout(ws_close=s)


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
        self.last_mic = time.monotonic()
        self.indicator = Indicator(cfg.get("chrome_debug"))
        self.awake = False                # session state ACTIVE, from the server
        self._mic_peak = 0.0              # loudest block since the indicator last looked (written by the audio threads, read and reset by feed_indicator)
        self._out_peak = 0.0
        self._speak_until = 0.0

    # ---- audio threads (PortAudio callbacks): hand everything to the event loop, never block
    def _mic_cb(self, indata, frames, t, status):
        if status:
            log.debug("mic status %s", status)
        self.last_mic = time.monotonic()
        a = np.frombuffer(indata, np.int16)
        if self.cfg["mic_gain"] != 1.0:
            a = np.clip(a.astype(np.float32) * self.cfg["mic_gain"], -32768, 32767).astype(np.int16)
        a = self.warm(a)
        if len(a) and self.awake:
            self._mic_peak = max(self._mic_peak, level_of(a))
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
        blk = self.play.fill(frames)
        outdata[:] = blk.tobytes()
        if self.awake and not self.play.alarm_active:
            lv = level_of(blk)
            if lv > 0.05:                  # her own speech is playing (silence between words keeps it on for a moment, see feed_indicator)
                self._out_peak = max(self._out_peak, lv)
                self._speak_until = time.monotonic() + 0.5

    # ---- server messages
    def on_json(self, m):
        t = m.get("t")
        if t == "hello":
            log.info("connected: tts=%s stt=%s", m.get("tts"), m.get("stt"))
        elif t == "mgr":
            log.info("session %s (wake: %s)", m.get("state"), m.get("wake"))
            self.awake = indicator_wanted(m.get("state"))
            if not self.awake:
                self.indicator.set(None)
        elif t == "timer_alarm":
            self.play.alarm(m.get("active", False))
            log.info("timer alarm %s", "ringing" if self.play.alarm_active else "dismissed")
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
                self.awake = False
                self.indicator.set(None)
                self.connected = False
                self.play.alarm(False)
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
        self.last_mic = time.monotonic()
        asyncio.ensure_future(self.watchdog())
        with inp, out:
            log.info("audio running (mic %s, speaker %s); server %s", inp.device, out.device, url)
            async with aiohttp.ClientSession() as http:
                self.indicator.start(http)
                feed = asyncio.ensure_future(self.feed_indicator())
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
                feed.cancel()
                self.indicator.set(None)
                self.indicator.stop()

    async def feed_indicator(self):
        """~30 times a second: the live level of whatever is happening. Her voice while she speaks (orange), otherwise the microphone (blue)."""
        while True:
            await asyncio.sleep(Indicator.PUSH_S)
            if not self.awake:
                continue
            if time.monotonic() < self._speak_until:
                self.indicator.set("speak", self._out_peak)
            else:
                self.indicator.set("listen", self._mic_peak)
            self._mic_peak = self._out_peak = 0.0

    async def watchdog(self):
        """Audio streams do not recover by themselves when PipeWire restarts: if the microphone callback goes quiet, exit non-zero and let systemd restart the service."""
        while not self.stopping:
            await asyncio.sleep(2)
            if time.monotonic() - self.last_mic > self.cfg["mic_stall_s"]:
                log.error("no microphone audio for %.0f s: exiting so the service restarts", time.monotonic() - self.last_mic)
                os._exit(3)

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
