"""Tools for the live voice assistant. Both run in the background; the conversation loop never waits for them.

  search : SearXNG API wrapper (docker2 :8091) first, `ddgs` meta-search fallback (SearXNG's upstream engines are currently
           CAPTCHA'd / suspended, so the fallback is what answers today)
  hermes : an agent on AI1 (:8643, its own llama-server) for deeper, multi-step thinking. AI1 on purpose: AI2's Hermes shares the
           single llama slot the voice loop runs on, so a long agent run there would stall every conversation tick.
"""
import asyncio
import json
import os
import subprocess
import sys
import time

import aiohttp

import household
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


async def calendar(http, when="today"):
    import datetime as dt
    when = (when or "today").lower()
    now = dt.datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "tomorrow" in when:
        start += dt.timedelta(days=1)
    end = start + dt.timedelta(days=7 if "week" in when else 1)

    async def one(ent):
        try:
            async with http.get(HA_URL + "/api/calendars/" + ent, headers=_hdr(), timeout=aiohttp.ClientTimeout(total=10),
                                params={"start": start.isoformat(), "end": end.isoformat()}) as r:
                return [(e, ent) for e in await r.json()]
        except Exception:
            return []

    cals = CALENDARS or [i for i in await _states_list(http) if i.startswith("calendar.") and not any(x in i for x in household.get("calendar_exclude", None))]
    evs = sorted((x for part in await asyncio.gather(*[one(c) for c in cals]) for x in part),
                 key=lambda p: (p[0].get("start", {}).get("dateTime") or p[0].get("start", {}).get("date") or ""))
    if not evs:
        return "No events on the calendar for %s." % when
    lines = []
    for e, _ in evs[:8]:
        s = e.get("start", {})
        when_s = s.get("dateTime", "")
        if when_s:
            d = dt.datetime.fromisoformat(when_s)
            when_s = d.strftime("%A %-I:%M %p") if "week" in when else d.strftime("%-I:%M %p")
        else:
            when_s = "all day"
        lines.append("%s: %s" % (when_s, e.get("summary", "event")))
    return "; ".join(lines)
