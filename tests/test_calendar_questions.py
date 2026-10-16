"""Which spoken questions start the calendar tool, and how event locations are condensed (no HA, no models)."""
import os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
src = open(os.path.join(os.path.dirname(__file__), "..", "live", "server.py")).read()
ns = {"re": re}
exec(src[src.index("_EVENT_WORDS ="):src.index("RESPONDER =")], ns)
CAL_RE = ns["CAL_RE"]
import tools                                                                   # noqa: E402


def test_calendar_questions_are_recognised():
    for q in ("what's on my schedule tomorrow", "Do you know where my workout is?", "What is the location for my workout tomorrow morning?",
              "what time is my dentist appointment", "when is the soccer game", "where's my meeting today", "what's on my calendar"):
        assert CAL_RE.search(q), q


def test_ordinary_questions_are_not():
    for q in ("where is my phone", "when is the weather going to clear up", "what time is it", "where is Paris", "what is the game of thrones about"):
        assert not CAL_RE.search(q), q


def test_compact_location():
    assert tools.compact_location("5501 Main St\nSpringfield, VA 22041\nUSA") == "5501 Main St, Springfield, VA 22041"
    assert tools.compact_location(None) == "" and tools.compact_location("  ") == ""
    assert tools.compact_location("Home") == "Home"


def test_calendar_payload_keeps_locations_apart():
    import asyncio, datetime as dt
    start = dt.datetime.now().astimezone().replace(hour=12, minute=30, second=0, microsecond=0)
    evs = [{"summary": "Workout", "start": {"dateTime": start.isoformat()}, "location": "5501 Main St\nSpringfield, VA 22041\nUSA"},
           {"summary": "Breakfast", "start": {"dateTime": start.isoformat()}}]
    class R:
        async def __aenter__(s): return s
        async def __aexit__(s, *a): return False
        async def json(s): return evs
    class Http:
        def get(s, *a, **k): return R()
    tools.CALENDARS = ["calendar.x"]
    out = asyncio.run(tools.calendar(Http(), "today"))
    head, _, locs = out.partition(" || ")
    assert "5501" not in head and head.count(":") >= 2                            # the readable list has no address
    assert "Workout: 5501 Main St, Springfield, VA 22041" in locs and "only if the user asks" in locs
    evs[0].pop("location"); assert " || " not in asyncio.run(tools.calendar(Http(), "today"))   # no locations, no section
