import asyncio, datetime as dt
from needle_voice import schedule_ops as s
from needle_voice.schedule_ops import SchedConfig, SchedState
from needle_voice.result import Status

TZ = dt.timezone(dt.timedelta(hours=-4))
NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=TZ)           # a Wednesday, 9 AM


def mk(**kw):
    log = {"created": [], "deleted": []}

    async def create(d):
        log["created"].append(d)
        return kw.get("uid", "u1")

    async def exists(d, uid):
        return kw.get("exists", True)

    async def delete(cal, uid):
        log["deleted"].append((cal, uid))
        return kw.get("gone", True)
    cfg = SchedConfig(calendars={"family": "calendar.family", "salil": "calendar.salil"}, labels={"calendar.family": "Family", "calendar.salil": "Salil"},
                      speaker_calendars={"salil": "calendar.salil"}, create_event=create, event_exists=exists, delete_event=delete)
    return cfg, SchedState(), log


def say(text, cfg, st, speaker="salil", now=NOW, t=0.0):
    return asyncio.run(s.handle(text, st, cfg, speaker, now, t))


def test_intent():
    assert s.parse_intent("add a dentist appointment to my calendar tomorrow at 3") == "create"
    assert s.parse_intent("remind me to call mom at 5") == "remind"
    assert s.parse_intent("schedule a meeting with Bob on friday at 2") == "create"
    assert s.parse_intent("undo that") == "undo"
    assert s.parse_intent("what's on my calendar today") == ""
    assert s.parse_intent("move my dentist appointment to friday") == ""
    assert s.parse_intent("turn on the lights") == ""


def test_parse_full_event():
    d = s.parse_create("add a dentist appointment to my calendar tomorrow at 3 pm for 2 hours", NOW)
    assert d.title == "Dentist appointment" and d.start == dt.datetime(2026, 10, 15, 15, tzinfo=TZ) and d.end - d.start == dt.timedelta(hours=2)
    assert d.calendar_name == ""


def test_parse_named_calendar_and_default_duration():
    d = s.parse_create("put soccer practice on the family calendar on friday at 5", NOW)
    assert d.title == "Soccer practice" and d.calendar_name == "family"
    assert d.start == dt.datetime(2026, 10, 16, 17, tzinfo=TZ) and d.end - d.start == dt.timedelta(hours=1)


def test_possessive_calendar():
    assert s.parse_create("add lunch to Salil's calendar tomorrow at noon", NOW).calendar_name == "salil"


def test_bare_hour_meridiem_guess():
    assert s.parse_create("add standup tomorrow at 9", NOW).start.hour == 9
    assert s.parse_create("add dinner tomorrow at 6", NOW).start.hour == 18
    late = NOW.replace(hour=13)
    assert s.parse_create("add call with Bob at 3 today", late).start.hour == 15


def test_time_without_date_rolls_forward():
    assert s.parse_create("add a meeting at 8 am to my calendar", NOW).start == dt.datetime(2026, 10, 15, 8, tzinfo=TZ)


def test_relative_and_dates():
    assert s.parse_create("remind me to take the pizza out in 20 minutes", NOW).start == NOW + dt.timedelta(minutes=20)
    assert s.parse_create("remind me to call mom in two hours", NOW).start == NOW + dt.timedelta(hours=2)
    assert s.parse_create("add party on october 31st at 7 pm", NOW).start == dt.datetime(2026, 10, 31, 19, tzinfo=TZ)
    assert s.parse_create("add party on january 5th at 7 pm", NOW).start.year == 2027
    assert s.parse_create("add dentist next tuesday at 2 pm", NOW).start.date() == dt.date(2026, 10, 20)


def test_all_day():
    d = s.parse_create("add anniversary on october 20th all day to my calendar", NOW)
    assert d.all_day and d.end - d.start == dt.timedelta(days=1) and d.title == "Anniversary"


def test_reminder_title_and_length():
    d = s.parse_create("remind me to call mom at 5 pm", NOW)
    assert d.kind == "reminder" and d.title == "Call mom" and d.end - d.start == dt.timedelta(minutes=15)


