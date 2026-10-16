#!/usr/bin/env python3
"""Smoke test for a running voice server: /api/config, WebSocket handshake, one typed question answered (and a wake-word list present).
Exit 0 = healthy. Used by deploy_voice.sh; safe to run by hand (it opens one short session)."""
import argparse, asyncio, json, ssl, sys, time
import aiohttp
import os

def _svc_headers():
    """Server-side scripts authenticate with the service token (live/auth.py)."""
    try:
        return {"X-Nina-Service": open(os.path.join(os.path.expanduser(os.environ.get("NINA_AUTH_DIR", "~/.config/nina")), "service_token")).read().strip()}
    except OSError:
        return {}


_H = _svc_headers()


ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8443)
ap.add_argument("--tts-any", action="store_true", help="accept Piper (a trial instance runs without the GPU)")
a = ap.parse_args()


async def main():
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    base = f"https://localhost:{a.port}"
    async with aiohttp.ClientSession() as s:
        async with s.get(base + "/api/config", ssl=ctx, headers=_H) as r:
            cfg = await r.json()
        print("config: tts=%s voices=%d wake_words=%d note=%r" % (cfg["tts"], len(cfg["voices"]), len(cfg["wake_words"]), cfg.get("tts_note")))
        if cfg["tts"] != "kokoro" and not a.tts_any:
            print("FAIL: TTS is %s, not kokoro (%s)" % (cfg["tts"], cfg.get("tts_note"))); return 1
        if not cfg["wake_words"] or not cfg["voices"]:
            print("FAIL: empty wake words or voices"); return 1
        async with s.ws_connect(f"wss://localhost:{a.port}/ws", headers=_H, ssl=ctx, max_msg_size=1 << 24) as ws:
            got, said, t0 = {}, [], time.time()
            async def rd():
                async for m in ws:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        d = json.loads(m.data); got[d["t"]] = d
                        if d["t"] == "assistant": said.append(d["text"])
            rt = asyncio.ensure_future(rd()); await asyncio.sleep(2)
            if "hello" not in got or "mgr" not in got:
                print("FAIL: no hello/mgr from the websocket"); return 1
            await ws.send_str(json.dumps({"t": "cfg", "wake": "off"}))
            await ws.send_str(json.dumps({"t": "text", "text": "What is the capital of Italy?"}))
            while not said and time.time() - t0 < 25: await asyncio.sleep(0.5)
            rt.cancel()
            print("answer: %r in %.1fs" % (said[:1], time.time() - t0))
            if not said or "rome" not in " ".join(said).lower():
                print("FAIL: no (correct) answer"); return 1
    print("SMOKE OK"); return 0

sys.exit(asyncio.run(main()))
