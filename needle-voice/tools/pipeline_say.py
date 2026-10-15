import asyncio, json, sys, time, aiohttp
from ha_ws import TOKEN
PID = "01kvr2fjzpn2xyjgpd7dw4r015"
async def run(text, timeout=300):
    t = time.time()
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("ws://homeassistant.local:8123/api/websocket", max_msg_size=0) as ws:
            await ws.receive_json(); await ws.send_json({"type": "auth", "access_token": TOKEN}); await ws.receive_json()
            await ws.send_json({"id": 1, "type": "assist_pipeline/run", "start_stage": "intent", "end_stage": "intent", "input": {"text": text}, "pipeline": PID})
            speech = engine = None
            while True:
                m = await asyncio.wait_for(ws.receive_json(), timeout)
                if m.get("type") == "event":
                    ev = m["event"]
                    if ev["type"] == "intent-start": engine = ev["data"].get("engine")
                    if ev["type"] == "intent-end":
                        speech = ev["data"]["intent_output"]["response"]["speech"]["plain"]["speech"]
                    if ev["type"] in ("run-end", "error"):
                        if ev["type"] == "error": speech = f"ERROR {ev['data']}"
                        break
    return time.time() - t, engine, speech
for text in sys.argv[1:]:
    dt, engine, speech = asyncio.run(run(text))
    print(f"{dt:6.2f}s engine={engine} | {text!r}\n        -> {speech!r}", flush=True)
