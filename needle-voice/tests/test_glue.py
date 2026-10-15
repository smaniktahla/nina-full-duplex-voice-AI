"""HA glue (conversation.py, __init__.py, config_flow.py) against STUBBED Home
Assistant modules. Proves this package's own logic; it cannot prove the real HA
APIs behave the way the stubs assume -- the live tests do that."""
import asyncio, importlib, json, sys, types
import pytest


class Ctx:
    id = "ctx-1"


class UserInput:
    def __init__(self, text, conversation_id="conv-1", language="en", device_id="dev-1"):
        self.text, self.conversation_id, self.language, self.device_id, self.context = text, conversation_id, language, device_id, Ctx()


class FakeIntentResponse:
    def __init__(self, language=None):
        self.language, self.speech, self.error = language, None, None

    def async_set_speech(self, s):
        self.speech = s

    def async_set_error(self, code, msg):
        self.error = (code, msg)


class FakeResult:
    def __init__(self, response, conversation_id=None, continue_conversation=False):
        self.response, self.conversation_id, self.continue_conversation = response, conversation_id, continue_conversation


@pytest.fixture(scope="module")
def glue():
    mp = pytest.MonkeyPatch()

    def mod(name):
        m = types.ModuleType(name)
        mp.setitem(sys.modules, name, m)
        return m

    mod("homeassistant"); comps = mod("homeassistant.components"); conv = mod("homeassistant.components.conversation")

    class ConversationEntity: ...
    class AbstractConversationAgent: ...
    class Feature: CONTROL = 1
    conv.ConversationEntity, conv.AbstractConversationAgent = ConversationEntity, AbstractConversationAgent
    conv.ConversationEntityFeature, conv.ConversationInput, conv.ConversationResult = Feature, UserInput, FakeResult
    conv.calls = []

    async def async_converse(hass, text, conversation_id, context, language=None, agent_id=None, device_id=None, satellite_id=None):
        conv.calls.append(dict(text=text, conversation_id=conversation_id, context=context, language=language, agent_id=agent_id,
                               device_id=device_id, satellite_id=satellite_id))
        if conv.raise_next:
            conv.raise_next = False
            raise RuntimeError("hermes down")
        return conv.hermes_result
    conv.async_converse, conv.raise_next, conv.hermes_result = async_converse, False, object()
    comps.conversation = conv

    const = mod("homeassistant.const"); const.MATCH_ALL = "*"
    helpers = mod("homeassistant.helpers"); intent = mod("homeassistant.helpers.intent")
    intent.IntentResponse = FakeIntentResponse
    intent.IntentResponseErrorCode = types.SimpleNamespace(UNKNOWN="unknown")
    helpers.intent = intent
    util = mod("homeassistant.util"); ulid = mod("homeassistant.util.ulid"); ulid.ulid_now = lambda: "ULID123"; util.ulid = ulid

    ce = mod("homeassistant.config_entries")
    class ConfigFlow:
        def __init_subclass__(cls, domain=None, **kw): cls.domain = domain
        async def async_set_unique_id(self, u): self.uid = u
        def _abort_if_unique_id_configured(self): ...
        def async_show_form(self, step_id): return {"type": "form", "step_id": step_id}
        def async_create_entry(self, title, data): return {"type": "create_entry", "title": title, "data": data}
    ce.ConfigFlow = ConfigFlow

    for name in [n for n in sys.modules if n == "needle_voice" or n.startswith("needle_voice.")]:
        del sys.modules[name]
    m = importlib.import_module("needle_voice.conversation")
    m.CONV = conv
    yield m
    mp.undo()
    for name in [n for n in sys.modules if n == "needle_voice" or n.startswith("needle_voice.")]:
        del sys.modules[name]


SNAP = {"entities": [{"entity_id": "light.kitchen_main", "name": "Kitchen Main", "aliases": [], "area_id": "kitchen"}],
        "areas": [{"area_id": "kitchen", "name": "Kitchen", "aliases": []}]}


class Hass:
    def __init__(self, cfg=None):
        self.data = {"needle_voice": {"config": {**__import__("needle_voice.const", fromlist=["x"]).DEFAULT_CONFIG, **(cfg or {}),
                                                  "hermes_agent_id": "conversation.hermes_test"}}}
        self.service_calls, self.jobs, self.tasks = [], [], []
        self.services = types.SimpleNamespace(async_call=self._call)
        self.states = types.SimpleNamespace(get=lambda e: types.SimpleNamespace(state="off", entity_id=e, attributes={}, name=e))

    async def _call(self, domain, service, data, blocking=False, return_response=False, context=None):
        self.service_calls.append((domain, service, data, blocking))

    def async_create_task(self, coro):
        self.tasks.append(coro)

    def async_add_executor_job(self, fn, *a):
        fut = asyncio.get_event_loop().create_future()
        try:
            fut.set_result(fn(*a))
        except Exception as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut


