"""display_ops: parsing, and every operation against a fake Home Assistant + viewer. The fake is a small state machine: the show script flips the flag
and the page path, the viewer 'fetches' the document a moment later. The rules under test: nothing is claimed that was not observed, nothing is sent to
an offline or unknown display, and a display command never computes a route."""
import asyncio, os

import pytest

from needle_voice import display_ops as d
from needle_voice import route_store as rs
from needle_voice.result import Status

SENSOR = "sensor.test_kiosk_browser_path"
CFG = d.DisplayConfig(targets={"kitchen": SENSOR}, verify_timeout_s=3.0)
RESP = {"routes": [
    {"duration": "1200s", "staticDuration": "900s", "distanceMeters": 5000, "description": "A", "polyline": {"encodedPolyline": "x"}, "legs": []},
    {"duration": "1500s", "distanceMeters": 6000, "description": "B", "polyline": {"encodedPolyline": "y"}, "legs": []}]}


class Fake:
    """HA + viewer. mode: 'ok' | 'reject' (call refused) | 'ignore' (accepted, nothing happens) | 'no_viewer' (page changes, nobody fetches)."""
    def __init__(self, mode="ok", path="/dashboard-weather/0", online=True):
        self.mode, self.state, self.calls, self.docs, self.t = mode, {SENSOR: path if online else "unavailable", CFG.active_flag: "off", CFG.pause_flag: "off"}, [], [], 0.0
        self.key, self.key_t, self.pending = None, -99.0, None
        self.io = d.DisplayIO(self.act, self.get_state, self.write_document, self.served, self.sleep)

    async def act(self, action, entity):
        self.calls.append((action, entity))
        if self.mode == "reject":
            return False
        if self.mode == "ignore":
            return True
        if action == "show":
            self.state[CFG.active_flag], self.state[CFG.pause_flag] = "on", "on"
            self.state[SENSOR] = CFG.route_path + "/"
            if self.mode != "no_viewer":
                self.pending = (self.t + 1.0, self.docs[-1])
        elif action == "close":
            self.state[CFG.active_flag] = "off"; self.state[SENSOR] = "/dashboard-news/0"
        elif action in ("pause", "resume"):
            self.state[entity] = "on" if action == "pause" else "off"
        return True

    async def get_state(self, e):
        return self.state.get(e)

    async def write_document(self, doc):
        self.docs.append(doc); return True

    def served(self):
        return self.key, (self.t - self.key_t) if self.key else float("inf")

    async def sleep(self, s):
        self.t += s
        if self.pending and self.t >= self.pending[0]:
            doc = self.pending[1]; self.key, self.key_t, self.pending = "%s|%s" % (doc["id"], doc["updated"]), self.t, None


def store(two=True):
    s = rs.RouteStore(); routes = rs.parse_routes(RESP)[0]
    s.set("Secret Destination", routes if two else routes[:1], (40.1, -73.1), (40.2, -73.2)); return s


def run(c):
    return asyncio.run(c)


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize("text,offered,want", [
    ("show me the route on the kitchen screen", False, "show"), ("put that on the kitchen display", False, "show"),
    ("show me the directions", False, "show"), ("display the map on the kitchen screen", False, "show"),
    ("yes, show me", True, "show"), ("yeah", True, "show"), ("sure", True, "show"), ("yes", False, ""),
    ("show me the alternate route", False, "alternate"), ("is there another way", False, "alternate"), ("show me the other route", False, "alternate"),
    ("close the map", False, "close"), ("dismiss the route", False, "close"), ("go back to the normal dashboard", False, "back"),
    ("go back to the dashboards", False, "back"), ("what's on the kitchen screen", False, "state"),
    ("pause the dashboard rotation", False, "pause"), ("resume the dashboards", False, "resume"),
    ("show me the weather map", False, ""), ("show me the pool camera", False, ""), ("turn on the kitchen light", False, ""),
    ("how long to get to the station", False, ""), ("", False, ""),
])
def test_parse(text, offered, want):
    assert d.parse(text, offered) == want, text


# ---------------------------------------------------------------- show
def test_show_success_is_observed_not_assumed():
    f, s = Fake(), store()
    r = run(d.show_route(s, f.io, CFG))
    assert r.status is Status.SUCCESS and r.side_effect and r.speech == "It's on the kitchen screen." and r.telemetry["code"] == "shown"
    assert f.calls == [("show", CFG.show_script)] and f.docs[-1]["id"] == s.get().id
    assert "Secret Destination" not in str(r.telemetry) and "40." not in str(r.telemetry)


def test_show_nothing_to_show_and_stale_id_send_nothing():
    f = Fake()
    r = run(d.show_route(rs.RouteStore(), f.io, CFG)); assert r.status is Status.NO_MATCH and not f.calls and not f.docs
    s = store(); r = run(d.show_route(s, f.io, CFG, route_id="old")); assert r.status is Status.NO_MATCH and r.reason == "route_replaced" and not f.calls


def test_unknown_target_offline_and_rejected_call():
    f, s = Fake(), store()
    r = run(d.show_route(s, f.io, CFG, target="garage")); assert r.status is Status.UNSUPPORTED and not f.calls and not f.docs
    f = Fake(online=False); r = run(d.show_route(s, f.io, CFG)); assert r.status is Status.UNAVAILABLE and not f.calls and not f.docs      # nothing sent to a dead display
    f = Fake("reject"); r = run(d.show_route(s, f.io, CFG)); assert r.status is Status.EXECUTION_FAILED and not r.side_effect


