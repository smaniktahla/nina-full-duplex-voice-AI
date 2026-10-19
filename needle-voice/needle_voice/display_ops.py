"""Show the active route on the kitchen kiosk, and take it down again. Requests like "show me the route on the kitchen screen", "put that on the kitchen
display", "show me the alternate route", "close the map", "go back to the normal dashboard" (and a bare "yes, show me" right after Nina offered).

Same contract as home_ops / calendar_ops (result.ToolResult, honest speech). The pieces:

  * The route comes from route_store.RouteStore: a display command acts on the route the user was just told about and NEVER computes a new one.
  * Nothing here talks to Home Assistant or the disk directly. I/O is injected (`DisplayIO`): act, get_state, write_document, served, sleep. `act` names one of
    four fixed ACTIONS (show/close/pause/resume) and an entity; the service behind each lives in executor.DISPLAY_ACTIONS, the only place that may call HA.
  * Which kiosk is which comes from `DisplayConfig.targets` ({"kitchen": "<browser path sensor entity>"}): household config, never code. Anything
    other than a configured target is refused.
  * Success is only claimed when it was OBSERVED: the kiosk's browser-path sensor shows the route page AND the viewer has fetched the document that was just
    written (a "key" = route id + updated time). A service call that was accepted but not seen to take effect is UNVERIFIED (side_effect=True) and is never
    retried here. An offline kiosk is UNAVAILABLE and nothing is sent.
  * HA-side behaviour (pause rotation, popups overlay, restore after a popup, resume on close) is the kiosk_route package in ha-dashboard-manager; this
    module only calls its two scripts and reads its flag.
  * Telemetry: result codes, ids and timings only. No destination text, no coordinates.

Pure, stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .result import Status, ToolResult

DOMAIN = "display"
VERIFY_TIMEOUT_S = 12.0          # the viewer polls every ~3 s; the rotator navigates within ~1 s
POLL_S = 0.5
SERVED_MAX_AGE_S = 15.0

Act = Callable[[str, str], Awaitable[bool]]
GetState = Callable[[str], Awaitable[Optional[str]]]
WriteDocument = Callable[[Optional[dict]], Awaitable[bool]]
Served = Callable[[], tuple]
Sleep = Callable[[float], Awaitable[None]]


@dataclass
class DisplayIO:
    act: Act                                   # (action, entity_id) -> True if HA accepted the call; raises/returns False otherwise
    get_state: GetState                        # entity_id -> state string, None if it cannot be read
    write_document: WriteDocument              # publish the route document the viewer reads (None = clear); True if written
    served: Served                             # () -> (key|None, age_seconds): the last document key a viewer fetched
    sleep: Sleep
    show_page: Optional[Callable[[str, int], Awaitable[bool]]] = None          # (path, hold_s) -> True if HA accepted: pop a pinned page up for a while
    write_agenda: Optional[Callable[[dict], Awaitable[bool]]] = None           # publish the agenda document the agenda viewer reads
    served_agenda: Optional[Served] = None                                     # last agenda document key a viewer fetched


@dataclass
class DisplayConfig:
    targets: dict                                                  # {"kitchen": "sensor.<browser>_browser_path"}
    route_path: str = "/kiosk-route/route"
    active_flag: str = "input_boolean.kiosk_route_active"
    show_script: str = "script.kiosk_route_show"
    close_script: str = "script.kiosk_route_close"
    pause_flag: str = "input_boolean.dashboard_rotation_paused"
    agenda_path: str = "/kiosk-agenda/agenda"
    agenda_hold_s: int = 60
    default_target: str = "kitchen"
    verify_timeout_s: float = VERIFY_TIMEOUT_S
    extra: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------------------------- parsing (pure)
_ROUTE_WORD = r"(?:route|directions|map)"
_WHERE = r"(?:kitchen|screen|display|kiosk|monitor|tv)"
_NOT_NAV = re.compile(r"(?i)\b(weather|radar|forecast|pool|camera|cameras|doorbell|temperature|calendar|news|plex)\b")
_SHOW_VERB = r"(?:show|put|display|bring\s+up|pull\s+up|throw|cast)"
_SHOW_EXPLICIT = re.compile(r"(?i)\b%s\b.*\b%s\b" % (_SHOW_VERB, _ROUTE_WORD))
_SHOW_PRONOUN = re.compile(r"(?i)\b%s\b.*\b(?:that|it|this)\b.*\b(?:on|to)\b.*\b%s\b" % (_SHOW_VERB, _WHERE))
_ALT = re.compile(r"(?i)\b(?:alternat(?:e|ive)|another|other|different|second)\s+(?:routes?|ways?|options?)\b|\b(?:route|way)\s+(?:two|2)\b|\balternat(?:e|ive)s?\b")
_PHONE = re.compile(r"(?i)\b(?:phone|mobile|text me|message me|email me)\b")
_CLOSE = re.compile(r"(?i)\b(?:close|dismiss|hide|clear|remove|take\s+down|get\s+rid\s+of|turn\s+off)\b.*\b%s\b|\btake\b.*\b%s\b.*\bdown\b" % (_ROUTE_WORD, _ROUTE_WORD))
_BACK = re.compile(r"(?i)\b(?:go|get|switch|put\s+it|return)\s+back\b.*\b(?:normal|regular|usual|dashboards?|rotation)\b|\bback\s+to\s+(?:the\s+)?(?:normal|regular|usual)?\s*(?:dashboards?|rotation)\b|\b(?:normal|regular)\s+dashboards?\b")
_STATE = re.compile(r"(?i)\b(?:is|what'?s|what\s+is)\b.*\b(?:showing|on)\b.*\b%s\b|\bwhat'?s\s+on\s+the\s+%s\b" % (_WHERE, _WHERE))
_PAUSE = re.compile(r"(?i)\bpause\b.*\b(?:rotation|dashboards?)\b|\bstop\b.*\b(?:rotating|rotation)\b")
_RESUME = re.compile(r"(?i)\b(?:resume|restart|start)\b.*\b(?:rotation|dashboards?|rotating)\b")
_YES = re.compile(r"(?i)^\s*(?:yes|yeah|yep|yup|sure|please|ok(?:ay)?|do\s+it|go\s+ahead|show\s+me|absolutely|definitely|of\s+course)\b(?![^?.!]*\b(?:no|not|don'?t|never|later)\b)[\s,.!]*"
                  r"(?:(?:yes|please|sure|show(?:\s+(?:me|it|that|the\s+route))?|do\s+it|put\s+(?:it|that)\s+(?:up|on(?:\s+the\s+(?:screen|kitchen\s+screen|display))?)|go\s+ahead)[\s,.!]*)*$")


def parse(text: str, offered: bool = False) -> str:
    """-> 'show' | 'alternate' | 'close' | 'back' | 'state' | 'pause' | 'resume' | '' (not a display request).
    `offered` is True only right after Nina offered to show a route: it makes a bare 'yes / show me' mean show. Requests about other displays' content
    (weather, cameras, ...) are not route requests and return ''."""
    t = (text or "").strip()
    if not t:
        return ""
    if _PHONE.search(t):                                           # "send the directions to my phone", "text me the route" belong to the navigation module
        return ""
    if offered and _YES.match(t):
        return "show"
    if _NOT_NAV.search(t):
        return ""
    if _CLOSE.search(t):
        return "close"
    if _BACK.search(t):
        return "back"
    if _ALT.search(t) and (re.search(r"(?i)\b(?:alternat(?:e|ive)|another|other|different|second)\s+(?:routes?|ways?|options?)\b|\b(?:route|way)\s+(?:two|2)\b", t) or len(t.split()) <= 6):
        return "alternate"                                         # "is there an alternate" / "any other routes": a bare 'alternate' only counts in a short sentence
    if _PAUSE.search(t):
        return "pause"
    if _RESUME.search(t):
        return "resume"
    if _SHOW_EXPLICIT.search(t) or _SHOW_PRONOUN.search(t):
        return "show"
    if _STATE.search(t):
        return "state"
    return ""


def _mentions_route(text: str) -> bool:
    return bool(re.search(r"(?i)\b%s\b" % _ROUTE_WORD, text or ""))


# ----------------------------------------------------------------------------------------------- results
def _res(status, speech, code, side_effect=False, **tel) -> ToolResult:
    return ToolResult(status, speech, reason=code, telemetry={"code": code, **tel}, domain=DOMAIN, side_effect=side_effect)


def _target(cfg: DisplayConfig, target: Optional[str]):
    name = (target or cfg.default_target or "").strip().lower()
    sensor = cfg.targets.get(name)
    return name, sensor


def _unknown_target(name):
    return _res(Status.UNSUPPORTED, "I can only show routes on the kitchen screen.", "unknown_target", target=name)


async def _path(io: DisplayIO, sensor: str):
    st = await io.get_state(sensor)
    return None if st in (None, "", "unknown", "unavailable") else st


async def _until(io: DisplayIO, cond, timeout: float) -> bool:
    waited = 0.0
    while True:
        if await cond():
            return True
        if waited >= timeout:
            return False
        await io.sleep(POLL_S)
        waited += POLL_S


async def _showing(io, cfg, sensor, key) -> bool:
    """Route page is up AND the viewer fetched this very document recently."""
    p = await _path(io, sensor)
    if p is None or not p.startswith(cfg.route_path):
        return False
    k, age = io.served()
    return k == key and age <= SERVED_MAX_AGE_S


def _key(doc: dict) -> str:
    return "%s|%s" % (doc.get("id"), doc.get("updated"))


# ----------------------------------------------------------------------------------------------- operations
async def show_route(store, io: DisplayIO, cfg: DisplayConfig, route_id: Optional[str] = None, target: Optional[str] = None) -> ToolResult:
    """Put the active route on the kiosk (or refresh it when it is already there). Never computes a route."""
    name, sensor = _target(cfg, target)
    if not sensor:
        return _unknown_target(name)
    active = store.get()
    if active is None:
        return _res(Status.NO_MATCH, "I don't have a route to show yet. Ask me how long it takes to get somewhere first.", "no_active_route")
    if route_id and route_id != active.id:
        return _res(Status.NO_MATCH, "That route has been replaced, so I'm not showing it.", "route_replaced", route_id=route_id)
    if await _path(io, sensor) is None:
        return _res(Status.UNAVAILABLE, "The kitchen screen isn't responding, so I can't show it there.", "kiosk_offline", target=name)
    doc = store.to_document()
    key = _key(doc)
    already = await _showing(io, cfg, sensor, key)
    if not await io.write_document(doc):
        return _res(Status.EXECUTION_FAILED, "I couldn't prepare the route for the screen.", "document_write_failed", route_id=active.id)
    if already:                                                    # same route already up: nothing to send
        return _res(Status.SUCCESS, "It's already on the kitchen screen.", "already_showing", route_id=active.id)
    try:
        accepted = await io.act("show", cfg.show_script)
    except Exception:                                              # noqa: BLE001
        accepted = False
    if not accepted:
        return _res(Status.EXECUTION_FAILED, "I couldn't reach Home Assistant, so the route isn't on the screen.", "show_call_failed", route_id=active.id)

    async def up():
        return await _showing(io, cfg, sensor, key) and (await io.get_state(cfg.active_flag)) == "on"
    if await _until(io, up, cfg.verify_timeout_s):
        return _res(Status.SUCCESS, "It's on the kitchen screen.", "shown", side_effect=True, route_id=active.id)
    return _res(Status.UNVERIFIED, "I sent it to the kitchen screen but couldn't confirm it's showing. Please check it.", "show_unconfirmed",
                side_effect=True, route_id=active.id)


async def show_alternate(store, io: DisplayIO, cfg: DisplayConfig, target: Optional[str] = None) -> ToolResult:
    """Switch the stored route to its next alternative and refresh the screen. No recalculation: the alternates were fetched with the route."""
    name, sensor = _target(cfg, target)
    if not sensor:
        return _unknown_target(name)
    if store.get() is None:
        return _res(Status.NO_MATCH, "I don't have a route to show yet.", "no_active_route")
    if len(store.get().routes) < 2:
        return _res(Status.NO_MATCH, "There's no alternate route for that trip.", "no_alternate")
    before = store.get().selected
    store.select_alternate()
    r = await show_route(store, io, cfg, target=name)
    if r.status is Status.SUCCESS:
        a = store.get(); sel = a.routes[a.selected]
        mins = max(1, round(sel.duration_s / 60))
        r.speech = "Showing the alternate route on the kitchen screen: about %d minutes." % mins
        r.reason = "alternate_shown"; r.telemetry["code"] = "alternate_shown"
    elif r.status in (Status.EXECUTION_FAILED, Status.UNAVAILABLE) and not r.side_effect:
        store.select(before)                                       # nothing changed on screen: do not leave the store pointing at the unseen route
    return r


async def close_route(io: DisplayIO, cfg: DisplayConfig, target: Optional[str] = None) -> ToolResult:
    """Take the route down and let the normal rotation resume. The stored route is kept ("show it again" still works)."""
    name, sensor = _target(cfg, target)
    if not sensor:
        return _unknown_target(name)
    if await _path(io, sensor) is None:
        return _res(Status.UNAVAILABLE, "The kitchen screen isn't responding, so I can't tell what it's showing.", "kiosk_offline", target=name)
    p = await _path(io, sensor)
    if (await io.get_state(cfg.active_flag)) != "on" and not p.startswith(cfg.route_path):
        return _res(Status.SUCCESS, "There's no route on the kitchen screen.", "nothing_to_close")
    try:
        accepted = await io.act("close", cfg.close_script)
    except Exception:                                              # noqa: BLE001
        accepted = False
    if not accepted:
        return _res(Status.EXECUTION_FAILED, "I couldn't reach Home Assistant, so the route is still up.", "close_call_failed")

    async def down():
        cur = await _path(io, sensor)
        return (await io.get_state(cfg.active_flag)) == "off" and cur is not None and not cur.startswith(cfg.route_path)
    if await _until(io, down, cfg.verify_timeout_s):
        return _res(Status.SUCCESS, "Okay, the dashboards are back.", "closed", side_effect=True)
    return _res(Status.UNVERIFIED, "I asked for the route to close but couldn't confirm it. Please check the screen.", "close_unconfirmed", side_effect=True)


async def get_state(store, io: DisplayIO, cfg: DisplayConfig, target: Optional[str] = None) -> ToolResult:
    name, sensor = _target(cfg, target)
    if not sensor:
        return _unknown_target(name)
    p = await _path(io, sensor)
    if p is None:
        return _res(Status.UNAVAILABLE, "The kitchen screen isn't responding.", "kiosk_offline", target=name)
    on = (await io.get_state(cfg.active_flag)) == "on" and p.startswith(cfg.route_path)
    a = store.get()
    return _res(Status.SUCCESS, "The kitchen screen is showing a route." if on else "The kitchen screen isn't showing a route.", "state",
                showing=on, route_id=a.id if a else None, has_route=bool(a))


async def _set_pause(io, cfg, want_on: bool) -> ToolResult:
    flag = cfg.pause_flag
    if (await io.get_state(flag)) is None:
        return _res(Status.UNAVAILABLE, "I can't reach the dashboard rotation control.", "pause_flag_unreadable")
    if (await io.get_state(cfg.active_flag)) == "on":
        return _res(Status.UNSUPPORTED, "A route is on the screen, so the dashboards are already held. Say close the map to bring them back.",
                    "route_active")
    try:
        accepted = await io.act("pause" if want_on else "resume", flag)
    except Exception:                                              # noqa: BLE001
        accepted = False
    if not accepted:
        return _res(Status.EXECUTION_FAILED, "I couldn't reach Home Assistant, so nothing changed.", "pause_call_failed")

    async def done():
        return (await io.get_state(flag)) == ("on" if want_on else "off")
    if await _until(io, done, 4.0):
        return _res(Status.SUCCESS, "Dashboard rotation is paused." if want_on else "Dashboard rotation has resumed.", "paused" if want_on else "resumed",
                    side_effect=True)
    return _res(Status.UNVERIFIED, "I sent that but couldn't confirm the rotation changed.", "pause_unconfirmed", side_effect=True)


async def pause_rotation(io: DisplayIO, cfg: DisplayConfig, target: Optional[str] = None) -> ToolResult:
    return await _set_pause(io, cfg, True) if _target(cfg, target)[1] else _unknown_target(_target(cfg, target)[0])


async def resume_rotation(io: DisplayIO, cfg: DisplayConfig, target: Optional[str] = None) -> ToolResult:
    return await _set_pause(io, cfg, False) if _target(cfg, target)[1] else _unknown_target(_target(cfg, target)[0])


# ----------------------------------------------------------------------------------------------- the request
def owns(text: str, store, offered: bool = False) -> bool:
    """True when this module should answer `text` (callers use it to decide whether to route the request here at all). A pronoun-only request with no
    active route is not ours: 'that' may mean anything."""
    intent = parse(text, offered)
    if not intent:
        return False
    if intent in ("show", "alternate", "state") and store.get() is None and not (offered or _mentions_route(text)):
        return False
    return True


async def handle(text: str, store, io: DisplayIO, cfg: DisplayConfig, offered: bool = False) -> ToolResult:
    """Spoken request -> one operation. A request this module does not own comes back UNSUPPORTED with speech None (the caller may try other agents);
    a pronoun-only request ('put that on the kitchen display') with no active route is also left to others, since 'that' may mean something else."""
    intent = parse(text, offered)
    if not intent:
        return ToolResult(Status.UNSUPPORTED, None, reason="not_display", domain=DOMAIN)
    if not owns(text, store, offered):
        return ToolResult(Status.UNSUPPORTED, None, reason="no_referent", domain=DOMAIN)
    if intent in ("show", "alternate"):
        return await (show_alternate(store, io, cfg) if intent == "alternate" else show_route(store, io, cfg))
    if intent in ("close", "back"):
        return await close_route(io, cfg)
    if intent == "state":
        return await get_state(store, io, cfg)
    return await (pause_rotation(io, cfg) if intent == "pause" else resume_rotation(io, cfg))


async def show_agenda(io: DisplayIO, cfg: DisplayConfig, doc: dict, target: Optional[str] = None) -> ToolResult:
    """Pop the day's agenda up on the kiosk for `cfg.agenda_hold_s` seconds, then the dashboard manager resumes the rotation. A side effect of a calendar answer, so it
    never speaks for itself (the caller already answered): the result is for logs and tests. A pinned route is never replaced by a popup; an offline kiosk is not asked;
    success is claimed only when the kiosk shows the agenda page AND the viewer fetched this very document."""
    name, sensor = _target(cfg, target)
    if not sensor:
        return _unknown_target(name)
    if io.show_page is None or io.write_agenda is None or io.served_agenda is None:
        return _res(Status.UNSUPPORTED, None, "agenda_not_configured")
    if await _path(io, sensor) is None:
        return _res(Status.UNAVAILABLE, None, "kiosk_offline", target=name)
    if (await io.get_state(cfg.active_flag)) == "on":
        return _res(Status.UNAVAILABLE, None, "route_on_screen")
    key = "%s|%s" % (doc.get("id"), doc.get("updated"))
    if not await io.write_agenda(doc):
        return _res(Status.EXECUTION_FAILED, None, "agenda_write_failed")
    try:
        accepted = await io.show_page(cfg.agenda_path, int(cfg.agenda_hold_s))
    except Exception:                                              # noqa: BLE001
        accepted = False
    if not accepted:
        return _res(Status.EXECUTION_FAILED, None, "agenda_show_failed")

    async def up():
        p = await _path(io, sensor)
        k, age = io.served_agenda()
        return p is not None and p.startswith(cfg.agenda_path) and k == key and age <= SERVED_MAX_AGE_S
    if await _until(io, up, cfg.verify_timeout_s):
        return _res(Status.SUCCESS, "The agenda is on the kitchen screen.", "agenda_shown", side_effect=True)
    return _res(Status.UNVERIFIED, None, "agenda_unconfirmed", side_effect=True)
