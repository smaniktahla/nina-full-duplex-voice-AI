"""route_store: Routes response parsing, one-active-route semantics (no recompute on select/alternate), the viewer document, persistence."""
import json
import os

from needle_voice import route_store as rs

RESP = {"routes": [
    {"duration": "1260s", "staticDuration": "840s", "distanceMeters": 5200, "description": "Broadway", "polyline": {"encodedPolyline": "abc"},
     "legs": [{"startLocation": {"latLng": {"latitude": 40.1, "longitude": -73.1}}, "endLocation": {"latLng": {"latitude": 40.2, "longitude": -73.2}},
               "steps": [{"distanceMeters": 300, "navigationInstruction": {"instructions": "Head north"}}, {"distanceMeters": 50},
                         {"distanceMeters": 900, "navigationInstruction": {"instructions": "Turn right"}}]}]},
    {"duration": "1500s", "distanceMeters": 5900, "description": "Madison Ave", "polyline": {"encodedPolyline": "def"}, "legs": []},
    {"staticDuration": "10s"},                                                   # no duration: dropped
]}


def test_parse_routes():
    routes, o, d = rs.parse_routes(RESP)
    assert len(routes) == 2 and (o, d) == ((40.1, -73.1), (40.2, -73.2))
    r = routes[0]
    assert (r.duration_s, r.static_duration_s, r.distance_m, r.summary, r.polyline) == (1260, 840, 5200, "Broadway", "abc")
    assert [s.instruction for s in r.steps] == ["Head north", "Turn right"]       # a step without an instruction is skipped
    assert routes[1].static_duration_s == 1500 and routes[1].steps == []


def test_parse_defensive():
    assert rs.parse_routes({}) == ([], None, None)
    assert rs.parse_routes({"routes": [{"duration": "nonsense"}]})[0] == []
    r = rs.parse_routes({"routes": [{"duration": "60s"}]})[0][0]                  # no polyline, no legs: still a usable route
    assert r.polyline == "" and r.steps == [] and r.distance_m == 0


def test_detail_request_asks_for_alternates_without_touching_the_base():
    base = {"travelMode": "DRIVE"}
    assert rs.detail_request(base)["computeAlternativeRoutes"] is True and "computeAlternativeRoutes" not in base
    assert "routes.polyline.encodedPolyline" in rs.DETAIL_FIELD_MASK and "routes.legs.steps.navigationInstruction" in rs.DETAIL_FIELD_MASK


def store_with_route(path=None):
    s = rs.RouteStore(path)
    routes, o, d = rs.parse_routes(RESP)
    s.set("Example Museum", routes, o, d)
    return s


def test_selection_never_recomputes():
    s = store_with_route()
    a = s.get(); rid = a.id
    assert s.select_alternate() == 1 and s.get().selected == 1 and s.get().id == rid      # same route, new selection
    assert s.select_alternate() == 0                                                      # wraps
    assert s.select(5) is False and s.get().selected == 0
    assert rs.RouteStore().select_alternate() is None and rs.RouteStore().select(0) is False


def test_single_route_has_no_alternate():
    s = rs.RouteStore(); s.set("X", rs.parse_routes(RESP)[0][:1])
    assert s.select_alternate() is None


def test_new_route_replaces_and_failed_lookup_keeps_old():
    s = store_with_route(); old = s.get().id
    assert s.set("Nowhere", []) is None and s.get().id == old                             # empty result must not wipe the screen
    s.set("Second Place", rs.parse_routes(RESP)[0]); assert s.get().id != old and s.get().destination_label == "Second Place"
    s.clear(); assert s.get() is None and s.to_document() is None


def test_document_matches_viewer_contract():
    s = store_with_route(); s.select_alternate()
    d = s.to_document({"close": "u/close", "phone": "u/phone", "empty": ""})
    assert d["selected"] == 1 and d["destination"] == {"lat": 40.2, "lng": -73.2, "label": "Example Museum"} and d["origin"]["lat"] == 40.1
    assert d["actions"] == {"close": "u/close", "phone": "u/phone"}                          # empty action dropped
    r0 = d["routes"][0]
    assert set(r0) == {"id", "summary", "duration_s", "static_duration_s", "distance_m", "polyline", "steps"} and r0["steps"][0] == {"instruction": "Head north", "distance_m": 300}
    ex = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "kiosk-route", "route.example.json")))
    assert set(ex["routes"][0]) == set(r0) and set(ex) - {"actions"} <= set(d)               # same shape as the viewer's own example
    json.dumps(d)


def test_document_without_endpoints():
    s = rs.RouteStore(); s.set("Somewhere", rs.parse_routes({"routes": [{"duration": "60s"}]})[0])
    d = s.to_document(); assert "origin" not in d and d["destination"] == {"label": "Somewhere"}


def test_persistence_roundtrip_and_privacy(tmp_path):
    p = str(tmp_path / "sub" / "route.json")
    s = store_with_route(p); s.select_alternate()
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    s2 = rs.RouteStore(p)
    assert s2.get().id == s.get().id and s2.get().selected == 1 and s2.get().routes[0].steps[0].instruction == "Head north" and s2.get().origin == (40.1, -73.1)
    s2.clear(); assert not os.path.exists(p) and rs.RouteStore(p).get() is None


def test_corrupt_file_means_no_route(tmp_path):
    p = tmp_path / "route.json"; p.write_text("{not json")
    assert rs.RouteStore(str(p)).get() is None
    p.write_text(json.dumps({"id": "x"})); assert rs.RouteStore(str(p)).get() is None


def test_summary_has_no_location():
    s = store_with_route(); t = json.dumps(s.summary())
    assert "Museum" not in t and "40." not in t and s.summary()["routes"] == 2
