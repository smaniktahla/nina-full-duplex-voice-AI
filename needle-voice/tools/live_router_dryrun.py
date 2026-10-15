import asyncio, json, sys, time, urllib.request
sys.path.insert(0, "/home/salil/needle-voice")
from needle_voice import needle_adapter as na, router as r, executor as ex
na.NEEDLE_ENDPOINT = "http://ai1.local:8793"; na.NEEDLE_TIMEOUT_S = 1.0
snap = json.load(open("" + __import__("os").path.join(__import__("os").path.dirname(__file__), "..", "tests", "real_snapshot.json") + ""))
LEX = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
from needle_voice import candidates as cm
snap = cm.merge_lexicon(snap, LEX)

async def needle_route(t): return await asyncio.get_event_loop().run_in_executor(None, na.route, t)
async def qwen_post(payload, timeout):
    def go():
        req = urllib.request.Request("http://ai1.local:8082/v1/chat/completions", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    return await asyncio.get_event_loop().run_in_executor(None, go)
planned = []
async def execute(p): planned.append(p); return f"[dry-run] {p.intent} -> {[ (c.domain,c.service,c.data) for c in p.calls] or p.entity_ids}"
io = r.IO(needle_route, qwen_post, execute)
cfg = r.RouterConfig()
tests = ["Turn on the kitchen lights.", "Turn off everything downstairs.", "What's the pool temperature?", "Pause Plex.",
         "Why do you think the pool temperature is dropping faster tonight?", "what's the weather today", "is the pool exhaust fan on",
         "turn off the kitchen lights", "set the living room thermostat to 70 degrees", "pause the living room tv", "turn on the pool fan", "how's the weather"]
for t in tests:
    t0 = time.perf_counter()
    o = asyncio.run(r.handle(t, snap, cfg, io))
    ms = (time.perf_counter() - t0) * 1000
    q = o.telemetry.get("qwen") or {}
    print(f"{ms:6.0f}ms {o.route:9s} | {t!r}\n          {o.speech or ('-> hermes: ' + str(o.fallback_reason))}" + (f"\n          qwen_raw={q.get('raw')}" if q.get('raw') else "") + (f"\n          needle_skipped={o.telemetry.get('needle_skipped')}" if o.telemetry.get('needle_skipped') else ""))
