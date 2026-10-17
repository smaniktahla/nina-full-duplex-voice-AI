"""Turns a VALIDATED intent into HA service calls / state reads, and speech.

This is the only module in the component that can change the house, and it
does so through a fixed table (intents.INTENTS): an intent maps to specific
(domain, service) pairs, a target must be in the exposed-entity snapshot, and
an area target expands to that area's exposed `light` entities only. No
model-provided service name, entity list or free-form data ever reaches
`hass.services.async_call`.

`hass` is duck-typed (states.get, services.async_call) so this is testable
without Home Assistant.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .intents import INTENTS
from .result import Status


class PlanError(Exception):
    """The validated intent can't be turned into a safe action. Caller escalates."""


class ExecutionError(Exception):
    """A read/call failed while running. Caller escalates."""


@dataclass(frozen=True)
class ValidatedIntent:
    intent: str
    target: str                 # entity_id or "area:<area_id>"
    arguments: dict
    source: str                 # "needle" | "qwen_fast"


@dataclass(frozen=True)
class ServiceCall:
    domain: str
    service: str
    data: dict


@dataclass(frozen=True)
class Plan:
    intent: str
    kind: str                   # "call" | "read"
    calls: tuple[ServiceCall, ...]
    entity_ids: tuple[str, ...]
    label: str                  # what to call the target when speaking
    arguments: dict


def _find_entity(snapshot: dict, entity_id: str) -> dict | None:
    for e in snapshot.get("entities") or []:
        if isinstance(e, dict) and e.get("entity_id") == entity_id:
            return e
    return None


def plan(vi: ValidatedIntent, snapshot: dict | None, *, allow_sensitive: bool) -> Plan:
    if not isinstance(snapshot, dict):
        raise PlanError("no_snapshot")
    spec = INTENTS.get(vi.intent)
    if spec is None:
        raise PlanError("unknown_intent")
    if spec.get("sensitive") and not allow_sensitive:
        raise PlanError("sensitive_intent_disabled")

    # ---- area target: expand to the area's exposed lights, nothing else ----
    if vi.target.startswith("area:"):
        area_id = vi.target[len("area:"):]
        area = next((a for a in snapshot.get("areas") or [] if isinstance(a, dict) and a.get("area_id") == area_id), None)
        if area is None or not spec.get("area_domains"):
            raise PlanError("area_target_not_allowed")
        domain = spec["area_domains"][0]
        ids = tuple(
            e["entity_id"] for e in snapshot.get("entities") or []
            if isinstance(e, dict) and e.get("area_id") == area_id and str(e.get("entity_id", "")).startswith(domain + ".")
        )
        if not ids:
            raise PlanError("area_has_no_exposed_entities")
        service = spec["services"][domain]
        data: dict = {"entity_id": list(ids)}
        if vi.intent == "set_brightness":
            data["brightness_pct"] = vi.arguments["brightness"]
        label = f"{area.get('name')} {domain}s" if len(ids) > 1 else f"{area.get('name')} {domain}"
        return Plan(vi.intent, "call", (ServiceCall(domain, service, data),), ids, label, dict(vi.arguments))

    # ---- entity target: must be exposed, domain must fit the intent ----
    ent = _find_entity(snapshot, vi.target)
    if ent is None:
        raise PlanError("target_not_exposed")
    domain = vi.target.split(".")[0]
    label = str(ent.get("name") or vi.target)

    if spec["kind"] == "read":
        if domain not in spec.get("read_domains", ()):
            raise PlanError(f"domain_mismatch:{domain}")
        return Plan(vi.intent, "read", (), (vi.target,), label, dict(vi.arguments))

    service = spec["services"].get(domain)
    if service is None:
        raise PlanError(f"domain_mismatch:{domain}")
    data = {"entity_id": vi.target}
    if vi.intent == "set_brightness":
        data["brightness_pct"] = vi.arguments["brightness"]
    elif vi.intent == "set_temperature":
        data["temperature"] = vi.arguments["temperature"]
    return Plan(vi.intent, "call", (ServiceCall(domain, service, data),), (vi.target,), label, dict(vi.arguments))


