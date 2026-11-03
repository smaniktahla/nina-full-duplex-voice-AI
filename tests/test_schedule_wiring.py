"""The glue between the voice server and Needle's schedule_ops: claim routing (gated on household config), verbatim replies, speaker -> calendar. The logic itself is
tested in needle-voice/tests/test_schedule_ops.py; here the HA writes are fakes."""
import asyncio, datetime as dt, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "needle-voice"))
import tools                                                                   # noqa: E402
from needle_voice import schedule_ops as s                                      # noqa: E402

SCHED = {"calendars": {"alex": "calendar.alex", "family": "calendar.family"},
         "labels": {"calendar.alex": "Alex's calendar", "calendar.family": "the Family calendar"},
         "speaker_calendars": {"alex": "calendar.alex"}}
ADD = "add dentist appointment to my calendar tomorrow at 3 pm"


def setup(monkeypatch, sched=SCHED):
    monkeypatch.setattr(tools, "SCHEDULE", sched)
    monkeypatch.setattr(tools, "_sched", {})
    log = {"created": []}

    async def create(d): log["created"].append(d); return ""
    async def exists(d, uid): return "u1"
    async def delete(cal, uid, a, b): return True
    cfg = s.SchedConfig(calendars={"alex": "calendar.alex", "family": "calendar.family"}, labels=sched.get("labels", {}),
                        speaker_calendars={"alex": "calendar.alex"}, create_event=create, event_exists=exists, delete_event=delete)
    return cfg, log


def say(text, cfg, who="alex"):
    return asyncio.run(tools.schedule_request(None, text, who, cfg))


def test_off_without_household_config(monkeypatch):
    setup(monkeypatch, {})
    assert not tools.schedule_configured() and tools.claim(ADD) == "calendar"          # today's behavior: the calendar tool refuses the write


def test_claims_add_and_pending_replies_only(monkeypatch):
    cfg, _ = setup(monkeypatch)
    assert tools.claim(ADD) == "schedule" and tools.claim("remind me to call mom at 5 pm") == "schedule"
    assert tools.claim("yes") != "schedule" and tools.claim("undo that") != "schedule"          # nothing pending, nothing to undo
    say(ADD, cfg)
    assert tools.claim("yes") == "schedule" and tools.claim("make it 4") == "schedule"
    assert tools.claim("what's the weather like tomorrow") == "weather"                       # an unrelated question is not swallowed
    assert tools.claim("what's on my calendar today") == "calendar"


def test_read_back_then_yes_writes_to_the_speakers_calendar(monkeypatch):
    cfg, log = setup(monkeypatch)
    r = say(ADD, cfg)
    assert r == "I'll add Dentist appointment to Alex's calendar tomorrow at 3 PM for an hour. Is that right?" and not log["created"]
    r = say("yes", cfg)
    assert r.startswith("Done. Dentist appointment is on Alex's calendar") and log["created"][0].calendar == "calendar.alex"
    assert tools.claim("undo that") == "schedule"


def test_unrecognised_speaker_is_asked_and_never_defaulted(monkeypatch):
    cfg, log = setup(monkeypatch)
    assert say(ADD, cfg, who=None) == "Which calendar: Alex's calendar, or the Family calendar?"
    assert say("the family calendar", cfg, who=None).startswith("I'll add Dentist appointment to the Family calendar")
    assert say("yes", cfg, who=None) and log["created"][0].calendar == "calendar.family"


def test_errors_are_spoken_not_raised(monkeypatch):
    cfg, _ = setup(monkeypatch)
    cfg.create_event = None
    say(ADD, cfg)
    assert "can't reach your calendar" in say("yes", cfg)


def test_default_calendar_from_household_config(monkeypatch):
    cfg, log = setup(monkeypatch, {**SCHED, "default_calendar": "alex"})
    real = tools.schedule_cfg(None)
    assert real.default_calendar == "calendar.alex"
    cfg.default_calendar = real.default_calendar
    assert "Alex's calendar" in say(ADD, cfg, who=None)