@pytest.fixture
def entity(glue, monkeypatch):
    glue.CONV.calls.clear(); glue.CONV.raise_next = False; glue.CONV.hermes_result = object()
    records = []
    monkeypatch.setattr(glue.telemetry, "write", lambda path, rec: records.append(rec) or True)
    monkeypatch.setattr(glue, "build_entity_snapshot", lambda hass: SNAP)
    monkeypatch.setattr(glue, "read_runtime_files", lambda a, b: ({}, False))

    def make(cfg=None):
        e = glue.NeedleVoiceEntity(Hass(cfg), types.SimpleNamespace(entry_id="entry-1"))
        e.records = records
        return e
    return make


def needle_says(glue, monkeypatch, tool, args):
    na = glue.needle_adapter
    monkeypatch.setattr(na, "route", lambda text: na.RouteDecision("ha_action", True, True, tool_name=tool, arguments=args, confidence=0.4, needle_raw={}))


def process(e, text, **kw):
    return asyncio.run(e.async_process(UserInput(text, **kw)))


def test_fast_path_answers_without_touching_hermes(glue, entity, monkeypatch):
    needle_says(glue, monkeypatch, "turn_on_light", {"area": "kitchen"})
    e = entity()
    res = process(e, "turn on the kitchen lights")
    assert res.response.speech == "Turned on the Kitchen light."
    assert res.conversation_id == "conv-1" and res.continue_conversation is False
    assert res.response.language == "en"
    assert e.hass.service_calls == [("light", "turn_on", {"entity_id": ["light.kitchen_main"]}, True)]
    assert glue.CONV.calls == []
    rec = e.records[0]
    assert rec["route"] == "needle" and rec["final_success"] is True and rec["fallback_reason"] is None
    assert rec["total_latency_ms"] >= 0 and rec["snapshot"] == {"entities": 1, "areas": 1} and rec["device_id"] == "dev-1"


def test_fallback_hands_the_original_request_to_hermes_unchanged(glue, entity):
    e = entity()
    res = process(e, "Why do you think the pool temperature is dropping faster tonight?", conversation_id="conv-9", language="en", device_id="sat-3")
    assert res is glue.CONV.hermes_result                            # Hermes's result returned as-is
    call = glue.CONV.calls[0]
    assert call["text"] == "Why do you think the pool temperature is dropping faster tonight?"
    assert (call["conversation_id"], call["language"], call["device_id"], call["agent_id"]) == ("conv-9", "en", "sat-3", "conversation.hermes_test")
    assert call["context"] is not None
    assert e.hass.service_calls == []
    rec = e.records[0]
    assert rec["route"] == "hermes" and rec["fallback_reason"] == "guard:reasoning_markers" and rec["hermes_latency_ms"] is not None


def test_kwargs_are_filtered_to_what_async_converse_accepts(glue, entity, monkeypatch):
    seen = {}
    async def narrow(hass, text, conversation_id, context, language=None, agent_id=None):
        seen.update(text=text, agent_id=agent_id)
        return "R"
    monkeypatch.setattr(glue.conversation, "async_converse", narrow)
    e = entity()
    assert process(e, "explain why the sky is blue") == "R" and seen["agent_id"] == "conversation.hermes_test"


def test_hermes_failure_yields_a_spoken_error_not_an_exception(glue, entity):
    glue.CONV.raise_next = True
    e = entity()
    res = process(e, "explain why the sky is blue")
    assert res.response.error is not None and "assistant" in res.response.error[1]
    assert e.records[0]["final_success"] is False


def test_fallback_disabled_says_sorry_without_calling_hermes(glue, entity):
    e = entity({"hermes_fallback_enabled": False})
    res = process(e, "explain why the sky is blue")
    assert res.response.error is not None and glue.CONV.calls == []


