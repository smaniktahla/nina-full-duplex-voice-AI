"""The glue between the voice server and Needle's display_ops: reply convention, config gating, referent rule, the viewer's Close button. The logic itself is
tested in needle-voice/tests/test_display_ops.py; here the HA and viewer I/O is a fake."""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "needle-voice"))
import tools                                                                   # noqa: E402
from needle_voice import display_ops as d, route_store as rs                    # noqa: E402

SENSOR = "sensor.test_kiosk_browser_path"
CFG = d.DisplayConfig(targets={"kitchen": SENSOR}, verify_timeout_s=2.0)


class Fake:
    def __init__(self, online=True, accept=True):
        self.state = {SENSOR: "/dashboard-news/0" if online else "unavailable", CFG.active_flag: "off", CFG.pause_flag: "off"}
        self.accept, self.key, self.t, self.pending, self.acts = accept, None, 0.0, None, []
        self.store = rs.RouteStore(); self.store.set("X", rs.parse_routes({"routes": [{"duration": "600s", "distanceMeters": 3000, "legs": []}]})[0])
        self.io = d.DisplayIO(self.act, self.get_state, self.write, lambda: (self.key, self.t - self.kt if self.key else float("inf")), self.sleep); self.kt = 0.0

    async def act(self, action, entity):
        self.acts.append((action, entity))
        if not self.accept:
            return False
        if action == "show":
            self.state[CFG.active_flag], self.state[SENSOR] = "on", CFG.route_path; self.pending = self.t + 1
        if action == "close":
            self.state[CFG.active_flag], self.state[SENSOR] = "off", "/dashboard-news/0"
        return True

    async def get_state(self, e): return self.state.get(e)
    async def write(self, doc): return True

    async def sleep(self, s):
        self.t += s
        if self.pending is not None and self.t >= self.pending:
            doc = self.store.to_document(); self.key, self.kt, self.pending = "%s|%s" % (doc["id"], doc["updated"]), self.t, None

    @property
    def env(self): return self.store, self.io, CFG


def run(c): return asyncio.run(c)


def test_reply_convention(monkeypatch):
    monkeypatch.setattr(tools, "KIOSK_TARGETS", {"kitchen": SENSOR})
    f = Fake(); assert run(tools.display_request(None, "show me the route on the kitchen screen", env=f.env)) == "[DONE] It's on the kitchen screen."
    f = Fake(online=False); r = run(tools.display_request(None, "show me the route on the kitchen screen", env=f.env)); assert r.startswith("[NOT DONE] ") and f.acts == []
    f = Fake(accept=False); assert run(tools.display_request(None, "show me the route", env=f.env)).startswith("[NOT DONE] ")


def test_unconfigured_display_says_so_and_owns_nothing(monkeypatch):
    monkeypatch.setattr(tools, "KIOSK_TARGETS", {})
    assert run(tools.display_request(None, "show me the route")).startswith("[NOT DONE] The kitchen display isn't set up")
    assert tools.display_owns("show me the route on the kitchen screen") is False


def test_owns_needs_a_referent(monkeypatch):
    monkeypatch.setattr(tools, "KIOSK_TARGETS", {"kitchen": SENSOR})
    empty = rs.RouteStore(); monkeypatch.setitem(tools._disp, "store", empty)
    assert tools.display_owns("put that on the kitchen display") is False                      # no route: 'that' is not ours
    assert tools.display_owns("show me the route") is True and tools.display_owns("what's the weather") is False
    monkeypatch.setitem(tools._disp, "store", Fake().store)
    assert tools.display_owns("put that on the kitchen display") is True


def test_viewer_close_button_is_true_only_when_seen_to_close():
    f = Fake(); f.state[CFG.active_flag], f.state[SENSOR] = "on", CFG.route_path
    assert run(tools.kiosk_close(None, env=f.env)) is True and f.acts == [("close", CFG.close_script)]
    n = Fake()                                                                                  # nothing on the screen: already closed, no call sent
    assert run(tools.kiosk_close(None, env=n.env)) is True and n.acts == []
    g = Fake(accept=False); g.state[CFG.active_flag], g.state[SENSOR] = "on", CFG.route_path
    assert run(tools.kiosk_close(None, env=g.env)) is False                                    # HA refused: the button must not report success


def test_server_routes_display_requests_deterministically():
    src = open(os.path.join(os.path.dirname(__file__), "..", "live", "server.py")).read()
    assert 'tools_mod.claim(user_said' in src and 'self.start_tool(claimed, user_said)' in src and 'k == "display"' in src and 'on_close=kiosk_close' in src
    assert 'replaced by @%s' in src                                       # a model-chosen tool tag must not take a request a deterministic tool claims


