import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import claims
import clock_tool


def test_current_time_questions_are_claimed_without_device_control():
    for text in ("What time is it?", "what time is it right now", "what's the time?",
                 "can you tell me the time", "tell me the current time", "Nina, what time is it?"):
        assert claims.claim(text, display=False, nav=False) == "clock"


def test_clock_does_not_claim_appointments_timers_or_other_timezones():
    for text in ("what time is dinner", "set a timer for 5 minutes", "what time is it in London",
                 "what's the weather", "turn on the lights"):
        assert not clock_tool.owns(text)


def test_configured_timezone_midnight_noon_and_daylight_saving():
    utc = dt.timezone.utc
    assert clock_tool.answer("America/New_York", dt.datetime(2026, 1, 1, 5, 3, tzinfo=utc)) == "It's 12:03 AM."
    assert clock_tool.answer("America/New_York", dt.datetime(2026, 7, 1, 16, 0, tzinfo=utc)) == "It's 12:00 PM."
    assert clock_tool.answer("America/Los_Angeles", dt.datetime(2026, 7, 1, 16, 0, tzinfo=utc)) == "It's 9:00 AM."
