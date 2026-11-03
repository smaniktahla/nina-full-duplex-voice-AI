"""One-shot Home Assistant websocket command (auth, send, read result). Injected into executor.RestHass as `ws_transport`; kept here because
needle_voice/executor.py is stdlib-only. Carries no policy: what may be sent is decided by the executor's fixed tables."""

from __future__ import annotations

import asyncio

import aiohttp

from needle_voice.executor import ExecutionError


async def ws_command(url: str, token: str, payload: dict, timeout: float = 10.0):
    try:
        async with aiohttp.ClientSession() as sess, sess.ws_connect(url, timeout=timeout) as ws:
            if (await ws.receive_json(timeout=timeout)).get("type") != "auth_required":
                raise ExecutionError("ws_unexpected")
            await ws.send_json({"type": "auth", "access_token": token})
            if (await ws.receive_json(timeout=timeout)).get("type") != "auth_ok":
                raise ExecutionError("rest_auth_failed")
            await ws.send_json({"id": 1, **payload})
            res = await ws.receive_json(timeout=timeout)
    except ExecutionError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        raise ExecutionError("ws_failed:" + type(e).__name__) from e
    if not res.get("success"):
        raise ExecutionError("ws_rejected:" + str((res.get("error") or {}).get("code", "")))
    return res.get("result")
