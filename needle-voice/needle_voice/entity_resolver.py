"""Deterministic entity resolution — grounds a Needle-proposed argument in
(a) what the user actually said and (b) what Home Assistant actually
exposes to the conversation agent.

Why this exists: the first real shadow observation (2026-09-24) had Needle
answer "Is this running pool exhaust fan on?" with
get_sensor_state(entity="pool temperature"). The adapter's schema check and
the policy engine both passed it, because it was a well-formed single read.
Nothing verified the entity was one the user meant, or that it exists.

Two independent checks, both deterministic (no LLM, no fuzzy matching, no
guessing — when unsure the answer is "not resolved"):

  1. Grounding: every word of the proposed argument must appear in the
     utterance. ("pool temperature" is not grounded in "...pool exhaust
     fan on?" — "temperature" was never said.) Needle's own
     validation.ungrounded flag is honored as additional evidence.
  2. Resolution: the argument must match exactly one exposed entity (by
     friendly name or alias) in an allowed domain, or exactly one area that
     has an exposed light. Exact match after case/punctuation
     normalization only. Substring and partial matches are deliberately not
     accepted, so many natural phrasings will fail to resolve — that is an
     accepted false-negative cost; false positives are the expensive kind.

Standalone, stdlib only, no Home Assistant import. The HA side builds a
plain-dict snapshot (see needle_shadow/__init__.py: build_entity_snapshot)
and passes it in, which keeps this module testable offline.

The output feeds policy_engine.PolicyContext.resolved_target_count:
0 -> insufficient_context, >1 -> ambiguous_request, 1 -> passes. A missing
or unusable snapshot resolves to 0 (status "unavailable"), NOT to None —
"we couldn't check" must never look like "checked and fine".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Sequence

ResolutionStatus = Literal[
    "resolved", "unresolved", "ambiguous", "ungrounded", "unavailable", "not_applicable",
]

# Manually kept in sync with needle_adapter.TOOL_SCHEMA (tool names, the
# argument that names a target, enum values); test_entity_resolver.py has a
# drift guard. `domains` restricts what get_sensor_state may resolve to —
# the tool is described as reading a *sensor*, so a switch/fan/light is not
# a valid target for it (a general entity-state tool is a catalog decision
# for later, not something to smuggle in here).
RESOLUTION_RULES: dict[str, dict[str, Any]] = {
    "get_sensor_state": {"arg": "entity", "kind": "entity", "domains": ("sensor", "binary_sensor")},
    "turn_on_light": {"arg": "area", "kind": "area", "target_domain": "light"},
    "get_backend_health": {"arg": "backend", "kind": "enum", "values": ("AI1", "AI2")},
}

_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Resolution:
    status: ResolutionStatus
    resolved_target_count: int | None  # None only for not_applicable
    kind: str | None
    matched: tuple[str, ...]
    reason: str


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _norm(text: str) -> str:
    return " ".join(_tokens(text))


def snapshot_stats(snapshot: Any) -> dict[str, int] | None:
    if not _snapshot_ok(snapshot):
        return None
    return {"entities": len(snapshot["entities"]), "areas": len(snapshot["areas"])}


def _snapshot_ok(snapshot: Any) -> bool:
    return (
        isinstance(snapshot, dict)
        and isinstance(snapshot.get("entities"), list)
        and isinstance(snapshot.get("areas"), list)
    )


def _names_of(item: dict) -> set[str]:
    names = set()
    name = item.get("name")
    if isinstance(name, str) and _norm(name):
        names.add(_norm(name))
    aliases = item.get("aliases")
    if isinstance(aliases, (list, tuple)):
        for a in aliases:
            if isinstance(a, str) and _norm(a):
                names.add(_norm(a))
    return names


def resolve(
    *,
    tool_name: str | None,
    arguments: dict | None,
    utterance: str,
    snapshot: Any,
    needle_ungrounded: Sequence[str] = (),
) -> Resolution:
    """Never raises. Any internal failure resolves to count 0 (fail closed)."""
    try:
        return _resolve(tool_name, arguments, utterance, snapshot, needle_ungrounded)
    except Exception as e:  # noqa: BLE001
        return Resolution("unavailable", 0, None, (), f"resolver error: {type(e).__name__}: {e}")


def _resolve(tool_name, arguments, utterance, snapshot, needle_ungrounded) -> Resolution:
    rule = RESOLUTION_RULES.get(tool_name) if isinstance(tool_name, str) else None
    if rule is None:
        return Resolution("not_applicable", None, None, (), f"no resolution rule for tool {tool_name!r}")

    kind = rule["kind"]
    arg_name = rule["arg"]
    value = arguments.get(arg_name) if isinstance(arguments, dict) else None
    if not isinstance(value, str) or not _norm(value):
        return Resolution("unresolved", 0, kind, (), f"argument {arg_name!r} missing or empty")

    # --- 1. grounding in the utterance ---
    if f"{tool_name}.{arg_name}" in list(needle_ungrounded or ()):
        return Resolution("ungrounded", 0, kind, (), f"Needle itself flagged {tool_name}.{arg_name} as ungrounded")
    utterance_tokens = set(_tokens(utterance if isinstance(utterance, str) else ""))
    missing = [t for t in _tokens(value) if t not in utterance_tokens]
    if missing:
        return Resolution(
            "ungrounded", 0, kind, (),
            f"argument {value!r} is not grounded in the request; words never said: {missing}",
        )

    # --- 2a. enum: no HA lookup needed ---
    if kind == "enum":
        if value in rule["values"]:
            return Resolution("resolved", 1, kind, (value,), "value is a known enum member")
        return Resolution("unresolved", 0, kind, (), f"{value!r} not in {list(rule['values'])}")

    # --- 2b. HA-backed kinds need a usable snapshot ---
    if not _snapshot_ok(snapshot):
        return Resolution("unavailable", 0, kind, (), "no usable HA entity snapshot; cannot verify target")

    target = _norm(value)

    if kind == "entity":
        allowed = tuple(rule["domains"])
        hits: dict[str, str] = {}
        for ent in snapshot["entities"]:
            if not isinstance(ent, dict):
                continue
            eid = ent.get("entity_id")
            if not isinstance(eid, str) or eid.split(".")[0] not in allowed:
                continue
            if target in _names_of(ent):
                hits[eid] = eid
        matched = tuple(sorted(hits))
        if len(matched) == 1:
            return Resolution("resolved", 1, kind, matched, f"exactly one exposed {'/'.join(allowed)} named {value!r}")
        if len(matched) == 0:
            return Resolution("unresolved", 0, kind, (), f"no exposed {'/'.join(allowed)} entity named {value!r}")
        return Resolution("ambiguous", len(matched), kind, matched, f"{len(matched)} exposed entities named {value!r}")

    if kind == "area":
        area_hits = []
        for area in snapshot["areas"]:
            if isinstance(area, dict) and target in _names_of(area):
                area_hits.append(area)
        if len(area_hits) == 0:
            return Resolution("unresolved", 0, kind, (), f"no area named {value!r}")
        if len(area_hits) > 1:
            names = tuple(sorted(str(a.get("area_id")) for a in area_hits))
            return Resolution("ambiguous", len(area_hits), kind, names, f"{len(area_hits)} areas named {value!r}")
        area = area_hits[0]
        domain = rule["target_domain"]
        n = sum(
            1 for ent in snapshot["entities"]
            if isinstance(ent, dict)
            and ent.get("area_id") == area.get("area_id")
            and isinstance(ent.get("entity_id"), str)
            and ent["entity_id"].split(".")[0] == domain
        )
        if n == 0:
            return Resolution("unresolved", 0, kind, (str(area.get("area_id")),), f"area {value!r} has no exposed {domain} entities")
        return Resolution("resolved", 1, kind, (str(area.get("area_id")),), f"one area {value!r} with {n} exposed {domain} entities")

    return Resolution("unresolved", 0, kind, (), f"unhandled rule kind {kind!r}")
