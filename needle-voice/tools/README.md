# needle-voice tools (all need HA_TOKEN in the environment; the long-lived token is in the homelab manifest)
- ha_ws.py            websocket helper: `call([{...}, ...])`; e.g. {"type":"system_log/list"} returns HA errors WITH tracebacks
                      (HAOS has no log file and /api/error_log 404s; this is how to see why a custom component failed)
- ha_say.py           POST /api/conversation/process to an agent directly:  python3 ha_say.py "text" (agent = conversation.needle_voice)
- pipeline_say.py     run text through the real Assist pipeline (same path the voice satellite uses)
- set_pipeline_engine.py <conversation.entity_id>   switch the "hermes on AI2" pipeline's agent (BACKOUT = pass the Hermes entity id)
- prompt_ab.py        labeled-set A/B of Qwen prompt variants against the live server (ALWAYS warm the server first; see notes)
- live_router_dryrun.py  run the real router against real Needle+Qwen with a FAKE executor (touches nothing)
- pipeline_hermes_on_ai2_BEFORE.json   (private, not in git; see pipeline_BEFORE.example.json) exact pipeline config before Needle Voice was selected
