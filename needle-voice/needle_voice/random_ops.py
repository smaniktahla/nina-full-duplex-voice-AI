"""Randomness for the voice assistant: "flip a coin", "roll a d20", "pick a number between 1 and 10", "choose pizza, tacos or sushi".

A language model cannot produce randomness, so this category never reaches Hermes or Qwen: a deterministic parser turns the request into a
call to the shared RNG service (OS CSPRNG, inclusive unbiased ranges; see the rng-service project) and the answer is spoken as is.

Same contract as home_ops / calendar_ops (result.ToolResult):
  * the service is reached through an injected `fetch(tool, params) -> dict | None` (None = unreachable). The caller owns the HTTP;
  * if the service cannot be reached the result is UNAVAILABLE *with speech*, so the router answers honestly instead of letting another
    agent improvise a "random" number;
  * a request this module does not recognise is UNSUPPORTED with no speech (the router carries on);
  * telemetry carries the tool name only, never the drawn value or the items offered (they can be private).

Pure, stdlib only.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable

from .result import Status, ToolResult

Fetch = Callable[[str, dict], Awaitable["dict | None"]]

_W = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
      "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
      "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def spoken_number(s):
    """'7', 'twenty five', 'one hundred', 'a thousand' -> int, or None."""
    s = (s or "").strip().lower().replace("-", " ")
    s = re.sub(r"^(?:a|an)\s+(?=hundred|thousand)", "one ", s)
    s = re.sub(r"^(a|an|the)\s+", "", s)
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    total, cur, seen = 0, 0, False
    for w in s.split():
        if w == "and":
            continue
        if w in _W:
            cur += _W[w]; seen = True
        elif w == "hundred" and seen:
            cur *= 100
        elif w == "thousand" and seen:
            total += cur * 1000; cur = 0
        else:
            return None
    return total + cur if seen else None


_NOUN = r"(?:number|digit|integer)"
_FILLER = re.compile(r"\b(?:a|an|the|one|random|randomly|please|for me|at random|you)\b", re.I)


def parse(text: str):
    """A spoken request for randomness -> (tool, params) for the RNG service, or None. Pure and deterministic."""
    t = " ".join(re.sub(r"[?!.]", " ", (text or "").lower()).replace(",", " , ").split())
    if not t or len(t.split()) > 24:
        return None
    if re.search(r"\b(?:flip|toss|spin)\b[^,]{0,20}\bcoins?\b|\bheads (?:or|and) tails\b|\bcoin (?:flip|toss)\b", t):
        m = re.search(r"\b(\w+)\s+times\b", t) or re.search(r"\b(?:flip|toss)\s+(\w+)\s+coins\b", t)
        n = spoken_number(m.group(1)) if m else None
        return "coin", ({"count": n} if n and 1 < n <= 20 else {})
    m = re.search(r"\b(\d{0,4})\s*d\s*(\d{1,4})\s*(?:(kh|kl)\s*(\d+))?\s*([+-]\s*\d+)?\b", t)
    if m and re.search(r"\b(?:roll|dice|die|d\s*\d)\b", t):
        expr = "%sd%s%s%s" % (m.group(1) or "", m.group(2), (m.group(3) + m.group(4)) if m.group(3) else "", (m.group(5) or "").replace(" ", ""))
        return "dice", {"expr": expr}
    m = re.search(r"\broll\b[^,]*?\b(\w+)\s+(?:(\w+)[ -]sided\s+)?(?:die|dice)\b", t)
    if m:
        n = spoken_number(m.group(1)) if m.group(1) not in ("a", "an") else 1
        sides = spoken_number(m.group(2)) if m.group(2) else 6
        if n and sides:
            return "dice", {"expr": "%dd%d" % (n, sides)}
    if re.search(r"\broll\b[^,]{0,12}\b(?:a )?(?:die|dice)\b", t):
        return "dice", {"expr": "1d6"}
    m = re.search(r"\b%s\b[^,]{0,15}?\b(?:between|from)\s+(.+?)\s+(?:and|to|through|thru)\s+(.+)$" % _NOUN, t) \
        or re.search(r"\b%s\s+(\S+(?: \S+)?)\s+(?:to|through|thru)\s+(\S+(?: \S+)?)$" % _NOUN, t)
    if m and re.search(r"\b(?:random|pick|choose|give|say|name|think|generate|come up)\b", t):
        lo, hi = spoken_number(m.group(1)), spoken_number(m.group(2))
        if lo is not None and hi is not None:
            return "integer", {"min": min(lo, hi), "max": max(lo, hi)}
    if re.search(r"\brandom %s\b" % _NOUN, t) and not re.search(r"\b(?:between|from)\b", t):
        return "integer", {"min": 1, "max": 100}
    m = re.search(r"\b(?:pick|choose|decide|select|settle)\b(?:[^,]{0,30}?\b(?:between|from|of)\b)?\s*(.+)$", t)
    if m and re.search(r"\b(?:or|between)\b", t):
        tail = re.sub(r"\bfor me\b|\bplease\b|\bat random\b", " ", m.group(1))
        if re.match(r"(?:whether|if|how|what|why|when|which)\b", tail.strip()):
            return None
        parts = [p for p in re.split(r"\s*,\s*|\s+or\s+|\s+and\s+", tail) if p.strip()]
        items = [i for i in (" ".join(_FILLER.sub(" ", p).split()) for p in parts) if i]
        if 2 <= len(items) <= 12:
            return "choice", {"items": items}
    return None


def _say_list(xs) -> str:
    xs = [str(x) for x in xs]
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def speak(tool: str, params: dict, out: dict) -> str:
    """What to say for a service answer (natural, short: this is the final spoken answer on the voice path)."""
    r = out.get("result")
    if tool == "coin":
        return "It's %s." % r if not isinstance(r, list) else "You got %s." % _say_list(r)
    if tool == "dice":
        rolls, kept, mod, expr = out.get("rolls", []), out.get("kept", []), out.get("modifier", 0), str(out.get("expr", ""))
        if len(rolls) == 1 and not mod:
            return "You rolled %s." % r
        text = "You rolled %s" % _say_list(rolls)
        if kept != rolls:
            text += ". Keeping the %s %d, %s" % ("highest" if "kh" in expr else "lowest", len(kept), _say_list(kept))
        if mod:
            text += ", %s %d" % ("plus" if mod > 0 else "minus", abs(mod))
        return "%s, for a total of %s." % (text, r)
    if tool == "integer":
        return "Your number is %s." % r
    return "I pick %s." % r


def _fail(code: str, speech: str, status: Status = Status.UNAVAILABLE) -> ToolResult:
    return ToolResult(status, speech, reason=code, telemetry={"code": code}, domain="random")


async def handle(text: str, fetch: "Fetch | None") -> ToolResult:
    req = parse(text)
    if not req:
        return ToolResult(Status.UNSUPPORTED, None, reason="not_random", domain="random")
    tool, params = req
    if fetch is None:
        return _fail("not_configured", "I can't reach the random number generator right now.")
    try:
        out = await fetch(tool, params)
    except Exception:  # noqa: BLE001 - never raise into the router
        out = None
    if not isinstance(out, dict):
        return _fail("unreachable", "I can't reach the random number generator right now, so I won't make one up.")
    if not out.get("ok"):
        err = str(out.get("error") or "")[:120]
        return _fail("refused", ("I couldn't do that: %s." % err.rstrip(".")) if err else "I couldn't do that one.", Status.EXECUTION_FAILED)
    return ToolResult(Status.SUCCESS, speak(tool, params, out), reason="read", telemetry={"code": tool}, domain="random")
