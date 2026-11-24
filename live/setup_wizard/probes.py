"""Connectivity probes. Each returns {"ok": bool, "detail": str}; secrets are used for the request but never echoed back."""
import asyncio, os
import aiohttp
from . import core

TIMEOUT = aiohttp.ClientTimeout(total=6)


def _val(values, key):
    return values.get(key, core.effective().get(key))


async def _get(session, url, headers=None):
    try:
        async with session.get(url, headers=headers or {}, allow_redirects=False, timeout=TIMEOUT) as r:
            return r.status, (await r.text())[:300]
    except Exception as e:                                          # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, str(e)[:120])


def _secret(env, key):
    v = str((env or {}).get(key) or "").strip()
    if v:
        return v
    return core.parse_env(core.read_env_lines()).get(key) or os.environ.get(key, "")


async def ha(session, values, env):
    url = _val(values, "ha_url")
    tok = _secret(env, "HA_TOKEN")
    if not tok:
        return {"ok": False, "detail": "no Home Assistant token set"}
    st, body = await _get(session, url + "/api/", {"Authorization": "Bearer " + tok})
    if st == 200:
        return {"ok": True, "detail": "Home Assistant reachable, token accepted"}
    return {"ok": False, "detail": "token rejected (401)" if st == 401 else "HTTP %s" % st if st else body}


async def hermes(session, values, env):
    url = _val(values, "hermes_url")
    key = _secret(env, "HERMES_API_KEY")
    st, body = await _get(session, url + "/health", {"Authorization": "Bearer " + key} if key else None)
    if st == 200:
        return {"ok": True, "detail": "Hermes reachable" + ("" if key else " (no API key set; it will be read over ssh/locally at runtime)")}
    return {"ok": False, "detail": "HTTP %s" % st if st else body}


async def searx(session, values, env):
    st, body = await _get(session, _val(values, "searx_url") + "/healthz")
    if st is None or st >= 500:
        st, body = await _get(session, _val(values, "searx_url") + "/")
    return {"ok": bool(st and st < 400), "detail": "SearXNG reachable" if st and st < 400 else ("HTTP %s" % st if st else body)}


async def llama(session, values, env):
    st, body = await _get(session, os.environ.get("NINA_LLAMA_URL", "http://localhost:8080") + "/health")
    return {"ok": st == 200, "detail": "llama-server healthy" if st == 200 else ("HTTP %s" % st if st else body)}


async def docmost(session, values, env):
    url = _val(values, "docmost_mcp_url")
    if not url:
        return {"ok": True, "detail": "not configured (conversation notes stay off)"}
    st, body = await _get(session, url)
    return {"ok": st is not None, "detail": "endpoint answered (HTTP %s)" % st if st else body}


async def wake(session, values, env):
    host, port = _val(values, "wake_host"), int(_val(values, "wake_port"))
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
        w.close()
        return {"ok": True, "detail": "wake-word service accepts connections on %s:%d" % (host, port)}
    except Exception as e:                                          # noqa: BLE001
        return {"ok": False, "detail": "%s: %s" % (type(e).__name__, str(e)[:100])}


PROBES = {"ha": ha, "hermes": hermes, "searx": searx, "llama": llama, "docmost": docmost, "wake": wake}


async def run(session, name, values, env):
    clean, errors = core.validate(values)
    if errors:
        return {"ok": False, "detail": "invalid: " + "; ".join("%s %s" % kv for kv in errors.items())}
    return await PROBES[name](session, clean, env)
