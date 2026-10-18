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
    "docmost_mcp_url": "",                      # DocMost MCP endpoint (JSON-RPC over HTTP), e.g. http://<host>:8012/mcp ; empty = notes feature off
    "docmost_parent_id": "",                    # optional page id the conversation notes are created under
    "notes_default_user": "",                   # who is speaking, until speaker recognition can say (shown in every note)
    "rng_url": "http://127.0.0.1:8794",         # the shared random-number service (coin, dice, numbers, picks); never invent a random result
    "kiosk_targets": {},                         # {"kitchen": "sensor.<browser id>_browser_path"}: displays Nina may show routes on (empty = route display off)
    "route_store_path": "~/.local/share/needle_voice/route_store.json",   # the active route (holds home coordinates: private, never in the repo)
    "google_routes_key_file": "~/.config/nina/google_routes_key",   # Google Routes API key (chmod 600, never in the repo); missing = navigation off
    "places": {},                               # frequent destinations {"Canonical Name": "search text for Google, or empty"}: heard names are matched to these
    "stt_vocabulary": [],                       # extra words/names the speech recogniser should expect (places are included automatically)
    "phone_notify_service": "",                 # "mobile_app_<phone>": where 'send it to my phone' pushes directions (empty = off, the button is hidden)
    "timezone": "America/New_York",             # note dates/times are written in this zone
}


def get(key, env=None):
    if env and os.environ.get(env):
        v = os.environ[env]
        return type(DEFAULTS[key])(v) if not isinstance(DEFAULTS[key], list) else v.split(",")
    return _DATA.get(key, DEFAULTS[key])
