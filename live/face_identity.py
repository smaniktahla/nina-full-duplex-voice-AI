"""Ask the existing CompreFace service for conservative, on-demand identification."""
import asyncio
import json
import os
import re

import vision

MIN_SIMILARITY = 0.85
MIN_MARGIN = 0.10


def confirmed_candidate(faces):
    """One clearly matched face, with separation from the next enrolled subject."""
    if len(faces) != 1 or not faces[0]:
        return None
    first, *others = sorted(faces[0], key=lambda item: float(item["similarity"]), reverse=True)
    name = first["subject"]
    score = float(first["similarity"])
    runner_up = float(others[0]["similarity"]) if others else 0.0
    if score >= MIN_SIMILARITY and score - runner_up >= MIN_MARGIN and re.fullmatch(r"[^\W\d_][\w .'-]{0,39}", name):
        return name.replace("_", " ")
    return None


async def recognize(jpeg, host):
    process = await asyncio.create_subprocess_exec(
        "ssh", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oConnectTimeout=3",
        host, "python3 ~/.local/lib/nina/compreface.py",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(jpeg), 12)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        return None
    return confirmed_candidate(json.loads(stdout))


async def identify(first_frame):
    host = os.environ.get("NINA_COMPREFACE_SSH", "")
    if not first_frame or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@-]*", host):
        return None
    try:
        first = await recognize(first_frame, host)
        if first is None:
            return None
        await asyncio.sleep(1)
        second_frame = await vision.fresh_snapshot()
        if second_frame is None or second_frame == first_frame:
            return None
        second = await recognize(second_frame, host)
        return first if second == first else None
    except (OSError, ValueError, TypeError, KeyError, asyncio.TimeoutError) as error:
        print("CompreFace identification unavailable:", type(error).__name__, flush=True)
        return None
