import asyncio, datetime as dt
from needle_voice import calendar_ops as c
from needle_voice.calendar_ops import CalConfig, CalContext, Event
from needle_voice.result import Status

TZ = dt.timezone(dt.timedelta(hours=-4))
NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=TZ)           # a Wednesday


def ev(title, h, day=0, loc="", mins=60, all_day=False):
    s = dt.datetime(2026, 10, 14 + day, h, 0, tzinfo=TZ)
    return Event(title, s, s + dt.timedelta(minutes=mins), all_day, loc)


def run(text, events, ctx=None, now_s=0.0, fail=None):
    async def fetch(a, b):
        if fail == "raise":
            raise RuntimeError("boom")
        return None if fail == "none" else [e for e in events if e.start < b and e.end > a]
    return asyncio.run(c.handle(text, ctx, CalConfig(fetch_events=fetch), NOW, now_s))


EVENTS = [ev("Dentist", 14, loc="12 Example Street"), ev("Standup", 10), ev("Team lunch", 12, day=1), ev("Review", 11, day=1, loc="Room 4")]


def test_parse_intents():
    assert c.parse("what's on my calendar today")[0] == "list"
    assert c.parse("what's my next appointment")[0] == "next"
    assert c.parse("when is my dentist appointment")[:1] == ("when",) and c.parse("when is my dentist appointment")[2] == "dentist"
    assert c.parse("where is it")[0] == "where"
    assert c.parse("add a meeting to my calendar")[0] == "write"
    assert c.parse("turn on the lights")[0] == ""


def test_list_today_and_tomorrow():
    r = run("what's on my calendar today", EVENTS)
    assert r.status is Status.SUCCESS and "Standup at 10 AM" in r.speech and "Dentist at 2 PM" in r.speech and "Team lunch" not in r.speech
    r = run("what do I have tomorrow", EVENTS)
    assert "Review at 11 AM" in r.speech and "Dentist" not in r.speech


def test_empty_day_is_honest():
    r = run("anything on my calendar tomorrow", [])
    assert r.status is Status.SUCCESS and "Nothing" in r.speech


def test_next_event_remembers_location():
    ctx = CalContext()
    r = run("what's my next event", EVENTS, ctx, now_s=5)
    assert r.status is Status.SUCCESS and "Standup" in r.speech and ctx.events[0].summary == "Standup"
    r = run("when is my dentist appointment", EVENTS, ctx, now_s=6)
    assert r.status is Status.SUCCESS and "Dentist" in r.speech and "2 PM" in r.speech
    assert ctx.nav_target() == ("12 Example Street", EVENTS[0].start)
    r = run("where is it", EVENTS, ctx, now_s=7)
    assert r.status is Status.SUCCESS and "12 Example Street" in r.speech


def test_where_without_context_or_location():
    assert run("where is it", EVENTS, CalContext()).status is Status.AMBIGUOUS
    ctx = CalContext()
    run("what's my next event", EVENTS, ctx, now_s=1)
    assert run("where is it", EVENTS, ctx, now_s=2).status is Status.NO_MATCH          # Standup has no location
    assert run("where is it", EVENTS, ctx, now_s=1 + c.CTX_TTL_S + 1).status is Status.AMBIGUOUS


def test_when_no_match_and_ambiguous():
    assert run("when is my haircut", EVENTS).status is Status.NO_MATCH
    two = EVENTS + [ev("Team standup", 15)]
    assert run("when is my team", two).status in (Status.SUCCESS, Status.AMBIGUOUS)


def test_writes_are_denied_and_never_fetch():
    called = []

    async def fetch(a, b):
        called.append(1)
        return []
    for t in ("add a dentist appointment to my calendar", "cancel my meeting", "reschedule the dentist appointment"):
        r = asyncio.run(c.handle(t, None, CalConfig(fetch_events=fetch), NOW))
        assert r.status is Status.DENIED
    assert not called


def test_failures_do_not_pretend():
    assert run("what's on my calendar today", EVENTS, fail="none").status is Status.EXECUTION_FAILED
    r = run("what's on my calendar today", EVENTS, fail="raise")
    assert r.status is Status.EXECUTION_FAILED and r.telemetry["err"] == "RuntimeError"
    r = asyncio.run(c.handle("what's on my calendar today", None, CalConfig(), NOW))
    assert r.status is Status.UNAVAILABLE


def test_many_events_capped_and_telemetry_clean():
    many = [ev("Meeting %d" % i, 10 + i % 8) for i in range(7)]
    r = run("what's on my calendar today", many)
    assert "plus 3 more" in r.speech and r.telemetry["n"] == 7
    r = run("when is my dentist appointment", EVENTS)
    blob = repr(r.telemetry)
    assert "Dentist" not in blob and "Example Street" not in blob


def test_parse_events_rows():
    rows = [{"summary": "Dentist", "start": {"dateTime": "2026-10-14T14:00:00-04:00"}, "end": {"dateTime": "2026-10-14T15:00:00-04:00"}, "location": "Somewhere"},
            {"summary": "Holiday", "start": {"date": "2026-10-15"}, "end": {"date": "2026-10-16"}}, {"bad": 1}, "junk"]
    out = c.parse_events(rows, TZ)
    assert [e.summary for e in out] == ["Dentist", "Holiday"] and out[1].all_day and out[0].location == "Somewhere"
    assert c.parse_events(None, TZ) == []
