"""The day's agenda for the kitchen screen: which day(s) a spoken calendar question is about, and the JSON document the kiosk agenda viewer (kiosk-agenda/) draws.

Pure and deterministic, stdlib only. Events come in as calendar_ops.Event (parse_events turns Home Assistant's rows into them); nothing here reads a calendar,
talks to Home Assistant or touches the disk. Showing the document on the kiosk is display_ops.show_agenda.

  resolve_days("what's on my schedule friday", now) -> ([date(2026-10-23)], "Friday")
  build_document(events, days, now, hold_s=60, title="Friday") -> {"id", "updated", "title", "hold_s", "days": [{"date", "label", "relative", "today", "events": [...]}]}

Document rows carry titles and a compact location (one line, no country). The document is served only through the signed kiosk link; telemetry never carries any of it.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

from .calendar_ops import Event

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MAX_DAYS = 7
MAX_EVENTS = 40


def _next_weekday(today: dt.date, wd: int, nxt: bool) -> dt.date:
    delta = (wd - today.weekday()) % 7
    if nxt and delta == 0:
        delta = 7
    return today + dt.timedelta(days=delta)


def resolve_days(text: str, now: dt.datetime, base: dt.date | None = None) -> tuple:
    """-> (list of dates, title). One date for a day question; several for 'this week' / 'the weekend' / 'next week'. Default: today.
    `base` is the day the kitchen screen is already showing: "the next day" / "the day after that" / "the previous day" are relative to it (and mean today +/- 1 without one)."""
    t = (text or "").lower()
    today = now.date()
    anchor = base or today
    if re.search(r"\bday after tomorrow\b", t):
        return [today + dt.timedelta(days=2)], "The day after tomorrow"
    if re.search(r"\b(?:next|following)\s+day\b|\bday after (?:that|it)\b|\bthe day after\b", t):
        d = anchor + dt.timedelta(days=1)
        return [d], _name(d, today)
    if re.search(r"\b(?:previous|prior)\s+day\b|\bday before\b", t):
        d = anchor - dt.timedelta(days=1)
        return [d], _name(d, today)
    if re.search(r"\byesterday\b", t):
        return [today - dt.timedelta(days=1)], "Yesterday"
    if re.search(r"\btomorrow\b", t):
        return [today + dt.timedelta(days=1)], "Tomorrow"
    m = re.search(r"\b(?:(next|this|on)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t)
    if m:
        d = _next_weekday(today, WEEKDAYS.index(m.group(2)), m.group(1) == "next")
        return [d], ("Today" if d == today else m.group(2).capitalize())
    if re.search(r"\bweekend\b", t):
        sat = _next_weekday(today, 5, False) if today.weekday() < 5 else (today if today.weekday() == 5 else today - dt.timedelta(days=1))
        return [d for d in (sat, sat + dt.timedelta(days=1)) if d >= today], "This weekend"
    if re.search(r"\bnext week\b", t):
        mon = today + dt.timedelta(days=7 - today.weekday())
        return [mon + dt.timedelta(days=i) for i in range(MAX_DAYS)], "Next week"
    if re.search(r"\b(week|next few days|coming days)\b", t):
        return [today + dt.timedelta(days=i) for i in range(MAX_DAYS)], "This week"
    return [today], "Today"


def clock(t: dt.datetime) -> str:
    return t.strftime("%-I:%M %p")                                   # "9:00 AM": a wall display shows minutes always


def _name(d: dt.date, today: dt.date) -> str:
    k = (d - today).days
    return "Today" if k == 0 else "Tomorrow" if k == 1 else "Yesterday" if k == -1 else d.strftime("%A")


def _fmt(d: dt.date) -> str:
    return d.strftime("%A, %B ") + str(d.day)


def _relative(d: dt.date, today: dt.date) -> str:
    k = (d - today).days
    return "Today" if k == 0 else "Tomorrow" if k == 1 else ""


def compact_location(loc: str) -> str:
    """'5501 Main St\nSpringfield, VA 22041\nUSA' -> '5501 Main St, Springfield, VA 22041' (one line, no country)."""
    parts = [p.strip() for p in str(loc or "").replace("\r", "").split("\n") if p.strip()]
    if parts and parts[-1].upper() in ("USA", "US", "UNITED STATES"):
        parts.pop()
    return ", ".join(parts)[:120]


def _covers(e: Event, d: dt.date, tz) -> bool:
    """All-day events cover [start date, end date) (the calendar end is exclusive); timed events belong to the day they start."""
    if e.all_day:
        return e.start.astimezone(tz).date() <= d < max(e.end.astimezone(tz).date(), e.start.astimezone(tz).date() + dt.timedelta(days=1))
    return e.start.astimezone(tz).date() == d


def build_document(events: list, days: list, now: dt.datetime, hold_s: int = 60, title: str = "") -> dict:
    tz = now.tzinfo or dt.timezone.utc
    today = now.date()
    out_days = []
    for d in days[:MAX_DAYS]:
        rows = []
        for e in sorted((e for e in events if _covers(e, d, tz)), key=lambda e: (not e.all_day, e.start)):
            rows.append({"title": (e.summary or "(no title)")[:120], "all_day": bool(e.all_day),
                         "start": e.start.astimezone(tz).isoformat(), "end": e.end.astimezone(tz).isoformat(),
                         "time": "" if e.all_day else clock(e.start.astimezone(tz)),
                         "end_time": "" if (e.all_day or e.end <= e.start) else clock(e.end.astimezone(tz)),
                         "location": compact_location(e.location)})
        out_days.append({"date": d.isoformat(), "label": _fmt(d), "relative": _relative(d, today), "today": d == today, "events": rows[:MAX_EVENTS]})
    ttl = title or (_relative(days[0], today) or days[0].strftime("%A") if len(days) == 1 else "Agenda")
    return {"id": uuid.uuid4().hex[:10], "updated": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "title": ttl, "hold_s": int(hold_s), "days": out_days}


def doc_key(doc: dict) -> str:
    return "%s|%s" % (doc.get("id"), doc.get("updated"))


def summary(doc: dict) -> dict:
    """Telemetry: counts only, never titles or locations."""
    return {"days": len(doc.get("days") or []), "events": sum(len(d.get("events") or []) for d in doc.get("days") or [])}
