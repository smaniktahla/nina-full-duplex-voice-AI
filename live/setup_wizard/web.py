"""Routes for the setup wizard. Admin-only: auth.ADMIN_ONLY covers /setup and /api/setup."""
import os
from aiohttp import web
from . import core, probes

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def page(request):
    return web.FileResponse(os.path.join(HERE, "static", "setup.html"))


async def get_state(request):
    return web.json_response(core.state())


async def _body(request):
    b = await request.json()
    return (b.get("values") or {}), (b.get("env") or {})


async def preview(request):
    values, env = await _body(request)
    p = core.plan(values, env)
    return web.json_response({k: p[k] for k in ("errors", "changes", "diff")}, status=400 if p["errors"] else 200)


async def apply_(request):
    values, env = await _body(request)
    r = core.apply(values, env)
    return web.json_response(r, status=200 if r["ok"] else 400)


async def probe(request):
    b = await request.json()
    name = b.get("probe")
    if name not in probes.PROBES:
        return web.json_response({"ok": False, "detail": "unknown probe"}, status=400)
    session = request.app.get("http")
    if session is None:                                              # not mounted on the live server (tests)
        import aiohttp
        async with aiohttp.ClientSession() as s:
            return web.json_response(await probes.run(s, name, b.get("values") or {}, b.get("env") or {}))
    return web.json_response(await probes.run(session, name, b.get("values") or {}, b.get("env") or {}))


def add_routes(app):
    app.router.add_get("/setup", page)
    app.router.add_get("/api/setup/state", get_state)
    app.router.add_post("/api/setup/preview", preview)
    app.router.add_post("/api/setup/apply", apply_)
    app.router.add_post("/api/setup/probe", probe)
