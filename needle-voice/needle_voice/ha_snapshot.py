"""Home Assistant readers: the exposed-entity snapshot and the runtime files.

The only HA imports in the pure-logic half of the component, all lazy so the
rest of the package imports (and tests) without Home Assistant installed.
"""

from __future__ import annotations

import json
import logging
import os

_LOGGER = logging.getLogger(__name__)
_lex_cache: dict[str, tuple[float, dict]] = {}


def build_entity_snapshot(hass) -> dict | None:
    """Same set extended_openai_conversation puts in its prompt: entities
    exposed to the conversation assistant. Names/aliases/area links only.
    MUST run on the event loop. Returns None on any failure (callers then
    escalate to Hermes)."""
    try:
        from homeassistant.components import conversation as ha_conversation
        from homeassistant.components.homeassistant.exposed_entities import async_should_expose
        from homeassistant.helpers import area_registry as ar
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        ent_reg, dev_reg, area_reg = er.async_get(hass), dr.async_get(hass), ar.async_get(hass)
        entities = []
        skipped = 0
        for state in hass.states.async_all():
            try:
                eid = state.entity_id
                if not async_should_expose(hass, ha_conversation.DOMAIN, eid):
                    continue
                entry = ent_reg.async_get(eid)
                # HA 2026.9: registry `aliases` may hold a ComputedNameType marker next to
                # plain strings, so sorted() on the raw set raises. Keep strings only.
                aliases = sorted(a for a in entry.aliases if isinstance(a, str)) if entry is not None and entry.aliases else []
                area_id = entry.area_id if entry is not None else None
                if area_id is None and entry is not None and entry.device_id:
                    dev = dev_reg.async_get(entry.device_id)
                    area_id = dev.area_id if dev is not None else None
                entities.append({"entity_id": eid, "name": state.name, "aliases": aliases, "area_id": area_id})
            except Exception:  # noqa: BLE001 - one odd entity must not blank the whole snapshot
                skipped += 1
        if skipped:
            _LOGGER.warning("needle_voice: skipped %d entities while building the snapshot", skipped)
        areas = [
            {"area_id": a.id, "name": a.name, "aliases": sorted(x for x in a.aliases if isinstance(x, str))}
            for a in area_reg.async_list_areas()
        ]
        return {"entities": entities, "areas": areas}
    except Exception:  # noqa: BLE001
        _LOGGER.exception("needle_voice: could not build entity snapshot")
        return None


def read_runtime_files(lexicon_path: str, passthrough_path: str) -> tuple[dict, bool]:
    """Blocking file I/O: run via hass.async_add_executor_job. Lexicon is
    cached by mtime; any error yields an empty lexicon (never an exception)."""
    passthrough = False
    lex: dict = {}
    try:
        passthrough = os.path.exists(passthrough_path)
    except Exception:  # noqa: BLE001
        passthrough = False
    try:
        mtime = os.stat(lexicon_path).st_mtime
        cached = _lex_cache.get(lexicon_path)
        if cached and cached[0] == mtime:
            lex = cached[1]
        else:
            with open(lexicon_path, encoding="utf-8") as f:
                data = json.load(f)
            lex = data if isinstance(data, dict) else {}
            _lex_cache[lexicon_path] = (mtime, lex)
    except FileNotFoundError:
        lex = {}
    except Exception:  # noqa: BLE001
        _LOGGER.warning("needle_voice: lexicon unreadable, ignoring")
        lex = {}
    return lex, passthrough
