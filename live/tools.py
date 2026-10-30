"""Tools for the live voice assistant. Both run in the background; the conversation loop never waits for them.

  search : SearXNG API wrapper (docker2 :8091) first, `ddgs` meta-search fallback (SearXNG's upstream engines are currently
           CAPTCHA'd / suspended, so the fallback is what answers today)
  hermes : an agent on AI1 (:8643, its own llama-server) for deeper, multi-step thinking. AI1 on purpose: AI2's Hermes shares the
           single llama slot the voice loop runs on, so a long agent run there would stall every conversation tick.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time

import aiohttp

import household
from timers import Timer

_timer = Timer()
_timer_task = None
_timer_listeners = set()
_timer_ringing = False


async def timer_sound(active):
    """Notify connected voice clients independently of kiosk availability."""
    await asyncio.gather(*(listener(t="timer_alarm", active=active) for listener in tuple(_timer_listeners)), return_exceptions=True)


SEARX = household.get("searx_url", "SEARX_URL")
HERMES_URL = household.get("hermes_url", "HERMES_URL")
HERMES_HOST = household.get("hermes_ssh_host", "HERMES_HOST")   # ssh alias used once to read the API key; "" = read local file
_KEY = {}


def hermes_key():
    if "k" in _KEY:
        return _KEY["k"]
    k = os.environ.get("HERMES_API_KEY", "")
    if not k:
        cmd = "grep API_SERVER_KEY ~/.hermes/config.yaml | awk '{print $2}'"
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", HERMES_HOST, cmd] if HERMES_HOST else ["bash", "-c", cmd],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        k = out
    _KEY["k"] = k
    return k


def _fmt(items):
    parts = []
    for it in items[:5]:
        title = (it.get("title") or "").strip()
        body = (it.get("content") or it.get("snippet") or it.get("body") or "").strip().replace("\n", " ")
        parts.append("%s: %s" % (title[:80], body[:360]))
    return " | ".join(parts)[:1800]


def _ddgs(q):
    from ddgs import DDGS
    return DDGS().text(q, max_results=5)


async def web_search(http: aiohttp.ClientSession, q: str) -> str:
    items, src = [], "searxng"
    try:
        async with http.get(SEARX + "/search", params={"q": q}, timeout=aiohttp.ClientTimeout(total=5)) as r:
            items = (await r.json()).get("results", [])
    except Exception:
        items = []
    if not items:
        src = "ddgs"
        items = await asyncio.get_running_loop().run_in_executor(None, _ddgs, q)
    return ("(web search via %s) " % src + _fmt(items)) if items else "(web search found nothing)"


HERMES_WRAP = ("You are being asked by a live voice assistant. Use your tools if needed, think it through, then reply in at most "
               "four short spoken sentences with no markdown, lists or links. Task: ")


async def ask_hermes(http: aiohttp.ClientSession, task: str) -> str:
    body = {"model": "hermes", "messages": [{"role": "user", "content": HERMES_WRAP + task}]}
    async with http.post(HERMES_URL + "/v1/chat/completions", json=body, headers={"Authorization": "Bearer " + hermes_key()},
                         timeout=aiohttp.ClientTimeout(total=300)) as r:
        d = await r.json()
    return d["choices"][0]["message"]["content"].strip()[:1200]


# ------------------------------------------------------------------ Home Assistant (fast, local, no LLM in the path)
HA_URL = household.get("ha_url", "HA_URL")
CALENDARS = household.get("calendars", "CALENDARS")                 # empty = discover every calendar.* entity (minus calendar_exclude)


def _ha_token():
    if "ha" not in _KEY:
        tok = os.environ.get("HA_TOKEN", "")
        if not tok:
            for line in open(os.path.expanduser("~/.hermes/.env")):
                if line.startswith("HASS_TOKEN="):
                    tok = line.split("=", 1)[1].strip()
        _KEY["ha"] = tok
    return _KEY["ha"]


def _hdr():
    return {"Authorization": "Bearer " + _ha_token(), "Content-Type": "application/json"}


_home = {}
HOME_LOG = os.path.expanduser("~/.local/share/needle_voice/home_requests.jsonl")
VOICE_LEXICON = os.path.expanduser("~/.local/share/needle_voice/lexicon_voice.json")
HA_LEXICON_SSH = household.get("ha_lexicon_ssh", "HA_LEXICON_SSH")


async def _lex_file():
    try:
        return json.load(open(VOICE_LEXICON))
    except Exception:                                                     # noqa: BLE001
        return {}


async def _lex_ha():
    """Needle's own household vocabulary on HA (cached 5 min): the same file the satellite pipeline uses."""
    c = _home.setdefault("lex_ha", (0.0, {}))
    if time.time() - c[0] < 300:
        return c[1]
    host, path = HA_LEXICON_SSH.split(":", 1)
    try:
        p = await asyncio.create_subprocess_exec("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", host, "cat", path,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 8)
        data = json.loads(out)
    except Exception:                                                     # noqa: BLE001
        data = c[1]
    _home["lex_ha"] = (time.time(), data)
    return data