# --------------------------------------------------------------------------
# speech
# --------------------------------------------------------------------------

_DONE = {
    "turn_on": "Turned on {x}.", "turn_off": "Turned off {x}.", "toggle": "Toggled {x}.",
    "lock": "Locked {x}.", "unlock": "Unlocked {x}.", "open": "Opening {x}.", "close": "Closing {x}.",
    "run_script": "Running {x}.", "media_play": "Playing {x}.", "media_pause": "Paused {x}.", "media_stop": "Stopped {x}.",
}


def _num(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:.1f}"


_BINARY_WORDS = {
    "door": ("open", "closed"), "window": ("open", "closed"), "garage_door": ("open", "closed"),
    "opening": ("open", "closed"), "motion": ("detected", "clear"), "occupancy": ("detected", "clear"),
    "presence": ("detected", "clear"), "moisture": ("wet", "dry"), "smoke": ("detected", "clear"),
    "connectivity": ("connected", "disconnected"),
}


def _describe_state(st: Any) -> str:
    raw = str(st.state)
    attrs = st.attributes
    dc = attrs.get("device_class")
    domain = st.entity_id.split(".")[0]
    if domain == "binary_sensor" and dc in _BINARY_WORDS:
        return _BINARY_WORDS[dc][0 if raw == "on" else 1]
    unit = attrs.get("unit_of_measurement")
    try:
        float(raw)
        return f"{_num(raw)} {unit}".strip() if unit else _num(raw)
    except ValueError:
        return raw.replace("_", " ")


def _live_state(hass: Any, entity_id: str) -> Any:
    st = hass.states.get(entity_id)
    if st is None or str(st.state) in ("unavailable", "unknown"):
        raise ExecutionError(f"state_unavailable:{entity_id}")
    return st


def _condition_words(cond: str) -> str:
    return {"partlycloudy": "partly cloudy", "clear-night": "clear", "sunny": "sunny"}.get(cond, cond.replace("-", " ").replace("_", " "))


async def _speak_weather(hass: Any, entity_id: str, context: Any) -> str:
    st = _live_state(hass, entity_id)
    unit = st.attributes.get("temperature_unit") or ""
    now = f"Right now it's {_num(st.attributes.get('temperature'))} degrees and {_condition_words(str(st.state))}."
    try:
        resp = await hass.services.async_call(
            "weather", "get_forecasts", {"entity_id": entity_id, "type": "daily"},
            blocking=True, return_response=True, context=context,
        )
        today = resp[entity_id]["forecast"][0]
        parts = []
        if today.get("temperature") is not None:
            parts.append(f"a high of {_num(today['temperature'])}")
        if today.get("templow") is not None:
            parts.append(f"a low of {_num(today['templow'])}")
        text = f"Today: {_condition_words(str(today.get('condition', '')))}".rstrip(": ")
        if parts:
            text += ", with " + " and ".join(parts)
        if today.get("precipitation_probability") is not None:
            text += f", {_num(today['precipitation_probability'])} percent chance of precipitation"
        return f"{now} {text}."
    except Exception:  # noqa: BLE001 - forecast is a bonus; current conditions alone are still a fine answer
        return now


