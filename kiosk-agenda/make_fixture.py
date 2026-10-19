#!/usr/bin/env python3
"""Writes agenda.example.json: a synthetic day (no household data) for developing and testing the agenda viewer.   python3 make_fixture.py"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "needle-voice"))
import datetime as dt
from needle_voice import agenda_ops as a
from needle_voice.calendar_ops import Event

tz = dt.timezone(dt.timedelta(hours=-4))
now = dt.datetime(2026, 10, 18, 21, 0, tzinfo=tz)
day = lambda h, m=0, mins=60: (dt.datetime(2026, 10, 19, h, m, tzinfo=tz), dt.datetime(2026, 10, 19, h, m, tzinfo=tz) + dt.timedelta(minutes=mins))
ev = lambda t, h, m=0, mins=60, loc="": Event(t, *day(h, m, mins), False, loc)
events = [Event("School holiday", dt.datetime(2026, 10, 19, tzinfo=tz), dt.datetime(2026, 10, 20, tzinfo=tz), True),
          ev("Morning standup", 9, 0, 30), ev("Dentist", 10, 15, 45, "12 Example St\nTown, ST 11111\nUSA"), ev("Lunch with a friend", 12, 0, 90, "Example Cafe"),
          ev("Design review", 13, 0, 60), ev("Pickup", 15, 30, 30, "Example Elementary School"), ev("Dinner", 18, 30, 120)]
doc = a.build_document(events, [dt.date(2026, 10, 19)], now, hold_s=60)
json.dump(doc, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "agenda.example.json"), "w"), indent=1)