MUTATION_AUDIT_FLAG = os.path.expanduser("~/.local/share/needle_voice/mutation_audit")   # exists => audit mode on
_ON, _OFF = {"on", "enable", "activate"}, {"off", "disable", "deactivate"}
_MULTI = {"and", "then", "also", "both", "all"}


def ntel_id():
    import uuid
    return uuid.uuid4().hex[:8]


def audit_reject(tool_text, transcript):
    """Reason to refuse a proposed @ha action, else None: contradictory polarity, several actions in one request, or polarity that disagrees
    with the user's committed transcript."""
    import re
    tw = set(re.findall(r"[a-z]+", tool_text.lower()))
    if tw & _ON and tw & _OFF:
        return "both_on_and_off"
    if "turn" in tw and re.search(r"\bturn\b.*\b(and|then)\b.*\bturn\b", tool_text.lower()):
        return "multiple_actions"
    if transcript:
        uw = set(re.findall(r"[a-z]+", transcript.lower()))
        if (tw & _ON and not uw & _ON) or (tw & _OFF and not uw & _OFF):
            return "polarity_disagrees_with_transcript"
    return None


# ---------------------------------------------------------------- random numbers (Needle's random_ops + the shared RNG service)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "needle-voice"))
from needle_voice import random_ops                                          # noqa: E402  (the parser and the spoken answers live in Needle)

RNG_URL = household.get("rng_url", "RNG_URL")


# ---------------------------------------------------------------- route on the kitchen display (Needle's display_ops + route_store + kiosk.py)
ROUTINES = household.get("routines")                                             # {"morning": {...}, "night": {...}}: see routines.py (empty = routines off)
KIOSK_TARGETS = household.get("kiosk_targets")
_disp = {}


def route_store():
    """The one active route for this process (persisted, so a restart keeps what the kiosk shows). Whoever computes a route puts it here."""
    from needle_voice import route_store as rs
    if "store" not in _disp:
        _disp["store"] = rs.RouteStore(os.path.expanduser(household.get("route_store_path")))
    return _disp["store"]


def display_env(http):
    """(store, io, cfg) for display_ops. All I/O is injected here: HA through Needle's executor (the only module allowed to call services), states over REST,
    the document straight from the store (the kiosk listener reads it live, so 'writing' it is already done), what the viewer fetched from kiosk.served()."""
    from needle_voice import display_ops as d, executor
    import kiosk
    if "io" not in _disp:
        hass = executor.RestHass(HA_URL, _ha_token())

        async def act(action, entity):
            return await executor.execute_display(hass, action, entity)

        async def get_state(entity):
            try:
                async with http.get(HA_URL + "/api/states/" + entity, headers=_hdr(), timeout=aiohttp.ClientTimeout(total=5)) as r:
                    return (await r.json()).get("state") if r.status == 200 else None
            except Exception:                                                # noqa: BLE001
                return None

        async def write_document(doc):
            return bool(doc) and route_store().to_document() is not None

        async def show_page(path, hold_s):
            return await executor.execute_show_page(hass, path, hold_s)

        async def write_agenda(doc):
            _disp["agenda"] = doc
            _disp["agenda_t"] = time.monotonic()
            return True

        _disp["io"] = d.DisplayIO(act, get_state, write_document, kiosk.served, asyncio.sleep, show_page=show_page, write_agenda=write_agenda, served_agenda=kiosk.served_agenda)
        _disp["cfg"] = d.DisplayConfig(targets=dict(KIOSK_TARGETS or {}))
    return route_store(), _disp["io"], _disp["cfg"]