async def execute(hass: Any, p: Plan, context: Any = None) -> str:
    """Runs the plan and returns short speech. Raises ExecutionError on failure."""
    if p.kind == "read":
        eid = p.entity_ids[0]
        if p.intent == "get_weather":
            return await _speak_weather(hass, eid, context)
        st = _live_state(hass, eid)
        if p.intent == "get_temperature":
            domain = eid.split(".")[0]
            if domain == "climate":
                cur = st.attributes.get("current_temperature")
                if cur is None:
                    raise ExecutionError("no_current_temperature")
                return f"The {p.label} is at {_num(cur)} degrees."
            if domain == "weather":
                return f"It's {_num(st.attributes.get('temperature'))} degrees outside."
        return f"The {p.label} is {_describe_state(st)}."

    # Never claim success for a device that can't respond: drop unavailable
    # targets BEFORE calling anything, and refuse outright if none are left.
    calls = []
    skipped = 0
    for c in p.calls:
        ids = c.data["entity_id"] if isinstance(c.data["entity_id"], list) else [c.data["entity_id"]]
        live = [i for i in ids if _is_available(hass, i)]
        skipped += len(ids) - len(live)
        if not live:
            raise ExecutionError(f"target_unavailable:{','.join(ids)}")
        data = {**c.data, "entity_id": live if isinstance(c.data["entity_id"], list) else live[0]}
        calls.append(ServiceCall(c.domain, c.service, data))
    try:
        for c in calls:
            await hass.services.async_call(c.domain, c.service, c.data, blocking=True, context=context)
    except Exception as e:  # noqa: BLE001
        raise ExecutionError(f"service_call_failed:{type(e).__name__}") from e

    note = f" {skipped} of them {'was' if skipped == 1 else 'were'} unavailable." if skipped else ""
    return _say(p) + note


# The kiosk display: the only house-changing calls display_ops may make. display_ops names an ACTION and an entity; the (domain, service) pair comes from
# this fixed table and the entity must belong to the pair's domain, so a display request can start/stop one of the kiosk scripts or flip the rotation
# pause flag and nothing else. Entity ids come from household config, never from a model or from what was said.
DISPLAY_ACTIONS = {
    "show": ("script", "turn_on"),
    "close": ("script", "turn_on"),
    "pause": ("input_boolean", "turn_on"),
    "resume": ("input_boolean", "turn_off"),
}


async def execute_display(hass: Any, action: str, entity_id: str, context: Any = None) -> bool:
    """Run one fixed display action. True once HA accepted the call; ExecutionError if the action/entity is not allowed or the call failed.
    Acceptance is not an outcome: display_ops verifies the result itself before claiming success."""
    spec = DISPLAY_ACTIONS.get(action)
    if not spec or not isinstance(entity_id, str) or entity_id.split(".", 1)[0] != spec[0] or entity_id.count(".") != 1:
        raise ExecutionError(f"display_action_refused:{action}")
    try:
        await hass.services.async_call(spec[0], spec[1], {"entity_id": entity_id}, blocking=True, context=context)
    except Exception as e:  # noqa: BLE001
        raise ExecutionError(f"service_call_failed:{type(e).__name__}") from e
    return True


def _is_available(hass: Any, entity_id: str) -> bool:
    st = hass.states.get(entity_id)
    return st is not None and str(st.state) != "unavailable"


def _say(p: "Plan") -> str:
    if p.intent == "set_brightness":
        return f"Set the {p.label} to {p.arguments['brightness']} percent."
    if p.intent == "set_temperature":
        return f"Set the {p.label} to {_num(p.arguments['temperature'])} degrees."
    return _DONE[p.intent].format(x=f"the {p.label}")


# --------------------------------------------------------------------------
# verified execution: "HA accepted the call" is not "it happened"
# --------------------------------------------------------------------------

VERIFY_TIMEOUT_S = 2.5          # how long to wait for the state to settle after an accepted call
VERIFY_POLL_S = 0.25
_SKIP_IF_ALREADY = frozenset({"turn_on", "turn_off", "lock", "close", "open", "media_play", "media_pause", "media_stop"})


@dataclass
class EntityResult:
    entity_id: str
    label: str
    # already | verified | not_observed | unverifiable | unavailable | call_failed | call_unknown
    outcome: str
    before: str = ""
    after: str = ""
    detail: str = ""


@dataclass
class ExecReport:
    status: Status
    speech: str
    results: list[EntityResult] = field(default_factory=list)
    reason: str = ""
    side_effect: bool = False


