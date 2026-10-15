"""Household-specific settings, kept OUT of the repository.

Looked up in this order: environment variable -> private JSON file -> placeholder default.
The JSON file is `$NINA_HOUSEHOLD` or `~/.config/nina/household.json` (never committed; see household.example.json).
"""
import json
import os

_PATH = os.environ.get("NINA_HOUSEHOLD") or os.path.expanduser("~/.config/nina/household.json")
try:
    _DATA = json.load(open(_PATH))
except Exception:                                                    # noqa: BLE001 - no file: env vars / placeholders only
    _DATA = {}

DEFAULTS = {
    "ha_url": "http://homeassistant.local:8123",
    "searx_url": "http://searxng.local:8091",
    "hermes_url": "http://hermes.local:8643",
    "hermes_ssh_host": "",                      # ssh alias to read Hermes' API key from; "" = read ~/.hermes/config.yaml locally
    "wake_host": "homeassistant.local",         # HA's openWakeWord (Wyoming) host
    "wake_port": 10400,
    "ha_lexicon_ssh": "",                       # "alias:/path/to/needle_voice/lexicon.json" to merge Needle's household vocabulary
    "calendars": [],                            # calendar entity ids; empty = every calendar.* entity not excluded below
    "calendar_exclude": ["holidays", "astronomy", "weather", "birthdays"],
    "weather_entity": "",                       # weather.* entity for current conditions; empty = first weather.* entity
    "forecast_today_sensor": "",                # text forecast sensors; empty = first sensor.*today*forecast*
    "forecast_tomorrow_sensor": "",
}


def get(key, env=None):
    if env and os.environ.get(env):
        v = os.environ[env]
        return type(DEFAULTS[key])(v) if not isinstance(DEFAULTS[key], list) else v.split(",")
    return _DATA.get(key, DEFAULTS[key])
