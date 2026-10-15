"""Calendar questions for the voice assistant: "what's on my calendar today", "what's my next appointment", "when is my dentist appointment",
"where is it". READ-ONLY by design.

Same contract as home_ops (result.ToolResult, honest speech). Nothing here chooses a calendar or a service:

  * Events come from an injected `fetch_events(start, end) -> list[Event] | None` (the caller owns HA access; None means the read failed).
  * Create / move / cancel / delete requests are refused with DENIED (and never fall back to another agent, which could improvise a write)
    until someone explicitly authorises a write path.
  * What was just spoken is remembered in a CalContext so "where is it?" works, and `CalContext.nav_target()` hands the event's location and start
    time to navigation_ops ("when should I leave for it?"). Context supplies a referent only, never authorisation, and expires after CTX_TTL_S.
  * Telemetry carries a result code and counts only: no event titles, locations or descriptions, no addresses.

Pure, stdlib only.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .result import Status, ToolResult

CTX_TTL_S = 600.0
MAX_SPOKEN = 4
LOOKAHEAD_DAYS = 14

FetchEvents = Callable[[dt.datetime, dt.datetime], Awaitable["list[Event] | None"]]


@dataclass
class Event:
    summary: str
    start: dt.datetime                  # timezone-aware; all-day events start at local midnight
    end: dt.datetime
    all_day: bool = False
    location: str = ""
    calendar: str = ""


@dataclass
class CalContext:
    """The events the last answer was about (so follow-ups have a referent). `t` is a monotonic timestamp set by the caller via touch()."""
    events: list = field(default_factory=list)
    t: float = 0.0

    def touch(self, now_s: float) -> None:
        self.t = now_s

    def fresh(self, now_s: float) -> bool:
        return bool(self.events) and now_s - self.t <= CTX_TTL_S

    def nav_target(self):
        """(destination, start) for navigation_ops when the answer was about exactly one event that has a location, else None."""
        if len(self.events) == 1 and self.events[0].location:
            e = self.events[0]
            return e.location, e.start
        return None


@dataclass
class CalConfig:
    fetch_events: FetchEvents | None = None
    max_spoken: int = MAX_SPOKEN
    lookahead_days: int = LOOKAHEAD_DAYS


# ----------------------------------------------------------------------------------------------- parsing (pure)
_WRITE = re.compile(r"(?i)\b(add|create|make|schedule|book|set up|put|move|reschedule|postpone|push back|cancel|delete|remove|clear|change|update|edit|rename|invite)\b"
                    r".*\b(event|appointment|meeting|calendar|reminder|call)\b|\b(reschedule|postpone)\b")
_NEXT = re.compile(r"(?i)\b(next (?:event|appointment|meeting|thing|one)|what'?s next|what is next|when is my next|what do i have next|anything next)\b")
_LIST = re.compile(r"(?i)\b(calendar|schedule|agenda|appointments?|meetings?|events?)\b|\b(do i have|have i got|what do i have|what'?s on|what is on|am i (?:busy|free))\b")
_WHERE = re.compile(r"(?i)\b(where is (?:it|that|the (?:appointment|meeting|event))|where'?s (?:it|that)|what'?s the (?:address|location)|what is the (?:address|location)|where (?:is|are) (?:i|we) (?:going|supposed))\b")
_WHEN = re.compile(r"(?i)\bwhen(?:'s| is| are| does| do)\b\s+(?:my |the |our )?(?P<what>.+?)\s*[?.!]*$")
_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_STOP = frozenset({"my", "the", "a", "an", "our", "appointment", "meeting", "event", "on", "at", "this", "next", "start", "starts", "begin", "begins", "is", "when"})


def parse(text: str) -> tuple:
    """-> (intent, window, subject). intent in write | where | next | list | when | "" (not a calendar request). window like 'today', 'rest', 'tomorrow',
    'week', 'nextweek', 'weekend', a weekday name, or ''."""
    t = (text or "").strip()
    if not t:
        return "", "", ""
    if _WRITE.search(t):
        return "write", "", ""
    low = t.lower()
    window = ""
    if re.search(r"\b(rest of (?:the|my) day|rest of today|later today|tonight|this evening|this afternoon|remaining today)\b", low):
        window = "rest"
    elif re.search(r"\btomorrow\b", low):
        window = "tomorrow"
    elif re.search(r"\bnext week\b", low):
        window = "nextweek"
    elif re.search(r"\bthis weekend\b|\bover the weekend\b", low):
        window = "weekend"
    elif re.search(r"\b(this week|the week|rest of the week)\b", low):
        window = "week"
    elif re.search(r"\btoday\b", low):
        window = "today"
    else:
        for d in _DAYS:
            if re.search(r"\b(on |this |next )?" + d + r"\b", low):
                window = d
                break
    if _WHERE.search(t):
        return "where", window, ""
    if _NEXT.search(t):
        return "next", window, ""
    m = _WHEN.search(t)
    if m and not _LIST.search(t.replace(m.group("what"), "")):
        words = [w for w in re.findall(r"[a-z0-9']+", m.group("what").lower()) if w not in _STOP]
        if words and not re.search(r"\b(i|we) (?:leave|go|get)\b", low):
            return "when", window, " ".join(words)
    if _LIST.search(t) or window:
        return "list", window or "today", ""
    return "", "", ""


# ----------------------------------------------------------------------------------------------- time helpers (pure)
def day_start(d: dt.datetime) -> dt.datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def window_bounds(window: str, now: dt.datetime) -> tuple:
    """-> (start, end, label). 'today' lists what has not ended yet; 'rest' starts at now."""
    today = day_start(now)
    one = dt.timedelta(days=1)
    if window in ("", "today"):
        return now, today + one, "today"
    if window == "rest":
        return now, today + one, "the rest of today"
    if window == "tomorrow":
        return today + one, today + 2 * one, "tomorrow"
    if window == "week":
        return now, today + (7 - today.weekday()) * one, "this week"
    if window == "nextweek":
        s = today + (7 - today.weekday()) * one
        return s, s + 7 * one, "next week"
    if window == "weekend":
        s = today + ((5 - today.weekday()) % 7) * one
        if today.weekday() >= 5:
            s = today
        return max(s, now), day_start(s) + (2 if s.weekday() == 5 else 1) * one, "this weekend"
    if window in _DAYS:
        delta = (_DAYS.index(window) - today.weekday()) % 7 or 7
        s = today + delta * one
        return s, s + one, "on " + window.capitalize()
    return now, today + one, "today"


def clock(t: dt.datetime) -> str:
    h = t.hour % 12 or 12
    return ("%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")) if t.minute else ("%d %s" % (h, "AM" if t.hour < 12 else "PM"))


def when_words(e: Event, now: dt.datetime, with_day: bool = False, bare: bool = False) -> str:
    """bare: the caller already named the day, so say only the time."""
    if e.all_day:
        return "all day"
    day = ""
    if not bare and (with_day or day_start(e.start) != day_start(now)):
        delta = (day_start(e.start) - day_start(now)).days
        day = "tomorrow" if delta == 1 else (e.start.strftime("%A") if delta < 7 else e.start.strftime("%A %B %-d"))
    return ("%s at %s" % (day, clock(e.start))) if day else ("at " + clock(e.start))


def _fail(code: str, speech: str, status: Status = Status.EXECUTION_FAILED, **tel) -> ToolResult:
    return ToolResult(status, speech, reason=code, telemetry={"code": code, **tel}, domain="calendar")


# ----------------------------------------------------------------------------------------------- HA rows -> Event (pure)
def _when(x, tz) -> "tuple[dt.datetime, bool] | None":
    if isinstance(x, dict):
        x = x.get("dateTime") or x.get("date")
    if not isinstance(x, str):
        return None
    try:
        if "T" in x:
            d = dt.datetime.fromisoformat(x.replace("Z", "+00:00"))
            return (d if d.tzinfo else d.replace(tzinfo=tz)).astimezone(tz), False
        return dt.datetime.fromisoformat(x).replace(tzinfo=tz), True
    except ValueError:
        return None


def parse_events(rows, tz: dt.tzinfo, calendar: str = "") -> list:
    """HA calendar rows ({summary, start:{dateTime|date}|str, end, location}) -> Events; malformed rows are skipped."""
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        s, e = _when(r.get("start"), tz), _when(r.get("end"), tz)
        if not s or not e:
            continue
        out.append(Event(str(r.get("summary") or "(no title)"), s[0], e[0], s[1], str(r.get("location") or ""), calendar))
    return out


# ----------------------------------------------------------------------------------------------- the handler
async def handle(text: str, ctx: CalContext | None, cfg: CalConfig, now: dt.datetime | None = None, now_s: float = 0.0) -> ToolResult:
    """`now` must be timezone-aware (defaults to local now). `now_s` is the caller's monotonic clock, used only for context expiry."""
    now = now or dt.datetime.now().astimezone()
    intent, window, subject = parse(text)
    if not intent:
        return ToolResult(Status.UNSUPPORTED, None, reason="not_calendar", domain="calendar")
    if intent == "write":
        return ToolResult(Status.DENIED, "I can read your calendar, but I can't add or change anything on it.",
                          reason="calendar_write_not_supported", telemetry={"code": "write_denied"}, domain="calendar")
    if intent == "where":
        if not ctx or not ctx.fresh(now_s) or not ctx.events:
            return ToolResult(Status.AMBIGUOUS, "Which event do you mean?", reason="no_context", telemetry={"code": "where_no_context"}, domain="calendar")
        withloc = [e for e in ctx.events if e.location]
        if not withloc:
            return ToolResult(Status.NO_MATCH, "That event doesn't have a location.", reason="no_location", telemetry={"code": "no_location"}, domain="calendar")
        if len(ctx.events) > 1:
            return ToolResult(Status.AMBIGUOUS, "Which one? " + _names(ctx.events[:3]), reason="several_events", telemetry={"code": "where_several", "n": len(ctx.events)}, domain="calendar")
        e = withloc[0]
        return ToolResult(Status.SUCCESS, "%s is at %s." % (e.summary, e.location), reason="read", telemetry={"code": "where"}, domain="calendar")

    if not cfg.fetch_events:
        return _fail("not_configured", "I can't reach your calendar yet.", Status.UNAVAILABLE)

    if intent in ("next", "when"):
        start, end = now, now + dt.timedelta(days=cfg.lookahead_days)
        label = "in the next %d days" % cfg.lookahead_days
        if window:
            start, end, label = window_bounds(window, now)
    else:
        start, end, label = window_bounds(window, now)
    try:
        events = await cfg.fetch_events(start, end)
    except Exception as e:                                                      # noqa: BLE001
        return _fail("fetch_error", "I couldn't read your calendar just now.", err=type(e).__name__)
    if events is None:
        return _fail("fetch_failed", "I couldn't read your calendar just now.")
    events = sorted((e for e in events if e.end >= now or e.all_day and day_start(e.start) >= day_start(now)), key=lambda e: e.start)

    if intent == "next":
        events = [e for e in events if e.start >= now - dt.timedelta(minutes=1) or e.all_day]
        if not events:
            return ToolResult(Status.SUCCESS, "Nothing coming up %s." % label, reason="read", telemetry={"code": "next_none", "n": 0}, domain="calendar")
        e = events[0]
        _remember(ctx, [e], now_s)
        return ToolResult(Status.SUCCESS, "Your next event is %s, %s." % (e.summary, when_words(e, now)), reason="read",
                          telemetry={"code": "next", "n": 1, "has_location": bool(e.location)}, domain="calendar")

    if intent == "when":
        want = set(subject.split())
        hits = [e for e in events if want and want <= set(re.findall(r"[a-z0-9']+", e.summary.lower()))]
        if not hits:
            hits = [e for e in events if want & set(re.findall(r"[a-z0-9']+", e.summary.lower()))] if len(want) > 1 else []
        if not hits:
            return ToolResult(Status.NO_MATCH, "I don't see anything like that %s." % label, reason="no_match", telemetry={"code": "when_none", "n": 0}, domain="calendar")
        titles = {e.summary.lower() for e in hits}
        if len(titles) > 1:
            _remember(ctx, hits[:3], now_s)
            return ToolResult(Status.AMBIGUOUS, "I see a few: " + _names(hits[:3]) + ". Which one?", reason="several_matches",
                              telemetry={"code": "when_several", "n": len(hits)}, domain="calendar")
        e = hits[0]
        _remember(ctx, [e], now_s)
        return ToolResult(Status.SUCCESS, "%s is %s." % (e.summary, when_words(e, now)), reason="read",
                          telemetry={"code": "when", "n": 1, "has_location": bool(e.location)}, domain="calendar")

    # list
    if not events:
        speech = {"today": "Nothing more on your calendar today.", "the rest of today": "Nothing else on your calendar today."}.get(label, "Nothing on your calendar %s." % label.replace("on ", "on "))
        return ToolResult(Status.SUCCESS, speech, reason="read", telemetry={"code": "list_none", "n": 0, "window": window or "today"}, domain="calendar")
    spoken = events[:cfg.max_spoken]
    multi_day = len({day_start(e.start) for e in spoken}) > 1
    parts = ["%s %s" % (e.summary, when_words(e, now, with_day=multi_day, bare=not multi_day)) for e in spoken]
    more = len(events) - len(spoken)
    lead = "You have %d event%s %s: " % (len(events), "" if len(events) == 1 else "s", label if label != "today" else "today")
    body = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + ", and " + parts[-1]
    _remember(ctx, spoken, now_s)
    return ToolResult(Status.SUCCESS, lead + body + (", plus %d more" % more if more else "") + ".", reason="read",
                      telemetry={"code": "list", "n": len(events), "spoken": len(spoken), "window": window or "today"}, domain="calendar")


def _names(events) -> str:
    return " or ".join(e.summary for e in events)


def _remember(ctx: CalContext | None, events: list, now_s: float) -> None:
    if ctx is not None:
        ctx.events = list(events)
        ctx.touch(now_s)