# ---------------------------------------------------------------- navigation glue
import json                                                                    # noqa: E402
from needle_voice import navigation_ops as nav                                 # noqa: E402

DETAIL = {"routes": [{"duration": "1200s", "staticDuration": "900s", "distanceMeters": 8000, "description": "A", "polyline": {"encodedPolyline": "x"}, "legs": []},
                     {"duration": "1500s", "distanceMeters": 9000, "description": "B", "polyline": {"encodedPolyline": "y"}, "legs": []}]}


def nav_cfg_fake(store, offer=True, notify=True):
    async def post(url, headers, body): return 200, DETAIL
    async def push(t, m, u, w): return True
    return nav.NavConfig(api_key="k", home=(40.0, -73.0), notify=push if notify else None, post_json=post, store=store, offer_display=offer)


def test_eta_sets_the_offer_and_a_yes_then_reaches_display_ops(monkeypatch):
    monkeypatch.setattr(tools, "KIOSK_TARGETS", {"kitchen": SENSOR}); tools._disp.pop("offer_t", None)
    f = Fake(); f.store = rs.RouteStore(); monkeypatch.setitem(tools._disp, "store", f.store)
    assert tools.offer_fresh() is False and tools.display_owns("yes show me", tools.offer_fresh()) is False                 # no offer, no hijacked 'yes'
    out = run(tools.nav_request(None, "how long to get to Example Museum", cfg=nav_cfg_fake(f.store)))
    assert out.startswith("[DONE] It's about 20 minutes") and out.endswith("Want me to show it on the kitchen screen?")
    assert tools.offer_fresh() is True and tools.display_owns("yes show me", tools.offer_fresh()) is True
    assert run(tools.display_request(None, "yes show me", offered=True, env=(f.store, f.io, CFG))) == "[DONE] It's on the kitchen screen."
    assert tools.offer_fresh() is False                                                                                       # answered: a later 'yes' means nothing


def test_failed_navigation_makes_no_offer(monkeypatch):
    tools._disp.pop("offer_t", None)
    async def post(url, headers, body): return 500, {}
    c = nav_cfg_fake(rs.RouteStore()); c.post_json = post
    assert run(tools.nav_request(None, "how long to get to Example Museum", cfg=c)).startswith("[NOT DONE]") and tools.offer_fresh() is False


def test_viewer_phone_button_reports_only_what_was_sent():
    s = rs.RouteStore(); s.set("X", rs.parse_routes(DETAIL)[0])
    assert run(tools.kiosk_phone(None, cfg=nav_cfg_fake(s))) is True
    assert run(tools.kiosk_phone(None, cfg=nav_cfg_fake(rs.RouteStore()))) is False                                          # no route: nothing sent
    assert run(tools.kiosk_phone(None, cfg=nav_cfg_fake(s, notify=False))) is False                                          # no phone configured


def test_nav_is_off_without_a_key(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "ROUTES_KEY_FILE", str(tmp_path / "nokey"))
    assert tools.nav_configured() is False and tools.nav_owns("how long to get to Example Museum") is False
    (tmp_path / "k").write_text("not-a-real-key-123"); monkeypatch.setattr(tools, "ROUTES_KEY_FILE", str(tmp_path / "k"))
    assert tools.nav_configured() is True and tools.nav_owns("how long to get to Example Museum") is True and tools.nav_owns("how long to boil an egg") is False


def test_server_replaces_model_tools_for_navigation_and_display():
    src = open(os.path.join(os.path.dirname(__file__), "..", "live", "server.py")).read()
    assert 'tools_mod.claim(utter_d)' in src and 'k == "nav"' in src and '"display", "nav", "random"' in src and 'on_phone=kiosk_phone' in src
    assert "import claims" in src and "claims.calendar_when" in src and "claims.weather_when" in src               # one routing table, shared with tests/test_routing_corpus.py


def test_spoken_results_keep_a_closing_question():
    """The first live navigation test dropped 'Want me to show it on the kitchen screen?' (the reply model compressed the result to one sentence), so the user was
    never told they could say yes. The result-speaking prompt must carry the rule."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "live", "server.py")).read()
    assert "ends with a question" in src and "ask it as the last thing you say" in src


def test_route_intercept_sees_each_utterance_once():
    """Live finding: typed 'yes show me' reached the intercept as 'yes show me yes show me' (user_buf + text), so the anchored yes-pattern failed and the
    request went to the general Home Assistant tool. The intercept text must use the same de-duplication as user_said."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "live", "server.py")).read()
    assert 'utter_d = (self.user_buf + " " + text).strip() if text and text not in self.user_buf else self.user_buf' in src