# ---------------------------------------------------------------- navigation (Needle's navigation_ops: ETA + the active route + phone push)
PHONE_NOTIFY = household.get("phone_notify_service")
PLACES = household.get("places")                                                 # {"Takoda Beer Garden": ""}: frequent destinations (canonical spelling; value = search text or "")


def stt_hotwords():
    """Names the speech recogniser should expect: the household's places plus any extra words in household.json `stt_vocabulary`."""
    return ", ".join(list(PLACES or {}) + list(household.get("stt_vocabulary") or []))
ROUTES_KEY_FILE = os.path.expanduser(household.get("google_routes_key_file"))
OFFER_TTL_S = 120.0


def nav_configured():
    """Navigation answers only when a Routes key exists; otherwise 'how long to X' goes where it always did (the model)."""
    try:
        return os.path.getsize(ROUTES_KEY_FILE) > 8
    except OSError:
        return False


def offer_fresh():
    """True for a short while after Nina offered to show a route, so a bare 'yes, show me' means that. Not consumed by unrelated requests; cleared on use."""
    return time.monotonic() - _disp.get("offer_t", -1e9) < OFFER_TTL_S


async def _home_latlng(http):
    if "home" not in _disp:
        try:
            async with http.get(HA_URL + "/api/states/zone.home", headers=_hdr(), timeout=aiohttp.ClientTimeout(total=5)) as r:
                a = (await r.json())["attributes"]
            _disp["home"] = (float(a["latitude"]), float(a["longitude"]))
        except Exception:                                                    # noqa: BLE001
            return None
    return _disp["home"]


async def nav_cfg(http):
    from needle_voice import navigation_ops as nav, executor
    hass = executor.RestHass(HA_URL, _ha_token())

    async def post_json(url, headers, body):
        async with http.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=10)) as r:
            try:
                return r.status, await r.json()
            except Exception:                                                # noqa: BLE001
                return r.status, {}

    async def notify(title, message, app_uri, web_uri):
        return await executor.execute_notify(hass, PHONE_NOTIFY, title, message, app_uri, web_uri)
    return nav.NavConfig(api_key=open(ROUTES_KEY_FILE).read().strip(), home=await _home_latlng(http) or (), notify=notify if PHONE_NOTIFY else None,
                         post_json=post_json, store=route_store(), offer_display=bool(KIOSK_TARGETS), places=dict(PLACES or {}), use_places=True, region=str(household.get("region") or ""))


def nav_owns(text):
    from needle_voice import navigation_ops as nav
    return nav_configured() and nav.owns(text, route_store())


async def _calendar_events(http, start, end):
    """Timed and all-day events from every household calendar between two datetimes, as calendar_ops.Event (empty if HA cannot be reached)."""
    from needle_voice import calendar_ops

    async def one(ent):
        try:
            async with http.get(HA_URL + "/api/calendars/" + ent, headers=_hdr(), timeout=aiohttp.ClientTimeout(total=10),
                                params={"start": start.isoformat(), "end": end.isoformat()}) as r:
                return [x for e in await r.json() for x in calendar_ops.parse_events([e], start.tzinfo, ent)]
        except Exception:                                                        # noqa: BLE001
            return []
    cals = CALENDARS or [i for i in await _states_list(http) if i.startswith("calendar.") and not any(x in i for x in household.get("calendar_exclude", None))]
    return [x for part in await asyncio.gather(*[one(c) for c in cals]) for x in part]


