"""Creating calendar events and reminders by voice: "add a dentist appointment tomorrow at 3", "remind me to call mom at 5".

Same contract as calendar_ops / home_ops (result.ToolResult, honest speech). Nothing here talks to Home Assistant:

  * The model never writes. A request is parsed by regex into a Draft, validated, and READ BACK; nothing is written until the speaker says yes.
  * Writes go through injected callables (`create_event`, `event_exists`, `delete_event`) owned by the executor; this module only decides when to call them.
  * SUCCESS is claimed only when `event_exists` observed the new event; a write that cannot be observed is UNVERIFIED and is never retried.
  * One event per request, no attendees, no recurrence. Calendars come from an allowlist (`cfg.calendars`); the speaker's own calendar is the default.
  * A just-created event can be undone ("undo that") for UNDO_TTL_S; writes are rate limited.
  * Telemetry carries a result code and counts only: no titles, locations or calendar names.

Pure, stdlib only.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .calendar_ops import clock, day_start
from .result import Status, ToolResult

PENDING_TTL_S = 60.0
UNDO_TTL_S = 300.0
DEFAULT_MINUTES = 60
REMINDER_MINUTES = 15
MAX_TITLE = 120
MAX_LOCATION = 200
PAST_GRACE = dt.timedelta(minutes=5)
HORIZON = dt.timedelta(days=730)
MAX_DURATION = dt.timedelta(hours=24)
RATE_MAX, RATE_WINDOW_S = 10, 3600.0

_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
_NUMS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
         "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty five": 45, "forty-five": 45, "sixty": 60, "ninety": 90}
_NUM = r"(?:\d+|" + "|".join(sorted((re.escape(k) for k in _NUMS), key=len, reverse=True)) + r")"
_GENERIC = frozenset({"event", "appointment", "meeting", "reminder", "call", "thing", "something", "entry", "calendar event", "calendar entry", "that", "it", "this"})

CreateEvent = Callable[["Draft"], Awaitable["str | None"]]          # -> "" or a uid once the write was accepted, None when it failed
EventExists = Callable[["Draft", str], Awaitable["str | None"]]     # observed on the calendar? -> its uid ("" if unknown), None if not seen
DeleteEvent = Callable[[str, str, dt.datetime, dt.datetime], Awaitable[bool]]   # (calendar, uid, start, end) -> observed gone?


@dataclass
class Draft:
    title: str = ""
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    all_day: bool = False
    calendar: str = ""                  # calendar id once resolved
    calendar_name: str = ""             # what the speaker called it, before resolution ("" = their own)
    kind: str = "event"                 # event | reminder
    location: str = ""
    duration: dt.timedelta | None = None
    time_given: bool = False
    ampm_open: bool = False             # a bare hour with nothing to decide morning vs evening: start holds the AM reading until _settle_ampm or the speaker answers


@dataclass
class Pending:
    draft: Draft
    stage: str                          # title | time | ampm | calendar | confirm | location
    t: float = 0.0
    speaker: str | None = None


@dataclass
class Undo:
    calendar: str
    uid: str
    title: str
    t: float
    start: dt.datetime | None = None
    end: dt.datetime | None = None


@dataclass
class SchedState:
    pending: Pending | None = None
    undo: Undo | None = None
    writes: list = field(default_factory=list)          # monotonic timestamps of recent writes


@dataclass
class SchedConfig:
    calendars: dict = field(default_factory=dict)         # spoken alias (lowercase) -> calendar id
    labels: dict = field(default_factory=dict)            # calendar id -> spoken label
    speaker_calendars: dict = field(default_factory=dict)  # speaker name (lowercase) -> calendar id
    default_calendar: str = ""                            # used when the speaker is not recognised and no calendar was named ("" = ask)
    create_event: CreateEvent | None = None
    event_exists: EventExists | None = None
    delete_event: DeleteEvent | None = None


# ----------------------------------------------------------------------------------------------- intent (pure)
_REMIND = re.compile(r"(?i)\bremind (?:me|us)\b\s*(?:to |about |that )?(?P<rest>.*)")
_CREATE = re.compile(r"(?i)\b(?:add|create|make|schedule|book|put|set up|set)\b\s+(?P<rest>.+)")
_THING = re.compile(r"(?i)\b(?:calendar|event|appointment|meeting|reminder|call)\b|^\s*(?:schedule|book)\b|\b(?:schedule|book)\s+(?:a|an|the)\b")
_UNDO = re.compile(r"(?i)^\W*(?:undo(?: that| it)?|take that back|delete that|remove that|cancel that one|scratch that)\W*$")
_YES_W = r"(?:yes|yeah|yep|yup|sure|ok|okay|do it|go ahead|go for it|confirm|confirmed|correct|that'?s (?:right|correct)|please do|please|sounds good|perfect|great|thanks|thank you)"
_YES = re.compile(r"(?i)^\W*" + _YES_W + r"(?:\W+" + _YES_W + r")*\W*$")       # "yes, go ahead" / "yeah that's right, thanks"; "yes but make it 4" is not a confirmation
_NO = re.compile(r"(?i)^\W*(?:no|nope|nah|cancel|never ?mind|forget it|don'?t|stop|abort|scratch that)\b\W*")


def parse_intent(text: str) -> str:
    """-> 'remind' | 'create' | 'undo' | ''."""
    t = (text or "").strip()
    if _UNDO.match(t):
        return "undo"
    if _REMIND.search(t):
        return "remind"
    m = _CREATE.search(t)
    if m and _THING.search(t) and not re.match(r"(?i)^\W*(?:(?:hey,?\s+)?nina,?\s*)?(?:please\s+|can you\s+|could you\s+)?(?:move|reschedule|cancel|delete|remove)\b", t):          # a title may contain "cancel"
        return "create"
    return ""


# ----------------------------------------------------------------------------------------------- slot extraction (pure)
def _num(s: str) -> int:
    s = s.strip().lower()
    return int(s) if s.isdigit() else _NUMS.get(s, 0)


def _cut(work: str, m: "re.Match") -> str:
    return work[:m.start()] + " " + work[m.end():]


def _parse_duration(work: str):
    m = re.search(r"(?i)\bfor\s+(?:half an? hour|(?P<n>%s)\s*(?P<u>hours?|hrs?|minutes?|mins?))\b" % _NUM, work)
    if not m:
        m = re.search(r"(?i)\bfor\s+(?P<n>an?)\s+(?P<u>hour)\b", work)
    if not m:
        return None, work
    if m.group("n") is None:
        return dt.timedelta(minutes=30), _cut(work, m)
    n, u = _num(m.group("n")), m.group("u").lower()
    return (dt.timedelta(hours=n) if u.startswith("h") else dt.timedelta(minutes=n)), _cut(work, m)


def _parse_date(work: str, now: dt.datetime):
    """-> (date | None, relative_start | None, work)."""
    today = day_start(now)
    m = re.search(r"(?i)\bin\s+(?P<n>%s)\s*(?P<u>minutes?|mins?|hours?|hrs?|days?|weeks?)\b" % _NUM, work)
    if m:
        n, u = _num(m.group("n")), m.group("u").lower()
        if u.startswith(("min", "h")):
            d = dt.timedelta(minutes=n) if u.startswith("m") else dt.timedelta(hours=n)
            return None, (now + d).replace(second=0, microsecond=0), _cut(work, m)
        return today + dt.timedelta(days=n * (7 if u.startswith("w") else 1)), None, _cut(work, m)
    for pat, off in ((r"\bday after tomorrow\b", 2), (r"\btomorrow\b", 1), (r"\b(?:today|tonight|this evening)\b", 0)):
        m = re.search("(?i)" + pat, work)
        if m:
            return today + dt.timedelta(days=off), None, _cut(work, m)
    m = re.search(r"(?i)\b(?:on |this |next |coming )?(?P<d>%s)\b" % "|".join(_DAYS), work)
    if m:
        delta = (_DAYS.index(m.group("d").lower()) - today.weekday()) % 7
        if m.group(0).lower().startswith("next ") and delta < 7:
            delta = delta or 7
        elif delta == 0 and not m.group(0).lower().startswith(("on ", "this ")):
            delta = 0
        return today + dt.timedelta(days=delta), None, _cut(work, m)
    m = re.search(r"(?i)\b(?:on |the )?(?P<mo>%s)\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s*(?P<y>20\d\d))?" % "|".join(_MONTHS), work)
    if m:
        return _month_day(m, now), None, _cut(work, m)
    m = re.search(r"(?i)\b(?:on |the )?(?P<d>\d{1,2})(?:st|nd|rd|th)? of (?P<mo>%s)\b(?:,?\s*(?P<y>20\d\d))?" % "|".join(_MONTHS), work)
    if m:
        return _month_day(m, now), None, _cut(work, m)
    return None, None, work


def _month_day(m: "re.Match", now: dt.datetime):
    mo, d = _MONTHS.index(m.group("mo").lower()) + 1, int(m.group("d"))
    y = int(m.group("y")) if m.group("y") else now.year
    try:
        out = now.replace(year=y, month=mo, day=d, hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        return None
    if not m.group("y") and out < day_start(now) - dt.timedelta(days=30):    # a few days back is 'already passed', not next year
        try:
            out = out.replace(year=y + 1)
        except ValueError:
            return None
    return out


def _parse_clock(work: str):
    """-> ((hour, minute, explicit_meridiem) | None, work)."""
    m = re.search(r"(?i)\b(?:at |around |by )?(?P<h>\d{1,2})(?:[:.](?P<m>\d\d))?\s*(?P<ap>[ap])\.?m\.?(?!\w)", work)
    if m:
        h, mi, ap = int(m.group("h")), int(m.group("m") or 0), m.group("ap").lower()
        if 1 <= h <= 12 and mi < 60:
            return (h % 12 + (12 if ap == "p" else 0), mi, True), _cut(work, m)
    m = re.search(r"(?i)\b(?:at |around |by )?(?P<w>noon|midnight)\b", work)
    if m:
        return ((12 if m.group("w").lower() == "noon" else 0), 0, True), _cut(work, m)
    m = re.search(r"(?i)\bat\s+(?P<h>\d{1,2})(?:[:.](?P<m>\d\d))?(?![\w:])(?!\s*(?:o'?clock\s*)?(?:people|items))", work)
    if m:
        h, mi = int(m.group("h")), int(m.group("m") or 0)
        if 1 <= h <= 12 and mi < 60:
            return (h, mi, False), _cut(work, m)
    return None, work


_PM_CUE = re.compile(r"(?i)\b(?:tonight|this evening|evening|at night|tomorrow night|afternoon|this afternoon)\b")
_AM_CUE = re.compile(r"(?i)\b(?:morning|this morning)\b")


def _cued_hour(h: int, cue: str) -> int:
    """No am/pm said but the speaker named a part of the day: 'tonight at 8:30' is 20:30, 'tomorrow morning at 8' is 08:00."""
    return h % 12 + (12 if cue == "pm" else 0)


def _guess_meridiem(h: int) -> int:
    """No am/pm said: 7-11 means morning, 12 noon, 1-6 afternoon. The read-back is what catches a wrong guess."""
    return h if 7 <= h <= 11 else (12 if h == 12 else h + 12)


_CAL = re.compile(r"(?i)\b(?:on|to|in|onto|into)\s+(?:the\s+|my\s+|our\s+)?(?:(?P<n>[\w']+?)(?:'s)?\s+)?calendar\b")
_LEAD = re.compile(r"(?i)^\s*(?:hey,?\s+nina,?\s*|nina,?\s*|please\s+|can you\s+|could you\s+|would you\s+)*")


def _clean_title(s: str) -> str:
    s = re.sub(r"(?i)\b(?:called|named|titled|that says|saying)\b", " ", s)
    s = re.sub(r"(?i)\b(?:add|create|make|schedule|book|put|set up|set)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.;:!?-")
    s = re.sub(r"(?i)^(?:(?:an?|the|my|new|me)\s+)+", "", s)
    s = re.sub(r"(?i)(?:\s+(?:on|at|for|in|to|and|by|around|my|the|a|an|of|from|onto|into))+$", "", s).strip(" ,.;:!?-")
    s = re.sub(r"(?i)^(?:event|appointment|reminder)\s+(?:for|to|about|called|named)\s+", "", s).strip()
    s = re.sub(r"(?i)^(?:to|about|that)\s+", "", s).strip()
    return (s[:1].upper() + s[1:]) if s else ""


_LOC_MARK = re.compile(r"(?i)\b(?:the\s+)?(?:location|address)\s*(?:is|of|to|:)?\s+(?:at\s+)?(?P<l>\S.*)$")
_LOC_AT = re.compile(r"(?i)\bat\s+(?P<l>(?!\d)\S.*)$")
_LOC_CLEAR = re.compile(r"(?i)\b(?:no|remove|clear|delete|without|drop)\b.*\blocation\b|\blocation\b.*\b(?:remove|clear|delete|none)\b")
_LOC_ASK = re.compile(r"(?i)^\W*(?:(?:can you |please )?(?:add|set|give it|put|include)\s+)?(?:with\s+)?(?:a|the)\s+(?:location|address)\W*$|^\W*(?:add|set)\s+(?:a\s+)?(?:location|address)\W*$")


def _clean_loc(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip(" ,.;:!?-")
    s = re.sub(r"(?i)^(?:it'?s |it is |at |in |the location is )+", "", s).strip(" ,.;:!?-")
    s = re.sub(r"(?i)(?:\s+(?:on|at|for|in|to|and|by|around|my|the|a|an|of|from))+$", "", s).strip(" ,.;:!?-")
    return s[:MAX_LOCATION]


def _split_location(work: str):
    """Event text left after time words are gone -> (text_without_location, location)."""
    m = _LOC_MARK.search(work) or _LOC_AT.search(work)
    if not m:
        return work, ""
    return work[:m.start()], _clean_loc(m.group("l"))


def parse_create(text: str, now: dt.datetime, intent: str | None = None) -> Draft:
    """Pull title / time / duration / calendar out of one utterance. Missing slots stay empty/None; the handler asks for them."""
    intent = intent or parse_intent(text)
    work = _LEAD.sub("", text or "").strip()
    first = _REMIND.search(work) if intent == "remind" else _CREATE.search(work)
    if first:
        work = work[first.start():]                  # drop anything said before the request ("Yeah, what's up? Hey, can you add ...")
    d = Draft(kind="reminder" if intent == "remind" else "event")
    m = _REMIND.search(work) if intent == "remind" else None
    if m:
        work = m.group("rest")
    m = _CAL.search(work)
    if m and intent != "remind":
        d.calendar_name = (m.group("n") or "").lower()
        work = _cut(work, m)
    dur, work = _parse_duration(work)
    allday = re.search(r"(?i)\ball[- ]day\b", work)
    if allday:
        work = _cut(work, allday)
    cue = "pm" if _PM_CUE.search(work) else "am" if _AM_CUE.search(work) else ""
    date, rel, work = _parse_date(work, now)
    clk, work = _parse_clock(work)
    if intent != "remind":
        work, d.location = _split_location(work)
    d.title = _clean_title(work)
    if d.title.lower() in _GENERIC and intent != "remind":
        d.title = ""
    d.duration = dur
    d.all_day = bool(allday)
    if rel is not None and not allday:
        d.start, d.time_given = rel, True
    elif allday and (date or rel is None):
        d.start = date or day_start(now)
        d.time_given = True
    elif clk:
        d.time_given = True
        base = date or day_start(now)
        h, mi, explicit = clk
        if not explicit:
            if cue:
                h = _cued_hour(h, cue)
            elif h == 12:
                pass                                         # "at 12" is noon
            else:
                d.ampm_open, h = True, h % 12                # decided in _settle_ampm, once the title is known
        d.start = base.replace(hour=h, minute=mi)
        if date is None and not d.ampm_open and d.start < now - PAST_GRACE:
            if not explicit and h < 12 and d.start + dt.timedelta(hours=12) > now:
                d.start += dt.timedelta(hours=12)          # "at 3" said at 1 pm means 3 pm, not tomorrow 3 am
            else:
                d.start += dt.timedelta(days=1)
    elif date:
        d.start = date                                       # a date but no time: stage "time" asks, d.time_given stays False
    return _finish(d)


_AM_WORDS = re.compile(r"(?i)\b(?:a\.?m\.?|morning|breakfast|sunrise)\b")
_PM_WORDS = re.compile(r"(?i)\b(?:p\.?m\.?|evening|night|tonight|afternoon|dinner|supper)\b")
_MEAL_AM = re.compile(r"(?i)\b(?:breakfast|brunch|wake[- ]?up|school bus|drop[- ]?off)\b")
_MEAL_PM = re.compile(r"(?i)\b(?:dinner|supper|date night|movie night|bedtime|happy hour)\b")
_LUNCH = re.compile(r"(?i)\blunch")


def _settle_ampm(d: Draft, now: dt.datetime) -> bool:
    """Resolve a bare hour from what is known, in this order: a meal in the title (breakfast = morning, dinner = evening, lunch = 11 am / 1 pm), then
    today's clock (when only one reading is still ahead, it is that one). True = settled; False = Nina has to ask."""
    hr = d.start.hour
    pick = ""
    t = d.title or ""
    if _MEAL_AM.search(t):
        pick = "am"
    elif _MEAL_PM.search(t):
        pick = "pm"
    elif _LUNCH.search(t):
        pick = "am" if hr == 11 else "pm"
    elif day_start(d.start) == day_start(now):
        ahead = [k for k, c in (("am", d.start), ("pm", d.start + dt.timedelta(hours=12))) if c >= now - PAST_GRACE]
        if len(ahead) == 1:
            pick = ahead[0]
    if not pick:
        return False
    _apply_ampm(d, pick)
    return True


