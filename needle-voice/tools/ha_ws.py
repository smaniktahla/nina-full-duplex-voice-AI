import asyncio, json, sys, aiohttp
TOKEN = __import__("os").environ["HA_TOKEN"]  # long-lived token; see the homelab manifest
async def call(msgs, url="ws://homeassistant.local:8123/api/websocket"):
    """msgs: list of dicts (each gets an id). Returns list of responses in order."""
    out = []
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url, max_msg_size=0) as ws:
            assert (await ws.receive_json())["type"] == "auth_required"
            await ws.send_json({"type": "auth", "access_token": TOKEN})
            assert (await ws.receive_json())["type"] == "auth_ok"
            for i, m in enumerate(msgs, 1):
                await ws.send_json({**m, "id": i})
                while True:
                    r = await asyncio.wait_for(ws.receive_json(), 60)
                    if r.get("id") == i and r["type"] in ("result", "event") and (r["type"] == "result" or m.get("_collect") is None):
                        out.append(r); break
    return out
if __name__ == "__main__":
    res = asyncio.run(call([json.loads(a) for a in sys.argv[1:]]))
    print(json.dumps(res, indent=1)[:200000])