async def nav_request(http, text, cfg=None, events=None):
    """Spoken navigation request -> navigation_ops. Reply convention as ha_say. The ETA answer stores the route and may end with an offer to show it.
    'When should I leave (for X)' needs the calendar: the event is picked here (navigation_ops.pick_event) and its location and start go in as context."""
    import datetime as dt
    from needle_voice import navigation_ops as nav
    from needle_voice.result import Status
    cfg = cfg or await nav_cfg(http)
    ctx = None
    if nav.parse(text)[0] == "leave":
        now = dt.datetime.now().astimezone()
        evs = events if events is not None else await _calendar_events(http, now - dt.timedelta(minutes=15), now + dt.timedelta(hours=nav.HORIZON_H))
        ev, why, kind = nav.pick_event(text, evs, now)
        if ev is not None:
            ctx = nav.NavContext(ev.location, ev.start, ev.summary)
        elif not (kind == "no_match" and nav.parse(text)[1]):                    # a named PLACE with no matching event goes on to ask what time you need to be there
            return "[NOT DONE] " + why
    res = await nav.handle(text, ctx, cfg)
    if res.telemetry.get("offer"):
        _disp["offer_t"] = time.monotonic()
    if res.status is Status.SUCCESS:
        return "[DONE] " + res.speech
    return "[NOT DONE] " + (res.speech or "I couldn't work that out.")


async def kiosk_phone(http, cfg=None):
    """The viewer's Send-to-phone button: the active route's destination to the phone. True once HA accepted the push (no read-back exists)."""
    from needle_voice import navigation_ops as nav
    res = await nav.send_active(cfg or await nav_cfg(http))
    return bool(res.side_effect)


# ---------------------------------------------------------------- the agenda popup (display_ops.show_agenda + kiosk.py's agenda viewer)
AGENDA_HOLD_S = int(household.get("agenda_hold_s") or 60)


def agenda_fresh():
    """True for a couple of minutes after an agenda went to the screen, so 'and tomorrow?' is read as a calendar follow-up."""
    return time.monotonic() - _disp.get("agenda_t", -1e9) < 150.0


def agenda_base():
    """The first day of the agenda last shown (a date), or None: 'the next day' is relative to it."""
    import datetime as dt
    try:
        return dt.date.fromisoformat(((_disp.get("agenda") or {}).get("days") or [{}])[0]["date"]) if agenda_fresh() else None
    except (KeyError, ValueError, IndexError, TypeError):
        return None


def agenda_doc():
    """The agenda document the kiosk viewer fetches (None until a calendar question has been answered with a display configured)."""
    doc = _disp.get("agenda")
    if doc and doc.get("kind") == "timer" and _timer.current and doc.get("id") == _timer.current["id"]:
        return dict(doc, remaining_s=_timer.remaining())
    return doc


async def agenda_popup(http, events, days, title, now):
    from needle_voice import agenda_ops, display_ops
    try:
        doc = agenda_ops.build_document(events, days, now, hold_s=AGENDA_HOLD_S, title=title if len(days) > 1 else "")
        store, io, cfg = display_env(http)
        cfg.agenda_hold_s = AGENDA_HOLD_S
        res = await display_ops.show_agenda(io, cfg, doc)
        print("agenda popup: %s %s %s" % (res.status.value, res.reason, agenda_ops.summary(doc)), flush=True)
    except Exception as e:                                                       # noqa: BLE001 - a failed popup must never break the answer
        print("agenda popup failed:", type(e).__name__, str(e)[:80], flush=True)


def claim(text):
    """Which deterministic tool claims this request ('' = none): claims.claim with this server's state (active route, fresh offer, what is configured)."""
    import claims
    if _timer.owns(text):
        return "timer"
    return claims.claim(text, store=route_store(), offered=offer_fresh(), display=bool(KIOSK_TARGETS), nav=nav_configured(), places=PLACES, agenda_fresh=agenda_fresh(), routine_cfg=ROUTINES)


def display_owns(text, offered=False):
    """Should this utterance be answered by display_ops? Only when a display is configured."""
    from needle_voice import display_ops as d
    return bool(KIOSK_TARGETS) and d.owns(text, route_store(), offered)


async def display_request(http, text, offered=False, env=None):
    """Spoken display request -> display_ops. Same reply convention as ha_say: '[DONE] ...' only for a verified result, '[NOT DONE] ...' otherwise (relay it
    as written, never claim it happened, never retry)."""
    from needle_voice import display_ops as d
    from needle_voice.result import Status
    if not KIOSK_TARGETS and env is None:
        return "[NOT DONE] The kitchen display isn't set up for routes yet."
    store, io, cfg = env or display_env(http)
    res = await d.handle(text, store, io, cfg, offered)
    _disp["offer_t"] = -1e9                                                  # whatever was offered has now been answered
    if res.status is Status.SUCCESS:
        return "[DONE] " + res.speech
    return "[NOT DONE] " + (res.speech or "I couldn't do that on the display.")