def test_stt_gets_the_household_names_as_hotwords(monkeypatch):
    monkeypatch.setattr(tools, "PLACES", {"Takoda Beer Garden": "", "Example Cafe": "x"})
    assert tools.stt_hotwords().startswith("Takoda Beer Garden, Example Cafe")
    monkeypatch.setattr(tools, "PLACES", {}); assert tools.stt_hotwords() == ""
    root = os.path.join(os.path.dirname(__file__), "..", "live")
    assert "hotwords=self.hotwords" in open(os.path.join(root, "stt_stream.py")).read() and "hotwords=tools_mod.stt_hotwords()" in open(os.path.join(root, "server.py")).read()


def test_nav_cfg_carries_the_places(monkeypatch):
    monkeypatch.setattr(tools, "PLACES", {"Takoda Beer Garden": ""})
    monkeypatch.setattr(tools, "ROUTES_KEY_FILE", __file__)
    monkeypatch.setenv("HA_TOKEN", "test-token"); monkeypatch.delitem(tools._KEY, "ha", raising=False)       # no ~/.hermes/.env on a CI runner
    class H:
        def get(self, *a, **k): raise OSError("no ha in tests")
    assert run(tools.nav_cfg(H())).places == {"Takoda Beer Garden": ""}


# ---------------------------------------------------------------- calendar answer + agenda popup glue
import datetime as dt                                                          # noqa: E402
import claims                                                                  # noqa: E402


class CalHttp:
    """HA's calendar REST endpoint: records the window asked for and returns two events on whatever day starts the window."""
    def __init__(self): self.params = []
    def get(self, url, headers=None, timeout=None, params=None):
        self.params.append(params); outer = self
        start = dt.datetime.fromisoformat(params["start"])
        rows = [{"summary": "Dentist", "start": {"dateTime": start.replace(hour=14).isoformat()}, "end": {"dateTime": start.replace(hour=15).isoformat()}, "location": "12 Main St\nTown, ST\nUSA"},
                {"summary": "Holiday", "start": {"date": start.date().isoformat()}, "end": {"date": (start.date() + dt.timedelta(days=1)).isoformat()}}]
        class R:
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
            async def json(s): return rows
        return R()


def run_calendar(monkeypatch, when, targets=True):
    popups = []
    async def fake_popup(http, events, days, title, now): popups.append((events, days, title))
    monkeypatch.setattr(tools, "KIOSK_TARGETS", {"kitchen": SENSOR} if targets else {}); monkeypatch.setattr(tools, "CALENDARS", ["calendar.test"])
    monkeypatch.setattr(tools, "_hdr", lambda: {}); monkeypatch.setattr(tools, "agenda_popup", fake_popup)
    h = CalHttp()
    async def go():
        out = await tools.calendar(h, when); await asyncio.sleep(0); return out
    return run(go()), popups, h


def test_calendar_answer_is_unchanged_and_the_agenda_is_scheduled(monkeypatch):
    out, popups, h = run_calendar(monkeypatch, "tomorrow")
    assert "2:00 PM: Dentist" in out and "all day: Holiday" in out and "|| LOCATIONS" in out                      # the spoken payload, as before
    (events, days, title), = popups
    assert sorted(e.summary for e in events) == ["Dentist", "Holiday"]
    assert days == [dt.date.today() + dt.timedelta(days=1)] and dt.datetime.fromisoformat(h.params[0]["start"]).date() == days[0]
    assert dt.datetime.fromisoformat(h.params[0]["end"]) - dt.datetime.fromisoformat(h.params[0]["start"]) == dt.timedelta(days=1)


def test_calendar_windows_for_a_weekday_a_week_and_a_missing_display(monkeypatch):
    out, popups, h = run_calendar(monkeypatch, "date:2026-10-23")
    assert popups[0][1] == [dt.date(2026, 10, 23)]
    out, popups, h = run_calendar(monkeypatch, "week")
    assert len(popups[0][1]) == 7 and dt.datetime.fromisoformat(h.params[0]["end"]) - dt.datetime.fromisoformat(h.params[0]["start"]) == dt.timedelta(days=7)
    assert any(day in out for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))      # a multi-day answer names the weekday
    out, popups, h = run_calendar(monkeypatch, "today", targets=False)
    assert popups == [] and "Dentist" in out                                                                          # no display configured: no popup, same answer


