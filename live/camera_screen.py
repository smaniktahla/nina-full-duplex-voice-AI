"""Show the configured camera's live view on a registered screen."""
import os
import re

import aiohttp
import route_screen
import tools


def requested(text):
    words = " ".join(re.findall(r"[a-z]+", text.lower()))
    return bool(os.environ.get("NINA_CAMERA_LIVE_URL") and re.fullmatch(
        r"(?:(?:hey )?nina )?(?:show me what you see(?: right now)?|show me the live (?:camera|feed))",
        words,
    ))


async def show(http, peer, screen=None):
    browser = route_screen.target(peer, screen=screen)
    if not browser:
        return "I couldn't identify this screen for the live camera view."
    sensor = "sensor." + browser.lower().replace("-", "_") + "_browser_path"
    _, io, _ = tools.display_env(http)
    if await io.get_state(sensor) in (None, "", "unavailable", "unknown"):
        return "This screen isn't connected to Home Assistant."
    async with http.post(tools.HA_URL + "/api/services/browser_mod/popup", headers=tools._hdr(),
                         timeout=aiohttp.ClientTimeout(total=4), json={
                             "browser_id": [browser], "title": "Kinect live view", "size": "wide",
                             "timeout": 60000,
                             "content": {"type": "iframe", "url": os.environ["NINA_CAMERA_LIVE_URL"],
                                         "aspect_ratio": "65%"},
                         }) as response:
        if response.status != 200:
            return "I couldn't open the live camera view."
    return "I sent the live camera view to this screen."
