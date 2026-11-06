"""Spoken routines ("good morning", "good night"): a fixed list of devices switched on or off, then optional spoken reports.

Deterministic, no model in the loop. What a routine may touch is the household's own list (`routines` in the private household.json), limited here to
lights, switches and media players, and every call is checked against the entity's state afterwards: the reply only says "turned on" for what was seen to change.

  "routines": {"morning": {"turn_on": ["light.x", "switch.y"], "report": ["weather", "calendar"]},
               "night":   {"turn_off": ["switch.y", "media_player.tv"]}}

or, so the device list is edited in Home Assistant's own script editor instead of this file:

  "routines": {"morning": {"script": "script.good_morning", "report": ["weather", "calendar"]},
               "night":   {"script": "script.good_night"}}

With `script`, Nina runs the HA script, then reads the script's own simple on/off steps (homeassistant/light/switch/media_player turn_on/turn_off) and
checks those devices ended in the right state. Steps it cannot read (templates, choose, delays) still run, they just are not verified.

Pure: HA access is injected (`io`), so the logic is tested without a house.
"""
import asyncio
import re

DOMAINS = {"light", "switch", "media_player"}
ACTIONS = {"turn_on": "on", "turn_off": "off"}
REPORTS = ("weather", "calendar")
_PHRASE = re.compile(r"(?:(?:hey )?nina )?(?:please )?good ?(morning|night)(?: nina)?")
_BARE_MORNING = re.compile(r"(?:(?:hey )?nina )?morning(?: nina)?")        # speech recognition sometimes clips the leading 'good'


def parse(text):
    """'morning' | 'night' | '' for a bare greeting-style request. Whole utterance only: 'good morning, what's the weather' is a weather question."""
    t = " ".join(re.sub(r"[^a-z' ]", " ", (text or "").lower().replace("’", "'")).split())
    m = _PHRASE.fullmatch(t)
    return m.group(1) if m else "morning" if _BARE_MORNING.fullmatch(t) else ""


def owns(text, cfg):
    """The routine this utterance names, if the household configured it ('' = not ours)."""
    name = parse(text)
    return name if name and name in (cfg or {}) else ""


def problems(cfg):
    """Config mistakes worth failing loudly on at startup: unknown action/report, entity outside the allowed domains."""
    out = []
    for name, r in (cfg or {}).items():
        for key, val in r.items():
            if key == "report":
                out += ["%s: unknown report %r" % (name, x) for x in val if x not in REPORTS]
            elif key == "script":
                if not str(val).startswith("script.") or val.count(".") != 1:
                    out.append("%s: script must be a script.<id> entity, got %r" % (name, val))
                if any(k in r for k in ACTIONS):
                    out.append("%s: use either script or turn_on/turn_off, not both" % name)
            elif key in ACTIONS:
                out += ["%s: %s not allowed in %s" % (name, e, key) for e in val if e.split(".")[0] not in DOMAINS]
            else:
                out.append("%s: unknown key %r" % (name, key))
    return out


def _is(domain, want, state):
    if want == "on":
        return state not in ("off", "standby", "unavailable", "unknown") if domain == "media_player" else state == "on"
    return state in ("off", "standby") if domain == "media_player" else state == "off"


async def _one(io, action, entity, settle_s):
    """-> (entity, label, outcome) with outcome in verified | already | unavailable | not_observed | call_failed."""
    domain, want = entity.split(".")[0], ACTIONS[action]
    st = await io.state(entity)
    label = ((st or {}).get("attributes") or {}).get("friendly_name") or entity
    if st is None or st.get("state") in ("unavailable", "unknown"):
        return entity, label, "unavailable"
    if _is(domain, want, st.get("state")):
        return entity, label, "already"
    if not await io.call(domain, action, entity):
        return entity, label, "call_failed"
    return entity, label, await _wait(io, domain, want, entity, settle_s)


async def _wait(io, domain, want, entity, settle_s):
    """Poll until the entity reaches the wanted state: 'verified', else 'not_observed'."""
    waited = 0.0
    while waited <= settle_s:
        await io.sleep(0.5)
        waited += 0.5
        st = await io.state(entity)
        if st and _is(domain, want, st.get("state")):
            return "verified"
    return "not_observed"


def script_targets(config):
    """[(action, entity)] for the simple on/off steps of an HA script config. Anything else (choose, templates, delays) is skipped: it cannot be verified from state."""
    out = []
    for step in (config or {}).get("sequence") or []:
        if not isinstance(step, dict) or step.get("enabled") is False:
            continue
        dom, _, act = str(step.get("action") or step.get("service") or "").partition(".")
        if dom not in DOMAINS | {"homeassistant"} or act not in ACTIONS:
            continue
        ents = (step.get("target") or {}).get("entity_id") or (step.get("data") or {}).get("entity_id") or step.get("entity_id") or []
        for e in ([x.strip() for x in ents.split(",")] if isinstance(ents, str) else ents):
            if isinstance(e, str) and e.split(".")[0] in DOMAINS:
                out.append((act, e))
    return out


async def _after_script(io, action, entity, settle_s):
    domain, want = entity.split(".")[0], ACTIONS[action]
    st = await io.state(entity)
    label = ((st or {}).get("attributes") or {}).get("friendly_name") or entity
    if st is None or st.get("state") in ("unavailable", "unknown"):
        return entity, label, "unavailable"
    if _is(domain, want, st.get("state")):
        return entity, label, "verified"
    return entity, label, await _wait(io, domain, want, entity, settle_s)


async def run(name, cfg, io, settle_s=5.0):
    """Do the routine's device steps. -> (speech, ok): speech lists what did NOT work by name; ok is False if anything failed."""
    r = (cfg or {}).get(name) or {}
    if r.get("script"):
        sid = r["script"]
        targets = script_targets(await io.script_config(sid))
        if not await io.run_script(sid):
            return "The %s script did not run." % sid.split(".", 1)[1].replace("_", " "), False
        if not targets:
            return "I ran the %s script, but I can't check what it did." % sid.split(".", 1)[1].replace("_", " "), True
        jobs, res = targets, await asyncio.gather(*[_after_script(io, a, e, settle_s) for a, e in targets])
    else:
        jobs = [(a, e) for a in ACTIONS for e in r.get(a, [])]
        res = await asyncio.gather(*[_one(io, a, e, settle_s) for a, e in jobs])
    good = [x for x in res if x[2] in ("verified", "already")]
    bad = [x for x in res if x[2] not in ("verified", "already")]
    acts = {a for a, _ in jobs}
    verb = "on" if acts == {"turn_on"} else "off" if acts == {"turn_off"} else "set"
    speech = "%d of %d devices are %s." % (len(good), len(res), verb) if bad else "All %d devices are %s." % (len(res), verb)
    if bad:
        why = {"unavailable": "unavailable", "call_failed": "would not accept the command", "not_observed": "did not change"}
        speech += " Problem: " + "; ".join("%s %s" % (label, why[o]) for _, label, o in bad) + "."
    return speech, not bad
