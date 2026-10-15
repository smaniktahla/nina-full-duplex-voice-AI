"""Switch the STT engine of the "hermes on AI2" pipeline. Usage: HA_TOKEN=... python3 set_pipeline_stt.py stt.faster_whisper
Backout = pass the old engine: stt.faster_whisper_2 (the on-box tiny-int8 add-on)."""
import asyncio, json, sys
from ha_ws import call
PID = "01kvr2fjzpn2xyjgpd7dw4r015"
async def main(engine):
    lst = (await call([{"type": "assist_pipeline/pipeline/list"}]))[0]["result"]["pipelines"]
    cur = next(p for p in lst if p["id"] == PID)
    print("before:", cur["stt_engine"])
    msg = {"type": "assist_pipeline/pipeline/update", "pipeline_id": PID, **{k: v for k, v in cur.items() if k != "id"}, "stt_engine": engine}
    r = await call([msg, {"type": "assist_pipeline/pipeline/list"}])
    print("update:", r[0].get("success"), r[0].get("error"))
    print("after:", next(p for p in r[1]["result"]["pipelines"] if p["id"] == PID)["stt_engine"])
asyncio.run(main(sys.argv[1]))