def test_calendar_when_keywords():
    today = dt.date.today(); wd = (today + dt.timedelta(days=3))
    assert claims.calendar_when("what's on my calendar tomorrow") == "tomorrow" and claims.calendar_when("check my calendar") == "today"
    assert claims.calendar_when("what's on my schedule this week") == "week" and claims.calendar_when("anything next week") == "next week" and claims.calendar_when("this weekend") in ("weekend", "today")
    assert claims.calendar_when("what's on my calendar " + wd.strftime("%A")) == "date:" + wd.isoformat()


def test_agenda_context_for_follow_ups(monkeypatch):
    import time
    monkeypatch.setitem(tools._disp, "agenda_t", -1e9); monkeypatch.setitem(tools._disp, "agenda", None)
    assert tools.agenda_fresh() is False and tools.agenda_base() is None
    monkeypatch.setitem(tools._disp, "agenda_t", time.monotonic()); monkeypatch.setitem(tools._disp, "agenda", {"days": [{"date": "2026-10-23"}]})
    assert tools.agenda_fresh() is True and tools.agenda_base() == dt.date(2026, 10, 23)
    monkeypatch.setitem(tools._disp, "agenda_t", time.monotonic() - 400); assert tools.agenda_fresh() is False and tools.agenda_base() is None
    monkeypatch.setitem(tools._disp, "agenda_t", time.monotonic()); monkeypatch.setitem(tools._disp, "agenda", {"days": []}); assert tools.agenda_base() is None


def test_followups_only_count_with_an_agenda_showing_and_only_day_words():
    f = lambda t, fresh=True: claims.claim(t, agenda_fresh=fresh)
    assert f("and tomorrow") == "calendar" and f("what about friday") == "calendar" and f("the next day") == "calendar" and f("how's tomorrow look") == "calendar"
    assert f("and tomorrow", False) == "" and f("how about pizza tomorrow") == "" and f("remind me to call mom tomorrow") == ""
    assert f("what's the weather tomorrow") == "weather"                                    # a weather question stays a weather question
    base = dt.date.today() + dt.timedelta(days=5)                                           # relative to today: a fixed date turns into "today" once the calendar reaches it
    assert claims.calendar_when("the next day", base) == "date:%s" % (base + dt.timedelta(days=1)).isoformat()


def test_server_always_reads_the_day_from_the_words_for_calendar_and_weather():
    src = open(os.path.join(os.path.dirname(__file__), "..", "live", "server.py")).read()
    assert 'elif owned in ("calendar", "weather"):' in src and "day corrected" in src and "tools_mod.agenda_base()" in src


def test_leave_by_picks_the_calendar_event_and_says_when_there_is_none(monkeypatch):
    import datetime as dt
    from needle_voice.calendar_ops import Event
    tools._disp.pop("offer_t", None)
    now = dt.datetime.now().astimezone()
    mk = lambda t, h, loc="12 Example Road": Event(t, now + dt.timedelta(hours=h), now + dt.timedelta(hours=h + 1), False, loc)
    s = rs.RouteStore(); c = nav_cfg_fake(s)
    out = run(tools.nav_request(None, "when should I leave for my dentist appointment", cfg=c, events=[mk("Standup", 1, ""), mk("Dentist", 5)]))
    assert out.startswith("[DONE] ") and ("Leave by" in out or "You should leave now" in out) and "for Dentist" in out and s.get().destination_label == "Dentist"
    assert tools.offer_fresh() is True                                                                                          # 'yes show me' now means this trip
    assert run(tools.nav_request(None, "when should I leave", cfg=nav_cfg_fake(rs.RouteStore()), events=[])) == "[NOT DONE] I don't see anything on your calendar in the next day and a half."
    out = run(tools.nav_request(None, "when should I leave", cfg=nav_cfg_fake(rs.RouteStore()), events=[mk("Standup", 1, "")]))
    assert out.startswith("[NOT DONE] Your next event, Standup") and "doesn't have a location" in out
    out = run(tools.nav_request(None, "when should I leave to get to Takoda Beer Garden", cfg=nav_cfg_fake(rs.RouteStore()), events=[mk("Dentist", 5)]))
    assert out == "[NOT DONE] What time do you need to be there?"                                                             # a named place, no matching event: ask for the time
