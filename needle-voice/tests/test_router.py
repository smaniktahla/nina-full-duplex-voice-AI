import asyncio, json, os
from needle_voice import needle_adapter as na
from needle_voice import router as r
from needle_voice import executor as ex

REAL = json.load(open(os.path.join(os.path.dirname(__file__), "real_snapshot.json")))

SNAP = {
    "entities": [
        {"entity_id": "light.kitchen_main", "name": "Kitchen Main", "aliases": [], "area_id": "kitchen"},
        {"entity_id": "light.den_lamp", "name": "Den Lamp", "aliases": [], "area_id": "den"},
        {"entity_id": "media_player.plex", "name": "Plex", "aliases": [], "area_id": None},
        {"entity_id": "sensor.pool_temp", "name": "Pool Temp", "aliases": [], "area_id": None},
        {"entity_id": "weather.home", "name": "Forecast Home", "aliases": [], "area_id": None},
    ],
    "areas": [{"area_id": "kitchen", "name": "Kitchen", "aliases": []}, {"area_id": "den", "name": "Den", "aliases": []}],
}


def adapter(tool=None, args=None, error=None, raw=None):
    if error:
        return na.RouteDecision("conversation", False, False, error=error)
    if tool is None:
        return na.RouteDecision("conversation", False, False, confidence=0.05, needle_raw=raw or {})
    return na.RouteDecision("ha_action" if tool == "turn_on_light" else "sensor_query", True, True, tool_name=tool,
                            arguments=args or {}, confidence=0.4, needle_raw=raw or {})


def qwen_resp(obj):
    return {"choices": [{"message": {"content": obj if isinstance(obj, str) else json.dumps(obj)}}]}


class Rig:
    def __init__(self, needle=None, qwen=None, exec_result="Done.", passthrough=False):
        self.needle, self.qwen, self.exec_result, self.pt = needle, qwen, exec_result, passthrough
        self.needle_calls = self.qwen_calls = 0
        self.executed = []

    async def needle_route(self, text):
        self.needle_calls += 1
        if isinstance(self.needle, Exception):
            raise self.needle
        return self.needle if self.needle is not None else adapter()

    async def qwen_post(self, payload, timeout):
        self.qwen_calls += 1
        if isinstance(self.qwen, Exception):
            raise self.qwen
        return self.qwen

    async def execute(self, plan):
        self.executed.append(plan)
        if isinstance(self.exec_result, Exception):
            raise self.exec_result
        return self.exec_result

    def io(self):
        return r.IO(self.needle_route, self.qwen_post, self.execute, lambda: self.pt)


def go(text, rig, snap=SNAP, **cfg):
    return asyncio.run(r.handle(text, snap, r.RouterConfig.from_dict(cfg), rig.io()))


# ---------------- straight to Hermes, no fast tier touched ----------------
def test_disabled_and_passthrough_and_guards_skip_both_tiers():
    rig = Rig()
    assert go("turn on the kitchen lights", rig, enabled=False).fallback_reason == "disabled"
    assert go("turn on the kitchen lights", Rig(passthrough=True)).fallback_reason == "passthrough_flag"
    for text, reason in [("Why do you think the pool is cooling faster tonight?", "guard:reasoning_markers"),
                         ("turn off everything downstairs", "guard:multi_target_markers"),
                         ("turn off the kitchen and den lights", "guard:multi_target_markers"),
                         ("don't turn on the light", "guard:negation")]:
        rig = Rig()
        o = go(text, rig)
        assert (o.route, o.speech, o.fallback_reason) == ("hermes", None, reason)
        assert rig.needle_calls == 0 and rig.qwen_calls == 0 and rig.executed == []


def test_no_snapshot_goes_to_hermes():
    o = go("turn on the kitchen lights", Rig(), snap=None)
    assert o.route == "hermes" and o.fallback_reason == "no_snapshot"