def _state_matches(intent: str, domain: str, st: Any, before: str, args: dict) -> bool | None:
    """True/False if the intent's expected end state can be judged from `st`; None if it cannot be verified from state at all."""
    s = str(st.state)
    if intent == "turn_on":
        if domain in ("script", "scene"):
            return None
        return s not in ("off", "unavailable", "unknown") if domain == "media_player" else s == "on"
    if intent == "turn_off":
        return s in ("off", "standby") if domain == "media_player" else s == "off"
    if intent == "toggle":
        return s in ("on", "off") and before in ("on", "off") and s != before
    if intent == "set_brightness":
        b = st.attributes.get("brightness")
        want = round(float(args["brightness"]) * 255 / 100)
        if args["brightness"] == 0:
            return s == "off"
        return s == "on" and b is not None and abs(int(b) - want) <= 3
    if intent == "set_temperature":
        t = st.attributes.get("temperature")
        return t is not None and abs(float(t) - float(args["temperature"])) < 0.51
    if intent == "lock":
        return s in ("locked", "locking")
    if intent == "close":
        return s in ("closed", "closing")
    if intent == "open":
        return s in ("open", "opening")
    if intent == "media_play":
        return s == "playing"
    if intent == "media_pause":
        return s == "paused"
    if intent == "media_stop":
        return s in ("idle", "off", "standby")
    return None


async def _fresh_state(hass: Any, entity_id: str) -> Any:
    """An INDEPENDENT read: RestHass.read_state goes back to HA; a real in-process hass has live states already."""
    rs = getattr(hass, "read_state", None)
    if rs is not None:
        return await rs(entity_id)
    return hass.states.get(entity_id)


def _ids_of(c: ServiceCall) -> list[str]:
    return list(c.data["entity_id"]) if isinstance(c.data["entity_id"], list) else [c.data["entity_id"]]


def _failure_kind(e: Exception) -> str:
    """Did the command possibly take effect? timeouts/unknown transport errors: yes (call_unknown); HA answered with an error: no."""
    r = str(e)
    return "call_failed" if r.startswith(("rest_http_", "rest_auth")) else "call_unknown"


async def execute_verified(hass: Any, plans: list[Plan], context: Any = None) -> ExecReport:
    """Runs WRITE plans, then re-reads each target from HA and says only what was observed. Never raises for per-entity problems.

    Per entity: already (state was already as asked; no call sent) | verified | not_observed (call accepted, state did not change) |
    unverifiable (call accepted, intent has no observable state) | unavailable | call_failed | call_unknown (timeout: may have run).
    """
    results: list[EntityResult] = []
    side_effect = False
    for p in plans:
        intent, args = p.intent, p.arguments
        for c in p.calls:
            ids = _ids_of(c)
            todo: list[str] = []
            for eid in ids:
                st = hass.states.get(eid)
                label = (getattr(st, "name", "") or eid) if st is not None else eid
                if len(p.entity_ids) == 1 and p.label:
                    label = p.label
                if st is None or str(st.state) in ("unavailable", "unknown"):
                    results.append(EntityResult(eid, label, "unavailable", before=str(getattr(st, "state", "missing"))))
                    continue
                before = str(st.state)
                if intent in _SKIP_IF_ALREADY and _state_matches(intent, eid.split(".")[0], st, before, args):
                    results.append(EntityResult(eid, label, "already", before=before, after=before))
                    continue
                todo.append(eid)
            if not todo:
                continue
            befores = {e: str(hass.states.get(e).state) for e in todo}
            data = {**c.data, "entity_id": todo if isinstance(c.data["entity_id"], list) else todo[0]}
            try:
                side_effect = True
                await hass.services.async_call(c.domain, c.service, data, blocking=True, context=context)
            except Exception as e:  # noqa: BLE001
                kind = _failure_kind(e)
                for eid in todo:
                    results.append(EntityResult(eid, _label(hass, eid, p), kind, before=befores[eid], detail=str(e)[:80]))
                continue
            for eid in todo:
                results.append(await _verify(hass, p, eid, befores[eid]))
    return _report(plans[0].intent if plans else "", plans, results, side_effect)