async def kiosk_close(http, env=None):
    """The viewer's Close button: same operation as saying 'close the map'. True only if the route was seen to close."""
    from needle_voice import display_ops as d
    from needle_voice.result import Status
    store, io, cfg = env or display_env(http)
    return (await d.close_route(io, cfg)).status is Status.SUCCESS


async def random_request(http, text):
    """Spoken request -> Needle's random_ops -> the RNG service. If the service is down the answer says so: a made-up number is worse."""
    async def fetch(tool, params):
        try:
            async with http.post("%s/v1/%s" % (RNG_URL.rstrip("/"), tool), json=params, timeout=aiohttp.ClientTimeout(total=5)) as r:
                return await r.json()
        except Exception:                                                    # noqa: BLE001
            return None
    res = await random_ops.handle(text, fetch)
    return res.speech or "(I could not tell what random thing was wanted)"


async def ha_say(http, text):
    """Home requests go through NEEDLE (needle_voice/home_ops.py). Its result is a structured ToolResult and the reply is derived from it:

        [DONE] ...        verified success (or a read)           -> say it
        [NOT DONE] ...    anything else (denied, ambiguous, unavailable, failed, unverified, cancelled) -> relay the sentence as written;
                          never claim an action happened and never retry it yourself
    Only UNSUPPORTED / NO_MATCH may go on to HA's own conversation agent, and only if home_ops.fallback_gate() passes; a DENIED request
    never reaches it. The fallback's own failure text is returned as [NOT DONE], not as something to improvise around."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.environ.get("NEEDLE_DIR") or next((p for p in (os.path.join(here, "..", "needle-voice"), os.path.expanduser("~/needle-voice")) if os.path.isdir(p)), ""))
    from needle_voice import telemetry as ntel
    from needle_voice.home_ops import HomeAgent, fallback_gate
    from needle_voice.result import Status
    if "agent" not in _home:
        _home["agent"] = HomeAgent(http, HA_URL, _ha_token(), [_lex_file, _lex_ha])
    rid = ntel_id()
    _home["last_rid"] = rid
    transcript = _home.pop("transcript", "")
    ts = lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z")                      # noqa: E731
    audit = os.path.exists(MUTATION_AUDIT_FLAG)
    if audit:
        # MUTATION AUDIT MODE (flag-gated: rm the flag file to turn off). Log what was PROPOSED before anything runs, and refuse requests whose
        # tool text is self-contradictory or disagrees with what the user actually said.
        bad = audit_reject(text, transcript)
        ntel.write(HOME_LOG, {"ts": ts(), "request_id": rid, "stage": "proposed", "reply_id": _home.get("parent_rid"), "tool_text": text, "transcript": transcript, "audit_reject": bad})
        if bad:
            ntel.write(HOME_LOG, {"ts": ts(), "request_id": rid, "stage": "executed", "status": "REJECTED_BY_AUDIT", "service_calls": []})
            return "[NOT DONE] I wasn't sure I heard that right, so I didn't change anything. Please say it again."
    o = await _home["agent"].request(text, request_id=rid)
    if audit:
        t = o.telemetry
        ntel.write(HOME_LOG, {"ts": ts(), "request_id": rid, "stage": "executed", "status": o.status.value, "plan": t.get("plan"),
                              "service_calls_sent": o.side_effect, "verify": t.get("verify")})
    log_row = {"ts": ts(), **o.telemetry, "handled": bool(o.speech), "reply_id": _home.get("parent_rid")}
    try:
        if o.status == Status.SUCCESS:
            return "[DONE] " + o.speech
        if o.speech:                                                      # AMBIGUOUS / DENIED / UNAVAILABLE / FAILED / UNVERIFIED: say exactly this
            return "[NOT DONE] " + o.speech
        if o.status == Status.CANCELLED:
            return "[NOT DONE] That request was replaced by a newer one."
        ok, why = fallback_gate(text)                                     # UNSUPPORTED / NO_MATCH: common policy check before any other agent
        if not ok:
            log_row.update(fallback="blocked", fallback_gate=why)
            return "[NOT DONE] I can't do that by voice."
        log_row["fallback"] = "attempted"
        async with http.post(HA_URL + "/api/conversation/process", headers=_hdr(), timeout=aiohttp.ClientTimeout(total=20),
                             json={"text": text, "language": "en", "agent_id": "conversation.home_assistant"}) as r:
            d = await r.json()
        resp = d["response"]
        speech = resp["speech"]["plain"]["speech"]
        log_row["fallback_result"] = resp["response_type"]
        if resp["response_type"] == "error":
            return "[NOT DONE] Home Assistant could not do that: %s" % speech
        # HA's agent has no verification step: an action it reports is relayed as its claim, not as a verified result.
        return "[DONE] " + speech
    finally:
        ntel.write(HOME_LOG, log_row)


async def _state(http, entity):
    async with http.get(HA_URL + "/api/states/" + entity, headers=_hdr(), timeout=aiohttp.ClientTimeout(total=8)) as r:
        return await r.json()


async def _states_list(http):
    async with http.get(HA_URL + "/api/states", headers=_hdr(), timeout=aiohttp.ClientTimeout(total=10)) as r:
        return [s["entity_id"] for s in await r.json()]


async def _weather_entities(http):
    """Configured entities (household.json), else discovered: first weather.*, first sensor.*today*forecast*, first sensor.*tomorrow*forecast*."""
    w, t_, m = (household.get("weather_entity", None), household.get("forecast_today_sensor", None), household.get("forecast_tomorrow_sensor", None))
    if not (w and t_ and m):
        ids = await _states_list(http)
        w = w or next(i for i in ids if i.startswith("weather."))
        t_ = t_ or next(i for i in ids if i.startswith("sensor.") and "today" in i and "forecast" in i)
        m = m or next(i for i in ids if i.startswith("sensor.") and "tomorrow" in i and "forecast" in i)
    return w, t_, m


_COND = {"rainy": "rain", "pouring": "heavy rain", "partlycloudy": "partly cloudy", "clear-night": "clear", "lightning": "thunderstorms",
         "lightning-rainy": "thunderstorms with rain", "snowy": "snow", "snowy-rainy": "rain and snow", "exceptional": "unusual weather"}


def _usable(st):
    return bool(st) and st.get("state") not in (None, "", "unknown", "unavailable")


def compose_forecast(entries, date):
    """One spoken sentence from HA twice_daily forecast entries for local date 'YYYY-MM-DD', or None."""
    day = [e for e in entries if (e.get("datetime") or "")[:10] == date]
    if not day:
        return None
    conds, highs, lows, pops = [], [], [], []
    for e in day:
        c = _COND.get(e.get("condition"), e.get("condition"))
        if c and c not in conds:
            conds.append(c)
        if e.get("temperature") is not None:
            (lows if e.get("is_daytime") is False else highs).append(e["temperature"])
        if e.get("precipitation_probability") is not None:
            pops.append(e["precipitation_probability"])
    bits = [" then ".join(conds)] if conds else []
    if highs:
        bits.append("high %d" % max(highs))
    if lows:
        bits.append("low %d" % min(lows))
    if pops and max(pops) >= 20:
        bits.append("%d percent chance of precipitation" % max(pops))
    return ", ".join(bits) or None


async def _forecast_service(http, ent_w, date):
    try:
        async with http.post(HA_URL + "/api/services/weather/get_forecasts?return_response", headers=_hdr(), json={"entity_id": ent_w, "type": "twice_daily"},
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
            return compose_forecast((await r.json())["service_response"][ent_w]["forecast"], date)
    except Exception:
        return None


async def _day_forecast(http, day, primary_state, ent_w, ent_primary):
    """Forecast text for 'today'|'tomorrow'. The configured sensor first; HA's text sensors can go 'unknown' (e.g. the detailed
    ones in the evening), so fall back to any other NWS forecast sensor for that day, then HA's own forecast service."""
    if _usable(primary_state):
        return primary_state["state"]
    try:
        for i in await _states_list(http):
            if i != ent_primary and i.startswith("sensor.") and day in i and "forecast" in i:
                st = await _state(http, i)
                if _usable(st):
                    return st["state"]
    except Exception:
        pass
    import datetime as dt
    date = (dt.datetime.now().astimezone().date() + dt.timedelta(days=1 if day == "tomorrow" else 0)).isoformat()
    return await _forecast_service(http, ent_w, date) or "no forecast available right now"