# ---------------- Needle tier ----------------
def test_needle_handles_a_resolvable_light_command():
    rig = Rig(needle=adapter("turn_on_light", {"area": "kitchen"}), exec_result="Turned on the Kitchen light.")
    o = go("turn on the kitchen lights", rig)
    assert (o.route, o.speech) == ("needle", "Turned on the Kitchen light.")
    assert rig.qwen_calls == 0
    assert rig.executed[0].calls[0].domain == "light" and rig.executed[0].calls[0].service == "turn_on"
    assert o.telemetry["needle"]["resolution"] == "resolved"


def test_needle_read_path_uses_get_temperature_wording():
    snap = {"entities": [{"entity_id": "sensor.pool_temp", "name": "Pool Temp", "aliases": [], "area_id": None}], "areas": []}
    rig = Rig(needle=adapter("get_sensor_state", {"entity": "pool temp"}), exec_result="The Pool Temp is 65 °F.")
    o = go("what is the pool temp", rig, snap=snap)
    assert o.route == "needle" and rig.executed[0].intent == "get_temperature"
    assert rig.executed[0].kind == "read" and rig.executed[0].calls == ()


def test_needle_wrong_direction_is_not_corroborated_and_falls_to_qwen():
    """Observed live: Needle answered 'turn OFF the bedroom light' with turn_on_light."""
    rig = Rig(needle=adapter("turn_on_light", {"area": "kitchen"}),
              qwen=qwen_resp({"decision": "handle", "intent": "turn_off", "target": "area:kitchen", "arguments": {}}))
    o = go("turn off the kitchen lights", rig)
    assert o.route == "qwen_fast"
    assert "needle_intent_not_corroborated" in o.telemetry["needle_skipped"]
    assert rig.executed[0].calls[0].service == "turn_off"          # never executed Needle's turn_on


def test_needle_unresolvable_target_is_not_executed():
    rig = Rig(needle=adapter("get_sensor_state", {"entity": "pool temperature"}))   # the live wrong-entity case
    o = go(" Is this running pool exhaust fan on?", rig)
    assert o.route == "hermes" and rig.executed == []


def test_needle_timeout_and_error_do_not_block_qwen():
    for needle in (asyncio.TimeoutError(), RuntimeError("down"), adapter(error="needle unavailable/timeout: x")):
        rig = Rig(needle=needle, qwen=qwen_resp({"decision": "handle", "intent": "media_pause", "target": "media_player.plex", "arguments": {}}))
        o = go("pause plex", rig)
        assert o.route == "qwen_fast", needle


def test_needle_can_be_disabled():
    rig = Rig(qwen=qwen_resp({"decision": "handle", "intent": "media_pause", "target": "media_player.plex", "arguments": {}}))
    o = go("pause plex", rig, needle_enabled=False)
    assert o.route == "qwen_fast" and rig.needle_calls == 0


# ---------------- Qwen tier ----------------
def test_qwen_handles_a_command_needle_cannot():
    rig = Rig(qwen=qwen_resp({"decision": "handle", "intent": "media_pause", "target": "media_player.plex", "arguments": {}}),
              exec_result="Paused the Plex.")
    o = go("pause plex", rig)
    assert (o.route, o.speech) == ("qwen_fast", "Paused the Plex.")
    assert o.telemetry["validation"] == {"ok": True, "reason": "ok"}
    assert o.telemetry["qwen"]["raw"].startswith("{")
    assert rig.executed[0].calls[0].service == "media_pause"


def test_every_qwen_failure_fails_open_with_a_reason_and_no_action():
    good = {"decision": "handle", "intent": "media_pause", "target": "media_player.plex", "arguments": {}}
    cases = {
        "qwen_timeout": asyncio.TimeoutError(),
        "qwen_http_error": ConnectionError("refused"),
        "qwen_bad_response": {"unexpected": True},
        "qwen:malformed_json": qwen_resp("nope{"),
        "qwen:qwen_escalated": qwen_resp({"decision": "escalate", "intent": None, "target": None, "arguments": {}}),
        "qwen:unknown_intent": qwen_resp({**good, "intent": "detonate"}),
        "qwen:unknown_target": qwen_resp({**good, "target": "media_player.invented"}),
        "qwen:schema_violation": qwen_resp({"decision": "handle"}),
        "qwen:intent_not_implied_by_words": qwen_resp({**good, "intent": "media_play"}),   # pause -> play
    }
    for expect, resp in cases.items():
        rig = Rig(qwen=resp)
        o = go("pause plex", rig)
        assert o.route == "hermes" and o.speech is None, expect
        assert o.fallback_reason.startswith(expect), (expect, o.fallback_reason)
        assert rig.executed == [], expect


