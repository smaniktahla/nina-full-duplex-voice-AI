"""The active route: parse a Google Routes response into detailed routes, remember which one the user is looking at, and render the JSON document the
kiosk route viewer (kiosk-route/) reads.

Why a store: "show that on the kitchen screen", "show me the alternate", "send it to my phone" and "close the map" must act on the route the user was
just told about, never trigger a fresh calculation. The caller that computes a route puts it here once; everything after reads or changes the selection.

  * `DETAIL_FIELD_MASK` / `detail_request()` are what navigation_ops must send to get polylines, steps and alternates (ETA-only calls keep the small mask).
  * `parse_routes()` is pure and defensive: a response missing a polyline or steps still yields a usable route (the viewer shows what it has); a response
    with no usable route yields [].
  * `RouteStore` holds one active route. No expiry: a route stays until it is replaced or cleared. Optional `path` persists it (atomic write, chmod 600)
    so a service restart does not lose what the kiosk is showing. The file holds the home coordinates, so it belongs in the private data dir, never the repo.
  * `to_document()` renders the viewer's document. Action URLs are supplied by the caller (opaque capability URLs); this module holds no credentials.

Pure, stdlib only. Telemetry-safe summary: `summary()` has counts and durations, never coordinates or addresses.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from dataclasses import dataclass, field

DETAIL_FIELD_MASK = ",".join((
    "routes.duration", "routes.staticDuration", "routes.distanceMeters", "routes.description", "routes.polyline.encodedPolyline",
    "routes.legs.startLocation", "routes.legs.endLocation", "routes.legs.steps.distanceMeters", "routes.legs.steps.navigationInstruction"))
MAX_ROUTES = 3
MAX_POINTS = 6000          # an encoded polyline is cheap, but a runaway response should not become a huge document


def detail_request(base_body: dict) -> dict:
    """Turn navigation_ops.build_request(...) into the request that also returns alternates."""
    return dict(base_body, computeAlternativeRoutes=True)


@dataclass
class Step:
    instruction: str
    distance_m: int = 0


@dataclass
class RouteDetail:
    duration_s: int
    static_duration_s: int
    distance_m: int
    summary: str = ""
    polyline: str = ""                                   # Google encoded polyline, passed through to the viewer untouched
    steps: list = field(default_factory=list)


@dataclass
class ActiveRoute:
    id: str
    created: str
    destination_label: str
    origin: tuple | None                                 # (lat, lng)
    destination: tuple | None
    routes: list
    selected: int = 0
    updated: str = ""


def _secs(v) -> int | None:
    if isinstance(v, (int, float)):
        return int(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", str(v or ""))
    return int(float(m.group(1))) if m else None


def _latlng(o) -> tuple | None:
    ll = ((o or {}).get("latLng") or {})
    try:
        return (float(ll["latitude"]), float(ll["longitude"]))
    except (KeyError, TypeError, ValueError):
        return None


def parse_routes(data: dict) -> tuple:
    """Routes API response -> (routes, origin, destination). Routes without a parsable duration are dropped; the first is the API's recommended route."""
    out, origin, dest = [], None, None
    for r in ((data or {}).get("routes") or [])[:MAX_ROUTES]:
        secs = _secs(r.get("duration"))
        if secs is None:
            continue
        legs = r.get("legs") or []
        steps = []
        for leg in legs:
            for s in leg.get("steps") or []:
                text = ((s.get("navigationInstruction") or {}).get("instructions") or "").strip()
                if text:
                    steps.append(Step(text, int(s.get("distanceMeters") or 0)))
        poly = ((r.get("polyline") or {}).get("encodedPolyline")) or ""
        if len(poly) > MAX_POINTS * 8:
            poly = ""
        out.append(RouteDetail(secs, _secs(r.get("staticDuration")) or secs, int(r.get("distanceMeters") or 0), (r.get("description") or "").strip(), poly, steps))
        if not out[1:] and legs:                         # endpoints come from the recommended route
            origin, dest = _latlng(legs[0].get("startLocation")), _latlng(legs[-1].get("endLocation"))
    return out, origin, dest


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")      # sub-second: two selections in one second still differ