async def weather(http, when="today"):
    """Real NWS data already in HA: current conditions + today/tomorrow text forecasts."""
    when = (when or "today").lower()
    ent_w, ent_t, ent_m = await _weather_entities(http)
    now, today, tomorrow = await asyncio.gather(_state(http, ent_w), _state(http, ent_t), _state(http, ent_m))
    a = now.get("attributes", {})
    cur = "Now: %s degrees, %s, humidity %s percent, wind %s mph." % (a.get("temperature"), now.get("state", "").replace("partlycloudy", "partly cloudy").replace("clear-night", "clear"),
                                                                       a.get("humidity"), a.get("wind_speed"))
    out = []
    if "tomorrow" not in when:
        out.append(cur)
        out.append("Today: " + await _day_forecast(http, "today", today, ent_w, ent_t))
    if "tomorrow" in when or "week" in when or "weekend" in when:
        out.append("Tomorrow: " + await _day_forecast(http, "tomorrow", tomorrow, ent_w, ent_m))
    return " ".join(out)


def compact_location(loc):
    """'5501 Main St\nSpringfield, VA 22041\nUSA' -> '5501 Main St, Springfield, VA 22041' (one speakable line, no country)."""
    parts = [p.strip() for p in str(loc or "").replace("\r", "").split("\n") if p.strip()]
    if parts and parts[-1].upper() in ("USA", "US", "UNITED STATES"):
        parts.pop()
    return ", ".join(parts)[:120]


