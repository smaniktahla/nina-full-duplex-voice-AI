"""Short-lived conversational context for follow-ups ("turn them on", "actually just the pendant lights").

Context only ever supplies a REFERENT (which entities "them" means). It never supplies authorization: every follow-up goes through the same
guards -> executor.plan (exposed-only, intent table, sensitive off) as a fresh request. It expires after TTL_S, is dropped on an
unrelated utterance, and a correction that carries no verb is never executed: it is turned into a clarifying question.
Pure, stdlib only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import guards

TTL_S = 120.0
PRONOUNS = frozenset({"them", "it", "those", "these", "that", "they"})
CORRECTION_MARKERS = frozenset({"actually", "instead", "rather", "just", "only", "meant"})


@dataclass
class Referent:
    entity_ids: list[str]
    labels: list[str]
    group: bool                 # came from a household-defined group (so a group write is allowed again)
    intent: str                 # what was last done/asked about them
    wrote: bool                 # the last turn changed (or tried to change) them
    t: float = field(default_factory=time.monotonic)


@dataclass
class Pending:
    """An unanswered clarification ("Did you mean A, B or C?"): the answer is bound to THIS candidate set, not re-parsed as a fresh request."""
    intent: str
    args: dict
    options: list[tuple[str, str]]      # (entity_id | area:<id>, spoken name)
    t: float = field(default_factory=time.monotonic)


_FILLER = frozenset({"a", "an", "the", "one", "please", "um", "uh", "yes", "yeah", "ok", "okay", "my", "in", "of"})
_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "last": -1}


class Context:
    def __init__(self, ttl: float = TTL_S, clock=time.monotonic):
        self.ttl, self.clock, self.last = ttl, clock, None
        self.pending: Pending | None = None

    def ask(self, p: Pending) -> None:
        p.t = self.clock()
        self.pending = p

    def choose(self, text: str) -> tuple[Pending, tuple[str, str]] | None:
        """If a fresh clarification is pending and `text` picks exactly one of its options, return (pending, option); the pending question is
        consumed either way (a non-answer starts a new request)."""
        p, self.pending = self.pending, None
        if p is None or self.clock() - p.t > self.ttl:
            return None
        toks = [t for t in guards.tokens(text) if t not in _FILLER]
        if not toks:
            return None
        if len(toks) == 1 and toks[0] in _ORDINALS and len(p.options) > _ORDINALS[toks[0]]:
            return p, p.options[_ORDINALS[toks[0]]]
        # exact (stemmed) name match wins; else the single option whose name contains every word said
        names = [(o, [t for t in guards.tokens(o[1]) if t not in _FILLER]) for o in p.options]
        exact = [o for o, nt in names if nt == toks]
        if len(exact) == 1:
            return p, exact[0]
        sub = [o for o, nt in names if set(toks) <= set(nt)]
        return (p, sub[0]) if len(sub) == 1 else None

    def remember(self, ref: Referent) -> None:
        ref.t = self.clock()
        self.last = ref

    def fresh(self) -> Referent | None:
        if self.last is not None and self.clock() - self.last.t > self.ttl:
            self.last = None
        return self.last

    def clear(self) -> None:
        self.last = None
        self.pending = None


def uses_pronoun(text: str) -> bool:
    return bool(set(guards.tokens(text)) & PRONOUNS)


def is_correction(text: str) -> bool:
    toks = guards.tokens(text)
    return bool(toks) and (toks[0] in CORRECTION_MARKERS or bool(set(toks) & {"actually", "instead", "rather"}))
