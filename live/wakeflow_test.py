#!/usr/bin/env python3
"""Headless check of the conversation manager: wake word -> conversation -> spoken dismissal -> ignored while asleep -> re-wake ->
inactivity timeout. Synthetic microphone (piper), real HA openWakeWord, real STT/LLM."""
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
def say(t):
    a = np.frombuffer(b"".join(c.audio_int16_bytes for c in v.synthesize(t)), np.int16)
    return resample_poly(a, 16000, v.config.sample_rate).astype(np.int16)
async def main():
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time(); p = lambda *a: print("%5.1fs" % (time.time() - t0), *a, flush=True)
    async with aiohttp.ClientSession() as s, s.ws_connect("wss://localhost:8443/ws", headers=_H, ssl=ctx, max_msg_size=1 << 22) as ws:
        async def rd():
            async for m in ws:
                if m.type != aiohttp.WSMsgType.TEXT: continue
                d = json.loads(m.data)
                if d["t"] == "mgr": p("SESSION :", d["state"], "id=%s tasks=%s" % (d["session"], d["tasks"]))
                elif d["t"] == "earcon": p("EARCON  :", d["kind"])
                elif d["t"] == "session_event" and d["event"] in ("transition", "dismissed", "task_added", "task_finished", "ambient_ignored"): p("  event  :", d["event"], d.get("from", ""), d.get("to", ""), d.get("reason", ""))
                elif d["t"] == "tool" and d["status"] != "revised": p("TOOL    :", d["kind"], d["status"], d.get("task", "")[:50])
                elif d["t"] == "assistant": p("ASSISTANT:", d["text"])
                elif d["t"] == "stt" and d["kind"] == "final": p("HEARD   :", d["text"])
        r = asyncio.ensure_future(rd())
        async def mic(a):
            for i in range(0, len(a), 640): await ws.send_bytes(a[i:i+640].tobytes()); await asyncio.sleep(0.04)
        async def quiet(sec):
            z = np.zeros(640, np.int16).tobytes()
            for _ in range(int(sec / 0.04)): await ws.send_bytes(z); await asyncio.sleep(0.04)
        await quiet(1.5)
        p(">> 'What is two plus two?' while asleep (must be ignored)"); await mic(say("What is two plus two?")); await quiet(4)
        p(">> 'Hey Jarvis'"); await mic(say("Hey Jarvis.")); await quiet(2)
        p(">> 'What is the capital of France?'"); await mic(say("What is the capital of France?")); await quiet(6)
        await ws.send_str(json.dumps({"t": "cfg", "active_timeout": 8, "idle_timeout": 6})); await quiet(0.5)
        p(">> silence: expect ACTIVE -> IDLE (8 s) -> DORMANT (6 s)"); await quiet(17)
        p(">> 'Hey Jarvis', typed hermes task, then spoken dismissal while it runs (expect SUSPENDED, not DORMANT)")
        await mic(say("Hey Jarvis.")); await quiet(2)
        await ws.send_str(json.dumps({"t": "cfg", "active_timeout": 25, "idle_timeout": 20}))
        await ws.send_str(json.dumps({"t": "text", "text": "Research the history of the bicycle in depth and give me a short summary."})); await quiet(6)
        await mic(say("Thanks, that's all.")); await quiet(5)
        p(">> waiting for the task to finish while suspended"); await quiet(float(sys.argv[1]) if len(sys.argv) > 1 else 100)
        p(">> 'Hey Jarvis' again: expect resume + result delivered"); await mic(say("Hey Jarvis.")); await quiet(25)
        r.cancel()
asyncio.run(main())
