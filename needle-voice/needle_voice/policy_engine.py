"""Deterministic policy layer — Step 6.

Answers exactly one question: given a validated Needle-adapter result and
explicit context metadata, is this request eligible to become a
`fast_path_candidate` for a LATER stage (HA-N0 shadow routing, step 7) to
evaluate — or must it stay on the normal conversation path?

`fast_path_candidate` does NOT mean "execute the HA action." It means
"policy sees no reason this request cannot proceed to the next evaluation
stage." Nothing here calls Needle, an LLM, or Home Assistant; nothing here
performs a network request, infers a missing argument, repairs a malformed
one, or treats Needle confidence as an authorization threshold.

This module is intentionally standalone: it imports nothing beyond the
stdlib, not even needle_adapter, so it cannot inherit that module's network
code even indirectly. `KNOWN_TOOLS` below is a manually-kept-in-sync copy of
needle_adapter.TOOL_SCHEMA's tool names/kinds — see
test_policy_engine.py::test_known_tools_match_adapter_schema for the drift
guard between the two files.

When uncertain, this module routes to "conversation". False negatives
(a genuinely fine request stays on conversation) are acceptable. False
positives (an ineligible request gets marked fast_path_candidate) are not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Route = Literal["conversation", "fast_path_candidate"]

PolicyRuleCode = Literal[
    "adapter_rejected",
    "tool_mismatch",
    "insufficient_context",
    "multi_call_required",
    "reasoning_required",
    "ambiguous_request",
    "sensitive_action",
    "malformed_input",
    "fast_path_eligible",
]

ActionClassLabel = Literal["READ", "WRITE", "UNKNOWN"]

# Manually kept in sync with needle_adapter.TOOL_SCHEMA (name -> kind).
# Duplicated rather than imported so this module has zero non-stdlib
# dependencies. None of these names currently match SENSITIVE_NAME_MARKERS
# below — there is no real sensitive tool in the schema yet (documented gap,
# see the Step 6 report).
KNOWN_TOOLS: dict[str, ActionClassLabel] = {
    "turn_on_light": "WRITE",
    "get_sensor_state": "READ",
    "get_backend_health": "READ",
}

# Speculative, name-based only — no sensitive tool exists in KNOWN_TOOLS yet.
# This exists so the mechanism is ready and testable before any such tool is
# added, per the architecture requirement that policy can express
# route=conversation/policy_rule=sensitive_action even when a tool matched.
SENSITIVE_NAME_MARKERS: tuple[str, ...] = (
    "lock", "unlock", "door", "garage", "alarm", "security", "water_shutoff", "shutoff",
)


@dataclass(frozen=True)
class PolicyContext:
    """Explicit request/context metadata, supplied by the (not-yet-built)
    caller. Every field defaults to "unknown" because nothing upstream
    currently populates them — the adapter (step 5) does no entity
    resolution and no multi-call detection. These fields exist so the
    policy mechanism is ready for step 7+ to wire up; until then, real
    callers pass PolicyContext() and only the adapter_result drives the
    decision.
    """

    resolved_target_count: int | None = None  # None=unknown, 0=none, 1=unambiguous, >1=ambiguous
    multi_call_required: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    route: Route
    reason: str
    policy_rule: PolicyRuleCode
    tool_match: bool
    fast_path_sufficient: bool
    action_class: ActionClassLabel | None = None


def _is_sensitive_tool(tool_name: str) -> bool:
    name = tool_name.lower()
    return any(marker in name for marker in SENSITIVE_NAME_MARKERS)


def _deny(
    rule: PolicyRuleCode,
    reason: str,
    tool_match: bool,
    fast_path_sufficient: bool,
    action_class: ActionClassLabel | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        route="conversation",
        reason=reason,
        policy_rule=rule,
        tool_match=bool(tool_match),
        fast_path_sufficient=bool(fast_path_sufficient),
        action_class=action_class,
    )


def evaluate_policy(adapter_result: Any, context: PolicyContext | None = None) -> PolicyDecision:
    """Pure function: no I/O, no network calls, no execution (see the static
    safety tests). `adapter_result` is duck-typed (a needle_adapter.RouteDecision
    in practice, but this module never imports that type) — every field is
    read defensively via getattr so a malformed/unexpected shape fails
    closed to "conversation" instead of raising.

    Rule D (confidence is telemetry only) is enforced structurally: this
    function never reads `adapter_result.confidence` at all — see
    test_policy_engine.py::test_policy_never_reads_confidence_attribute,
    which proves this with a tripwire object that raises if the attribute
    is ever accessed, not just by example.
    """
    context = context if context is not None else PolicyContext()

    # --- malformed context fails closed (section 8) ---
    rtc = context.resolved_target_count
    if rtc is not None and (isinstance(rtc, bool) or not isinstance(rtc, int)):
        return _deny("malformed_input", f"context.resolved_target_count must be int or None, got {rtc!r}", False, False)
    if not isinstance(context.multi_call_required, bool):
        return _deny("malformed_input", "context.multi_call_required must be bool", False, False)

    # --- never trust adapter_result's shape (section 8: unknown route, missing field, etc.) ---
    route = getattr(adapter_result, "route", None)
    tool_match = getattr(adapter_result, "tool_match", None)
    fast_path_sufficient = getattr(adapter_result, "fast_path_sufficient", None)
    tool_name = getattr(adapter_result, "tool_name", None)

    if route not in ("conversation", "ha_action", "sensor_query"):
        return _deny("malformed_input", f"unrecognized adapter route: {route!r}", False, False)
    if not isinstance(tool_match, bool) or not isinstance(fast_path_sufficient, bool):
        return _deny(
            "malformed_input",
            "adapter_result.tool_match/fast_path_sufficient must both be bool",
            bool(tool_match) if isinstance(tool_match, bool) else False,
            bool(fast_path_sufficient) if isinstance(fast_path_sufficient, bool) else False,
        )

    # --- Rule A: adapter rejection is terminal for fast path ---
    if route == "conversation":
        return _deny("adapter_rejected", "adapter already routed to conversation", tool_match, fast_path_sufficient)

    # From here the adapter claims a schema-valid single tool call.
    action_class = KNOWN_TOOLS.get(tool_name) if isinstance(tool_name, str) else None

    if not tool_match or action_class is None:
        return _deny(
            "tool_mismatch",
            f"tool_match={tool_match!r} or tool_name={tool_name!r} not recognized by policy's tool registry",
            tool_match, fast_path_sufficient,
        )

    # Defensive cross-check: the adapter's coarse route should agree with our
    # own action-class registry. A disagreement means the two schemas have
    # drifted apart (see test_known_tools_match_adapter_schema) — fail closed
    # rather than trust either side blindly.
    expected_route = "ha_action" if action_class == "WRITE" else "sensor_query"
    if route != expected_route:
        return _deny(
            "tool_mismatch",
            f"adapter route {route!r} disagrees with policy's action_class {action_class!r} for tool {tool_name!r}",
            tool_match, fast_path_sufficient, action_class,
        )

    if context.multi_call_required:
        return _deny("multi_call_required", "context indicates this request needs multiple calls", tool_match, fast_path_sufficient, action_class)

    if context.resolved_target_count == 0:
        return _deny("insufficient_context", "no target resolved for this request", tool_match, fast_path_sufficient, action_class)

    if context.resolved_target_count is not None and context.resolved_target_count > 1:
        return _deny(
            "ambiguous_request",
            f"{context.resolved_target_count} candidate targets resolved, ambiguous",
            tool_match, fast_path_sufficient, action_class,
        )

    if _is_sensitive_tool(tool_name):
        return _deny("sensitive_action", f"tool {tool_name!r} matches a sensitive-action name marker", tool_match, fast_path_sufficient, action_class)

    # --- Rule B/C: tool_match is necessary but not sufficient; fast_path_sufficient is required ---
    if not fast_path_sufficient:
        return _deny(
            "reasoning_required",
            "tool matched but adapter marked fast_path_sufficient=False (comparison/reasoning/synthesis required)",
            tool_match, fast_path_sufficient, action_class,
        )

    return PolicyDecision(
        route="fast_path_candidate",
        reason="all Step 6 eligibility conditions satisfied",
        policy_rule="fast_path_eligible",
        tool_match=tool_match,
        fast_path_sufficient=fast_path_sufficient,
        action_class=action_class,
    )
