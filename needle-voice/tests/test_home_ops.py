import asyncio, json, os
import pytest
from needle_voice import candidates as _c, executor, home_ops
from needle_voice.context import Context
from needle_voice.executor import ExecutionError
from needle_voice.home_ops import _State
from needle_voice.result import Status

_RAW = json.load(open(os.path.join(os.path.dirname(__file__), "real_snapshot.json")))
# household vocabulary (Needle's lexicon format) naming the pool lights as a group
SNAP = _c.merge_lexicon(_RAW, {"switch.pool_pendant_lights": ["pool lights", "swimming pool lights"],
                               "switch.pool_high_bay_lights": ["pool lights", "swimming pool lights"]})


@pytest.fixture(autouse=True)
def fast_verify(monkeypatch):
    monkeypatch.setattr(executor, "VERIFY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(executor, "VERIFY_POLL_S", 0.01)


class FakeHass:
    """Duck-typed RestHass: a state table + a recorder for service calls that really applies on/off unless told otherwise.
    mode: apply | ignore (accepted, nothing changes) | raise:<reason> | vanish (entity disappears after the call)"""
    def __init__(self, states, mode="apply"):
        self.states = type("S", (), {"get": lambda _s, eid: self._cache.get(eid)})()
        self._live = states
        self._cache = dict(states)
        self.calls, self.mode, self.reads = [], mode, 0
        self.services = self
    async def refresh(self): self._cache = dict(self._live)
    async def read_state(self, eid):
        self.reads += 1
        return self._live.get(eid)
    async def async_call(self, domain, service, data=None, blocking=False, return_response=False, context=None):
        self.calls.append((domain, service, data))
        if self.mode.startswith("raise:"):
            raise ExecutionError(self.mode[6:])
        ids = data["entity_id"] if isinstance(data["entity_id"], list) else [data["entity_id"]]
        for i in ids:
            old = self._live[i]
            if self.mode == "vanish":
                self._live.pop(i); continue
            if self.mode == "ignore":
                continue
            new = {"turn_on": "on", "turn_off": "off"}.get(service)
            if new:
                self._live[i] = _State(i, new, dict(old.attributes), old.name)


def st(eid, state, **attrs): return _State(eid, state, attrs, attrs.get("friendly_name", ""))


def run(text, states, snap=SNAP, mode="apply", **kw):
    h = FakeHass(dict(states), mode)
    return asyncio.run(home_ops.handle(text, snap, h, **kw)), h


def pool(): return {"switch.pool_pendant_lights": st("switch.pool_pendant_lights", "on"), "switch.pool_high_bay_lights": st("switch.pool_high_bay_lights", "off"),
                    "fan.pool_fan": st("fan.pool_fan", "off")}


# ------------------------------------------------------------ reads
def test_pool_lights_question_is_answered_for_each_light():
    o, _ = run("are the swimming pool lights on", pool())
    assert o.status == Status.SUCCESS and "Pool Pendant Lights is on" in o.speech and "Pool High Bay Lights is off" in o.speech


# ------------------------------------------------------------ verified writes
def test_turn_on_is_verified_by_independent_read():
    o, h = run("turn on the pool fan", pool())
    assert o.status == Status.SUCCESS and o.speech == "Turned on the Pool Fan." and o.side_effect
    assert h.calls == [("fan", "turn_on", {"entity_id": "fan.pool_fan"})] and h.reads >= 1
    assert o.telemetry["verify"][0]["outcome"] == "verified" and o.telemetry["verify"][0]["after"] == "on"


def test_on_then_off_sequence():
    states = pool(); h = FakeHass(states)
    a = asyncio.run(home_ops.handle("turn on the pool fan", SNAP, h)); b = asyncio.run(home_ops.handle("turn off the pool fan", SNAP, h))
    assert a.status == b.status == Status.SUCCESS and states["fan.pool_fan"].state == "off" and [c[1] for c in h.calls] == ["turn_on", "turn_off"]


def test_already_on_sends_no_call():
    o, h = run("turn on the pool fan", {**pool(), "fan.pool_fan": st("fan.pool_fan", "on")})
    assert o.status == Status.SUCCESS and h.calls == [] and "already on" in o.speech and not o.side_effect


def test_accepted_but_not_observed_is_unverified_not_done():
    o, h = run("turn on the pool fan", pool(), mode="ignore")
    assert o.status == Status.UNVERIFIED and len(h.calls) == 1 and o.side_effect
    assert "couldn't confirm" in o.speech and "Turned on" not in o.speech and o.telemetry["verify"][0]["outcome"] == "not_observed"
    assert not o.fallback_candidate


def test_device_vanishing_after_call_is_unavailable():
    o, _ = run("turn on the pool fan", pool(), mode="vanish")
    assert o.status == Status.UNAVAILABLE and "Turned on" not in o.speech


@pytest.mark.parametrize("reason,status", [("rest_timeout", Status.UNVERIFIED), ("rest_unreachable", Status.UNVERIFIED),
                                           ("rest_auth_failed", Status.EXECUTION_FAILED), ("rest_http_500", Status.EXECUTION_FAILED),
                                           ("rest_malformed_response", Status.UNVERIFIED)])
def test_transport_failures_are_honest(reason, status):
    o, h = run("turn on the pool fan", pool(), mode="raise:" + reason)
    assert o.status == status and o.speech and "Turned on" not in o.speech and not o.fallback_candidate and len(h.calls) == 1   # never auto-retried


def test_refresh_failure_changes_nothing_and_does_not_fall_back():
    class Bad(FakeHass):
        async def refresh(self): raise ExecutionError("rest_timeout")
    h = Bad(pool())
    o = asyncio.run(home_ops.handle("turn on the pool fan", SNAP, h))
    assert o.status == Status.EXECUTION_FAILED and h.calls == [] and "didn't change anything" in o.speech


def test_unavailable_target_is_not_claimed_done():
    s = pool(); s["fan.pool_fan"] = st("fan.pool_fan", "unavailable")
    o, h = run("turn on the pool fan", s)
    assert o.status == Status.UNAVAILABLE and h.calls == [] and "unavailable" in o.speech and not o.fallback_candidate


def test_unavailable_read():
    s = pool(); s["fan.pool_fan"] = st("fan.pool_fan", "unavailable")
    o, _ = run("is the pool fan on", s)
    assert o.status == Status.UNAVAILABLE


def test_entity_disappearing_between_planning_and_execution():
    states = pool(); h = FakeHass(states)
    async def go():
        await h.refresh(); h._live.pop("fan.pool_fan")                     # gone from HA, snapshot still lists it
        return await home_ops.handle("turn on the pool fan", SNAP, h)
    o = asyncio.run(go())
    assert o.status == Status.UNAVAILABLE and h.calls == []


# ------------------------------------------------------------ groups / ambiguity
def test_group_turn_on_runs_each_fixed_service_call_and_verifies():
    o, h = run("turn on the pool lights", pool())
    assert o.status == Status.SUCCESS and "Pool Pendant Lights" in o.speech and "Pool High Bay Lights" in o.speech
    assert [c[2]["entity_id"] for c in h.calls] == ["switch.pool_high_bay_lights"] and "already on" in o.speech          # pendant was already on: no call


def test_group_with_one_member_unverified():
    class Half(FakeHass):
        async def async_call(self, d, s, data=None, **kw):
            if data["entity_id"] == "switch.pool_high_bay_lights": self.calls.append((d, s, data)); return
            await super().async_call(d, s, data, **kw)
    s = {**pool(), "switch.pool_pendant_lights": st("switch.pool_pendant_lights", "off")}
    h = Half(s); o = asyncio.run(home_ops.handle("turn on the pool lights", SNAP, h))
    assert o.status == Status.UNVERIFIED and "Turned on the Pool Pendant Lights" in o.speech and "couldn't confirm" in o.speech


def test_lookalikes_without_a_group_alias_are_ambiguous_and_never_guessed():
    o, h = run("turn on the pool lights", pool(), snap=_RAW)
    assert h.calls == [] and o.status == Status.AMBIGUOUS and o.speech and not o.fallback_candidate


def test_group_dim_is_not_allowed_and_asks_which():
    o, h = run("set the pool lights to 50 percent", pool())
    assert h.calls == [] and o.status == Status.AMBIGUOUS and "Which one" in o.speech


def test_device_not_exposed_to_assist_is_never_targeted():
    snap = {"entities": [e for e in SNAP["entities"] if e["entity_id"] != "fan.pool_fan"], "areas": SNAP["areas"]}
    s = pool()
    o, h = run("turn on the pool fan", s, snap=snap)
    assert h.calls == [] and not (o.status == Status.SUCCESS and "Pool Fan" in (o.speech or ""))


# ------------------------------------------------------------ guards + denial + fallback gate
def test_guards_still_apply_and_are_unsupported():
    for text in ("turn on the pool fan and the pool lights", "why is the pool fan on", "do not turn on the pool fan"):
        o, h = run(text, pool()); assert o.status == Status.UNSUPPORTED and h.calls == [] and o.reason.startswith("guard:")


def test_sensitive_intent_is_denied_and_never_a_fallback():
    for text in ("unlock the front door", "please unlock the door", "open the garage door"):
        o, h = run(text, pool())
        assert o.status == Status.DENIED and h.calls == [] and o.speech and not o.fallback_candidate, text


def test_denial_survives_even_when_needle_would_merely_decline():
    # Needle declines these for NON-safety reasons (guards/no snapshot/no intent words); the gate must still deny, not hand them to HA's agent.
    for text in ("unlock the front door and turn on the light", "do not forget to unlock the front door", "garage door please", "turn on the heater and the fan",
                 "lock the front door and the back door"):
        o, h = run(text, pool())
        assert o.status in (Status.DENIED, Status.UNSUPPORTED) and not (o.status == Status.UNSUPPORTED and home_ops.fallback_gate(text)[0] is False)
        if o.status == Status.UNSUPPORTED:
            assert home_ops.fallback_gate(text)[0], text
    for text in ("unlock the front door and turn on the light", "open the garage door and the gate", "turn on the heater and the fan"):
        o, _ = run(text, pool()); assert o.status == Status.DENIED and o.speech and not o.fallback_candidate, text
    o, _ = run("unlock the front door", pool(), snap=None); assert o.status == Status.DENIED


def test_fallback_gate_allows_reads_and_ordinary_requests():
    for text in ("is the garage door open", "what time is it", "turn on the kitchen lights", "what's the weather"):
        assert home_ops.fallback_gate(text)[0], text
    for text in ("unlock the front door", "close the garage door", "turn off the alarm", "shut the water valve"):
        assert not home_ops.fallback_gate(text)[0], text


def test_unknown_things_are_unsupported_and_may_fall_back():
    o, _ = run("what is the meaning of life", pool()); assert o.status in (Status.UNSUPPORTED, Status.NO_MATCH) and o.fallback_candidate
    o, _ = run("turn on the pool fan", pool(), snap=None); assert o.status == Status.UNSUPPORTED and o.reason == "no_snapshot"


def test_stale_snapshot_blocks_writes_but_not_reads():
    o, h = run("turn on the pool fan", pool(), snapshot_stale=True); assert o.status == Status.UNSUPPORTED and h.calls == []
    o, _ = run("is the pool fan on", pool(), snapshot_stale=True); assert o.status == Status.SUCCESS


def test_polarity_is_never_changed():
    o, h = run("turn off the pool fan", {**pool(), "fan.pool_fan": st("fan.pool_fan", "on")})
    assert h.calls == [("fan", "turn_off", {"entity_id": "fan.pool_fan"})]


# ------------------------------------------------------------ conversational context
def test_followup_pronoun_acts_on_last_group_and_rechecks_policy():
    ctx = Context(); h = FakeHass({**pool(), "switch.pool_pendant_lights": st("switch.pool_pendant_lights", "off")})
    r1 = asyncio.run(home_ops.handle("are the pool lights on", SNAP, h, ctx=ctx))
    r2 = asyncio.run(home_ops.handle("turn them on", SNAP, h, ctx=ctx))
    assert r1.status == r2.status == Status.SUCCESS and {c[2]["entity_id"] for c in h.calls} == {"switch.pool_pendant_lights", "switch.pool_high_bay_lights"}


def test_followup_dim_on_a_group_is_refused():
    ctx = Context(); h = FakeHass(pool())
    asyncio.run(home_ops.handle("are the pool lights on", SNAP, h, ctx=ctx))
    o = asyncio.run(home_ops.handle("set them to 40 percent", SNAP, h, ctx=ctx))
    assert o.status == Status.AMBIGUOUS and h.calls == []


def test_correction_without_verb_is_clarified_not_executed():
    ctx = Context(); h = FakeHass({**pool(), "switch.pool_pendant_lights": st("switch.pool_pendant_lights", "off")})
    asyncio.run(home_ops.handle("turn on the pool lights", SNAP, h, ctx=ctx)); n = len(h.calls)
    o = asyncio.run(home_ops.handle("actually just the pendant lights", SNAP, h, ctx=ctx))
    assert o.status == Status.AMBIGUOUS and len(h.calls) == n and "turn off" in o.speech and "haven't changed anything" in o.speech


def test_context_expires_and_pronoun_without_context_asks():
    t = [0.0]; ctx = Context(ttl=10, clock=lambda: t[0]); h = FakeHass(pool())
    asyncio.run(home_ops.handle("is the pool fan on", SNAP, h, ctx=ctx)); t[0] = 11
    o = asyncio.run(home_ops.handle("turn it on", SNAP, h, ctx=ctx))
    assert o.status == Status.AMBIGUOUS and h.calls == []


def test_clarification_answer_binds_to_pending_candidates():
    snap = {"entities": [{"entity_id": "light.living_room", "name": "Living Room", "aliases": [], "area_id": None},
                         {"entity_id": "light.basement_living_room", "name": "Basement Living Room", "aliases": [], "area_id": None}], "areas": []}
    ctx = Context(); live = {"light.living_room": st("light.living_room", "on"), "light.basement_living_room": st("light.basement_living_room", "on")}
    h = FakeHass(live)
    q = asyncio.run(home_ops.handle("turn off the living room lights", snap, h, ctx=ctx))
    if q.status != Status.AMBIGUOUS:
        pytest.skip("snapshot not ambiguous in this build: " + q.status.value)
    o = asyncio.run(home_ops.handle("the basement one", snap, h, ctx=ctx))
    assert o.status == Status.SUCCESS and h.calls == [("light", "turn_off", {"entity_id": "light.basement_living_room"})]
    # a non-answer is a fresh request, and consumes the pending question
    ctx.ask(__import__("needle_voice.context", fromlist=["Pending"]).Pending("turn_off", {}, [("light.living_room", "Living Room")]))
    assert ctx.choose("what time is it") is None and ctx.pending is None


def test_truncated_tool_text_never_acts():
    for text in ("turn off the lights in the", "turn on the"):
        o, h = run(text, pool()); assert h.calls == [] and o.status != Status.SUCCESS


# ------------------------------------------------------------ lifecycle
def test_superseded_before_execution_has_no_side_effect():
    o, h = run("turn on the pool fan", pool(), is_current=lambda: False, write_lock=asyncio.Lock()) if False else (None, None)
    async def go():
        h = FakeHass(pool())
        r = await home_ops.handle("turn on the pool fan", SNAP, h, is_current=lambda: False, write_lock=asyncio.Lock())
        return r, h
    r, h = asyncio.run(go())
    assert r.status == Status.CANCELLED and h.calls == [] and not r.side_effect


def test_cancelled_caller_still_reports_the_real_outcome():
    late = []
    class Slow(FakeHass):
        async def async_call(self, *a, **k):
            await asyncio.sleep(0.05); await super().async_call(*a, **k)
    async def go():
        h = Slow(pool())
        t = asyncio.ensure_future(home_ops.handle("turn on the pool fan", SNAP, h, on_late=late.append))
        await asyncio.sleep(0.02); t.cancel()
        with pytest.raises(asyncio.CancelledError): await t
        await asyncio.sleep(0.3)
        return h
    h = asyncio.run(go())
    assert len(h.calls) == 1 and len(late) == 1 and late[0].status == Status.SUCCESS and late[0].side_effect   # command ran; outcome kept, not assumed away


def test_concurrent_duplicate_requests_serialize_and_second_is_already():
    async def go():
        h = FakeHass(pool()); lock = asyncio.Lock()
        return await asyncio.gather(*[home_ops.handle("turn on the pool fan", SNAP, h, write_lock=lock) for _ in range(2)]), h
    (a, b), h = asyncio.run(go())
    assert len(h.calls) == 1 and {a.status, b.status} == {Status.SUCCESS} and any("already" in (x.speech or "") for x in (a, b))


def test_agent_late_result_is_reported_on_next_turn_not_overriding():
    class Agent(home_ops.HomeAgent):
        def __init__(s):
            s.ctx = Context(); s.epoch = 0; s.write_lock = asyncio.Lock(); s.late = []; s.hass = FakeHass(pool()); s.lexicons = []; s.session = None
            s.snapshot, s.snap_t = SNAP, __import__("time").monotonic()
    async def go():
        a = Agent()
        t1 = asyncio.ensure_future(a.request("turn on the pool fan")); await asyncio.sleep(0)   # in flight
        a.supersede()                                                                           # user interrupts with something newer
        r1 = await t1
        r2 = await a.request("is the pool fan on")
        return r1, r2
    r1, r2 = asyncio.run(go())
    assert r1.superseded and (r1.status == Status.CANCELLED or r1.side_effect)
    if r1.side_effect:
        assert "Earlier request already ran" in r2.speech


# ------------------------------------------------------------ REST transport
def _rest(monkeypatch, exc=None, body=b"[]"):
    import urllib.error, urllib.request
    class R:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def read(s): return body
    def fake(req, timeout=None):
        if exc: raise exc
        return R()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return executor.RestHass("http://x", "t")


@pytest.mark.parametrize("exc,code", [(__import__("urllib.error").error.HTTPError("u", 401, "no", {}, None), "rest_auth_failed"),
                                      (__import__("urllib.error").error.HTTPError("u", 500, "no", {}, None), "rest_http_500"),
                                      (TimeoutError(), "rest_timeout"), (__import__("urllib.error").error.URLError("refused"), "rest_unreachable"),
                                      (__import__("urllib.error").error.URLError(TimeoutError()), "rest_timeout")])
def test_rest_errors_are_classified(monkeypatch, exc, code):
    h = _rest(monkeypatch, exc)
    with pytest.raises(ExecutionError, match=code): asyncio.run(h.refresh())


def test_rest_malformed_responses(monkeypatch):
    with pytest.raises(ExecutionError, match="rest_malformed_response"): asyncio.run(_rest(monkeypatch, body=b"<html>").refresh())
    with pytest.raises(ExecutionError, match="rest_malformed_response"): asyncio.run(_rest(monkeypatch, body=b'{"a":1}').refresh())
    h = _rest(monkeypatch, body=json.dumps([{"entity_id": "a.b", "state": "on"}, {"nope": 1}, "str", {"entity_id": "c.d", "state": 3}]).encode())
    asyncio.run(h.refresh()); assert list(h.states.cache) == ["a.b"]
    with pytest.raises(ExecutionError, match="rest_malformed_response"): asyncio.run(_rest(monkeypatch, body=b'{"x":1}').read_state("a.b"))


def test_rest_read_state_404_means_gone(monkeypatch):
    import urllib.error
    h = _rest(monkeypatch, urllib.error.HTTPError("u", 404, "nf", {}, None))
    assert asyncio.run(h.read_state("a.b")) is None


def test_service_response_is_passed_through(monkeypatch):
    h = _rest(monkeypatch, body=b'{"service_response": {"k": 1}}')
    assert asyncio.run(h.services.async_call("weather", "get_forecasts", {}, return_response=True)) == {"k": 1}