def _day_label(days, now):
    d0, d1 = days[0], days[-1]
    if d0 == d1:
        rel = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get((d0 - now.date()).days)
        return "%s%s" % (rel + ", " if rel else "", d0.strftime("%A %B %-d"))
    return "%s to %s" % (d0.strftime("%A %B %-d"), d1.strftime("%A %B %-d"))


async def calendar(http, when="today"):
    """Answer a calendar question and, when a kitchen display is configured, pop the day's agenda up on it (a side effect: the answer is spoken either way).
    `when`: today | tomorrow | week | weekend | next week | a weekday name | 'date:YYYY-MM-DD' (anything agenda_ops.resolve_days understands)."""
    import datetime as dt
    from needle_voice import agenda_ops, calendar_ops
    when = (when or "today").lower()
    now = dt.datetime.now().astimezone()
    if when.startswith("date:"):
        try:
            days, title = [dt.date.fromisoformat(when[5:15])], when[5:15]
        except ValueError:
            days, title = agenda_ops.resolve_days("today", now)
    else:
        days, title = agenda_ops.resolve_days(when, now)
    multi = len(days) > 1
    start = dt.datetime.combine(days[0], dt.time.min, tzinfo=now.tzinfo)
    end = dt.datetime.combine(days[-1], dt.time.min, tzinfo=now.tzinfo) + dt.timedelta(days=1)

    async def one(ent):
        try:
            async with http.get(HA_URL + "/api/calendars/" + ent, headers=_hdr(), timeout=aiohttp.ClientTimeout(total=10),
                                params={"start": start.isoformat(), "end": end.isoformat()}) as r:
                data = await r.json()
                return [(e, ent) for e in data] if isinstance(data, list) else []     # one calendar erroring (HA answers 500 {"message": ...}) must not break the rest
        except Exception:
            return []

    cals = CALENDARS or [i for i in await _states_list(http) if i.startswith("calendar.") and not any(x in i for x in household.get("calendar_exclude", None))]
    evs = sorted((x for part in await asyncio.gather(*[one(c) for c in cals]) for x in part),
                 key=lambda p: (p[0].get("start", {}).get("dateTime") or p[0].get("start", {}).get("date") or ""))
    if KIOSK_TARGETS:                                                                  # the agenda on the kitchen screen: never delays or changes the spoken answer
        norm = [x for e, ent in evs for x in calendar_ops.parse_events([e], now.tzinfo, ent)]
        asyncio.ensure_future(agenda_popup(http, norm, days, title, now))
    if not evs:
        return "No events on the calendar for %s." % _day_label(days, now)
    lines, locs = [], []
    for e, _ in evs[:8]:
        s = e.get("start", {})
        when_s = s.get("dateTime", "")
        if when_s:
            d = dt.datetime.fromisoformat(when_s)
            when_s = d.strftime("%A %-I:%M %p") if multi else d.strftime("%-I:%M %p")
        else:
            when_s = "all day"
        lines.append("%s: %s" % (when_s, e.get("summary", "event")))
        loc = compact_location(e.get("location"))
        if loc:
            locs.append("%s: %s" % (e.get("summary", "event"), loc))
    out = "; ".join(lines) + " (these are the events for %s: summarize the events; do not just state the date)" % _day_label(days, now)   # named, so the spoken answer says 'Friday', not 'today'
    if locs:                                       # in the payload for follow-ups, but kept apart so a summary doesn't read addresses out
        out += " || LOCATIONS (private; say one only if the user asks where an event is): " + "; ".join(locs)
    return out


