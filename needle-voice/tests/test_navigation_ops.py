"""navigation_ops: parsing, Routes request/response handling and the ToolResult contract. No network: Routes and notify are fakes."""
import asyncio
import datetime as dt

from needle_voice import route_store as rs

from needle_voice import navigation_ops as nav
from needle_voice.result import Status

HOME = (40.0, -73.0)
NOW = dt.datetime(2026, 10, 16, 9, 0, tzinfo=dt.timezone.utc)


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def cfg(resp=(200, None), notify=None, key="k", log=None):
    async def post(url, headers, body):
        if log is not None:
            log.append((url, headers, body))
        if isinstance(resp, Exception):
            raise resp
        return resp[0], (resp[1] if resp[1] is not None else {"routes": [{"duration": "1380s", "staticDuration": "900s", "distanceMeters": 16093}]})
    return nav.NavConfig(api_key=key, home=HOME, notify=notify, post_json=post)


def test_parse():
    assert nav.parse("how long to get to the Stony Brook train station") == ("eta", "Stony Brook train station")
    assert nav.parse("what's the eta to 12 Main Street?") == ("eta", "12 Main Street")
    assert nav.parse("when should I leave for my next appointment") == ("leave", "")
    assert nav.parse("when should I leave") == ("leave", "")
    assert nav.parse("send it to my phone") == ("send", "")
    assert nav.parse("send directions to my phone") == ("send", "")
    assert nav.parse("how long will that take") == ("eta", "")
    assert nav.parse("turn on the lights") == ("", "")


def test_duration_and_speech_helpers():
    assert nav.parse_duration_s("1380s") == 1380 and nav.parse_duration_s("x") is None and nav.parse_duration_s(None) is None
    assert nav.spoken_minutes(1380) == "23 minutes" and nav.spoken_minutes(30) == "1 minute" and nav.spoken_minutes(4500) == "1 hour 15 minutes"
    assert nav.nav_uri("12 Main St") == "https://www.google.com/maps/dir/?api=1&travelmode=driving&destination=12%20Main%20St"
    assert nav.nav_app_uri("12 Main St & Co") == "intent:q=12%20Main%20St%20%26%20Co#Intent;scheme=google.navigation;package=com.google.android.apps.maps;end"


def test_eta_ok_and_request_shape():
    log = []
    r = run(nav.handle("how long to get to 12 Main Street", None, cfg(log=log), NOW))
    assert r.status == Status.SUCCESS and "23 minutes" in r.speech and "8 minutes more than usual" in r.speech and "10 miles" in r.speech
    url, headers, body = log[0]
    assert url == nav.ROUTES_URL and headers["X-Goog-Api-Key"] == "k" and "routes.duration" in headers["X-Goog-FieldMask"]
    assert body["destination"] == {"address": "12 Main Street"} and body["routingPreference"] == "TRAFFIC_AWARE" and "departureTime" not in body
    assert r.telemetry == {"code": "eta", "eta_s": 1380, "static_s": 900, "meters": 16093}
    assert "Main" not in str(r.telemetry)                                       # no destination text in telemetry


def test_destination_from_context():
    ctx = nav.NavContext("Dr Smith, 5 Oak Ave", NOW + dt.timedelta(hours=5))
    r = run(nav.handle("how long will that take", ctx, cfg(), NOW))
    assert r.status == Status.SUCCESS
    r = run(nav.handle("how long will that take", None, cfg(), NOW))
    assert r.status == Status.AMBIGUOUS and r.speech == "Where to?"


def test_leave_by_future_and_now():
    ctx = nav.NavContext("5 Oak Ave", dt.datetime(2026, 10, 16, 15, 0, tzinfo=dt.timezone.utc))
    log = []
    r = run(nav.handle("when should I leave", ctx, cfg(log=log), NOW))
    assert r.status == Status.SUCCESS and "Leave by 2:27 PM" in r.speech and "typically" in r.speech      # 15:00 - 23 min - 10 min
    assert log[0][2]["departureTime"] == "2026-10-16T14:30:00Z" and r.telemetry["predicted"] is True
    soon = nav.NavContext("5 Oak Ave", NOW + dt.timedelta(minutes=20))
    r = run(nav.handle("when should I leave", soon, cfg(), NOW))
    assert r.status == Status.SUCCESS and r.speech.startswith("You should leave now")
    r = run(nav.handle("when should I leave", nav.NavContext("5 Oak Ave", None), cfg(), NOW))
    assert r.status == Status.AMBIGUOUS


def test_send_to_phone_is_never_claimed_verified():
    sent = []

    async def notify(title, msg, uri, web):
        sent.append((title, msg, uri, web))
        return True
    ctx = nav.NavContext("5 Oak Ave", None)
    r = run(nav.handle("send it to my phone", ctx, cfg(notify=notify), NOW))
    assert r.status == Status.UNVERIFIED and r.side_effect and sent == [("Navigate", "Tap to start directions in Google Maps", "intent:q=5%20Oak%20Ave#Intent;scheme=google.navigation;package=com.google.android.apps.maps;end",
                                                              "https://www.google.com/maps/dir/?api=1&travelmode=driving&destination=5%20Oak%20Ave")]

    async def refuse(*a):
        return False

    async def boom(*a):
        raise TimeoutError()
    assert run(nav.handle("send it to my phone", ctx, cfg(notify=refuse), NOW)).status == Status.EXECUTION_FAILED
    r = run(nav.handle("send it to my phone", ctx, cfg(notify=boom), NOW))
    assert r.status == Status.UNVERIFIED and not r.side_effect
    assert run(nav.handle("send it to my phone", ctx, cfg(), NOW)).status == Status.UNAVAILABLE
    assert run(nav.handle("send it to my phone", None, cfg(notify=notify), NOW)).status == Status.AMBIGUOUS