def _apply_ampm(d: Draft, which: str) -> None:
    if which == "pm":
        d.start += dt.timedelta(hours=12)
    d.ampm_open = False
    d.end = None
    _finish(d)


def _finish(d: Draft) -> Draft:
    if d.start is not None and d.time_given:
        if d.all_day:
            d.start = day_start(d.start)
            d.end = d.start + dt.timedelta(days=1)
        else:
            span = d.duration or dt.timedelta(minutes=REMINDER_MINUTES if d.kind == "reminder" else DEFAULT_MINUTES)
            d.end = d.start + span
    return d


# ----------------------------------------------------------------------------------------------- validation + speech (pure)
def validate(d: Draft, now: dt.datetime) -> str:
    """-> '' when fine, else a speakable reason (also the telemetry code after 'invalid:')."""
    if not d.title or len(d.title) > MAX_TITLE:
        return "bad_title"
    if d.start is None or d.end is None:
        return "no_time"
    if len(d.location) > MAX_LOCATION:
        return "bad_title"
    if d.end <= d.start or d.end - d.start > MAX_DURATION + (dt.timedelta(days=1) if d.all_day else dt.timedelta()):
        return "bad_duration"
    floor = day_start(now) if d.all_day else now - PAST_GRACE
    if d.start < floor:
        return "in_the_past"
    if d.start > now + HORIZON:
        return "too_far"
    return ""


