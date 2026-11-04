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
        return uid if kw.get("exists", True) else None

    async def delete(cal, uid, start, end):
        log["deleted"].append((cal, uid))
        return kw.get("gone", True)
    cfg = SchedConfig(calendars={"family": "calendar.family", "salil": "calendar.salil"}, labels={"calendar.family": "the Family calendar", "calendar.salil": "Salil's calendar"},
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
    d = s.parse_create("put soccer practice on the family calendar on friday at 5 pm", NOW)
    assert d.title == "Soccer practice" and d.calendar_name == "family"
    assert d.start == dt.datetime(2026, 10, 16, 17, tzinfo=TZ) and d.end - d.start == dt.timedelta(hours=1)


def test_possessive_calendar():
    assert s.parse_create("add lunch to Salil's calendar tomorrow at noon", NOW).calendar_name == "salil"


def test_bare_hour_is_decided_by_the_title_then_the_clock_else_asked():
    def settled(text, now=NOW):
        d = s.parse_create(text, now)
        return (d.start.hour if s._settle_ampm(d, now) else None)
    assert settled("add dinner tomorrow at 6") == 18 and settled("add breakfast with Bob tomorrow at 8") == 8      # a meal decides
    assert settled("add lunch tomorrow at 11") == 11 and settled("add lunch tomorrow at 1") == 13
    assert settled("add standup tomorrow at 9") is None
    assert settled("add call with Bob at 8:30 today", NOW.replace(hour=11, minute=36)) == 20                      # only the evening is still ahead
    assert settled("add call with Bob at 8:30 today", NOW.replace(hour=6)) is None                                # both are ahead: ask
    assert settled("add call with Bob at 8:30 tomorrow", NOW.replace(hour=11)) is None                            # another day: ask
    assert s.parse_create("add call at 12 tomorrow", NOW).start.hour == 12                                        # noon needs no question


def test_asks_morning_or_evening_and_applies_the_answer():
    cfg, st, log = mk()
    r = say("add standup tomorrow at 9 to my calendar", cfg, st)
    assert r.speech == "Is that 9 in the morning or in the evening?"
    r = say("morning", cfg, st, t=2)
    assert "tomorrow at 9 AM" in r.speech
    st.pending = None
    assert say("add check in to my calendar tomorrow at 8:30", cfg, st, t=3).speech == "Is that 8:30 in the morning or in the evening?"
    assert say("huh", cfg, st, t=4).speech == "Sorry, morning or evening?"
    r = say("in the evening", cfg, st, t=5)
    assert "tomorrow at 8:30 PM" in r.speech


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
    assert r.status is Status.AMBIGUOUS and "Salil's calendar tomorrow at 3 PM for an hour" in r.speech and not log["created"]
    r = say("yes", cfg, st, t=5)
    assert r.status is Status.SUCCESS and r.side_effect and len(log["created"]) == 1 and log["created"][0].calendar == "calendar.salil"
    assert "Dentist appointment is on Salil's calendar for tomorrow at 3 PM" in r.speech
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
    assert r.speech == "Is that 4 in the morning or in the evening?"
    r = say("afternoon", cfg, st, t=4.5)
    assert "Friday at 4 PM" in r.speech
    st.pending = None
    say("add haircut tomorrow to my calendar", cfg, st, t=5)
    r = say("3", cfg, st, t=6)
    assert r.speech == "Is that 3 in the morning or in the evening?"
    r = say("pm", cfg, st, t=7)
    assert "tomorrow at 3 PM" in r.speech


def test_unknown_speaker_is_asked_which_calendar():
    cfg, st, log = mk()
    r = say("add lunch tomorrow at noon to my calendar", cfg, st, speaker=None)
    assert r.speech.startswith("Which calendar")
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
    assert log["created"][0].location == "the office"


def test_location_question_can_be_declined():
    cfg, st, log = mk()
    say("add dentist tomorrow at 3 pm to my calendar", cfg, st)
    say("add a location", cfg, st, t=2)
    r = say("no", cfg, st, t=3)
    assert "Is that right" in r.speech and not log["created"]


def test_owns_only_plausible_turns():
    cfg, st, _ = mk()
    assert s.owns("add lunch tomorrow at noon to my calendar", st, 0) and not s.owns("what's the weather", st, 0)
    assert not s.owns("undo that", st, 0) and not s.owns("yes", st, 0)
    say("add lunch tomorrow at noon to my calendar", cfg, st)
    assert s.owns("yes", st, 1) and s.owns("make it 1 pm", st, 1) and s.owns("add a location", st, 1)
    assert not s.owns("what's the weather", st, 1) and not s.owns("yes", st, 1 + s.PENDING_TTL_S + 1)


def test_new_request_replaces_half_finished_one():
    cfg, st, log = mk()
    say("add haircut to my calendar", cfg, st)                       # asks when
    r = say("add dinner tomorrow at 6 pm to my calendar", cfg, st, t=2)
    assert "Dinner" in r.speech and st.pending.stage == "confirm"


def test_future_dates_carry_their_own_dst_offset():
    import zoneinfo
    ny = zoneinfo.ZoneInfo("America/New_York")
    winter = dt.datetime(2026, 11, 2, 9, 0, tzinfo=ny)                       # EST (-5)
    d = s.parse_create("add dentist on march 14th 2028 at 10 am to my calendar", winter)
    assert d.start.utcoffset() == dt.timedelta(hours=-4) and d.start.hour == 10 and d.end.hour == 11      # EDT (-4), wall clock kept
    assert s.parse_create("add dentist tomorrow at 10 am", winter).start.utcoffset() == dt.timedelta(hours=-5)


def test_titles_may_contain_edit_words():
    assert s.parse_intent("add cancel netflix to my calendar tomorrow at 9 am") == "create"
    assert s.parse_intent("add nina test delete me to my calendar on march 14th at 10 am") == "create"
    assert s.parse_intent("cancel my dentist appointment") == "" and s.parse_intent("hey nina, please delete the lunch event") == ""


def test_preface_is_ignored_and_calendar_answers_are_forgiving():
    cfg, st, log = mk()
    r = say("Yeah, what's up? Hey, can you add an event for tomorrow on my calendar at 11 am?", cfg, st, speaker=None)
    assert r.speech == "What should I call it?"
    r = say("Haircut", cfg, st, speaker=None, t=2)
    assert r.speech == "Which calendar: the Family calendar, or Salil's calendar?"
    r = say("Salil's calendar", cfg, st, speaker=None, t=3)
    assert r.speech.startswith("I'll add Haircut to Salil's calendar tomorrow at 11 AM")
    st.pending = None
    say("add lunch tomorrow at noon to my calendar", cfg, st, speaker=None, t=10)
    assert say("family", cfg, st, speaker=None, t=11).speech.startswith("I'll add Lunch to the Family calendar")


def test_default_calendar_when_speaker_unknown_and_none_named():
    cfg, st, log = mk()
    cfg.default_calendar = "calendar.salil"
    r = say("add lunch tomorrow at noon to my calendar", cfg, st, speaker=None)
    assert "Salil's calendar" in r.speech                                   # still named in the read-back
    st.pending = None
    assert "the Family calendar" in say("put lunch on the family calendar tomorrow at noon", cfg, st, speaker=None, t=5).speech     # naming one overrides


def test_dotted_times_and_part_of_day_cues():
    now = dt.datetime(2026, 11, 3, 11, 36).astimezone()
    for text, hh, mm in (("add dentist appointment tonight at 8.30 pm", 20, 30), ("dentist appointment tonight at 8:30", 20, 30),
                         ("add dentist appointment at 8:30 tonight", 20, 30), ("add dentist appointment this evening at 7", 19, 0),
                         ("add dentist appointment tomorrow morning at 8", 8, 0), ("add dentist appointment tomorrow at 8.15 am", 8, 15)):
        d = s.parse_create(text, now)
        assert d and (d.start.hour, d.start.minute) == (hh, mm) and not s.validate(d, now), text