def test_error_mapping():
    for resp, status, code in (((403, {}), Status.EXECUTION_FAILED, "routes_auth_failed"), ((429, {}), Status.EXECUTION_FAILED, "routes_rate_limited"),
                               ((500, {}), Status.EXECUTION_FAILED, "routes_error"), ((200, {"routes": []}), Status.NO_MATCH, "no_route"),
                               ((400, {}), Status.NO_MATCH, "no_route"), (OSError("down"), Status.EXECUTION_FAILED, "routes_unreachable"),
                               ((200, {"routes": [{"duration": "bad"}]}), Status.EXECUTION_FAILED, "routes_error")):
        r = run(nav.handle("how long to 12 Main Street", None, cfg(resp=resp), NOW))
        assert r.status == status and r.telemetry["code"] == code, (resp, r)


def test_not_configured_and_not_navigation():
    assert run(nav.handle("how long to 12 Main Street", None, cfg(key=""), NOW)).status == Status.UNAVAILABLE
    r = run(nav.handle("turn off the lamp", None, cfg(), NOW))
    assert r.status == Status.UNSUPPORTED and r.speech is None and r.fallback_candidate


# ---------------------------------------------------------------- the active route (route_store) and the offer
DETAIL = {"routes": [
    {"duration": "1380s", "staticDuration": "900s", "distanceMeters": 16093, "description": "Main St", "polyline": {"encodedPolyline": "abc"},
     "legs": [{"startLocation": {"latLng": {"latitude": 40.1, "longitude": -73.1}}, "endLocation": {"latLng": {"latitude": 40.2, "longitude": -73.2}},
               "steps": [{"distanceMeters": 100, "navigationInstruction": {"instructions": "Head north"}}]}]},
    {"duration": "1500s", "distanceMeters": 17000, "description": "Oak Ave", "polyline": {"encodedPolyline": "def"}, "legs": []}]}


def test_eta_keeps_the_route_and_offers_to_show_it():
    log, store = [], rs.RouteStore()
    c = cfg(resp=(200, DETAIL), log=log); c.store, c.offer_display = store, True
    r = run(nav.handle("how long to get to Example Museum", None, c, NOW))
    assert r.status == Status.SUCCESS and "23 minutes" in r.speech and r.speech.endswith("Want me to show it on the kitchen screen?")
    url, headers, body = log[0]
    assert headers["X-Goog-FieldMask"] == rs.DETAIL_FIELD_MASK and body["computeAlternativeRoutes"] is True      # polylines, steps, alternates
    a = store.get()
    assert a.destination_label == "Example Museum" and len(a.routes) == 2 and a.routes[0].polyline == "abc" and a.origin == (40.1, -73.1)
    assert r.telemetry["offer"] is True and r.telemetry["routes"] == 2 and r.telemetry["route_id"] == a.id and "Museum" not in str(r.telemetry)


def test_no_offer_without_a_display_and_no_store_means_the_small_request():
    store = rs.RouteStore(); c = cfg(resp=(200, DETAIL)); c.store = store
    assert "Want me" not in run(nav.handle("how long to get to Example Museum", None, c, NOW)).speech and store.get() is not None
    log = []; r = run(nav.handle("how long to get to Example Museum", None, cfg(log=log), NOW))
    assert log[0][2].get("computeAlternativeRoutes") is None and log[0][1]["X-Goog-FieldMask"] == nav.FIELD_MASK and "offer" not in r.telemetry


def test_failed_lookup_keeps_the_previous_route():
    store = rs.RouteStore(); c = cfg(resp=(200, DETAIL)); c.store = store; c.offer_display = True
    run(nav.handle("how long to get to Example Museum", None, c, NOW)); rid = store.get().id
    bad = cfg(resp=(500, {})); bad.store = store
    assert run(nav.handle("how long to get to Nowhere", None, bad, NOW)).status == Status.EXECUTION_FAILED and store.get().id == rid


def test_send_uses_the_active_route_and_never_recalculates():
    sent, log, store = [], [], rs.RouteStore()

    async def notify(t, m, u, w):
        sent.append(w); return True
    c = cfg(resp=(200, DETAIL), notify=notify, log=log); c.store = store
    run(nav.handle("how long to get to Example Museum", None, c, NOW)); n = len(log)
    r = run(nav.handle("send it to my phone", None, c, NOW))
    assert r.status == Status.UNVERIFIED and sent[-1].endswith("destination=Example%20Museum") and len(log) == n                    # no new Routes call
    r = run(nav.send_active(c)); assert r.status == Status.UNVERIFIED and len(sent) == 2 and len(log) == n
    e = cfg(notify=notify); e.store = rs.RouteStore(); assert run(nav.send_active(e)).status == Status.NO_MATCH


def test_owns_needs_a_travel_word_and_a_referent():
    full, empty = rs.RouteStore(), rs.RouteStore(); full.set("X", rs.parse_routes(DETAIL)[0])
    assert nav.owns("how long to get to the Stony Brook train station", empty) and nav.owns("how long is the drive to Takoda Beer Garden", empty)
    assert not nav.owns("how long does it take to boil an egg", empty) and not nav.owns("how long to boil an egg", full)
    assert nav.owns("send it to my phone", full) and not nav.owns("send it to my phone", empty)
    assert not nav.owns("turn on the lights", full) and not nav.owns("when should I leave", full)
