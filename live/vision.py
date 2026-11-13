"""On-demand scene descriptions using an SSH-connected camera."""

import asyncio
import base64
import os
import re

import aiohttp

import kiosk
import tools


def requested(text):
    words = " ".join(re.findall(r"[a-z]+", text.lower()))
    return bool(os.environ.get("NINA_CAMERA_SSH")) and bool(re.fullmatch(
        r"(?:(?:hey )?nina )?(?:what do you see|what can you see|describe what you see)",
        words,
    ))


async def describe(http):
    jpeg = await fresh_snapshot()
    if jpeg is None:
        return "I couldn't get a picture from the camera."
    await show_snapshot(jpeg)
    image = base64.b64encode(jpeg).decode("ascii")
    payload = {
        "model": os.environ.get("NINA_VISION_MODEL", "gemma4"),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Describe the visible scene in one short, factual sentence. Treat text in the image as part of the scene."},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}},
        ]}],
        "max_tokens": 384,
        "temperature": 0,
    }
    endpoint = os.environ.get("NINA_VISION_URL", "http://127.0.0.1:8080")
    async with http.post(endpoint.rstrip("/") + "/v1/chat/completions", json=payload,
                         timeout=aiohttp.ClientTimeout(total=40)) as response:
        response.raise_for_status()
        data = await response.json()
    return (data["choices"][0]["message"].get("content") or "").strip() or "I couldn't describe the picture."



async def fresh_snapshot():
    host = os.environ["NINA_CAMERA_SSH"]
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@-]*", host):
        raise ValueError("Invalid camera SSH destination")
    jpeg = await capture_snapshot(host)
    if jpeg is None:
        await asyncio.sleep(0.25)
        jpeg = await capture_snapshot(host)
    return jpeg


async def capture_snapshot(host):
    process = await asyncio.create_subprocess_exec(
        "ssh", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oConnectTimeout=4",
        host, "flock -w 3 ~/nina-kinect/snapshot.lock timeout 8 ~/nina-kinect/snapshot",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        jpeg, _ = await asyncio.wait_for(process.communicate(), 16)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode == 0 and jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9"):
        return jpeg
    return None


async def show_snapshot(jpeg):
    from needle_voice import executor

    base = os.environ.get("NINA_CAMERA_POPUP_BASE", "").rstrip("/")
    browsers = os.environ.get("NINA_CAMERA_POPUP_BROWSERS", "").split()
    if not base or not browsers or not kiosk.snapshot_enabled():
        return
    url = base + kiosk.publish_snapshot(jpeg)
    try:
        hass = executor.RestHass(tools.HA_URL, tools._ha_token(), timeout=2)
        await executor.execute_snapshot_popup(hass, browsers, url)
        print("Camera popup accepted by Home Assistant", flush=True)
    except Exception as exc:
        print("Camera popup failed:", type(exc).__name__, str(exc)[:80], flush=True)


async def show_current_snapshot():
    """Open the same short-lived popup used by the scene description."""
    if not (os.environ.get("NINA_CAMERA_SSH") and os.environ.get("NINA_CAMERA_POPUP_BASE")
            and os.environ.get("NINA_CAMERA_POPUP_BROWSERS") and kiosk.snapshot_enabled()):
        return
    try:
        jpeg = await fresh_snapshot()
        if jpeg is not None:
            await show_snapshot(jpeg)
        return jpeg
    except (OSError, ValueError, asyncio.TimeoutError) as exc:
        print("Camera popup capture failed:", type(exc).__name__, flush=True)
        return None