def test_qwen_disabled_goes_to_hermes():
    assert go("pause plex", Rig(), qwen_fast_enabled=False).fallback_reason == "qwen_disabled"


def test_no_candidates_and_ambiguous_skip_qwen_entirely():
    rig = Rig()
    assert go("play some jazz", rig).fallback_reason == "no_candidates" and rig.qwen_calls == 0
    snap = {"entities": [{"entity_id": "light.a", "name": "Garage Lamp", "aliases": [], "area_id": None},
                         {"entity_id": "light.b", "name": "Garage Lamp", "aliases": [], "area_id": None}], "areas": []}
    rig = Rig()
    assert go("turn on the garage lamp", rig, snap=snap).fallback_reason == "ambiguous_target" and rig.qwen_calls == 0


def test_qwen_naming_one_light_of_several_is_not_the_same_action_and_is_rejected():
    snap = {"entities": [{"entity_id": "light.kitchen_a", "name": "Kitchen A", "aliases": [], "area_id": "kitchen"},
                         {"entity_id": "light.kitchen_b", "name": "Kitchen B", "aliases": [], "area_id": "kitchen"}],
            "areas": [{"area_id": "kitchen", "name": "Kitchen", "aliases": []}]}
    rig = Rig(qwen=qwen_resp({"decision": "handle", "intent": "turn_off", "target": "light.kitchen_a", "arguments": {}}))
    o = go("turn off the kitchen lights", rig, snap=snap)   # retrieval's unique top is area:kitchen (2 lights)
    assert o.route == "hermes" and o.fallback_reason == "qwen:target_disagrees_with_retrieval" and rig.executed == []


def test_qwen_naming_the_only_light_in_the_area_is_the_same_action_and_accepted():
    rig = Rig(needle=adapter(), qwen=qwen_resp({"decision": "handle", "intent": "turn_off", "target": "light.kitchen_main", "arguments": {}}))
    o = go("turn off the kitchen lights", rig)               # SNAP: kitchen has exactly one light
    assert o.route == "qwen_fast"
    assert rig.executed[0].calls[0].data == {"entity_id": "light.kitchen_main"}


def test_thermostat_number_and_degrees_do_not_dilute_the_match():
    snap = {"entities": [{"entity_id": "climate.living_room_thermostat", "name": "Living Room Thermostat", "aliases": [], "area_id": "living_room"}],
            "areas": [{"area_id": "living_room", "name": "Living Room", "aliases": []}]}
    rig = Rig(needle=adapter(), qwen=qwen_resp({"decision": "handle", "intent": "set_temperature", "target": "climate.living_room_thermostat", "arguments": {"temperature": 70}}))
    o = go("set the living room thermostat to 70 degrees", rig, snap=snap)
    assert o.route == "qwen_fast" and rig.executed[0].calls[0].data == {"entity_id": "climate.living_room_thermostat", "temperature": 70.0}


# ---------------- weather rules tier (deterministic, read-only) ----------------
def test_plain_weather_questions_are_answered_by_rules_without_any_model():
    for text in ("what's the weather today", "how's the weather", "what is the weather", "what's the weather like right now"):
        rig = Rig(exec_result="Right now it's 60 degrees and cloudy.")
        o = go(text, rig)
        assert (o.route, o.speech) == ("rules", "Right now it's 60 degrees and cloudy."), text
        assert rig.needle_calls == 0 and rig.qwen_calls == 0
        assert rig.executed[0].intent == "get_weather" and rig.executed[0].entity_ids == ("weather.home",)


