#!/usr/bin/env python3
"""Headless check of tool use + task revision, using the typed-input path (no audio). Usage: toolstest.py [wss://host:8443/ws]"""
import asyncio, json, ssl, sys, time
import aiohttp

URL = sys.argv[1] if len(sys.argv) > 1 else "wss://localhost:8443/ws"
async def main():
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time()
    st = lambda *a: print("%6.1fs" % (time.time() - t0), *a, flush=True)
    async with aiohttp.ClientSession() as s, s.ws_connect(URL, ssl=ctx, max_msg_size=1 << 22) as ws:
        audio = {"n": 0}
        async def rd():
            async for m in ws:
                if m.type == aiohttp.WSMsgType.BINARY: audio["n"] += len(m.data)
                elif m.type == aiohttp.WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d["t"] == "assistant": st("ASSISTANT:", d["text"])
                    elif d["t"] == "tool": st("TOOL #%s %s %s%s" % (d["id"], d["kind"], d["status"], (": " + d["task"][:110]) if d.get("task") else ""))
                    elif d["t"] == "stt": st("USER     :", d["text"])
        r = asyncio.ensure_future(rd())
        async def say(text, wait):
            st(">>", text); await ws.send_str(json.dumps({"t": "text", "text": text})); await asyncio.sleep(wait)
        await asyncio.sleep(1)
        await say("What's the weather like in New York right now?", 14)
        await say("Find out what NVIDIA announced about their new inference architecture.", 5)
        await say("Actually, focus on whether it would help my dual RTX 3080 setup.", float(sys.argv[2]) if len(sys.argv) > 2 else 90)
        st("audio bytes received:", audio["n"]); r.cancel()
asyncio.run(main())
