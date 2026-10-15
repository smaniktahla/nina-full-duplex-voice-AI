"""Needle HA routing adapter — Step 5 (HA-N0 preparation only).

Resolves, validates, and classifies a transcript against a Needle backend.
Never calls HA services, never alters entities, never bypasses Hermes,
never alters Assist routing (requirement 8). Production HA Voice is
untouched by this module; nothing here is wired to a live caller yet.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# Requirement 1: configurable endpoint, never hard-coded to a specific host.
# Port 8793, not Needle's own 8790: the `needle` binary hard-binds to
# 127.0.0.1 with no way to change it (confirmed 2026-09-23 — neither
# `--port host:port` syntax nor a NEEDLE_HOST env var has any effect), so
# a small TCP proxy (needle_proxy.py, on AI1 at :8793) is what's actually
# reachable from other hosts. Needle itself stays on 127.0.0.1:8790.
NEEDLE_ENDPOINT = os.environ.get("NEEDLE_ENDPOINT", "http://10.10.10.105:8793").rstrip("/")
NEEDLE_TIMEOUT_S = float(os.environ.get("NEEDLE_TIMEOUT_S", "2.0"))

# The adapter's own source of truth. Needle's output is validated against this
# schema and never trusted just because it parsed as JSON (requirement 7).
# "kind" drives the route classification (read -> sensor_query, write -> ha_action).
TOOL_SCHEMA: dict[str, dict[str, Any]] = {
    "turn_on_light": {
        "kind": "write",
        "required": {"area": str},
        "optional": {},
    },
    "get_sensor_state": {
        "kind": "read",
        "required": {"entity": str},
        "optional": {},
    },
    "get_backend_health": {
        "kind": "read",
        "required": {"backend": str},
        "optional": {},
        "enum": {"backend": {"AI1", "AI2"}},
    },
}

# Requirement 4: a matched tool doesn't automatically mean a lookup answers the
# request. Comparison/causal language disqualifies fast-path sufficiency even
# when Needle finds a plausible tool — deliberately conservative until HA-N0
# data says otherwise. Confirmed live example: "why is the basement colder
# than upstairs?" routed to get_sensor_state(basement), which doesn't actually
# answer a comparison question.
REASONING_MARKERS = (
    "why", "how come", "compared to", "versus", " vs ",
    "colder than", "warmer than", "hotter than", "difference between",
)


@dataclass
class RouteDecision:
    route: str  # "ha_action" | "sensor_query" | "conversation"
    tool_match: bool  # Needle proposed a schema-valid tool call
    fast_path_sufficient: bool  # tool_match AND request doesn't look reasoning-heavy
    tool_name: str | None = None
    arguments: dict = field(default_factory=dict)
    confidence: float | None = None  # telemetry only (requirement 5) — never gates `route`
    reason: str = ""  # why this decision was made, for HA-N0 shadow-mode logging
    needle_raw: dict | None = None  # full raw response, kept for HA-N0 data collection
    latency_s: float | None = None
    error: str | None = None  # set only on fail-open (system-level failure, not a normal decline)


def _looks_reasoning_heavy(transcript: str) -> bool:
    t = transcript.lower()
    return any(m in t for m in REASONING_MARKERS)


def _validate_tool_call(call: Any) -> tuple[bool, str]:
    """Strict schema validation (requirement 7). Returns (valid, reason)."""
    if not isinstance(call, dict):
        return False, "tool call is not an object"
    name = call.get("name")
    if not isinstance(name, str) or name not in TOOL_SCHEMA:
        return False, f"unknown tool: {name!r}"
    schema = TOOL_SCHEMA[name]
    args = call.get("arguments")
    if not isinstance(args, dict):
        return False, "arguments not an object"

    allowed_fields = set(schema["required"]) | set(schema.get("optional", {}))
    unknown_fields = set(args) - allowed_fields
    if unknown_fields:
        return False, f"unknown fields: {sorted(unknown_fields)}"

    for field_name, field_type in schema["required"].items():
        if field_name not in args:
            return False, f"missing required field: {field_name}"
        if not isinstance(args[field_name], field_type):
            return False, f"field {field_name!r} has wrong type (expected {field_type.__name__})"

    for field_name, allowed_values in schema.get("enum", {}).items():
        if field_name in args and args[field_name] not in allowed_values:
            return False, f"field {field_name!r} value {args[field_name]!r} not in {sorted(allowed_values)}"

    return True, "ok"


def _fail_open(reason: str, latency_s: float | None = None) -> RouteDecision:
    """Requirement 6: any system-level failure routes to conversation and is
    marked with `error` so it's distinguishable from a normal Needle decline."""
    return RouteDecision(
        route="conversation",
        tool_match=False,
        fast_path_sufficient=False,
        reason=f"fail-open: {reason}",
        latency_s=latency_s,
        error=reason,
    )


