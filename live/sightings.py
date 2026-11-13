"""Recent, metadata-only person sightings from Frigate."""
import asyncio
from contextlib import closing
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

import aiohttp

DB = Path.home() / ".local/share/nina/sightings.sqlite3"
KEEP_SECONDS = 7 * 86400
CAMERA = os.environ.get("NINA_SIGHTINGS_CAMERA", "")
ROOM = os.environ.get("NINA_SIGHTINGS_ROOM", "office")
BASE = os.environ.get("NINA_FRIGATE_URL", "")
PREFIX = os.environ.get("NINA_FRIGATE_TOPIC", "frigate")
SCHEMA = """
CREATE TABLE IF NOT EXISTS sighting (
    id TEXT PRIMARY KEY, camera TEXT NOT NULL,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sighting_camera_time ON sighting (camera, last_seen);
CREATE TABLE IF NOT EXISTS presence (
    camera TEXT PRIMARY KEY, checked REAL NOT NULL, online INTEGER NOT NULL, count INTEGER
);
"""


def ingest(db, event):
    data = event.get("after") or {}
    if data.get("camera") != CAMERA or data.get("label") != "person" or data.get("false_positive"):
        return
    event_id = data.get("id")
    start = data.get("start_time")
    end = data.get("end_time") or data.get("frame_time") or start
    if (not isinstance(event_id, str) or not event_id
            or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in (start, end))
            or not time.time() - KEEP_SECONDS <= start <= end <= time.time() + 60):
        return
    with db:
        db.execute("""
            INSERT INTO sighting VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET last_seen=max(last_seen, excluded.last_seen)
        """, (event_id, CAMERA, start, end))


def intent(text):
    if not CAMERA:
        return ""
    words = " ".join(re.findall(r"[a-z]+", text.lower()))
    words = re.sub(r"^(?:hey )?nina ", "", words)
    room = re.escape(ROOM.lower())
    if re.fullmatch(r"(?:who (?:do|can) you see|who(?: s| is) (?:there|in (?:the )?" + room + "))", words):
        return "identity"
    suffix = rf"(?: there| in the {room})?"
    if re.fullmatch(r"(?:is anyone|is anybody|is someone) (?:there|in (?:the )?" + room + ")", words):
        return "presence"
    if re.fullmatch(r"when did you last see (?:someone|anyone)" + suffix, words):
        return "last_seen"
    return ""


def read_status(db_path):
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            current = db.execute("SELECT checked, online, count FROM presence WHERE camera=?", (CAMERA,)).fetchone()
            last = db.execute("SELECT max(last_seen) FROM sighting WHERE camera=? AND last_seen>=?",
                              (CAMERA, time.time() - KEEP_SECONDS)).fetchone()[0]
    except sqlite3.Error:
        return None
    online = current and current[1] and time.time() - current[0] < 20
    return bool(online), current[2] if online else None, last


def format_answer(kind, status):
    if status is None:
        return "My camera sightings are unavailable right now."
    online, count, last = status
    if kind in ("presence", "identity"):
        if not online:
            return f"The {ROOM} camera feed or person detector is offline."
        if count is None:
            return f"The {ROOM} camera is online. I'm still waiting for its current person count."
        if not count:
            return f"I don't see anyone in the {ROOM}."
        people = "one person" if count == 1 else f"{count} people"
        result = f"I can see {people} in the {ROOM}."
        if kind == "identity":
            result += " I don't have a confirmed identity."
        return result
    if count:
        return f"I can see {'someone' if count == 1 else 'people'} in the {ROOM} now."
    if last is None:
        return f"I have no recorded person sightings in the {ROOM} in the past seven days."
    when = datetime.fromtimestamp(last).strftime("%A, %B %-d at %-I:%M %p")
    return f"I last saw someone in the {ROOM} on {when}."


async def current_count(http):
    """Frigate MQTT counts are sent on change, so bootstrap from its live activity."""
    async with asyncio.timeout(4):
        async with http.ws_connect(BASE.rstrip("/") + "/ws") as ws:
            await ws.send_json({"topic": "onConnect", "message": ""})
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = message.json()
                if data.get("topic") != "camera_activity":
                    continue
                activity = data["payload"]
                if isinstance(activity, str):
                    activity = json.loads(activity)
                objects = activity.get(CAMERA, {}).get("objects")
                if objects is not None:
                    return sum(obj.get("label") in ("person", "person-verified") for obj in objects)
    return None


async def collect(db, http):
    import tools

    topics = {1: f"{PREFIX}/events", 2: f"{PREFIX}/{CAMERA}/person", 3: f"{PREFIX}/available"}
    count = None
    available = None

    async def health():
        nonlocal count
        while True:
            online = False
            try:
                async with http.get(BASE.rstrip("/") + "/api/stats") as response:
                    response.raise_for_status()
                    stats = await response.json()
                camera = stats.get("cameras", {}).get(CAMERA, {})
                online = available != "offline" and camera.get("camera_fps", 0) > 0 and camera.get("detection_enabled", True)
                if online and count is None:
                    initial_count = await current_count(http)
                    if count is None:
                        count = initial_count
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                pass
            if not online:
                count = None
            with db:
                db.execute("INSERT OR REPLACE INTO presence VALUES (?, ?, ?, ?)",
                           (CAMERA, time.time(), bool(online), count if online else None))
                db.execute("DELETE FROM sighting WHERE last_seen < ?", (time.time() - KEEP_SECONDS,))
            await asyncio.sleep(5)

    async with http.ws_connect(tools.HA_URL + "/api/websocket", heartbeat=15) as ws:
        async with asyncio.timeout(10):
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": tools._ha_token()})
            if (await ws.receive_json()).get("type") != "auth_ok":
                raise RuntimeError("Home Assistant authentication failed")
            for subscription, topic in topics.items():
                await ws.send_json({"id": subscription, "type": "mqtt/subscribe", "topic": topic})
        task = asyncio.create_task(health())
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = message.json()
                if data.get("type") == "result" and not data.get("success"):
                    raise RuntimeError("MQTT subscription refused")
                if data.get("type") != "event":
                    continue
                payload = data["event"].get("payload")
                try:
                    if data.get("id") == 1:
                        ingest(db, json.loads(payload))
                    elif data.get("id") == 2:
                        count = max(0, int(payload))
                    elif data.get("id") == 3:
                        available = payload
                except (ValueError, TypeError):
                    continue
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with db:
                db.execute("INSERT OR REPLACE INTO presence VALUES (?, ?, 0, NULL)", (CAMERA, time.time()))


async def main():
    if not CAMERA or not BASE:
        raise SystemExit("Set NINA_SIGHTINGS_CAMERA and NINA_FRIGATE_URL")
    os.umask(0o077)
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB)) as db:
        db.executescript(SCHEMA)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
            while True:
                try:
                    await collect(db, http)
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
                    print("Sightings subscription will reconnect:", type(error).__name__, flush=True)
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())


async def answer_with_popup(kind):
    if kind in ("presence", "identity"):
        import vision

        status, jpeg = await asyncio.gather(asyncio.to_thread(read_status, DB), vision.show_current_snapshot())
        if kind == "identity" and status is not None and status[1] == 1:
            from face_identity import identify

            name = await identify(jpeg)
            if name:
                return f"I recognized {name} in the {ROOM}."
        return format_answer(kind, status)
    return format_answer(kind, await asyncio.to_thread(read_status, DB))