_INVALID_SPEECH = {
    "in_the_past": "That time has already passed. When should it be?",
    "too_far": "That's more than two years away, so I won't put it on the calendar. When should it be?",
    "bad_duration": "That doesn't work as a length of time. How long should it be?",
    "bad_title": "I didn't get a usable name for it. What should I call it?",
    "no_time": "When should it be?",
}


def day_words(d: dt.datetime, now: dt.datetime) -> str:
    delta = (day_start(d) - day_start(now)).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if 1 < delta < 7:
        return d.strftime("%A")
    return d.strftime("%A, %B %-d") + (d.strftime(", %Y") if d.year != now.year else "")


def length_words(span: dt.timedelta) -> str:
    mins = int(span.total_seconds() // 60)
    if mins == 60:
        return "an hour"
    if mins == 30:
        return "half an hour"
    if mins % 60 == 0:
        return "%d hours" % (mins // 60)
    return "%d minutes" % mins if mins < 60 else "%d hours %d minutes" % divmod(mins, 60)


def readback(d: Draft, now: dt.datetime, where: str) -> str:
    """`where` is the spoken calendar phrase from cfg.labels ("Salil's calendar"). The calendar is ALWAYS named, never "your calendar":
    the default comes from speaker recognition, and a wrong guess must be audible before the yes."""
    day = day_words(d.start, now)
    if d.kind == "reminder":
        return "I'll remind you to %s %s at %s. Is that right?" % (d.title[:1].lower() + d.title[1:], day, clock(d.start))
    if d.all_day:
        return "I'll add %s to %s for %s, all day%s. Is that right?" % (d.title, where, day, ", location " + d.location if d.location else "")
    loc = ", location %s" % d.location if d.location else ""
    return "I'll add %s to %s %s at %s for %s%s. Is that right?" % (d.title, where, day, clock(d.start), length_words(d.end - d.start), loc)


def done_words(d: Draft, now: dt.datetime, where: str = "the calendar") -> str:
    if d.kind == "reminder":
        return "Done. I'll remind you %s at %s." % (day_words(d.start, now), clock(d.start))
    return "Done. %s is on %s for %s%s." % (d.title, where, day_words(d.start, now), "" if d.all_day else " at " + clock(d.start))


# ----------------------------------------------------------------------------------------------- reply classification (pure)
def classify_reply(text: str) -> str:
    """-> 'yes' | 'no' | 'other'. 'yes' must be the whole utterance, so "yes but make it 4" is a patch, not a confirmation."""
    t = (text or "").strip()
    if _YES.match(t):
        return "yes"
    if _NO.match(t):
        return "no"
    return "other"


def patch(d: Draft, text: str, now: dt.datetime) -> Draft | None:
    """Apply a spoken correction ("make it 4", "tomorrow instead", "call it Foo", "for 2 hours"). None when nothing was understood."""
    work = re.sub(r"(?i)^\W*(?:no,?\s*|actually,?\s*|wait,?\s*|make it|change it to|change that to|move it to|let'?s (?:say|do)|instead)\b", " ", text or "")
    work = re.sub(r"(?i)\binstead\b", " ", work)
    nd = Draft(**{**d.__dict__})
    if d.kind == "event":
        if _LOC_CLEAR.search(text or ""):
            nd.location = ""
            return nd
        m = _LOC_MARK.search(text or "") or re.search(r"(?i)\b(?:put|set|have|hold|make) it (?:at|in)\s+(?P<l>\S.*)$", text or "")
        if m and _clean_loc(m.group("l")):
            nd.location = _clean_loc(m.group("l"))
            return nd
    m = re.search(r"(?i)\b(?:call it|name it|title it|rename it to|it'?s called)\s+(?P<t>.+)", work)
    if m:
        nd.title = _clean_title(m.group("t"))
        return _finish(nd) if nd.title else None
    dur, work = _parse_duration(work)
    date, rel, work = _parse_date(work, now)
    clk, work = _parse_clock(work)
    if not clk and not date and rel is None and dur is None:
        bare = re.fullmatch(r"\W*(\d{1,2})(?::(\d\d))?\W*", work)           # "make it 4"
        if bare and 1 <= int(bare.group(1)) <= 12 and int(bare.group(2) or 0) < 60:
            clk = (int(bare.group(1)), int(bare.group(2) or 0), False)
    if not (clk or date or rel is not None or dur):
        m = re.match(r"(?i)^\W*(?:at|in)\s+(?P<l>\S.*)$", text or "")
        if m and d.kind == "event" and _clean_loc(m.group("l")):
            nd.location = _clean_loc(m.group("l"))
            return nd
        return None
    if dur:
        nd.duration = dur
    if rel is not None:
        nd.start, nd.time_given = rel, True
    elif nd.start is not None or date or clk:
        base = date or (day_start(nd.start) if nd.start else day_start(now))
        if clk:
            h, mi, explicit = clk
            if not explicit:
                if nd.start and nd.start.hour >= 12 and 1 <= h <= 6 or not nd.start:
                    h = _guess_meridiem(h)
                elif nd.start:
                    h = h % 12 + (12 if nd.start.hour >= 12 and h < 12 else 0)
            nd.start, nd.time_given = base.replace(hour=h, minute=mi), True
        else:
            t0 = nd.start or base
            nd.start = base.replace(hour=t0.hour, minute=t0.minute)
            nd.time_given = nd.time_given or False
    nd.all_day = False if clk else nd.all_day
    return _finish(nd)


_QUESTION = re.compile(r"(?i)^\W*(?:what|what's|whats|how|when|where|who|why|which|is|are|do|does|did|can|could|will|should|tell me)\b")


def owns(text: str, st: SchedState, now_s: float) -> bool:
    """Should the voice server hand this utterance to handle()? A fresh request, "undo that" while there is something to undo, or a plausible
    reply to a pending question (so an unrelated question during the 60 s window still goes where it belongs)."""
    t = text or ""
    intent = parse_intent(t)
    if intent == "undo":
        return bool(st.undo and now_s - st.undo.t <= UNDO_TTL_S)
    if intent:
        return True
    p = st.pending
    if not p or now_s - p.t > PENDING_TTL_S:
        return False
    if p.stage == "confirm":
        now = dt.datetime.now().astimezone()
        if classify_reply(t) != "other" or _LOC_ASK.match(t):
            return True
        # a correction is short and is not a question ("what's the weather tomorrow" must not be read as "tomorrow instead")
        return len(t.split()) <= 9 and not _QUESTION.match(t) and patch(p.draft, t, now) is not None
    return len(t.split()) <= 10 and classify_reply(t) in ("other", "no")


# ----------------------------------------------------------------------------------------------- the handler
def _res(status: Status, speech: str, code: str, **tel) -> ToolResult:
    return ToolResult(status, speech, reason=code, telemetry={"code": code, **tel}, domain="schedule")


def _which(cfg: SchedConfig) -> str:
    labels = [cfg.labels[c] for c in dict.fromkeys(cfg.calendars.values()) if c in cfg.labels]
    return "Which calendar: %s?" % (", ".join(labels[:-1]) + ", or " + labels[-1] if len(labels) > 1 else labels[0]) if labels else "Which calendar should I use?"


def _resolve_calendar(d: Draft, speaker: str | None, cfg: SchedConfig) -> "ToolResult | None":
    """Fill d.calendar. -> None when resolved, else a ToolResult (unknown name / need to ask)."""
    if d.calendar_name:
        cid = cfg.calendars.get(d.calendar_name)
        if not cid:
            return _res(Status.NO_MATCH, "I don't have a calendar called %s." % d.calendar_name, "unknown_calendar")
        d.calendar = cid
        return None
    cid = cfg.speaker_calendars.get((speaker or "").lower())
    cid = cid or cfg.default_calendar
    if cid:
        d.calendar = cid
        return None
    return _res(Status.AMBIGUOUS, _which(cfg), "ask_calendar")


def _next_step(d: Draft, st: SchedState, speaker: str | None, cfg: SchedConfig, now: dt.datetime, now_s: float) -> ToolResult:
    """Decide what to ask next (title, time, calendar) or read the finished draft back."""
    if not d.title:
        st.pending = Pending(d, "title", now_s, speaker)
        return _res(Status.AMBIGUOUS, "What should I call it?", "ask_title")
    if d.start is None or not d.time_given:
        st.pending = Pending(d, "time", now_s, speaker)
        return _res(Status.AMBIGUOUS, "What time?" if d.start is not None else "When should it be?", "ask_time")
    if d.ampm_open and not _settle_ampm(d, now):
        st.pending = Pending(d, "ampm", now_s, speaker)
        hr, mi = d.start.hour, d.start.minute
        return _res(Status.AMBIGUOUS, "Is that %s in the morning or in the evening?" % (("%d:%02d" % (hr, mi)) if mi else str(hr)), "ask_ampm")
    bad = validate(d, now)
    if bad:
        d.start, d.end, d.time_given = None, None, False
        st.pending = Pending(d, "time", now_s, speaker)
        return _res(Status.AMBIGUOUS, _INVALID_SPEECH[bad], "invalid:" + bad)
    if not d.calendar:
        miss = _resolve_calendar(d, speaker, cfg)
        if miss:
            st.pending = Pending(d, "calendar", now_s, speaker) if miss.status is Status.AMBIGUOUS else None
            return miss
    st.pending = Pending(d, "confirm", now_s, speaker)
    return _res(Status.AMBIGUOUS, readback(d, now, cfg.labels.get(d.calendar, "that calendar")), "confirm_pending", kind=d.kind)


async def _write(p: Pending, st: SchedState, cfg: SchedConfig, now: dt.datetime, now_s: float) -> ToolResult:
    d = p.draft
    st.writes = [t for t in st.writes if now_s - t < RATE_WINDOW_S]
    if len(st.writes) >= RATE_MAX:
        return _res(Status.DENIED, "That's a lot of calendar changes in a row, so I'm pausing. Try again in a while.", "rate_limited")
    if not (cfg.create_event and cfg.event_exists):
        return _res(Status.UNAVAILABLE, "I can't reach your calendar to add things yet.", "not_configured")
    st.writes.append(now_s)
    try:
        uid = await cfg.create_event(d)
    except Exception as e:                                                  # noqa: BLE001
        return _res(Status.EXECUTION_FAILED, "I couldn't add that, and nothing was saved.", "create_error", err=type(e).__name__)
    if uid is None:
        return _res(Status.EXECUTION_FAILED, "I couldn't add that, and nothing was saved.", "create_failed")
    try:
        found = await cfg.event_exists(d, uid)
    except Exception:                                                       # noqa: BLE001
        found = None
    seen = found is not None
    uid = found or uid
    r = (_res(Status.SUCCESS, done_words(d, now, cfg.labels.get(d.calendar, "the calendar")), "created", kind=d.kind) if seen else
         _res(Status.UNVERIFIED, "I sent it, but I couldn't confirm it landed. Please check your calendar.", "created_unverified", kind=d.kind))
    r.side_effect = True
    st.undo = Undo(d.calendar, uid, d.title, now_s, d.start, d.end) if uid else None
    return r


async def _undo(st: SchedState, cfg: SchedConfig, now_s: float) -> ToolResult:
    u = st.undo
    if not u or now_s - u.t > UNDO_TTL_S:
        return _res(Status.NO_MATCH, "I don't have anything I just added to undo.", "undo_none")
    if not cfg.delete_event:
        return _res(Status.UNAVAILABLE, "I can't remove things from your calendar yet.", "undo_not_configured")
    try:
        gone = await cfg.delete_event(u.calendar, u.uid, u.start, u.end)
    except Exception:                                                       # noqa: BLE001
        gone = False
    if not gone:
        return _res(Status.UNVERIFIED, "I tried to remove it but couldn't confirm. Please check your calendar.", "undo_unverified")
    st.undo = None
    r = _res(Status.SUCCESS, "Okay, I removed %s." % u.title, "undone")
    r.side_effect = True
    return r


async def handle(text: str, st: SchedState, cfg: SchedConfig, speaker: str | None = None,
                 now: dt.datetime | None = None, now_s: float = 0.0) -> ToolResult:
    """One user turn. `st` is the per-household state (mutated). `now` is timezone-aware; `now_s` is the caller's monotonic clock.
    Returns UNSUPPORTED/NO_MATCH when the turn is not about scheduling and nothing is pending (the caller may route it elsewhere)."""
    now = now or dt.datetime.now().astimezone()
    p = st.pending
    if p and now_s - p.t > PENDING_TTL_S:
        st.pending = p = None
    if p and parse_intent(text) in ("create", "remind"):
        st.pending = p = None                                   # a fresh, complete request replaces the half-finished one
    if p:
        return await _pending_turn(text, p, st, cfg, speaker, now, now_s)
    intent = parse_intent(text)
    if not intent:
        return ToolResult(Status.UNSUPPORTED, None, reason="not_schedule", domain="schedule")
    if intent == "undo":
        return await _undo(st, cfg, now_s)
    d = parse_create(text, now, intent)
    return _next_step(d, st, speaker, cfg, now, now_s)


async def _pending_turn(text: str, p: Pending, st: SchedState, cfg: SchedConfig, speaker: str | None, now: dt.datetime, now_s: float) -> ToolResult:
    d = p.draft
    speaker = p.speaker or speaker
    kind = classify_reply(text)
    if p.stage == "location":
        if kind == "no" or _LOC_CLEAR.search(text or ""):
            p.stage = "confirm"
        else:
            d.location = _clean_loc(text)
        return _next_step(d, st, speaker, cfg, now, now_s)
    if p.stage == "confirm" and d.kind == "event" and _LOC_ASK.match(text or ""):
        st.pending = Pending(d, "location", now_s, speaker)
        return _res(Status.AMBIGUOUS, "Where is it?", "ask_location")
    if p.stage == "ampm":
        am, pm = _AM_WORDS.search(text or ""), _PM_WORDS.search(text or "")
        if kind == "no" or (not am and not pm):
            if kind == "no":
                st.pending = None
                return _res(Status.CANCELLED, "Okay, I won't add it.", "declined")
            return _res(Status.AMBIGUOUS, "Sorry, morning or evening?", "ask_ampm_again")
        _apply_ampm(d, "pm" if pm and not am else "am")
        return _next_step(d, st, speaker, cfg, now, now_s)
    if kind == "no" and p.stage in ("confirm", "title", "time", "calendar"):
        st.pending = None
        return _res(Status.CANCELLED, "Okay, I won't add it.", "declined")
    if p.stage == "confirm":
        if kind == "yes":
            st.pending = None
            return await _write(p, st, cfg, now, now_s)
        nd = patch(d, text, now)
        if nd is None:
            return _res(Status.AMBIGUOUS, "Sorry, was that a yes or a no? You can also tell me what to change.", "confirm_unclear")
        return _next_step(nd, st, speaker, cfg, now, now_s)
    if p.stage == "title":
        d.title = _clean_title(text)
    elif p.stage == "time":
        nd = parse_create(text, now, "remind" if d.kind == "reminder" else "create")
        if nd.start is None:
            bare = re.fullmatch(r"\W*(?:at )?(\d{1,2})(?::(\d\d))?\W*", text or "")
            if bare and 1 <= int(bare.group(1)) <= 12 and int(bare.group(2) or 0) < 60:
                nd = parse_create("at " + bare.group(0).strip(" .,!?").replace("at ", ""), now, "create")
        if nd.start is None:
            return _res(Status.AMBIGUOUS, "Sorry, I didn't catch the time. When should it be?", "ask_time_again")
        if d.start is not None and not nd.time_given:
            nd.start, nd.time_given = d.start, False
        if d.start is not None and not d.time_given and _parse_date(text or "", now)[:2] == (None, None):
            nd.start = day_start(d.start).replace(hour=nd.start.hour, minute=nd.start.minute)       # keep the day already given
            nd.end = None
        nd.title, nd.calendar_name, nd.calendar, nd.kind = d.title, d.calendar_name, d.calendar, d.kind
        nd.duration = nd.duration or d.duration
        d = _finish(nd)
    elif p.stage == "calendar":
        name = re.sub(r"(?i)^(?:the |my |on |to |in |it'?s |put it on |use )+|\s*calendar$", "", (text or "").strip(" .,!?")).strip().lower()
        name = re.sub(r"(?i)'s$", "", name).strip()
        cid = cfg.calendars.get(name) or cfg.calendars.get(name.split(" ")[0]) if name else None
        if not cid:
            return _res(Status.AMBIGUOUS, _which(cfg), "ask_calendar_again")
        d.calendar_name, d.calendar = name, cid
    return _next_step(d, st, speaker, cfg, now, now_s)
