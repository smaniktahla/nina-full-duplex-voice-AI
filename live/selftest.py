#!/usr/bin/env python3
"""Headless end-to-end check: a fake microphone (piper-synthesised speech, real-time pacing) talks to the live server over
WSS, so the whole loop (STT -> controller -> llama -> TTS audio back -> barge-in cancel) is exercised without a browser."""
import asyncio, json, ssl, sys, time
import aiohttp, numpy as np
from scipy.signal import resample_poly
from piper import PiperVoice

URL = sys.argv[1] if len(sys.argv) > 1 else "wss://localhost:8443/ws"
v = PiperVoice.load("voices/en_US-lessac-medium.onnx")

def say(text):
    pcm = b"".join(c.audio_int16_bytes for c in v.synthesize(text))
    a = np.frombuffer(pcm, np.int16)
    return resample_poly(a, 16000, v.config.sample_rate).astype(np.int16)

async def main():
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time(); ev = []
    def stamp(*a): print("%6.2fs" % (time.time() - t0), *a, flush=True)
    async with aiohttp.ClientSession() as s, s.ws_connect(URL, ssl=ctx, max_msg_size=1 << 22) as ws:
        audio_bytes = {"n": 0, "last": 0.0}
        async def reader():
            async for m in ws:
                if m.type == aiohttp.WSMsgType.BINARY:
                    audio_bytes["n"] += len(m.data) - 4; audio_bytes["last"] = time.time() - t0
                elif m.type == aiohttp.WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d["t"] in ("stt",) and d["kind"] == "final": stamp("STT final  :", d["text"])
                    elif d["t"] == "assistant": stamp("ASSISTANT  :", d["text"])
                    elif d["t"] == "cancel": stamp("CANCEL     :", d["why"]); ev.append(("cancel", time.time() - t0)); audio_bytes["at_cancel"] = audio_bytes["n"]
                    elif d["t"] == "state": stamp("state      :", d["state"])
        rd = asyncio.ensure_future(reader())
        async def mic(pcm16):
            for i in range(0, len(pcm16), 640):
                await ws.send_bytes(pcm16[i:i + 640].tobytes()); await asyncio.sleep(0.04)
        async def silence(sec):
            z = np.zeros(640, np.int16)
            for _ in range(int(sec / 0.04)): await ws.send_bytes(z.tobytes()); await asyncio.sleep(0.04)
        await ws.send_str(json.dumps({"t": "cfg", "wake": "off"}))        # selftest exercises the duplex engine, not the wake word
        await silence(1.0)
        stamp(">> user: What is the capital of Australia?"); await mic(say("What is the capital of Australia?")); await silence(5.0)
        stamp("audio bytes so far:", audio_bytes["n"]); n1 = audio_bytes["n"]
        stamp(">> user: Explain how a heat pump works in detail."); await mic(say("Explain how a heat pump works in detail.")); 
        # wait for assistant audio to start, then interrupt
        t = time.time()
        while audio_bytes["n"] == n1 and time.time() - t < 8: await silence(0.2)
        stamp("assistant audio started; letting it talk 1.5s"); await silence(1.5)
        n_before = audio_bytes["n"]; tcut = time.time() - t0
        stamp(">> user barges in: wait hold on"); await mic(say("Wait, hold on."))
        await silence(4.0)
        cancels = [c for c in ev if c[1] >= tcut]
        stamp("RESULT barge-in cancel latency after user started: %s" % ("%.2fs" % (cancels[0][1] - tcut) if cancels else "NO CANCEL"))
        await silence(3.0)
        stamp("audio bytes received AFTER the cancel message:", audio_bytes["n"] - audio_bytes.get("at_cancel", audio_bytes["n"]), "(0 = nothing obsolete was sent)")
        rd.cancel()
asyncio.run(main())
