import asyncio, json, sys
from ha_ws import call
engine = sys.argv[1]
before = json.load(open("pipeline_hermes_on_ai2_BEFORE.json"))
msg = {"type": "assist_pipeline/pipeline/update", "pipeline_id": before["id"],
       **{k: v for k, v in before.items() if k != "id"}, "conversation_engine": engine}
r = asyncio.run(call([msg, {"type": "assist_pipeline/pipeline/list"}]))
print("update success:", r[0].get("success"), r[0].get("error"))
for p in r[1]["result"]["pipelines"]:
    if p["id"] == before["id"]:
        print("now:", p["name"], "->", p["conversation_engine"], "| prefer_local_intents:", p["prefer_local_intents"], "| stt:", p["stt_engine"], "| tts:", p["tts_engine"], p["tts_voice"])
