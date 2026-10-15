import asyncio, json, sys, statistics, urllib.request
sys.path.insert(0, "/home/salil/needle-voice")
from needle_voice import router as r, qwen, needle_adapter as na
snap = json.load(open("/home/salil/needle-voice/tests/real_snapshot.json"))
snap["entities"].append({"entity_id": "input_boolean.needle_voice_test", "name": "Voice Test Lamp", "aliases": [], "area_id": None})
CASES = [
 ("is the pool exhaust fan on", "get_state", "switch.swimming_pool_exhaust_fan"),
 ("turn off the kitchen lights", "turn_off", "light.kitchen_recessed_light_1"),
 ("set the living room thermostat to 70 degrees", "set_temperature", "climate.living_room_thermostat"),
 ("pause the living room tv", "media_pause", "media_player.living_room_tv"),
 ("turn on the pool fan", "turn_on", "fan.pool_fan"),
 ("turn off the basement tv", "turn_off", "media_player.basement_tv"),
 ("resume the living room tv", "media_play", "media_player.living_room_tv"),
 ("stop the basement tv", "media_stop", "media_player.basement_tv"),
 ("turn on the guest room lamp", "turn_on", "light.guest_room_lamp_1"),
 ("dim the guest room lamp to 30 percent", "set_brightness", "light.guest_room_lamp_1"),
 ("turn on the voice test lamp", "turn_on", "input_boolean.needle_voice_test"),
 ("turn off the voice test lamp", "turn_off", "input_boolean.needle_voice_test"),
 ("toggle the voice test lamp", "toggle", "input_boolean.needle_voice_test"),
 ("is the voice test lamp on", "get_state", "input_boolean.needle_voice_test"),
 ("turn on the pool pendant lights", "turn_on", "switch.pool_pendant_lights"),
 ("turn off the pool high bay lights", "turn_off", "switch.pool_high_bay_lights"),
 ("is the pool camera wiper on", "get_state", "switch.swimming_pool_pool_camera_wiper"),
]
async def qpost(payload, timeout):
    def go():
        req = urllib.request.Request("http://ai1.local:8082/v1/chat/completions", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    return await asyncio.get_event_loop().run_in_executor(None, go)
async def nr(t): return na.RouteDecision("conversation", False, False, confidence=0.0, needle_raw={})
async def ex(p): return "ok"
io = r.IO(nr, qpost, ex); cfg = r.RouterConfig(needle_enabled=False)
def evaluate(label, reps=2):
    ok = wrong = esc = 0; lats = []; miss = []
    for u, intent, tgt in CASES:
        for _ in range(reps):
            o = asyncio.run(r.handle(u, snap, cfg, io))
            q = (o.telemetry.get("qwen") or {}).get("latency_ms")
            if q: lats.append(q)
            if o.route == "qwen_fast":
                e = o.telemetry["execution"]
                good = e["intent"] == intent and (tgt in e["entities"] or tgt == e["target"])
                ok += good; wrong += (not good)
                if not good: miss.append(("WRONG", u, e))
            else:
                esc += 1; miss.append(("esc", u, o.fallback_reason))
    n = len(CASES) * reps
    print(f"[{label}] correct={ok}/{n} WRONG-ACTION={wrong} escalated={esc} | latency p50={statistics.median(lats):.0f}ms max={max(lats):.0f}ms")
    seen = set()
    for m in miss:
        if (m[0], m[1]) not in seen: seen.add((m[0], m[1])); print("    ", m)
V0 = qwen.SYSTEM_PROMPT
V1 = (
    "You are a fast Home Assistant intent resolver.\n"
    "Return only JSON. Resolve home-control and home-status requests.\n"
    "The Candidates list was already matched to the request by name. If one candidate is clearly the device the "
    "user means, handle it. Return decision escalate ONLY when the request is not a supported command or question about "
    "a device, or when none of the candidates is what the user means.\n"
    "Never invent an entity. Never answer general questions. Pick target ONLY from the Candidates list, using its id exactly.\n"
    "Intents: turn_on, turn_off, toggle, set_brightness (arguments.brightness 0-100), set_temperature (arguments.temperature), "
    "lock, unlock, open, close, get_state, get_temperature, get_weather, run_script, media_play, media_pause, media_stop.\n"
    "Examples:\n"
    'Candidates:\n- light.den_lamp | Den Lamp\nRequest: switch off the den lamp -> {"decision":"handle","intent":"turn_off","target":"light.den_lamp","arguments":{}}\n'
    'Candidates:\n- switch.garden_pump | Garden Pump\nRequest: turn on the garden pump -> {"decision":"handle","intent":"turn_on","target":"switch.garden_pump","arguments":{}}\n'
    'Candidates:\n- input_boolean.guest_mode | Guest Mode\nRequest: turn off guest mode -> {"decision":"handle","intent":"turn_off","target":"input_boolean.guest_mode","arguments":{}}\n'
    'Candidates:\n- light.office | Office Light\nRequest: set the office light to 40 percent -> {"decision":"handle","intent":"set_brightness","target":"light.office","arguments":{"brightness":40}}\n'
    'Candidates:\n- cover.garage_door | Garage Door\nRequest: is the garage door open -> {"decision":"handle","intent":"get_state","target":"cover.garage_door","arguments":{}}\n'
    'Candidates:\n- media_player.living_room_tv | Living Room TV\nRequest: pause the living room tv -> {"decision":"handle","intent":"media_pause","target":"media_player.living_room_tv","arguments":{}}\n'
    'Candidates:\n- fan.attic_fan | Attic Fan\nRequest: toggle the attic fan -> {"decision":"handle","intent":"toggle","target":"fan.attic_fan","arguments":{}}\n'
    'Candidates:\n- light.den_lamp | Den Lamp\nRequest: why is it so cold in here -> {"decision":"escalate","intent":null,"target":null,"arguments":{}}\n'
)
from needle_voice import candidates as cm
def warm():
    c = [cm.Candidate("light.warmup", "entity", "Warm Up", "light", None, 1.0)]
    t = asyncio.run(qpost(qwen.build_request("qwen-fast", "turn on the warm up light", c), 60))
for label, prompt in (("ADOPTED prompt", qwen.SYSTEM_PROMPT),):
    warm(); evaluate(label, reps=2)