def _call_needle(transcript: str) -> tuple[dict | None, float, str | None]:
    """POST /complete with compact JSON.

    Requirement 2: Needle's parser has been confirmed (via live benchmark,
    2026-09-23) to misparse ordinary json.dumps() output because of the space
    after ':'. Compact separators are mandatory here, not a style choice —
    see test_needle_json_parsing_regression for the proof.

    Also resets session state first: Needle's /serve mode keeps conversation
    context across /complete calls, and each HA-N0 query must be independent
    (confirmed via live testing — without /reset, responses drift based on
    prior calls rather than reflecting the current transcript).
    """
    t0 = time.time()
    try:
        reset_req = urllib.request.Request(f"{NEEDLE_ENDPOINT}/reset", method="POST")
        urllib.request.urlopen(reset_req, timeout=NEEDLE_TIMEOUT_S)

        body = json.dumps({"input": transcript}, separators=(",", ":")).encode()
        req = urllib.request.Request(
            f"{NEEDLE_ENDPOINT}/complete",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=NEEDLE_TIMEOUT_S) as resp:
            raw = resp.read().decode()
        latency = time.time() - t0
        parsed = json.loads(raw)
        return parsed, latency, None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, time.time() - t0, f"needle unavailable/timeout: {e}"
    except (json.JSONDecodeError, ValueError) as e:
        return None, time.time() - t0, f"malformed needle response: {e}"


def route(transcript: str) -> RouteDecision:
    """Resolve/validate/classify a transcript. Read-only with respect to HA:
    never calls HA services, alters entities, bypasses Hermes, or alters
    Assist routing (requirement 8) — this is HA-N0 preparation only.
    """
    if not transcript or not transcript.strip():
        return _fail_open("empty transcript")

    parsed, latency_s, err = _call_needle(transcript)
    if err is not None:
        return _fail_open(err, latency_s)
    if not isinstance(parsed, dict):
        return _fail_open(f"non-object needle response: {parsed!r}", latency_s)
    if not parsed.get("success", False):
        return _fail_open(f"needle reported failure: {parsed}", latency_s)

    confidence = parsed.get("confidence")
    calls = parsed.get("function_calls") or []

    if not calls:
        # Needle itself declined — a normal, correct outcome, not a system failure.
        return RouteDecision(
            route="conversation",
            tool_match=False,
            fast_path_sufficient=False,
            confidence=confidence,
            reason="needle proposed no tool call",
            needle_raw=parsed,
            latency_s=latency_s,
        )

    if len(calls) > 1:
        return _fail_open(f"multiple tool calls returned ({len(calls)}), ambiguous", latency_s)

    call = calls[0]
    valid, why = _validate_tool_call(call)
    if not valid:
        # Invalid tool/arguments per requirement 6 -> conversation, but this is
        # a resolver-validation outcome, not a system failure, so no `error`.
        return RouteDecision(
            route="conversation",
            tool_match=False,
            fast_path_sufficient=False,
            confidence=confidence,
            reason=f"schema validation failed: {why}",
            needle_raw=parsed,
            latency_s=latency_s,
        )

    tool_name = call["name"]
    kind = TOOL_SCHEMA[tool_name]["kind"]
    tool_match = True
    fast_path_sufficient = tool_match and not _looks_reasoning_heavy(transcript)

    return RouteDecision(
        route="ha_action" if kind == "write" else "sensor_query",
        tool_match=tool_match,
        fast_path_sufficient=fast_path_sufficient,
        tool_name=tool_name,
        arguments=call.get("arguments", {}),
        confidence=confidence,
        reason="ok" if fast_path_sufficient else "tool matched but request looks reasoning-heavy; not fast-path sufficient",
        needle_raw=parsed,
        latency_s=latency_s,
    )
