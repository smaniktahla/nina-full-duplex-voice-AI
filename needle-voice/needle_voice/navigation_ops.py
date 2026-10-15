"""Navigation requests for the voice assistant: "how long to X", "when should I leave", "send it to my phone".

Same contract as home_ops (result.ToolResult, honest speech, no side effect claimed that wasn't observed). Nothing here chooses a service or an entity:

  * ETA comes from the Google Routes API (computeRoutes, DRIVE, TRAFFIC_AWARE). The destination is passed as an address string and Google resolves it;
    there is no place-search step yet, so a vague name returns Google's best guess, not an AMBIGUOUS question (known limit, see PIPELINE.md).
  * "when should I leave" = event start - ETA - buffer (default 10 min). A future departure is a prediction and is spoken as "typically".
  * "send it to my phone" hands the destination to a caller-supplied `notify(title, message, uri)` (the caller owns HA service calls). A tap on the
    notification opens turn-by-turn navigation. There is no read-back, so the best this can be is UNVERIFIED; it is never auto-retried.

I/O is injected so the module is pure and testable: `post_json(url, headers, body) -> (http_status, dict)` and `notify`. Telemetry carries durations and
a result code only: no destination text, no street address, no coordinates.
"""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .result import Status, ToolResult

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = "routes.duration,routes.staticDuration,routes.distanceMeters"
DEFAULT_BUFFER_MIN = 10
CTX_TTL_S = 600.0

PostJson = Callable[[str, dict, dict], Awaitable[tuple]]
Notify = Callable[[str, str, str], Awaitable[bool]]


@dataclass
class NavContext:
    """What the previous turns established. `destination`/`event_start` are normally the next calendar event with a location."""
    destination: str = ""
    event_start: dt.datetime | None = None          # timezone-aware
    t: float = 0.0                                  # monotonic time it was set (the caller expires it after CTX_TTL_S)


@dataclass
class NavConfig:
    api_key: str
    home: tuple                                     # (lat, lng) of zone.home; the origin for every route
    buffer_min: int = DEFAULT_BUFFER_MIN
    notify: Notify | None = None
    post_json: PostJson | None = None
    extra: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------------------------- parsing (pure)
_ETA = re.compile(r"(?i)\b(how (?:long|far)|eta|travel time|drive time|how much time)\b")
_LEAVE = re.compile(r"(?i)\b(when (?:should|do|must|shall) (?:i|we) (?:leave|go|head out|get going|set off)|what time (?:should|do) (?:i|we) (?:leave|go|head out)|time to leave|leave by)\b")
_SEND = re.compile(r"(?i)\b(send|push|put|text|share|get)\b.*\b(phone|mobile)\b|\b(navigate|directions?)\b.*\b(phone|mobile)\b|\bsend (?:it|that|the directions) to my phone\b")
_DEST = re.compile(r"(?i)\b(?:to|for|toward|towards)\s+(.+?)\s*[?.!]*$")
_LEAD = re.compile(r"(?i)^(?:(?:get|go|drive|walk|head)(?:\s+to)?|to|the)\s+")
_PRON = re.compile(r"(?i)\b(it|there|that|my next (?:appointment|meeting|event)|the appointment|the meeting|my appointment)\b")


def parse(text: str) -> tuple:
    """-> (intent, destination_or_''). intent is 'send' | 'leave' | 'eta' | ''. A pronoun / 'next appointment' destination is returned as ''
    (the caller's NavContext supplies it)."""
    t = (text or "").strip()
    intent = "send" if _SEND.search(t) else "leave" if _LEAVE.search(t) else "eta" if _ETA.search(t) else ""
    if not intent:
        return "", ""
    m = _DEST.search(t)
    dest = (m.group(1).strip(" ,") if m else "")
    while True:
        d2 = _LEAD.sub("", dest)
        if d2 == dest:
            break
        dest = d2
    if dest and (_PRON.fullmatch(dest) or _PRON.search(dest) and len(dest.split()) <= 4 or dest.lower() in ("my phone", "phone", "me", "my mobile")):
        dest = ""
    if intent == "send" and re.search(r"(?i)\bphone\b", dest):
        dest = ""
    return intent, dest