def test_router_crash_fails_open_to_hermes(glue, entity, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("router bug")
    monkeypatch.setattr(glue, "handle", boom)
    e = entity()
    res = process(e, "turn on the kitchen lights")
    assert res is glue.CONV.hermes_result and e.records[0]["fallback_reason"] == "glue_exception"


def test_snapshot_failure_goes_to_hermes(glue, entity, monkeypatch):
    monkeypatch.setattr(glue, "build_entity_snapshot", lambda hass: None)
    e = entity()
    assert process(e, "turn on the kitchen lights") is glue.CONV.hermes_result
    assert e.records[0]["fallback_reason"] == "no_snapshot" and e.records[0]["snapshot"] is None


def test_passthrough_flag_forces_hermes(glue, entity, monkeypatch):
    monkeypatch.setattr(glue, "read_runtime_files", lambda a, b: ({}, True))
    e = entity()
    assert process(e, "turn on the kitchen lights") is glue.CONV.hermes_result
    assert e.records[0]["fallback_reason"] == "passthrough_flag"


def test_missing_conversation_id_gets_a_new_one(glue, entity, monkeypatch):
    needle_says(glue, monkeypatch, "turn_on_light", {"area": "kitchen"})
    assert process(entity(), "turn on the kitchen lights", conversation_id=None).conversation_id == "ULID123"


def test_qwen_transport_is_used_for_tier_two(glue, entity, monkeypatch):
    monkeypatch.setattr(glue.needle_adapter, "route", lambda t: glue.needle_adapter.RouteDecision("conversation", False, False, confidence=0.0, needle_raw={}))
    snap = {"entities": [{"entity_id": "media_player.plex", "name": "Plex", "aliases": [], "area_id": None}], "areas": []}
    monkeypatch.setattr(glue, "build_entity_snapshot", lambda hass: snap)
    e = entity()
    sent = {}
    async def fake_post(payload, timeout):
        sent.update(payload=payload, timeout=timeout)
        return {"choices": [{"message": {"content": json.dumps({"decision": "handle", "intent": "media_pause", "target": "media_player.plex", "arguments": {}})}}]}
    e._qwen_post = fake_post
    res = process(e, "pause plex")
    assert res.response.speech == "Paused the Plex." and sent["timeout"] == 2.0
    assert e.hass.service_calls == [("media_player", "media_pause", {"entity_id": "media_player.plex"}, True)]
    assert e.records[0]["route"] == "qwen_fast" and e.records[0]["qwen"]["latency_ms"] >= 0


def test_warm_up_never_raises(glue, entity):
    e = entity()
    async def fail(payload, timeout):
        raise ConnectionError("down")
    e._qwen_post = fail
    asyncio.run(e.async_warm_up())


def test_entity_basics(glue, entity):
    e = entity()
    assert e.supported_languages == "*" and e._attr_unique_id == "entry-1" and e._attr_name == "Needle Voice"


def test_platform_setup_adds_entity_and_schedules_warmup(glue):
    added, scheduled = [], []
    entry = types.SimpleNamespace(entry_id="e", async_create_background_task=lambda hass, coro, name: (scheduled.append(name), coro.close()))
    asyncio.run(glue.async_setup_entry(Hass(), entry, lambda ents: added.extend(ents)))
    assert len(added) == 1 and scheduled == ["needle_voice_warm_up"]


# ---------------- __init__ and config flow ----------------
def test_config_schema_and_defaults(glue):
    import needle_voice as nv
    assert nv.CONFIG_SCHEMA({"needle_voice": {"qwen_timeout_seconds": 1.5, "qwen_fast_enabled": False}})
    assert nv.CONFIG_SCHEMA({"needle_voice": None})
    for bad in ({"qwen_timeout_seconds": 99}, {"qwen_fast_enabled": "maybe"}):
        with pytest.raises(Exception):
            nv.CONFIG_SCHEMA({"needle_voice": bad})


def test_setup_applies_config_and_requests_the_config_entry(glue):
    import needle_voice as nv
    created = []
    hass = types.SimpleNamespace(
        data={},
        config_entries=types.SimpleNamespace(async_entries=lambda d: [], flow=types.SimpleNamespace(async_init=lambda *a, **k: ("flow", a, k))),
        async_create_task=lambda x: created.append(x))
    assert asyncio.run(nv.async_setup(hass, {"needle_voice": {"qwen_endpoint": "http://x:1/v1/", "needle_endpoint": "http://n:2/", "needle_timeout_seconds": 0.7}}))
    cfg = hass.data["needle_voice"]["config"]
    assert cfg["qwen_endpoint"] == "http://x:1/v1/" and cfg["enabled"] is True
    assert nv.needle_adapter.NEEDLE_ENDPOINT == "http://n:2" and nv.needle_adapter.NEEDLE_TIMEOUT_S == 0.7
    assert created and created[0][2] == {"context": {"source": "import"}, "data": {}}


def test_config_flow_creates_single_entry(glue):
    cf = importlib.import_module("needle_voice.config_flow")
    flow = cf.NeedleVoiceConfigFlow()
    assert asyncio.run(flow.async_step_user())["type"] == "form"
    assert asyncio.run(flow.async_step_user({}))["title"] == "Needle Voice"
    assert asyncio.run(flow.async_step_import({}))["type"] == "create_entry"


def test_qwen_timeout_triggers_one_background_rewarm_at_a_time(glue, entity, monkeypatch):
    monkeypatch.setattr(glue.needle_adapter, "route", lambda t: glue.needle_adapter.RouteDecision("conversation", False, False, confidence=0.0, needle_raw={}))
    snap = {"entities": [{"entity_id": "media_player.plex", "name": "Plex", "aliases": [], "area_id": None}], "areas": []}
    monkeypatch.setattr(glue, "build_entity_snapshot", lambda hass: snap)
    e = entity()
    warmups = []

    async def slow(payload, timeout):
        raise asyncio.TimeoutError()

    async def fake_warm():
        warmups.append(1)
    e._qwen_post, e.async_warm_up = slow, fake_warm
    res = process(e, "pause plex")
    assert res is glue.CONV.hermes_result and e.records[0]["fallback_reason"] == "qwen_timeout"   # user still gets an answer
    assert len(e.hass.tasks) == 1 and e._warming is True
    process(e, "pause plex")                                    # second timeout while a warm-up is pending
    assert len(e.hass.tasks) == 1                               # no pile-up
    asyncio.run(e.hass.tasks[0])
    assert warmups == [1] and e._warming is False              # flag clears so a later timeout can re-warm


# ---------------- runaway-loop protection (observed live 09/24: satellite transcribed its own voice) ----------------
def _hermes_asks_a_question(glue):
    resp = FakeIntentResponse("en")
    resp.async_set_speech("What exactly do you want me to do with it, turn it on or check its status?")
    return FakeResult(resp, conversation_id="h-conv", continue_conversation=True)


def test_a_hermes_reply_that_asks_a_question_does_not_keep_the_mic_open(glue, entity):
    glue.CONV.hermes_result = _hermes_asks_a_question(glue)
    e = entity()
    res = process(e, "explain why the sky is blue")
    assert res is glue.CONV.hermes_result and res.continue_conversation is False       # same result, mic released
    assert res.response.speech.startswith("What exactly")                              # the answer itself is untouched
    assert e.records[0]["follow_up_suppressed"] is True


def test_follow_up_can_be_re_enabled_in_config(glue, entity):
    glue.CONV.hermes_result = _hermes_asks_a_question(glue)
    res = process(entity({"hermes_follow_up_enabled": True}), "explain why the sky is blue")
    assert res.continue_conversation is True


def test_a_frozen_result_is_rebuilt_rather_than_crashing(glue, entity):
    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class Frozen:
        response: object
        conversation_id: str
        continue_conversation: bool = True

    glue.CONV.hermes_result = Frozen(FakeIntentResponse("en"), "h-conv")
    res = process(entity(), "explain why the sky is blue")
    assert res.continue_conversation is False and res.conversation_id == "h-conv"


def test_fast_path_results_never_keep_the_mic_open(glue, entity, monkeypatch):
    needle_says(glue, monkeypatch, "turn_on_light", {"area": "kitchen"})
    assert process(entity(), "turn on the kitchen lights").continue_conversation is False


def test_loop_breaker_stops_calling_hermes_for_a_runaway_device(glue, entity):
    e = entity({"loop_breaker_max": 3, "loop_breaker_window_seconds": 300})
    for _ in range(3):
        assert process(e, "explain why the sky is blue", device_id="sat-A") is glue.CONV.hermes_result
    assert len(glue.CONV.calls) == 3
    res = process(e, "explain why the sky is blue", device_id="sat-A")               # 4th within the window
    assert len(glue.CONV.calls) == 3                                                 # Hermes NOT called
    assert res.response.speech == "Okay, I'll pause for a bit." and res.continue_conversation is False
    assert e.records[-1]["loop_breaker"] is True and e.records[-1]["final_success"] is False
    assert process(e, "explain why the sky is blue", device_id="sat-B") is glue.CONV.hermes_result   # other devices unaffected


def test_loop_breaker_clears_after_the_window(glue, entity, monkeypatch):
    e = entity({"loop_breaker_max": 2, "loop_breaker_window_seconds": 100})
    clock = [1000.0]
    monkeypatch.setattr(glue.time, "monotonic", lambda: clock[0])
    process(e, "explain why the sky is blue"); process(e, "explain why the sky is blue")
    process(e, "explain why the sky is blue")
    assert len(glue.CONV.calls) == 2                                                 # third refused
    clock[0] += 101
    assert process(e, "explain why the sky is blue") is glue.CONV.hermes_result       # window passed -> allowed again
    assert len(glue.CONV.calls) == 3


def test_refused_requests_do_not_extend_the_breaker(glue, entity, monkeypatch):
    e = entity({"loop_breaker_max": 1, "loop_breaker_window_seconds": 100})
    clock = [0.0]
    monkeypatch.setattr(glue.time, "monotonic", lambda: clock[0])
    process(e, "explain why the sky is blue")
    for _ in range(5):
        clock[0] += 10
        process(e, "explain why the sky is blue")                                    # refused, must not push the window out
    clock[0] = 101
    assert process(e, "explain why the sky is blue") is glue.CONV.hermes_result
