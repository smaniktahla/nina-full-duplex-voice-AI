import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import tools


class Resp:
    def __init__(self, status, data):
        self.status, self._d = status, data

    async def json(self):
        return self._d

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeHttp:
    def __init__(self, by_cal):
        self.by_cal = by_cal

    def get(self, url, **kw):
        return Resp(*self.by_cal[url.rsplit("/", 1)[1]])


def test_one_failing_calendar_does_not_break_the_others(monkeypatch):
    monkeypatch.setattr(tools, "CALENDARS", ["calendar.good", "calendar.broken"])
    monkeypatch.setattr(tools, "KIOSK_TARGETS", None)
    monkeypatch.setattr(tools, "_hdr", lambda: {})
    http = FakeHttp({"calendar.good": (200, [{"summary": "Dentist", "start": {"dateTime": "2026-10-30T09:00:00-04:00"}}]),
                     "calendar.broken": (500, {"message": "Error reading events"})})
    out = asyncio.run(tools.calendar(http, "today"))
    assert "Dentist" in out