def parse_duration_s(v) -> int | None:
    """Routes durations are strings like '1234s'."""
    if isinstance(v, (int, float)):
        return int(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", str(v or ""))
    return int(float(m.group(1))) if m else None


def spoken_minutes(seconds: int) -> str:
    m = max(1, round(seconds / 60))
    if m < 60:
        return "%d minute%s" % (m, "" if m == 1 else "s")
    h, r = divmod(m, 60)
    return "%d hour%s%s" % (h, "" if h == 1 else "s", (" %d minute%s" % (r, "" if r == 1 else "s")) if r else "")


def clock(t: dt.datetime) -> str:
    return t.strftime("%-I:%M %p").lstrip("0")


def nav_uri(destination: str) -> str:
    return "google.navigation:q=" + urllib.parse.quote(destination)


def build_request(home: tuple, destination: str, depart: dt.datetime | None) -> dict:
    body = {"origin": {"location": {"latLng": {"latitude": home[0], "longitude": home[1]}}},
            "destination": {"address": destination},
            "travelMode": "DRIVE", "routingPreference": "TRAFFIC_AWARE", "units": "IMPERIAL"}
    if depart is not None:
        body["departureTime"] = depart.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return body


# ----------------------------------------------------------------------------------------------- Routes API
async def route(cfg: NavConfig, destination: str, depart: dt.datetime | None = None):
    """-> ('ok', seconds, static_seconds, meters) | ('none',) | ('auth',) | ('rate',) | ('error', code) | ('down',)."""
    headers = {"X-Goog-Api-Key": cfg.api_key, "X-Goog-FieldMask": FIELD_MASK, "Content-Type": "application/json"}
    try:
        status, data = await cfg.post_json(ROUTES_URL, headers, build_request(cfg.home, destination, depart))
    except Exception as e:                                                      # noqa: BLE001 - network failures are one honest outcome
        return ("down", type(e).__name__)
    if status in (401, 403):
        return ("auth",)
    if status == 429:
        return ("rate",)
    if status == 404:
        return ("none",)
    if status == 400:                                                           # unresolvable address / bad time: nothing usable came back
        return ("none",)
    if status >= 400:
        return ("error", status)
    routes = (data or {}).get("routes") or []
    if not routes:
        return ("none",)
    r = routes[0]
    secs = parse_duration_s(r.get("duration"))
    if secs is None:
        return ("error", "malformed")
    return ("ok", secs, parse_duration_s(r.get("staticDuration")) or secs, int(r.get("distanceMeters") or 0))


def _fail(code: str, speech: str, status: Status = Status.EXECUTION_FAILED, **tel) -> ToolResult:
    return ToolResult(status, speech, reason=code, telemetry={"code": code, **tel}, domain="navigation")


def _from_route(res) -> ToolResult | None:
    kind = res[0]
    if kind == "ok":
        return None
    if kind == "none":
        return ToolResult(Status.NO_MATCH, "I couldn't find a driving route there.", reason="no_route", telemetry={"code": "no_route"}, domain="navigation")
    if kind == "auth":
        return _fail("routes_auth_failed", "Google rejected the navigation key, so I can't look up routes right now.")
    if kind == "rate":
        return _fail("routes_rate_limited", "Google says I'm asking too often. Try again in a minute.")
    if kind == "down":
        return _fail("routes_unreachable", "I couldn't reach Google Maps just now.")
    return _fail("routes_error", "Google Maps returned an error, so I can't give you a route.")


def traffic_note(secs: int, static: int) -> str:
    extra = secs - static
    return (", about %s more than usual because of traffic" % spoken_minutes(extra)) if extra >= 300 else ""


# ----------------------------------------------------------------------------------------------- the request
async def handle(text: str, ctx: NavContext | None, cfg: NavConfig, now: dt.datetime | None = None) -> ToolResult:
    now = now or dt.datetime.now().astimezone()
    intent, dest = parse(text)
    if not intent:
        return ToolResult(Status.UNSUPPORTED, None, reason="not_navigation", domain="navigation")
    if not cfg.api_key or not cfg.post_json or not cfg.home:
        return _fail("not_configured", "Navigation isn't set up yet: it needs a Google Maps key.", Status.UNAVAILABLE)
    destination = dest or (ctx.destination if ctx else "")
    if not destination:
        return ToolResult(Status.AMBIGUOUS, "Where to?", reason="no_destination", telemetry={"code": "no_destination"}, domain="navigation")
    start = ctx.event_start if (ctx and not dest and ctx.event_start) else None

    if intent == "send":
        if not cfg.notify:
            return _fail("no_phone_target", "I don't have a phone to send that to yet.", Status.UNAVAILABLE)
        try:
            ok = await cfg.notify("Navigate", "Tap to start directions", nav_uri(destination))
        except Exception as e:                                                  # noqa: BLE001
            return _fail("notify_unverified", "I tried to send it to your phone but couldn't tell whether it arrived.", Status.UNVERIFIED, err=type(e).__name__)
        if not ok:
            return _fail("notify_failed", "I couldn't send the directions to your phone.")
        return ToolResult(Status.UNVERIFIED, "I sent the directions to your phone. Tap the notification to start.", reason="notify_sent_no_readback",
                          telemetry={"code": "notify_sent"}, domain="navigation", side_effect=True)

    if intent == "leave":
        if not start:
            return ToolResult(Status.AMBIGUOUS, "What time do you need to be there?", reason="no_arrival_time", telemetry={"code": "no_arrival_time"}, domain="navigation")
        depart = max(now + dt.timedelta(minutes=1), start - dt.timedelta(minutes=30))
        res = await route(cfg, destination, depart if depart > now + dt.timedelta(minutes=2) else None)
        bad = _from_route(res)
        if bad:
            return bad
        _, secs, static, meters = res
        leave_at = start - dt.timedelta(seconds=secs) - dt.timedelta(minutes=cfg.buffer_min)
        predicted = depart > now + dt.timedelta(minutes=2)
        if leave_at <= now:
            speech = "You should leave now: the drive is %s and you need to be there at %s." % (spoken_minutes(secs), clock(start))
        else:
            speech = "Leave by %s to be there at %s. The drive is %s%s, plus a %d minute buffer." % (
                clock(leave_at), clock(start), ("typically " if predicted else "") + spoken_minutes(secs), traffic_note(secs, static), cfg.buffer_min)
        return ToolResult(Status.SUCCESS, speech, reason="read", telemetry={"code": "leave_by", "eta_s": secs, "buffer_min": cfg.buffer_min, "predicted": predicted},
                          domain="navigation")

    res = await route(cfg, destination, None)                                   # eta
    bad = _from_route(res)
    if bad:
        return bad
    _, secs, static, meters = res
    miles = meters / 1609.344
    speech = "It's about %s by car with current traffic%s, %.0f mile%s." % (spoken_minutes(secs), traffic_note(secs, static), miles, "" if round(miles) == 1 else "s")
    return ToolResult(Status.SUCCESS, speech, reason="read", telemetry={"code": "eta", "eta_s": secs, "static_s": static, "meters": meters}, domain="navigation")
