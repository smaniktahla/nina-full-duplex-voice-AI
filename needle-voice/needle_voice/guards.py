"""Deterministic guards and intent-verb corroboration. Pure, stdlib only.

The fast tiers (Needle, a 0.8B Qwen) only ever PROPOSE. Nothing they say is
executed unless code in this module independently agrees with it. Observed
2026-09-24 while benchmarking Qwen3.5-0.8B: it labeled "pause plex" as
media_play, and picked different targets for identical input across runs.
So:

  * pre_guard(): utterances that can't be a single simple command (several
    targets, negation, reasoning) never reach either fast tier.
  * intent_signals(): what the WORDS of the utterance imply. A proposed
    intent must be one of them, and contradictory signals escalate.

When any check is unsure the answer is "escalate to Hermes". Coverage lost
that way is acceptable; a wrong physical action is not.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    if not isinstance(text, str):
        return []
    return _WORD.findall(text.lower().replace("'", "").replace("’", ""))


# Groups/lists/negation/reasoning are not "one simple command".
MULTI_MARKERS = frozenset({"and", "then", "also", "plus", "both", "all", "every", "everything", "everywhere", "each"})
NEGATION_MARKERS = frozenset({"not", "dont", "never", "without", "cancel", "nothing", "no"})
REASONING_MARKERS = frozenset({"why", "explain", "because", "versus", "vs", "compare", "compared", "difference", "than"})
REASONING_PHRASES = ("how come",)


def pre_guard(utterance: str) -> str | None:
    """Reason code if the utterance must go straight to Hermes, else None."""
    toks = tokens(utterance)
    if not toks:
        return "empty_utterance"
    tset = set(toks)
    joined = " ".join(toks)
    if tset & REASONING_MARKERS or any(p in joined for p in REASONING_PHRASES):
        return "reasoning_markers"
    if tset & MULTI_MARKERS:
        return "multi_target_markers"
    if tset & NEGATION_MARKERS:
        return "negation"
    return None


WRITE_INTENTS = frozenset({
    "turn_on", "turn_off", "toggle", "set_brightness", "set_temperature",
    "lock", "unlock", "open", "close",
    "media_play", "media_pause", "media_stop", "run_script",
})
READ_INTENTS = frozenset({"get_state", "get_temperature", "get_weather"})

_SWITCHING = frozenset({"turn", "power", "put", "flip"})
_POLITE = frozenset({"please", "can", "could", "would", "you", "hey", "just", "go", "ahead", "and"})


def _switch_is_a_verb(toks: list[str]) -> bool:
    """"switch off the lamp" (verb) vs "is the test switch on" (noun): the verb
    opens the command, after any politeness words."""
    for t in toks:
        if t in _POLITE:
            continue
        return t == "switch"
    return False


def intent_signals(utterance: str) -> set[str]:
    """Intents the utterance's own words imply. Deliberately conservative."""
    toks = tokens(utterance)
    t = set(toks)
    sig: set[str] = set()
    switching = _SWITCHING | ({"switch"} if _switch_is_a_verb(toks) else set())

    if "on" in t and t & switching or t & {"enable", "activate"}:
        sig.add("turn_on")
    if ("off" in t and t & (switching | {"shut"})) or t & {"disable", "deactivate"}:
        sig.add("turn_off")
    if t & {"toggle"}:
        sig.add("toggle")
    if t & {"dim", "brighten", "brightness"} or ("percent" in t and t & {"set", "make", "put"}):
        sig.add("set_brightness")
    if ("thermostat" in t or "degrees" in t) and t & {"set", "make", "change", "put"}:
        sig.add("set_temperature")
    if "unlock" in t:
        sig.add("unlock")
    elif "lock" in t:
        sig.add("lock")
    if t & {"open", "raise"}:
        sig.add("open")
    if ("close" in t) or ("shut" in t and "off" not in t):
        sig.add("close")
    if t & {"pause", "hold"}:
        sig.add("media_pause")
    if t & {"play", "resume", "unpause"}:
        sig.add("media_play")
    if "stop" in t:
        sig.add("media_stop")
    if t & {"run", "execute", "trigger", "launch"}:
        sig.add("run_script")

    if t & {"weather", "forecast", "raining", "rain", "snowing", "snow"}:
        sig.add("get_weather")
    if t & {"temperature", "temp", "hot", "cold", "warm"} and "set_temperature" not in sig:
        sig.add("get_temperature")
    if t & {"is", "are", "status", "state", "whats", "what"} and not (sig & WRITE_INTENTS):
        sig.add("get_state")
        if "temperature" in t or "temp" in t:
            sig.add("get_temperature")
    return sig


def corroborates(proposed_intent: str, utterance: str) -> tuple[bool, str]:
    """True only if the utterance's words imply the proposed intent and do not
    imply a contradictory write intent."""
    sig = intent_signals(utterance)
    write_sigs = sig & WRITE_INTENTS
    if len(write_sigs) > 1:
        return False, f"conflicting_intent_words:{sorted(write_sigs)}"
    if proposed_intent not in sig:
        return False, f"intent_not_implied_by_words:proposed={proposed_intent},implied={sorted(sig)}"
    return True, "ok"