def test_create_read_back_then_yes_writes_once():
    cfg, st, log = mk()
    r = say("add dentist appointment to my calendar tomorrow at 3 pm", cfg, st)
    assert r.status is Status.AMBIGUOUS and "your calendar tomorrow at 3 PM for an hour" in r.speech and not log["created"]
    r = say("yes", cfg, st, t=5)
    assert r.status is Status.SUCCESS and r.side_effect and len(log["created"]) == 1 and log["created"][0].calendar == "calendar.salil"
    assert "Dentist appointment is on the calendar for tomorrow at 3 PM" in r.speech
    assert say("yes", cfg, st, t=6).status is Status.UNSUPPORTED and len(log["created"]) == 1


def test_no_and_timeout_write_nothing():
    cfg, st, log = mk()
    say("add lunch tomorrow at noon to my calendar", cfg, st)
    assert say("no", cfg, st, t=2).status is Status.CANCELLED
    say("add lunch tomorrow at noon to my calendar", cfg, st, t=10)
    assert say("yes", cfg, st, t=10 + s.PENDING_TTL_S + 1).status is Status.UNSUPPORTED and not log["created"]


def test_yes_with_extra_words_is_a_patch_not_a_confirmation():
    cfg, st, log = mk()
    say("add lunch tomorrow at noon to my calendar", cfg, st)
    r = say("yes but make it 1 pm", cfg, st, t=3)
    assert not log["created"] and r.status is Status.AMBIGUOUS


def test_patch_time_and_title():
    cfg, st, log = mk()
    say("add dentist tomorrow at 3 pm to my calendar", cfg, st)
    r = say("make it 4", cfg, st, t=2)
    assert "4 PM" in r.speech and not log["created"]
    r = say("call it Orthodontist", cfg, st, t=3)
    assert "Orthodontist" in r.speech
    say("yes", cfg, st, t=4)
    assert log["created"][0].title == "Orthodontist" and log["created"][0].start.hour == 16


def test_patch_day_keeps_time():
    cfg, st, log = mk()
    say("add dentist tomorrow at 3 pm to my calendar", cfg, st)
    r = say("friday instead", cfg, st, t=2)
    assert "Friday at 3 PM" in r.speech


def test_asks_for_missing_title_and_time():
    cfg, st, log = mk()
    r = say("add an appointment to my calendar tomorrow at 2 pm", cfg, st)
    assert r.speech == "What should I call it?"
    r = say("haircut", cfg, st, t=2)
    assert "Haircut" in r.speech and "tomorrow at 2 PM" in r.speech
    st.pending = None
    r = say("add haircut to my calendar", cfg, st, t=3)
    assert r.speech == "When should it be?"
    r = say("friday at 4", cfg, st, t=4)
    assert "Friday at 4 PM" in r.speech
    st.pending = None
    say("add haircut tomorrow to my calendar", cfg, st, t=5)
    r = say("3", cfg, st, t=6)
    assert "tomorrow at 3 PM" in r.speech


def test_unknown_speaker_is_asked_which_calendar():
    cfg, st, log = mk()
    r = say("add lunch tomorrow at noon to my calendar", cfg, st, speaker=None)
    assert r.speech == "Which calendar should I use?"
    r = say("the family calendar", cfg, st, speaker=None, t=2)
    assert "the Family calendar" in r.speech
    say("yes", cfg, st, speaker=None, t=3)
    assert log["created"][0].calendar == "calendar.family"


def test_named_calendar_overrides_and_unknown_is_refused():
    cfg, st, log = mk()
    assert "the Family calendar" in say("put dinner on the family calendar tomorrow at 6 pm", cfg, st).speech
    st.pending = None
    r = say("put dinner on the work calendar tomorrow at 6 pm", cfg, st)
    assert r.status is Status.NO_MATCH and not log["created"]


def test_validation_past_and_far_future():
    cfg, st, log = mk()
    r = say("add lunch on october 1st at noon to my calendar", cfg, st)
    assert "already passed" in r.speech
    st.pending = None
    r = say("add lunch on october 1st 2030 at noon to my calendar", cfg, st)
    assert "two years" in r.speech and not log["created"]