def _label(hass: Any, eid: str, p: Plan) -> str:
    if len(p.entity_ids) == 1 and p.label:
        return p.label
    st = hass.states.get(eid)
    return (getattr(st, "name", "") or eid) if st is not None else eid


async def _verify(hass: Any, p: Plan, eid: str, before: str) -> EntityResult:
    label = _label(hass, eid, p)
    domain = eid.split(".")[0]
    deadline = asyncio.get_event_loop().time() + VERIFY_TIMEOUT_S
    last = None
    while True:
        try:
            last = await _fresh_state(hass, eid)
        except Exception:  # noqa: BLE001 - cannot read back: say so, never "done"
            return EntityResult(eid, label, "unverifiable", before=before, detail="readback_failed")
        if last is None:
            return EntityResult(eid, label, "unavailable", before=before, detail="disappeared")
        ok = _state_matches(p.intent, domain, last, before, p.arguments)
        if ok is None:
            return EntityResult(eid, label, "unverifiable", before=before, after=str(last.state))
        if ok:
            return EntityResult(eid, label, "verified", before=before, after=str(last.state))
        if asyncio.get_event_loop().time() >= deadline:
            return EntityResult(eid, label, "not_observed", before=before, after=str(last.state))
        await asyncio.sleep(VERIFY_POLL_S)


def _cap(t: str) -> str:
    return t[:1].upper() + t[1:]


def _names(items: list[EntityResult]) -> str:
    return " and ".join("the " + r.label for r in items)


_STATE_WORD = {"turn_on": "on", "turn_off": "off", "lock": "locked", "close": "closed", "open": "open",
               "media_play": "playing", "media_pause": "paused", "media_stop": "stopped"}
_VERB_ING = {"turn_on": "turn on", "turn_off": "turn off", "toggle": "toggle", "lock": "lock", "close": "close", "open": "open",
             "set_brightness": "dim", "set_temperature": "set", "media_play": "play", "media_pause": "pause", "media_stop": "stop",
             "run_script": "run"}


def _report(intent: str, plans: list[Plan], results: list[EntityResult], side_effect: bool) -> ExecReport:
    by = lambda o: [r for r in results if r.outcome == o]  # noqa: E731
    verified, already, notobs = by("verified"), by("already"), by("not_observed")
    unver, unavail, failed, unknown = by("unverifiable"), by("unavailable"), by("call_failed"), by("call_unknown")
    parts: list[str] = []
    if verified:
        p0 = next((p for p in plans if verified[0].entity_id in p.entity_ids), plans[0])
        if (len(verified) == 1 and len(p0.entity_ids) == 1 and len(plans) == 1) or intent in ("set_brightness", "set_temperature"):
            parts.append(_say(p0).rstrip("."))
        else:
            parts.append(_DONE[intent].format(x=_names(verified)).rstrip("."))
    if already:
        w = _STATE_WORD.get(intent, "")
        parts.append(f"{_cap(_names(already))} {'is' if len(already) == 1 else 'are'} already {w}".rstrip())
    verb = _VERB_ING.get(intent, "do that to")
    if notobs:
        parts.append(f"I told Home Assistant to {verb} {_names(notobs)}, but I couldn't confirm it changed")
    if unver:
        parts.append(f"I sent that to {_names(unver)}, but I can't confirm it worked")
    if unknown:
        parts.append(f"I sent the command for {_names(unknown)} but it didn't answer, so I can't say whether it worked")
    if failed:
        parts.append(f"Home Assistant refused to {verb} {_names(failed)}")
    if unavail:
        parts.append(f"{_cap(_names(unavail))} {'is' if len(unavail) == 1 else 'are'} unavailable")
    speech = ". ".join(parts) + "."
    if notobs or unver or unknown:
        status, reason = Status.UNVERIFIED, "outcome_not_confirmed"
    elif failed:
        status, reason = Status.EXECUTION_FAILED, "service_call_refused"
    elif unavail:
        status, reason = Status.UNAVAILABLE, "target_unavailable"
    else:
        status, reason = Status.SUCCESS, ""
    return ExecReport(status, speech, results, reason, side_effect)


