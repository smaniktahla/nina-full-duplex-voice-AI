#!/usr/bin/env python3
"""Write route.example.json: a synthetic two-route document (public landmarks, no household data) for developing and testing the viewer."""
import json, datetime as dt

def enc(points):
    out, plat, plng = [], 0, 0
    for lat, lng in points:
        for v in (round(lat * 1e5) - plat, round(lng * 1e5) - plng):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1f)) + 63)); v >>= 5
            out.append(chr(v + 63))
        plat, plng = round(lat * 1e5), round(lng * 1e5)
    return "".join(out)

main = [(40.7536, -73.9832), (40.7580, -73.9855), (40.7644, -73.9795), (40.7712, -73.9742), (40.7794, -73.9632)]
alt = [(40.7536, -73.9832), (40.7500, -73.9760), (40.7560, -73.9680), (40.7680, -73.9640), (40.7794, -73.9632)]
doc = {
    "id": "example01", "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "selected": 0,
    "origin": {"lat": main[0][0], "lng": main[0][1], "label": "Start"},
    "destination": {"lat": main[-1][0], "lng": main[-1][1], "label": "Example Museum"},
    "routes": [
        {"id": "r0", "summary": "Broadway", "duration_s": 1260, "static_duration_s": 840, "distance_m": 5200, "polyline": enc(main),
         "steps": [{"instruction": "Head north on 5th Ave", "distance_m": 300}, {"instruction": "Turn right onto W 57th St", "distance_m": 900},
                   {"instruction": "Turn left onto Central Park S", "distance_m": 1500}, {"instruction": "Continue onto Central Park W", "distance_m": 1800},
                   {"instruction": "Turn right and arrive at the destination", "distance_m": 700}]},
        {"id": "r1", "summary": "Madison Ave", "duration_s": 1500, "static_duration_s": 1200, "distance_m": 5900, "polyline": enc(alt),
         "steps": [{"instruction": "Head east on E 42nd St", "distance_m": 800}, {"instruction": "Turn left onto Madison Ave", "distance_m": 3400},
                   {"instruction": "Arrive at the destination", "distance_m": 1700}]},
    ],
    "actions": {"close": "action/close", "phone": "action/phone"},
}
json.dump(doc, open("route.example.json", "w"), indent=1)
