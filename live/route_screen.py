"""Show the active route on the Browser Mod display associated with a voice session."""
import json
import os
import re

import aiohttp
import kiosk


def _screens():
    return json.loads(os.environ.get("NINA_ROUTE_SCREENS", "{}"))


def declared_screen(headers):
    """The screen a service client says it is (X-Nina-Screen), but only a Browser Mod id we already know and only from a service-token client.
    This is stable when the machine's IP changes; the IP map below stays as the fallback for browser clients."""
    name = headers.get("X-Nina-Screen", "")
    if not headers.get("X-Nina-Service") or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        return None
    screens = _screens()
    known = set(screens.get("named", {}).values()) | set(screens.get("peers", {}).values())
    return name if name in known else None


def target(peer, text="", screen=None):
    screens = _screens()
    for name, browser in screens.get("named", {}).items():
        if re.search(r"\b(?:on|to) (?:the |my )?" + re.escape(name) + r"(?: screen| display| kiosk)?\b", text, re.I):
            return browser
    return screen or screens.get("peers", {}).get(peer)


async def show(http, browser):
    import tools

    if not re.fullmatch(r"[a-zA-Z0-9_-]+", browser):
        return "[NOT DONE] That display isn't configured."
    if tools.route_store().get() is None:
        return "[NOT DONE] I don't have a route to show yet."
    base = os.environ.get("NINA_CAMERA_POPUP_BASE")
    if not base:
        return "[NOT DONE] The route viewer isn't configured."
    sensor = "sensor." + browser.lower().replace("-", "_") + "_browser_path"
    _, io, _ = tools.display_env(http)
    if await io.get_state(sensor) in (None, "", "unavailable", "unknown"):
        return "[NOT DONE] That screen isn't connected to Home Assistant."
    url = kiosk.make_link(base, days=1 / 24) + "?popup=1"
    async with http.post(tools.HA_URL + "/api/services/browser_mod/popup", headers=tools._hdr(),
                         timeout=aiohttp.ClientTimeout(total=4), json={
                             "browser_id": [browser], "title": "Directions", "size": "wide",
                             "content": {"type": "iframe", "url": url, "aspect_ratio": "65%"},
                         }) as response:
        if response.status != 200:
            return "[NOT DONE] Home Assistant couldn't open the route on that screen."
    return "[DONE] I sent the map to that screen."
