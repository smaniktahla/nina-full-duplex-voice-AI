import asyncio, json, ssl, aiohttp, time, sys
import os

def _svc_headers():
    """Server-side scripts authenticate with the service token (live/auth.py)."""
    try:
        return {"X-Nina-Service": open(os.path.join(os.path.expanduser(os.environ.get("NINA_AUTH_DIR", "~/.config/nina")), "service_token")).read().strip()}
    except OSError:
        return {}


_H = _svc_headers()

async def session(lines, waits):
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE; t0=time.time()
    async with aiohttp.ClientSession() as s, s.ws_connect("wss://localhost:8443/ws", headers=_H,ssl=ctx,max_msg_size=1<<24) as ws:
        async def rd():
            async for m in ws:
                if m.type==aiohttp.WSMsgType.TEXT:
                    d=json.loads(m.data)
                    if d["t"]=="assistant": print("%5.1fs ASSISTANT: %s"%(time.time()-t0,d["text"]),flush=True)
                    elif d["t"]=="tool": print("%5.1fs TOOL %s %s"%(time.time()-t0,d["kind"],d["status"]),flush=True)
        r=asyncio.ensure_future(rd()); await asyncio.sleep(1)
        for l,w in zip(lines,waits):
            print("%5.1fs >> %s"%(time.time()-t0,l),flush=True); await ws.send_str(json.dumps({"t":"text","text":l})); await asyncio.sleep(w)
        r.cancel()
print("--- session 1: teach a fact"); asyncio.run(session(["My dog's name is Biscuit and he is a beagle."],[9]))
print("--- facts file:"); print(open("/home/salil/.local/share/needle_voice/profile.json").read() if __import__("os").path.exists("/home/salil/.local/share/needle_voice/profile.json") else "(none)")
print("--- session 2 (new session, no shared history): ask"); asyncio.run(session(["What is my dog's name?"],[9]))
