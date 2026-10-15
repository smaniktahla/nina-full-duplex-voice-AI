import json
from needle_voice import candidates as c
from needle_voice import qwen

SNAP = {
    "entities": [
        {"entity_id": "light.den_lamp", "name": "Den Lamp", "aliases": [], "area_id": "den"},
        {"entity_id": "light.kitchen_main", "name": "Kitchen Main", "aliases": [], "area_id": "kitchen"},
        {"entity_id": "media_player.plex", "name": "Plex", "aliases": [], "area_id": None},
        {"entity_id": "sensor.pool_temp", "name": "Pool Temp", "aliases": [], "area_id": None},
        {"entity_id": "lock.front_door", "name": "Front Door", "aliases": [], "area_id": None},
        {"entity_id": "climate.upstairs", "name": "Upstairs Thermostat", "aliases": [], "area_id": None},
    ],
    "areas": [{"area_id": "kitchen", "name": "Kitchen", "aliases": []}, {"area_id": "den", "name": "Den", "aliases": []}],
}


def setup(text):
    cands = c.rank(text, SNAP)
    return cands, c.unique_top(cands)


def check(text, obj, allow_sensitive=False):
    cands, top = setup(text)
    content = obj if isinstance(obj, str) else json.dumps(obj)
    return qwen.validate(content, utterance=text, candidates=cands, top=top, allow_sensitive=allow_sensitive)


def H(intent, target, args=None):
    return {"decision": "handle", "intent": intent, "target": target, "arguments": args or {}}


def test_valid_entity_command():
    v = check("turn off the den lamp", H("turn_off", "light.den_lamp"))
    assert v.ok and (v.intent, v.target) == ("turn_off", "light.den_lamp")


def test_valid_area_light_command():
    v = check("turn off the kitchen lights", H("turn_off", "area:kitchen"))
    assert v.ok and v.target == "area:kitchen"


def test_valid_brightness_with_grounded_number():
    v = check("dim the den lamp to 30 percent", H("set_brightness", "light.den_lamp", {"brightness": 30}))
    assert v.ok and v.arguments == {"brightness": 30}


def test_escalate_is_not_ok():
    v = check("turn off the den lamp", {"decision": "escalate", "intent": None, "target": None, "arguments": {}})
    assert not v.ok and v.reason == "qwen_escalated"


def test_malformed_and_wrong_shape():
    assert check("turn off the den lamp", "not json{{").reason == "malformed_json"
    assert check("turn off the den lamp", [1, 2]).reason == "schema_violation"
    assert check("turn off the den lamp", {"decision": "handle"}).reason == "schema_violation"
    extra = H("turn_off", "light.den_lamp"); extra["say"] = "hi"
    assert check("turn off the den lamp", extra).reason == "schema_violation"
    cands, top = setup("turn off the den lamp")
    assert qwen.validate(None, utterance="x", candidates=cands, top=top, allow_sensitive=False).reason == "non_string_content"


def test_unknown_intent_and_target():
    assert check("turn off the den lamp", H("self_destruct", "light.den_lamp")).reason == "unknown_intent"
    assert check("turn off the den lamp", H("turn_off", "light.invented")).reason == "unknown_target"


def test_target_must_match_retrieval_top():
    # Qwen picks a real candidate that is NOT the deterministic top-1
    text = "turn off the kitchen lights"
    v = check(text, H("turn_off", "light.kitchen_main"))
    assert not v.ok and v.reason == "target_disagrees_with_retrieval"


def test_the_observed_pause_as_play_error_is_rejected():
    v = check("pause plex", H("media_play", "media_player.plex"))
    assert not v.ok and v.reason.startswith("intent_not_implied_by_words")
    assert check("pause plex", H("media_pause", "media_player.plex")).ok


def test_sensitive_intents_are_off_by_default_and_gated():
    v = check("unlock the front door", H("unlock", "lock.front_door"))
    assert not v.ok and v.reason == "sensitive_intent_disabled"
    assert check("unlock the front door", H("unlock", "lock.front_door"), allow_sensitive=True).ok
    assert check("lock the front door", H("lock", "lock.front_door")).ok   # the safe direction is allowed


def test_area_target_needs_a_light_word_and_an_area_capable_intent():
    assert check("turn off the kitchen", H("turn_off", "area:kitchen")).reason == "area_target_not_allowed_for_intent"
    assert check("pause the kitchen lights", H("media_pause", "area:kitchen")).reason == "area_target_not_allowed_for_intent"


def test_domain_must_fit_intent():
    v = check("turn on the pool temp", H("turn_on", "sensor.pool_temp"))
    assert not v.ok and v.reason.startswith("domain_mismatch")


def test_argument_rules():
    t = "dim the den lamp to 30 percent"
    assert check(t, H("set_brightness", "light.den_lamp")).reason == "missing_argument:brightness"
    assert check(t, H("set_brightness", "light.den_lamp", {"brightness": 130})).reason == "argument_out_of_range:brightness"
    assert check(t, H("set_brightness", "light.den_lamp", {"brightness": 45})).reason == "argument_not_grounded:brightness"
    assert check(t, H("set_brightness", "light.den_lamp", {"brightness": True})).reason == "bad_argument_type:brightness"
    assert check(t, H("set_brightness", "light.den_lamp", {"brightness": 30.5})).reason == "bad_argument_type:brightness"
    assert check(t, H("set_brightness", "light.den_lamp", {"brightness": 30, "color": "red"})).reason == "unexpected_arguments"
    assert check("turn on the den lamp", H("turn_on", "light.den_lamp", {"brightness": 30})).reason == "unexpected_arguments"


def test_temperature_argument_grounded_and_bounded():
    t = "set the upstairs thermostat to 70 degrees"
    assert check(t, H("set_temperature", "climate.upstairs", {"temperature": 70})).ok
    assert check(t, H("set_temperature", "climate.upstairs", {"temperature": 72})).reason == "argument_not_grounded:temperature"
    assert check("set the upstairs thermostat to 200 degrees", H("set_temperature", "climate.upstairs", {"temperature": 200})).reason == "argument_out_of_range:temperature"


def test_validator_never_raises():
    cands, top = setup("turn off the den lamp")
    for junk in (object(), 5, b"x", {"decision": {"a": 1}}):
        v = qwen.validate(junk, utterance="x", candidates=cands, top=top, allow_sensitive=False)
        assert not v.ok


def test_request_shape_and_cache_friendliness():
    cands, _ = setup("turn off the den lamp")
    a = qwen.build_request("qwen-fast", "turn off the den lamp", cands)
    b = qwen.build_request("qwen-fast", "something else", cands[:1])
    assert a["messages"][0] == b["messages"][0]                    # static prefix identical -> llama-server prompt cache hit
    assert a["messages"][1]["content"].endswith("Request: turn off the den lamp")
    enum = a["response_format"]["json_schema"]["schema"]["properties"]["target"]["enum"]
    assert enum == [x.id for x in cands] + [None]                  # the model can only choose a listed candidate
    assert a["chat_template_kwargs"] == {"enable_thinking": False}
    assert a["temperature"] == 0 and a["max_tokens"] <= 96
