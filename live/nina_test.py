#!/usr/bin/env python3
"""Phrase wake word 'hey Nina': ignored speech, wake + command in one breath, near-miss 'Hey Dina', dismissal. Synthetic mic (Piper)."""
import asyncio, json, ssl, sys, time
import aiohttp, numpy as np
from scipy.signal import resample_poly
from piper import PiperVoice
v = PiperVoice.load("voices/en_US-lessac-medium.onnx")
def say(t):
    a = np.frombuffer(b"".join(c.audio_int16_bytes for c in v.synthesize(t)), np.int16)
    return resample_poly(a, 16000, v.config.sample_rate).astype(np.int16)
async def main():
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time(); p = lambda *a: print("%5.1fs" % (time.time() - t0), *a, flush=True)
    async with aiohttp.ClientSession() as s, s.ws_connect("wss://localhost:8443/ws", ssl=ctx, max_msg_size=1 << 22) as ws:
        async def rd():
            async for m in ws:
                if m.type != aiohttp.WSMsgType.TEXT: continue
                d = json.loads(m.data)
                if d["t"] == "mgr": p("SESSION :", d["state"], "wake=%s" % d["wake"])
                elif d["t"] == "earcon": p("EARCON  :", d["kind"])
                elif d["t"] == "assistant": p("ASSISTANT:", d["text"])
                elif d["t"] == "stt" and d["kind"] == "final": p("HEARD   :", d["text"])
        r = asyncio.ensure_future(rd())
        async def mic(a):
            for i in range(0, len(a), 640): await ws.send_bytes(a[i:i+640].tobytes()); await asyncio.sleep(0.04)
        async def quiet(sec):
            z = np.zeros(640, np.int16).tobytes()
            for _ in range(int(sec / 0.04)): await ws.send_bytes(z); await asyncio.sleep(0.04)
        await quiet(1); await ws.send_str(json.dumps({"t": "cfg", "wake": "hey_nina"})); await quiet(1.5)
        p(">> asleep: 'What is two plus two? Tell me quickly please.' (must be ignored)"); await mic(say("What is two plus two? Tell me quickly please.")); await quiet(4)
        p(">> near miss: 'Hey Dina, what is the capital of Italy?' (must NOT wake)"); await mic(say("Hey Dina, what is the capital of Italy?")); await quiet(4)
        p(">> 'Hey Nina, what is the capital of France?' (wake + command in one breath)"); await mic(say("Hey Nina, what is the capital of France?")); await quiet(7)
        p(">> 'Thanks, that's all.'"); await mic(say("Thanks, that's all.")); await quiet(5)
        p(">> 'Hey Nina.' alone, then a separate question"); await mic(say("Hey Nina.")); await quiet(3); await mic(say("What is the capital of Spain?")); await quiet(7)
        r.cancel()
asyncio.run(main())
