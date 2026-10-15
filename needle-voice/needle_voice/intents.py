"""The fixed intent vocabulary and the ONLY services it may map to.

Qwen never names a service. An intent maps to a hard-coded (domain -> service)
table; a target's domain must be in that table or the request escalates.
Pure data, stdlib only.
"""

from __future__ import annotations

INTENTS: dict[str, dict] = {
    "turn_on": {"kind": "write", "services": {"light": "turn_on", "switch": "turn_on", "fan": "turn_on",
                "media_player": "turn_on", "input_boolean": "turn_on", "scene": "turn_on"}, "area_domains": ("light",)},
    "turn_off": {"kind": "write", "services": {"light": "turn_off", "switch": "turn_off", "fan": "turn_off",
                 "media_player": "turn_off", "input_boolean": "turn_off"}, "area_domains": ("light",)},
    "toggle": {"kind": "write", "services": {"light": "toggle", "switch": "toggle", "fan": "toggle",
               "input_boolean": "toggle"}, "area_domains": ("light",)},
    "set_brightness": {"kind": "write", "services": {"light": "turn_on"}, "area_domains": ("light",),
                       "args": {"brightness": ("int", 0, 100)}, "required": ("brightness",)},
    "set_temperature": {"kind": "write", "services": {"climate": "set_temperature"}, "area_domains": (),
                        "args": {"temperature": ("float", 45, 95)}, "required": ("temperature",)},
    "lock": {"kind": "write", "services": {"lock": "lock"}, "area_domains": ()},
    "unlock": {"kind": "write", "services": {"lock": "unlock"}, "area_domains": (), "sensitive": True},
    "open": {"kind": "write", "services": {"cover": "open_cover"}, "area_domains": (), "sensitive": True},
    "close": {"kind": "write", "services": {"cover": "close_cover"}, "area_domains": ()},
    "run_script": {"kind": "write", "services": {"script": "turn_on"}, "area_domains": ()},
    "media_play": {"kind": "write", "services": {"media_player": "media_play"}, "area_domains": ()},
    "media_pause": {"kind": "write", "services": {"media_player": "media_pause"}, "area_domains": ()},
    "media_stop": {"kind": "write", "services": {"media_player": "media_stop"}, "area_domains": ()},
    "get_state": {"kind": "read", "read_domains": ("light", "switch", "fan", "sensor", "binary_sensor", "lock",
                  "cover", "climate", "media_player", "weather", "input_boolean"), "area_domains": ()},
    "get_temperature": {"kind": "read", "read_domains": ("sensor", "climate", "weather"), "area_domains": ()},
    "get_weather": {"kind": "read", "read_domains": ("weather",), "area_domains": ()},
}

ALL_TARGET_DOMAINS = frozenset(
    d for spec in INTENTS.values() for d in (list(spec.get("services", {})) + list(spec.get("read_domains", ())))
)
INTENT_NAMES = tuple(INTENTS)
