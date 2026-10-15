import asyncio, json, os, sys, time, aiohttp
TOKEN = os.environ["HA_TOKEN"]
async def stt_only(ws, mid, pipeline_id, pcm):
    await ws.send_json({"id": mid, "type": "assist_pipeline/run", "start_stage": "stt", "end_stage": "stt",
                        "input": {"sample_rate": 16000}, "pipeline": pipeline_id})
    handler = None; text = None; t = time.time()
    while True:
        m = await asyncio.wait_for(ws.receive_json(), 60)
        if m.get("id") != mid: continue
        if m["type"] == "result" and not m.get("success"): return f"ERR {m.get('error')}", 0
        if m["type"] != "event": continue
        ev = m["event"]
        if ev["type"] == "run-start":
            handler = ev["data"]["runner_data"]["stt_binary_handler_id"]
            for i in range(0, len(pcm), 2048): await ws.send_bytes(bytes([handler]) + pcm[i:i+2048])
            await ws.send_bytes(bytes([handler]))
        if ev["type"] == "stt-end": text = ev["data"]["stt_output"]["text"]
        if ev["type"] == "error": return f"ERR {ev['data']}", 0
        if ev["type"] == "run-end": return text, time.time() - t
async def main(pipelines, limit_kinds):
    idx = json.load(open("pcm/index.json"))
    idx = [c for c in idx if c["kind"] in limit_kinds][:int(os.environ.get("N", "99"))]
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("ws://homeassistant.local:8123/api/websocket", max_msg_size=0) as ws:
            await ws.receive_json(); await ws.send_json({"type": "auth", "access_token": TOKEN}); await ws.receive_json()
            mid = 1
            for c in idx:
                pcm = open(c["file"], "rb").read(); row = []
                for label, pid in pipelines.items():
                    mid += 1; text, dt = await stt_only(ws, mid, pid, pcm); row.append(f"{label}: {text!r} ({dt:.1f}s)")
                print(f"[{c['kind']:5s}] {c['text']!r}\n        " + "\n        ".join(row), flush=True)
if __name__ == "__main__":
    pipelines = json.loads(sys.argv[1]); kinds = sys.argv[2].split(",")
    asyncio.run(main(pipelines, kinds))