def test_anything_beyond_a_plain_weather_question_goes_elsewhere():
    for text in ("what's the weather tomorrow", "will it rain today", "what's the weather in paris", "how's the weather and the traffic"):
        rig = Rig(needle=adapter())
        o = go(text, rig)
        assert o.route == "hermes" and rig.executed == [], text


def test_weather_needs_exactly_one_weather_entity():
    two = {"entities": SNAP["entities"] + [{"entity_id": "weather.other", "name": "Other", "aliases": [], "area_id": None}], "areas": []}
    assert go("what's the weather today", Rig(needle=adapter()), snap=two).route == "hermes"
    none = {"entities": [e for e in SNAP["entities"] if not e["entity_id"].startswith("weather.")], "areas": []}
    assert go("what's the weather today", Rig(needle=adapter()), snap=none).route == "hermes"


def test_weather_read_failure_goes_to_hermes():
    rig = Rig(exec_result=ex.ExecutionError("state_unavailable:weather.home"))
    o = go("what's the weather today", rig)
    assert o.route == "hermes" and o.fallback_reason.startswith("execution_error")


def test_sensitive_intent_never_reaches_execution_by_default():
    snap = {"entities": [{"entity_id": "lock.front_door", "name": "Front Door", "aliases": [], "area_id": None}], "areas": []}
    rig = Rig(qwen=qwen_resp({"decision": "handle", "intent": "unlock", "target": "lock.front_door", "arguments": {}}))
    o = go("unlock the front door", rig, snap=snap)
    assert o.route == "hermes" and o.fallback_reason == "qwen:sensitive_intent_disabled" and rig.executed == []
    o = go("unlock the front door", Rig(qwen=rig.qwen), snap=snap, allow_sensitive_intents=True)
    assert o.route == "qwen_fast"


# ---------------- execution failures ----------------
def test_execution_error_goes_to_hermes_without_trying_another_tier():
    rig = Rig(needle=adapter("turn_on_light", {"area": "kitchen"}), exec_result=ex.ExecutionError("service_call_failed:X"),
              qwen=qwen_resp({"decision": "escalate", "intent": None, "target": None, "arguments": {}}))
    o = go("turn on the kitchen lights", rig)
    assert o.route == "hermes" and o.fallback_reason.startswith("execution_error")
    assert rig.qwen_calls == 0                       # never risk repeating a half-done action via another tier


def test_unexpected_exception_from_executor_fails_open():
    rig = Rig(qwen=qwen_resp({"decision": "handle", "intent": "media_pause", "target": "media_player.plex", "arguments": {}}),
              exec_result=ValueError("kaboom"))
    o = go("pause plex", rig)
    assert o.route == "hermes" and o.fallback_reason == "execution_exception"


def test_router_bug_is_contained(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(r.qwen, "build_request", boom)
    o = go("pause plex", Rig())
    assert o.route == "hermes" and o.fallback_reason == "router_exception" and "RuntimeError" in o.telemetry["router_exception"]


def test_handle_never_raises_on_garbage():
    for text in (None, "", 5, "\x00\x01"):
        o = asyncio.run(r.handle(text, SNAP, r.RouterConfig(), Rig().io()))
        assert o.route == "hermes"


def test_telemetry_carries_the_fields_the_spec_asked_for():
    rig = Rig(needle=adapter(), qwen=qwen_resp({"decision": "handle", "intent": "media_pause", "target": "media_player.plex", "arguments": {}}))
    t = go("pause plex", rig).telemetry
    for key in ("input", "guard", "needle", "retrieval", "qwen", "validation", "execution", "router_latency_ms"):
        assert key in t
    assert t["qwen"]["latency_ms"] >= 0 and t["retrieval"][0]["id"] == "media_player.plex"


def test_real_household_data_kitchen_lights_end_to_end():
    rig = Rig(needle=adapter("turn_on_light", {"area": "kitchen"}), exec_result="ok")
    o = go("turn on the kitchen lights", rig, snap=REAL)
    assert o.route == "needle"
    assert rig.executed[0].calls[0].data == {"entity_id": ["light.kitchen_recessed_light_1"]}
