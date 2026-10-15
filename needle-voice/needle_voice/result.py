"""The shared tool-result contract for Needle's domain modules (home_ops today; calendar_ops / navigation_ops next).

Every domain returns a ToolResult. `status` is the only thing callers branch on; `speech` is what the voice assistant says (None only
when the caller is allowed to try a fallback). Invariants:

  1. DENIED (and every other terminal status except UNSUPPORTED / NO_MATCH) never falls back to another agent.
  2. SUCCESS is only claimed when the outcome was observed (or was already true); "the service accepted the call" is UNVERIFIED.
  3. A result whose request was superseded by a newer user turn carries superseded=True and must not be spoken as the current answer.

Pure, stdlib only.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field


class Status(str, enum.Enum):
    SUCCESS = "SUCCESS"                    # completed and verified (or already true / a plain read)
    UNSUPPORTED = "UNSUPPORTED"            # Needle does not implement this intent / shape of request
    NO_MATCH = "NO_MATCH"                  # nothing suitable identified
    AMBIGUOUS = "AMBIGUOUS"                # several plausible targets: ask the user
    DENIED = "DENIED"                      # safety/security policy refused; NEVER falls back
    UNAVAILABLE = "UNAVAILABLE"            # target exists but is unavailable
    EXECUTION_FAILED = "EXECUTION_FAILED"  # the call/tool failed (nothing is believed to have changed)
    UNVERIFIED = "UNVERIFIED"              # submitted, outcome not confirmed; never auto-retried
    CANCELLED = "CANCELLED"                # superseded by a newer user turn BEFORE any side effect


# Fallback to another agent is considered only for these, and only after the common fallback gate (home_ops.fallback_gate) passes.
FALLBACK_CANDIDATES = frozenset({Status.UNSUPPORTED, Status.NO_MATCH})


@dataclass
class ToolResult:
    status: Status
    speech: str | None = None                   # None => caller may try a fallback (only legal for FALLBACK_CANDIDATES)
    reason: str = ""                            # machine code, e.g. "guard:negation", "policy:sensitive_intent"
    telemetry: dict = field(default_factory=dict)
    domain: str = "home"
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    superseded: bool = False                    # a newer user turn arrived while this ran (see HomeAgent)
    side_effect: bool = False                   # True once a state-changing call was actually sent

    @property
    def fallback_candidate(self) -> bool:
        return self.status in FALLBACK_CANDIDATES and not self.side_effect

    @property
    def handled(self) -> bool:
        return self.speech is not None


def new_request_id() -> str:
    return uuid.uuid4().hex[:8]


def now_s() -> float:
    return time.monotonic()
