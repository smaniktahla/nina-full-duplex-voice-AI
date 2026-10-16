#!/usr/bin/env python3
"""Does the distant-voice filter ignore a quiet 'kid in another room' while accepting the near speaker? Synthetic mic."""
import asyncio, json, ssl, sys, time
import aiohttp, numpy as np
from scipy.signal import resample_poly
from piper import PiperVoice
import os

def _svc_headers():
    """Server-side scripts authenticate with the service token (live/auth.py)."""
    try:
        return {"X-Nina-Service": open(os.path.join(os.path.expanduser(os.environ.get("NINA_AUTH_DIR", "~/.config/nina")), "service_token")).read().strip()}
    except OSError:
        return {}


_H = _svc_headers()

v = PiperVoice.load("voices/en_US-lessac-medium.onnx")
def say(t, gain=1.0):
    a = np.frombuffer(b"".join(c.audio_int16_bytes for c in v.synthesize(t)), np.int16)
    return (resample_poly(a, 16000, v.config.sample_rate) * gain).astype(np.int16)
async def main():
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    async with aiohttp.ClientSession() as s, s.ws_connect("wss://localhost:8443/ws", headers=_H, ssl=ctx) as ws:
        t0 = time.time()
        async def rd():
            async for m in ws:
                if m.type == aiohttp.WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d["t"] == "stt" and d["kind"] == "final": print("%5.1fs ACCEPTED :" % (time.time() - t0), d["text"], flush=True)
                    elif d["t"] == "assistant": print("%5.1fs assistant :" % (time.time() - t0), d["text"], flush=True)
        r = asyncio.ensure_future(rd())
        async def mic(p):
            for i in range(0, len(p), 640): await ws.send_bytes(p[i:i+640].tobytes()); await asyncio.sleep(0.04)
        async def quiet(sec):
            for _ in range(int(sec/0.04)): await ws.send_bytes(np.zeros(640, np.int16).tobytes()); await asyncio.sleep(0.04)
        await ws.send_str(json.dumps({"t": "text", "text": "hello"})) if False else None
        await quiet(1)
        print(">> near speaker (full level) x2"); await mic(say("My favorite colour is green and I like hiking in the mountains.")); await quiet(7)
        await mic(say("Tell me one fun fact about otters please.")); await quiet(9)
        print(">> distant voice (same sentence at 12% level)"); await mic(say("Mommy can I have some juice right now please.", 0.12)); await quiet(5)
        print(">> near speaker again"); await mic(say("What is two plus two?")); await quiet(7)
        r.cancel()
asyncio.run(main())