# --------------------------------------------------------------------------
# REST transport: a duck-typed `hass` for processes that are not Home Assistant (the duplex voice assistant)
# --------------------------------------------------------------------------
# It lives here because executor.py is the one module allowed to reach HA services (see tests/test_static.py). It offers exactly what
# execute() uses: states.get(id) from a cache refreshed per request, and services.async_call(...) against HA's REST API.

@dataclass
class _State:
    entity_id: str
    state: str
    attributes: dict
    name: str = ""


class _States:
    def __init__(self):
        self.cache: dict = {}

    def get(self, entity_id: str):
        return self.cache.get(entity_id)


class _Services:
    def __init__(self, owner: "RestHass"):
        self.owner = owner

    async def async_call(self, domain, service, data=None, blocking=False, return_response=False, context=None):
        return await self.owner._post(f"/api/services/{domain}/{service}" + ("?return_response" if return_response else ""), data or {}, return_response)


class RestHass:
    def __init__(self, base: str, token: str, timeout: float = 10.0):
        self.base, self.timeout = base.rstrip("/"), timeout
        self.headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        self.states, self.services = _States(), _Services(self)

    def _http(self, method: str, path: str, body: dict | None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise ExecutionError("rest_auth_failed" if e.code in (401, 403) else f"rest_http_{e.code}") from e
        except TimeoutError as e:
            raise ExecutionError("rest_timeout") from e
        except urllib.error.URLError as e:
            raise ExecutionError("rest_timeout" if isinstance(e.reason, TimeoutError) else "rest_unreachable") from e
        except OSError as e:
            raise ExecutionError("rest_unreachable") from e
        try:
            return json.loads(raw or b"null")
        except ValueError as e:
            raise ExecutionError("rest_malformed_response") from e

    async def calendar_events(self, entity_id: str, start: str, end: str) -> list:
        """READ-ONLY: raw event rows from HA's calendar API for one calendar entity (ISO start/end). Raises ExecutionError."""
        from urllib.parse import quote
        rows = await asyncio.to_thread(self._http, "GET", "/api/calendars/%s?start=%s&end=%s" % (quote(entity_id, safe="."), quote(start), quote(end)), None)
        if not isinstance(rows, list):
            raise ExecutionError("rest_malformed_response")
        return rows

    @staticmethod
    def _to_state(x: Any) -> "_State | None":
        if not isinstance(x, dict) or not isinstance(x.get("entity_id"), str) or not isinstance(x.get("state"), str):
            return None
        attrs = x.get("attributes") if isinstance(x.get("attributes"), dict) else {}
        return _State(x["entity_id"], x["state"], attrs, str(attrs.get("friendly_name") or ""))

    async def refresh(self) -> None:
        rows = await asyncio.to_thread(self._http, "GET", "/api/states", None)
        if not isinstance(rows, list):
            raise ExecutionError("rest_malformed_response")
        self.states.cache = {s.entity_id: s for s in map(self._to_state, rows) if s is not None}

    async def read_state(self, entity_id: str):
        """Independent single-entity read straight from HA (used to verify a write). None if HA no longer has the entity."""
        try:
            x = await asyncio.to_thread(self._http, "GET", "/api/states/" + entity_id, None)
        except ExecutionError as e:
            if str(e) == "rest_http_404":
                self.states.cache.pop(entity_id, None)
                return None
            raise
        st = self._to_state(x)
        if st is None:
            raise ExecutionError("rest_malformed_response")
        self.states.cache[entity_id] = st
        return st

    async def _post(self, path: str, data: dict, return_response: bool):
        body = await asyncio.to_thread(self._http, "POST", path, data)
        return body.get("service_response") if return_response and isinstance(body, dict) else body