async def timer_popup(http):
    """Use the agenda viewer and its verified dashboard interruption path."""
    if not KIOSK_TARGETS:
        return
    from needle_voice import display_ops
    try:
        doc = _timer.document()
        if doc:
            _, io, cfg = display_env(http)
            await display_ops.show_agenda(io, cfg, doc)
    except Exception as exc:
        print("timer popup failed:", type(exc).__name__, flush=True)


async def timer_request(http, text):
    global _timer_task, _timer_ringing
    old = _timer.current
    answer, created = _timer.handle(text)
    if old is not _timer.current:
        if _timer_task:
            _timer_task.cancel()
            _timer_task = None
        _timer_ringing = False
        await timer_sound(False)
    if created:
        current = _timer.current

        async def expire():
            global _timer_ringing
            await asyncio.sleep(_timer.remaining())
            if _timer.current is current:
                _timer_ringing = True
                await timer_sound(True)
            while _timer.current is current:
                await timer_popup(http)
                await asyncio.sleep(45)
        _timer_task = asyncio.create_task(expire())
    if _timer.current:
        asyncio.create_task(timer_popup(http))
    elif old:
        # Clear a visible countdown without navigating away from another popup.
        if (_disp.get("agenda") or {}).get("kind") == "timer":
            _disp["agenda"] = None
    return "[DONE] " + answer


async def kiosk_timer_stop(http, timer_id):
    # A stale popup must never cancel a replacement timer.
    if not _timer.current or _timer.current["id"] != timer_id:
        return False
    await timer_request(http, "stop the timer")
    return True


async def clock_request(http, text):
    from clock_tool import answer
    return "[DONE] " + answer(household.get("timezone"))


async def routine_request(http, text):
    """'good morning' / 'good night': the configured devices (verified, routines.py), then the spoken reports. Same reply convention as ha_say."""
    import routines
    name = routines.parse(text)
    if name not in (ROUTINES or {}):
        return "[NOT DONE] That routine isn't set up."

    class IO:
        async def state(self, entity):
            try:
                async with http.get("%s/api/states/%s" % (HA_URL, entity), headers=_hdr(), timeout=aiohttp.ClientTimeout(total=8)) as r:
                    return await r.json() if r.status == 200 else None
            except Exception:                                                      # noqa: BLE001
                return None

        async def call(self, domain, service, entity):
            try:
                async with http.post("%s/api/services/%s/%s" % (HA_URL, domain, service), headers=_hdr(), json={"entity_id": entity},
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                    return r.status == 200
            except Exception:                                                      # noqa: BLE001
                return False

        async def sleep(self, s):
            await asyncio.sleep(s)

    speech, ok = await routines.run(name, ROUTINES, IO())
    out = ("[DONE] " if ok else "[NOT DONE] ") + speech
    reports = ROUTINES[name].get("report", [])
    for rep in reports:
        out += " || " + ("Weather: " + await weather(http, "today") if rep == "weather" else "Schedule: " + await calendar(http, "today"))
    if reports:
        out += " (say the devices result in one short sentence, then " + ", then ".join("the " + ("weather" if r == "weather" else "schedule") for r in reports) + ")"
    return out
