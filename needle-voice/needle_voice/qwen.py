"""Qwen fast tier: prompt, constrained-JSON request, and STRICT validation.

The model's output is a proposal. `validate()` accepts it only if every
independent check agrees (see guards.py / candidates.py). Pure, stdlib only;
the HTTP transport is injected by the router.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import guards
from .candidates import Candidate
from .intents import INTENT_NAMES, INTENTS

# Static prefix first, dynamic content last: llama-server caches the shared
# prefix, so only the candidate list + request are re-processed per call.
SYSTEM_PROMPT = (
    "You are a fast Home Assistant intent resolver.\n"
    "Return only JSON. Resolve home-control and home-status requests.\n"
    "If the request is conversational, explanatory, or outside the supported intents, return decision escalate. "
    "If a listed candidate clearly matches the device named in the request, handle it.\n"
    "Never invent an entity. Never answer general questions.\n"
    "Pick target ONLY from the Candidates list, using its id exactly.\n"
    "Intents: turn_on, turn_off, toggle, set_brightness (arguments.brightness 0-100), "
    "set_temperature (arguments.temperature), lock, unlock, open, close, get_state, get_temperature, "
    "get_weather, run_script, media_play, media_pause, media_stop.\n"
    "Examples:\n"
    'Request: turn on the garden pump -> {"decision":"handle","intent":"turn_on","target":"switch.garden_pump","arguments":{}}\n'
    'Request: turn off guest mode -> {"decision":"handle","intent":"turn_off","target":"input_boolean.guest_mode","arguments":{}}\n'
    'Request: switch off the den lamp -> {"decision":"handle","intent":"turn_off","target":"light.den_lamp","arguments":{}}\n'
    'Request: set the office light to 40 percent -> {"decision":"handle","intent":"set_brightness","target":"light.office","arguments":{"brightness":40}}\n'
    'Request: is the garage door open -> {"decision":"handle","intent":"get_state","target":"cover.garage_door","arguments":{}}\n'
    'Request: pause the living room tv -> {"decision":"handle","intent":"media_pause","target":"media_player.living_room_tv","arguments":{}}\n'
    'Request: why is it so cold in here -> {"decision":"escalate","intent":null,"target":null,"arguments":{}}\n'
)

MAX_TOKENS = 96


def build_request(model: str, utterance: str, candidates: list[Candidate]) -> dict:
    ids = [c.id for c in candidates]
    schema = {
        "type": "object",
        "properties": {
            "decision": {"enum": ["handle", "escalate"]},
            "intent": {"enum": list(INTENT_NAMES) + [None]},
            "target": {"enum": ids + [None]},
            "arguments": {"type": "object", "properties": {
                "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
                "temperature": {"type": "number"}}},
        },
        "required": ["decision", "intent", "target", "arguments"],
        "additionalProperties": False,
    }
    listing = "\n".join(
        f"- {c.id} | {c.name}" + (f" | area {c.area}" if c.area else "") for c in candidates
    )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Candidates:\n{listing}\nRequest: {utterance}"},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "response_format": {"type": "json_schema", "json_schema": {"name": "intent", "strict": True, "schema": schema}},
        "chat_template_kwargs": {"enable_thinking": False},
    }


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    intent: str | None = None
    target: str | None = None
    arguments: dict = field(default_factory=dict)


def _bad(reason: str) -> Verdict:
    return Verdict(False, reason)


def validate(
    content: object,
    *,
    utterance: str,
    candidates: list[Candidate],
    top: Candidate | None,
    allow_sensitive: bool,
    equivalent_ids: frozenset[str] = frozenset(),
) -> Verdict:
    """Never raises. Anything unexpected -> not ok."""
    try:
        return _validate(content, utterance, candidates, top, allow_sensitive, equivalent_ids)
    except Exception as e:  # noqa: BLE001
        return _bad(f"validator_error:{type(e).__name__}")


def _validate(content, utterance, candidates, top, allow_sensitive, equivalent_ids=frozenset()) -> Verdict:
    if not isinstance(content, str):
        return _bad("non_string_content")
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return _bad("malformed_json")
    if not isinstance(obj, dict) or set(obj) != {"decision", "intent", "target", "arguments"}:
        return _bad("schema_violation")
    if obj["decision"] == "escalate":
        return _bad("qwen_escalated")
    if obj["decision"] != "handle":
        return _bad("schema_violation")

    intent, target, args = obj["intent"], obj["target"], obj["arguments"]
    spec = INTENTS.get(intent) if isinstance(intent, str) else None
    if spec is None:
        return _bad("unknown_intent")
    by_id = {c.id: c for c in candidates}
    if not isinstance(target, str) or target not in by_id:
        return _bad("unknown_target")
    if top is None or (target != top.id and target not in equivalent_ids):
        return _bad("target_disagrees_with_retrieval")
    if not isinstance(args, dict):
        return _bad("schema_violation")

    ok, why = guards.corroborates(intent, utterance)
    if not ok:
        return _bad(why)
    if spec.get("sensitive") and not allow_sensitive:
        return _bad("sensitive_intent_disabled")

    cand = by_id[target]
    if cand.kind == "area":
        toks = set(guards.tokens(utterance))
        if not spec.get("area_domains") or not (toks & {"light", "lights", "lamp", "lamps"}):
            return _bad("area_target_not_allowed_for_intent")
    else:
        allowed = set(spec.get("services", {})) | set(spec.get("read_domains", ()))
        if cand.domain not in allowed:
            return _bad(f"domain_mismatch:{cand.domain}")

    allowed_args = spec.get("args", {})
    if set(args) - set(allowed_args):
        return _bad("unexpected_arguments")
    clean: dict = {}
    for name in spec.get("required", ()):
        if name not in args:
            return _bad(f"missing_argument:{name}")
    utt_tokens = set(guards.tokens(utterance))
    for name, (typ, lo, hi) in allowed_args.items():
        if name not in args:
            continue
        v = args[name]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return _bad(f"bad_argument_type:{name}")
        if typ == "int" and float(v) != int(v):
            return _bad(f"bad_argument_type:{name}")
        if not (lo <= v <= hi):
            return _bad(f"argument_out_of_range:{name}")
        # the number must actually have been said (digits), not invented
        if str(int(v)) not in utt_tokens:
            return _bad(f"argument_not_grounded:{name}")
        clean[name] = int(v) if typ == "int" else float(v)
    return Verdict(True, "ok", intent, target, clean)