def test_accepted_but_not_observed_is_unverified_and_never_retried():
    for mode in ("ignore", "no_viewer"):
        f = Fake(mode); r = run(d.show_route(store(), f.io, CFG))
        assert r.status is Status.UNVERIFIED and r.side_effect and "couldn't confirm" in r.speech, mode
        assert len(f.calls) == 1, mode


def test_already_showing_sends_no_call_and_new_route_replaces():
    f, s = Fake(), store()
    run(d.show_route(s, f.io, CFG)); f.calls.clear()
    r = run(d.show_route(s, f.io, CFG)); assert r.reason == "already_showing" and r.status is Status.SUCCESS and not f.calls
    s.set("Another", rs.parse_routes(RESP)[0]); r = run(d.show_route(s, f.io, CFG))
    assert r.reason == "shown" and f.docs[-1]["id"] == s.get().id and f.calls == [("show", CFG.show_script)]


def test_alternate_switches_stored_route_without_recompute():
    f, s = Fake(), store(); rid = s.get().id
    run(d.show_route(s, f.io, CFG))
    r = run(d.show_alternate(s, f.io, CFG))
    assert r.status is Status.SUCCESS and "25 minutes" in r.speech and s.get().selected == 1 and s.get().id == rid and f.docs[-1]["selected"] == 1
    r = run(d.show_alternate(store(two=False), Fake().io, CFG)); assert r.status is Status.NO_MATCH and "no alternate" in r.speech
    f2, s2 = Fake("reject"), store(); r = run(d.show_alternate(s2, f2.io, CFG))
    assert r.status is Status.EXECUTION_FAILED and s2.get().selected == 0                                   # failed: store is not left on an unseen route


# ---------------------------------------------------------------- close, state, pause
def test_close_variants():
    f, s = Fake(), store(); run(d.show_route(s, f.io, CFG)); f.calls.clear()
    r = run(d.close_route(f.io, CFG)); assert r.status is Status.SUCCESS and r.reason == "closed" and f.calls == [("close", CFG.close_script)] and s.get() is not None
    f.calls.clear(); r = run(d.close_route(f.io, CFG)); assert r.reason == "nothing_to_close" and not f.calls
    f = Fake(); run(d.show_route(store(), f.io, CFG)); f.mode = "reject"; r = run(d.close_route(f.io, CFG)); assert r.status is Status.EXECUTION_FAILED
    f = Fake(); run(d.show_route(store(), f.io, CFG)); f.mode = "ignore"; r = run(d.close_route(f.io, CFG)); assert r.status is Status.UNVERIFIED and r.side_effect
    r = run(d.close_route(Fake(online=False).io, CFG)); assert r.status is Status.UNAVAILABLE


def test_get_state():
    f, s = Fake(), store()
    r = run(d.get_state(s, f.io, CFG)); assert r.status is Status.SUCCESS and r.telemetry["showing"] is False
    run(d.show_route(s, f.io, CFG)); r = run(d.get_state(s, f.io, CFG)); assert r.telemetry["showing"] is True and r.telemetry["route_id"] == s.get().id
    assert run(d.get_state(s, Fake(online=False).io, CFG)).status is Status.UNAVAILABLE


def test_pause_resume_and_route_active_refusal():
    f = Fake()
    r = run(d.pause_rotation(f.io, CFG)); assert r.status is Status.SUCCESS and f.state[CFG.pause_flag] == "on"
    r = run(d.resume_rotation(f.io, CFG)); assert r.status is Status.SUCCESS and f.state[CFG.pause_flag] == "off"
    run(d.show_route(store(), f.io, CFG)); f.calls.clear()
    r = run(d.resume_rotation(f.io, CFG)); assert r.status is Status.UNSUPPORTED and r.speech and not f.calls      # refused with an explanation, nothing sent
    f2 = Fake("ignore"); r = run(d.pause_rotation(f2.io, CFG)); assert r.status is Status.UNVERIFIED


# ---------------------------------------------------------------- handle()
def test_handle_routing_and_referents():
    f, s = Fake(), store()
    assert run(d.handle("show me the route on the kitchen screen", s, f.io, CFG)).status is Status.SUCCESS
    assert run(d.handle("close the map", s, f.io, CFG)).reason == "closed"
    e = rs.RouteStore()
    r = run(d.handle("put that on the kitchen display", e, f.io, CFG)); assert r.status is Status.UNSUPPORTED and r.speech is None       # 'that' may mean anything: leave it to others
    r = run(d.handle("show me the route", e, f.io, CFG)); assert r.status is Status.NO_MATCH and "don't have a route" in r.speech
    r = run(d.handle("how long to the station", s, f.io, CFG)); assert r.status is Status.UNSUPPORTED and r.speech is None
    r = run(d.handle("yes show me", s, Fake().io, CFG, offered=True)); assert r.status is Status.SUCCESS
    r = run(d.handle("yes", s, Fake().io, CFG, offered=False)); assert r.speech is None


def test_module_never_computes_routes():
    src = open(os.path.join(os.path.dirname(d.__file__), "display_ops.py")).read()
    assert "navigation_ops" not in src.replace('"navigation_ops"', "") and "routes.googleapis" not in src and "post_json" not in src


def test_owns_matches_handle_rule():
    e, s = rs.RouteStore(), store()
    assert d.owns("show me the route", e) and not d.owns("put that on the kitchen display", e) and d.owns("put that on the kitchen display", s)
    assert d.owns("yes show me", e, offered=True) and not d.owns("yes show me", e) and not d.owns("show me the weather map", s)
    assert d.owns("close the map", e)                                           # closing needs no referent: it answers 'nothing to close'
