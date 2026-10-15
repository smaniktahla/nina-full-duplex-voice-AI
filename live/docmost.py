"""Minimal DocMost client over the DocMost MCP HTTP endpoint (JSON-RPC 2.0, replies as server-sent events or plain JSON).

Only what Nina needs: create a note, optionally under a parent page. The endpoint comes from household.json (`docmost_mcp_url`),
so nothing about the DocMost host is in the repository.
"""
import json
import re

import aiohttp

import household

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class DocmostError(RuntimeError):
    pass


def configured() -> bool:
    return bool(household.get("docmost_mcp_url"))


async def _rpc(http: aiohttp.ClientSession, url: str, name: str, arguments: dict, timeout: float = 30.0) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    async with http.post(url, json=body, headers={"Accept": "application/json, text/event-stream"},
                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        raw = await r.text()
        if r.status >= 400:
            raise DocmostError("DocMost returned HTTP %d" % r.status)
    payload = None
    for line in raw.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line[5:].strip())
            break
    if payload is None:
        try:
            payload = json.loads(raw)
        except ValueError:
            raise DocmostError("unreadable reply from DocMost")
    if "error" in payload:
        raise DocmostError(str(payload["error"])[:200])
    res = payload.get("result") or {}
    text = "".join(c.get("text", "") for c in res.get("content", []))
    if res.get("isError"):
        raise DocmostError(text[:200] or "DocMost reported an error")
    return {"text": text}


async def create_note(http: aiohttp.ClientSession, title: str, content: str, parent_id: str = "") -> dict:
    """-> {"id": <page uuid or "">, "text": <server message>}. Raises DocmostError."""
    url = household.get("docmost_mcp_url")
    if not url:
        raise DocmostError("DocMost is not configured (docmost_mcp_url in household.json)")
    args = {"title": title, "content": content}
    parent = parent_id or household.get("docmost_parent_id")
    if parent:
        args["parent_id"] = parent
    out = await _rpc(http, url, "create_note", args)
    m = _UUID.search(out["text"])
    return {"id": m.group(0) if m else "", "text": out["text"]}