class RouteStore:
    """One active route. Reads never recompute anything; `set` is the only way a route enters."""

    def __init__(self, path: str | None = None):
        self.path = path
        self._active: ActiveRoute | None = None
        if path:
            self._load()

    # ---- state
    def get(self) -> ActiveRoute | None:
        return self._active

    def set(self, destination_label: str, routes: list, origin=None, destination=None) -> ActiveRoute | None:
        """Replace the active route. No routes -> the previous route is kept and None returned (a failed lookup must not wipe what is on screen)."""
        if not routes:
            return None
        now = _now()
        self._active = ActiveRoute(uuid.uuid4().hex[:10], now, destination_label, origin, destination, list(routes), 0, now)
        self._save()
        return self._active

    def select(self, index: int) -> bool:
        """Switch to another stored route. False if there is no such route (nothing changes)."""
        a = self._active
        if not a or not 0 <= index < len(a.routes):
            return False
        if index != a.selected:
            a.selected, a.updated = index, _now()
            self._save()
        return True

    def select_alternate(self) -> int | None:
        """'Show me the alternate route': the next stored route after the selected one (wrapping). None if the route has no alternate."""
        a = self._active
        if not a or len(a.routes) < 2:
            return None
        i = (a.selected + 1) % len(a.routes)
        self.select(i)
        return i

    def clear(self) -> None:
        self._active = None
        self._save()

    # ---- rendering
    def to_document(self, actions: dict | None = None) -> dict | None:
        """The kiosk viewer's JSON document, or None when there is no active route (the viewer shows 'No active route')."""
        a = self._active
        if not a:
            return None
        pt = lambda p, label: {"lat": p[0], "lng": p[1], "label": label} if p else None
        doc = {"id": a.id, "updated": a.updated, "selected": a.selected,
               "origin": pt(a.origin, "Start"), "destination": pt(a.destination, a.destination_label) or {"label": a.destination_label},
               "routes": [{"id": "r%d" % i, "summary": r.summary, "duration_s": r.duration_s, "static_duration_s": r.static_duration_s,
                           "distance_m": r.distance_m, "polyline": r.polyline, "steps": [{"instruction": s.instruction, "distance_m": s.distance_m} for s in r.steps]}
                          for i, r in enumerate(a.routes)]}
        if not doc["origin"]:
            del doc["origin"]
        if actions:
            doc["actions"] = {k: v for k, v in actions.items() if v}
        return doc

    def summary(self) -> dict:
        """Telemetry: no coordinates, no address."""
        a = self._active
        return {"active": bool(a), "routes": len(a.routes) if a else 0, "selected": a.selected if a else None, "id": a.id if a else None}

    # ---- persistence
    def _save(self) -> None:
        if not self.path:
            return
        a = self._active
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if a is None:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            return
        body = {"id": a.id, "created": a.created, "updated": a.updated, "label": a.destination_label, "origin": a.origin, "destination": a.destination,
                "selected": a.selected, "routes": [r.__dict__ | {"steps": [s.__dict__ for s in r.steps]} for r in a.routes]}
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(body, f)
        os.replace(tmp, self.path)

    def _load(self) -> None:
        try:
            b = json.load(open(self.path))
            routes = [RouteDetail(r["duration_s"], r["static_duration_s"], r["distance_m"], r.get("summary", ""), r.get("polyline", ""),
                                  [Step(s["instruction"], s.get("distance_m", 0)) for s in r.get("steps", [])]) for r in b["routes"]]
            if routes:
                tup = lambda p: tuple(p) if p else None
                self._active = ActiveRoute(b["id"], b["created"], b["label"], tup(b.get("origin")), tup(b.get("destination")), routes,
                                           min(int(b.get("selected", 0)), len(routes) - 1), b.get("updated", b["created"]))
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            self._active = None                          # unreadable or missing: start with no active route, never crash the service