def test_write_failures_are_honest():
    cfg, st, log = mk(uid=None)
    say("add lunch tomorrow at noon to my calendar", cfg, st)
    r = say("yes", cfg, st, t=1)
    assert r.status is Status.EXECUTION_FAILED and not r.side_effect and st.undo is None
    cfg, st, log = mk(exists=False)
    say("add lunch tomorrow at noon to my calendar", cfg, st)
    r = say("yes", cfg, st, t=1)
    assert r.status is Status.UNVERIFIED and r.side_effect and "check" in r.speech


def test_undo():
    cfg, st, log = mk()
    say("add lunch tomorrow at noon to my calendar", cfg, st)
    say("yes", cfg, st, t=1)
    r = say("undo that", cfg, st, t=30)
    assert r.status is Status.SUCCESS and log["deleted"] == [("calendar.salil", "u1")] and "Lunch" in r.speech
    assert say("undo that", cfg, st, t=31).status is Status.NO_MATCH
    say("add lunch tomorrow at noon to my calendar", cfg, st, t=40)
    say("yes", cfg, st, t=41)
    assert say("undo that", cfg, st, t=41 + s.UNDO_TTL_S + 1).status is Status.NO_MATCH


def test_rate_limit():
    cfg, st, log = mk()
    for i in range(s.RATE_MAX):
        say("add lunch tomorrow at noon to my calendar", cfg, st, t=i * 10)
        assert say("yes", cfg, st, t=i * 10 + 1).status is Status.SUCCESS
    say("add lunch tomorrow at noon to my calendar", cfg, st, t=200)
    assert say("yes", cfg, st, t=201).status is Status.DENIED and len(log["created"]) == s.RATE_MAX


def test_unrelated_turns_pass_through():
    cfg, st, _ = mk()
    r = say("what's the weather", cfg, st)
    assert r.status is Status.UNSUPPORTED and r.speech is None


def test_no_telemetry_leaks_titles():
    cfg, st, _ = mk()
    r = say("add secret surprise party tomorrow at 7 pm to my calendar", cfg, st)
    assert "surprise" not in repr(r.telemetry).lower()


def test_location_in_creation():
    d = s.parse_create("add dentist appointment at Smile Dental tomorrow at 3 pm to my calendar", NOW)
    assert d.title == "Dentist appointment" and d.location == "Smile Dental" and d.start.hour == 15
    d = s.parse_create("put dinner on the family calendar friday at 6 pm, the location is Luigi's on Main Street", NOW)
    assert d.title == "Dinner" and d.location == "Luigi's on Main Street" and d.calendar_name == "family"
    assert s.parse_create("add lunch tomorrow at noon to my calendar", NOW).location == ""
    assert s.parse_create("remind me to look at the stars at 9 pm", NOW).location == ""


def test_location_in_readback_and_saved():
    cfg, st, log = mk()
    r = say("add dentist at Smile Dental tomorrow at 3 pm to my calendar", cfg, st)
    assert "location Smile Dental" in r.speech
    say("yes", cfg, st, t=2)
    assert log["created"][0].location == "Smile Dental"


def test_add_edit_and_clear_location_while_confirming():
    cfg, st, log = mk()
    say("add dentist tomorrow at 3 pm to my calendar", cfg, st)
    assert say("add a location", cfg, st, t=2).speech == "Where is it?"
    r = say("Smile Dental on Oak Street", cfg, st, t=3)
    assert "location Smile Dental on Oak Street" in r.speech
    r = say("the location is the Takoda Beer Garden", cfg, st, t=4)
    assert "location the Takoda Beer Garden" in r.speech and "Oak" not in r.speech
    r = say("remove the location", cfg, st, t=5)
    assert "location" not in r.speech and not log["created"]
    say("put it at the office", cfg, st, t=6)
    say("yes", cfg, st, t=7)
    assert log["created"][0].location == "office"


def test_location_question_can_be_declined():
    cfg, st, log = mk()
    say("add dentist tomorrow at 3 pm to my calendar", cfg, st)
    say("add a location", cfg, st, t=2)
    r = say("no", cfg, st, t=3)
    assert "Is that right" in r.speech and not log["created"]
