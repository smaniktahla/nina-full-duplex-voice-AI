# Kiosk route viewer

A standalone page that shows one route full screen: map with the real polyline, start and destination markers, traffic-aware ETA, distance, ordered
turn-by-turn steps, alternate routes, Close and Send-to-phone. It holds **no credentials**: routing happens server side, and the page only reads one JSON
document.

    index.html?src=<url of the route document>&poll=<seconds>      (defaults: route.json, 3)

It polls the document. A new `id`/`updated` replaces the displayed route; a 404/204/empty body shows "No active route"; a network or server error keeps the
last route and says so. While the document is unchanged nothing is redrawn, so a tapped alternate-route tab and the map position are kept.

## Document

```json
{ "id": "…", "updated": "ISO time", "selected": 0,
  "origin": {"lat": 0, "lng": 0, "label": "Start"}, "destination": {"lat": 0, "lng": 0, "label": "Place"},
  "routes": [{"id": "r0", "summary": "via …", "duration_s": 1260, "static_duration_s": 840, "distance_m": 5200,
              "polyline": "<Google encoded polyline or [[lat,lng],…]>", "steps": [{"instruction": "…", "distance_m": 300}]}],
  "actions": {"close": "<url>", "phone": "<url>"} }
```

`routes[0]` is the fastest; the rest are alternates. `selected` is the route Needle says is active ("show me the alternate" changes it). The two action
URLs are POSTed to by the page; a non-2xx answer shows "That didn't go through". Served by `live/kiosk.py` they are relative to a signed link (see that module); a 401/403 on the document shows "This link has expired".
Tiles are CARTO dark (OpenStreetMap data); Leaflet 1.9.4 is vendored in `vendor/` (BSD-2, licence included).

## Test

    python3 make_fixture.py && python3 test_viewer.py [--shot out.png]    # headless Chromium via playwright, tiles blocked, no network

`route.example.json` is synthetic (public landmarks). Not wired to Needle or Home Assistant yet.
