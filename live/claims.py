"""Which deterministic tool (if any) claims a spoken request. ONE table, used by the voice server and by the routing corpus test (tests/test_routing_corpus.py).

A claimed request is answered by code and a tool, never by the reply model's guess. Order matters and is the order the server always used: display, nav, calendar,
weather, random. '' means nobody claims it: it goes to the reply model / Hermes (general knowledge, chit-chat, home control by the model's @ha tag, ...).

  display   show / switch / close the route on the kitchen screen (needs an active route unless the words say route/map/directions; a bare yes needs a fresh offer)
  nav       distance / travel time / traffic / directions to a place, send to phone
  calendar  what is on the calendar, where/when is an appointment
  weather   forecast and current-conditions questions
  random    coin, dice, numbers, picks
  routine   'good morning' / 'good night': the household's configured device list, then spoken reports (routines.py)

Pure; the callers pass the state (active route store, fresh offer) and which tools are configured.
"""
import re
import clock_tool
import routines
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "needle-voice"))
from needle_voice import display_ops, navigation_ops, random_ops                              # noqa: E402

_EVENT_WORDS = "workout|practice|lesson|class|game|dentist|doctor|haircut|appointment|meeting|event|breakfast|lunch|dinner|recital|match|session"
CAL_RE = re.compile(r"\b(calendar|schedule|appointments?|events?|meetings?|agenda)\b"
                    r"|\b(where|when|what time|location|address)\b[^?.!]{0,40}\b(%s)\b" % _EVENT_WORDS                              # also "where is my workout?"
                    + r"|\b(?:am i|are we)\s+(?:busy|free|available)\b"
                    r"|\b(?:do i|do we|have i got|have we got|what do i|what do we)\s+(?:have\s+)?(?:anything|something|plans?|any plans|going on)?[^?.!]{0,25}\b(?:today|tomorrow|tonight|this\s+(?:week|weekend|morning|afternoon|evening)|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)

# Weather: a forecast / conditions question. Follow-ups ABOUT a weather answer ("what location is that weather for?", "where is the weather coming from") are not new
# lookups and stay with the reply model.
WEATHER_RE = re.compile(r"\b(weather|forecast|temperature outside|how (?:hot|cold|warm|chilly) is it(?: outside| out)?|is it (?:hot|cold|warm|chilly|raining|snowing)(?: outside| out)?|"
                        r"(?:will|is|going to|gonna) (?:it )?(?:rain|snow|storm)|(?:do|should) i need (?:a |an )?(?:umbrella|jacket|coat)|chance of (?:rain|snow)|"
                        r"what(?:'s| is) it like outside)\b", re.I)
WEATHER_FOLLOWUP = re.compile(r"\b(location|where|coming from|source|why|which (?:station|sensor)|that weather|this weather|accurate|wrong)\b", re.I)


_DAY_WORD = re.compile(r"\b(today|tomorrow|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next day|previous day|day after|day before|following day|weekend|next week|this week)\b", re.I)


_FOLLOWUP_FILLER = frozenset("and ok okay so then now what about how whats what's is the for on hows how's show me please agenda schedule calendar plans plan day that it um uh well "
                              "lets let's see tell give i have do there anything look looks looking like".split())


def agenda_followup(text: str) -> bool:
    """With an agenda on the screen, a short day-only request ("and tomorrow?", "what about Friday", "the next day") is a calendar follow-up, not a new topic: after the
    day phrase is taken out, only filler words may remain ("how about pizza tomorrow" is not a calendar question)."""
    t = (text or "").lower()
    if len(t.split()) > 8 or not _DAY_WORD.search(t):
        return False
    rest = _DAY_WORD.sub(" ", t.replace("'s", ""))
    return all(w in _FOLLOWUP_FILLER for w in re.findall(r"[a-z']+", rest))


def calendar_when(text: str, base=None) -> str:
    """The day(s) a calendar question is about, as the keyword tools.calendar understands: today | tomorrow | week | next week | weekend | date:YYYY-MM-DD (a weekday)."""
    import datetime as dt
    from needle_voice import agenda_ops
    now = dt.datetime.now().astimezone()
    days, title = agenda_ops.resolve_days(text, now, base)
    if len(days) > 1:
        return "next week" if title == "Next week" else "weekend" if title == "This weekend" else "week"
    k = (days[0] - now.date()).days
    return "today" if k == 0 else "tomorrow" if k == 1 else "date:" + days[0].isoformat()


def weather_when(text: str) -> str:
    low = (text or "").lower()
    return "tomorrow" if "tomorrow" in low else "week" if re.search(r"\b(week|weekend|next few days|this weekend|extended)\b", low) else "today"


def weather_question(text: str) -> bool:
    t = text or ""
    return bool(WEATHER_RE.search(t)) and not WEATHER_FOLLOWUP.search(t)


def claim(text: str, *, store=None, offered: bool = False, display: bool = True, nav: bool = True, places: dict = None, agenda_fresh: bool = False, routine_cfg: dict = None) -> str:
    t = text or ""
    if clock_tool.owns(t):
        return "clock"
    if routines.owns(t, routine_cfg):
        return "routine"
    if display and display_ops.owns(t, store, offered):
        return "display"
    if nav and navigation_ops.owns(t, store, places):
        return "nav"
    if CAL_RE.search(t):
        return "calendar"
    if weather_question(t):
        return "weather"
    if random_ops.parse(t):
        return "random"
    if agenda_fresh and agenda_followup(t):
        return "calendar"
    return ""


def collapse_loops(words, max_run=3):
    """Greedy decoding can fall into a loop ('for a f***ing f***ing f***ing ...' x100) on a trailing word it cannot hear; keep at most
    `max_run` repeats of any 1-3 word unit and drop the rest, so the tail cannot flood the transcript or pass for the user's words."""
    out = list(words)
    for n in (1, 2, 3):
        i = 0
        while i + n <= len(out):
            unit, j = [w.lower().strip(".,!?") for w in out[i:i + n]], i + n
            while [w.lower().strip(".,!?") for w in out[j:j + n]] == unit and len(out[j:j + n]) == n:
                j += n
            reps = (j - i) // n
            if reps > max_run:
                out[i + n * max_run:j] = []
            i += 1
    return out
