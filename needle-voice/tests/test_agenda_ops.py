"""agenda_ops: which day(s) a question is about, and the document the kiosk agenda viewer draws."""
import datetime as dt
import json

import pytest

from needle_voice import agenda_ops as a
from needle_voice.calendar_ops import Event

TZ = dt.timezone(dt.timedelta(hours=-4))
NOW = dt.datetime(2026, 10, 18, 21, 0, tzinfo=TZ)                    # a Sunday evening
D = lambda day: dt.date(2026, 10, day)


@pytest.mark.parametrize("text,days,title", [
    ("what's on my calendar today", [D(18)], "Today"), ("what's on my schedule tomorrow", [D(19)], "Tomorrow"), ("am I busy tonight", [D(18)], "Today"),
    ("what do I have the day after tomorrow", [D(20)], "The day after tomorrow"), ("what's on my calendar friday", [D(23)], "Friday"),
    ("do I have anything on Wednesday", [D(21)], "Wednesday"), ("what's on my schedule next monday", [D(19)], "Monday"), ("what's on this sunday", [D(18)], "Today"),
    ("what's on my schedule next sunday", [D(25)], "Sunday"), ("anything on the calendar this weekend", [D(18)], "This weekend"), ("check my calendar", [D(18)], "Today"),
])
def test_resolve_days(text, days, title):
    assert a.resolve_days(text, NOW) == (days, title)


def test_week_questions_give_seven_days():
    d, t = a.resolve_days("what's on my calendar this week", NOW); assert t == "This week" and d == [D(18) + dt.timedelta(days=i) for i in range(7)]
    d, t = a.resolve_days("anything next week", NOW); assert t == "Next week" and d[0] == D(19) and len(d) == 7
    sat = dt.datetime(2026, 10, 24, 9, 0, tzinfo=TZ); assert a.resolve_days("this weekend", sat)[0] == [D(24), D(25)]
    mon = dt.datetime(2026, 10, 19, 9, 0, tzinfo=TZ); assert a.resolve_days("this weekend", mon)[0] == [D(24), D(25)]


def ev(title, day, h, m=0, mins=60, loc="", all_day=False, days=1):
    s = dt.datetime(2026, 10, day, h, m, tzinfo=TZ)
    return Event(title, s, s + (dt.timedelta(days=days) if all_day else dt.timedelta(minutes=mins)), all_day, loc)


def test_document_orders_events_and_formats_times():
    events = [ev("Dentist", 19, 14, 30, loc="12 Main St\nTown, ST 11111\nUSA"), ev("Standup", 19, 9), ev("Holiday", 19, 0, all_day=True), ev("Other day", 20, 9)]
    doc = a.build_document(events, [D(19)], NOW, hold_s=45)
    d = doc["days"][0]
    assert doc["title"] == "Tomorrow" and doc["hold_s"] == 45 and d["label"] == "Monday, October 19" and d["relative"] == "Tomorrow" and d["today"] is False
    assert [e["title"] for e in d["events"]] == ["Holiday", "Standup", "Dentist"]                                      # all-day first, then by start; the 20th is not here
    s, den = d["events"][1], d["events"][2]
    assert (s["time"], s["end_time"]) == ("9:00 AM", "10:00 AM") and den["time"] == "2:30 PM" and den["location"] == "12 Main St, Town, ST 11111"
    assert d["events"][0]["all_day"] is True and d["events"][0]["time"] == ""
    json.dumps(doc)


def test_all_day_events_cover_their_whole_span_and_end_exclusive():
    three_days = ev("Trip", 19, 0, all_day=True, days=3)                                                            # 19th, 20th, 21st
    doc = a.build_document([three_days], [D(18), D(19), D(21), D(22)], NOW)
    assert [len(d["events"]) for d in doc["days"]] == [0, 1, 1, 0]


def test_today_flag_multi_day_and_caps():
    doc = a.build_document([ev(f"E{i}", 18, 8) for i in range(60)], [D(18)], NOW)
    assert doc["days"][0]["today"] is True and len(doc["days"][0]["events"]) == a.MAX_EVENTS and doc["title"] == "Today"
    week = a.build_document([], [D(18) + dt.timedelta(days=i) for i in range(10)], NOW, title="This week")
    assert len(week["days"]) == a.MAX_DAYS and week["title"] == "This week"


def test_untitled_and_long_titles_are_safe_and_summary_has_no_text():
    long = ev("x" * 500, 18, 8); none = ev("", 18, 9)
    doc = a.build_document([long, none], [D(18)], NOW)
    assert len(doc["days"][0]["events"][0]["title"]) == 120 and doc["days"][0]["events"][1]["title"] == "(no title)"
    s = json.dumps(a.summary(doc)); assert "x" * 10 not in s and a.summary(doc) == {"days": 1, "events": 2}
    assert a.doc_key(doc) == "%s|%s" % (doc["id"], doc["updated"])


def test_new_documents_have_new_ids():
    assert a.build_document([], [D(18)], NOW)["id"] != a.build_document([], [D(18)], NOW)["id"]
