"""Signed links for the kitchen kiosk's route viewer.

The voice server sits behind login (auth.py); a wall display has no login. Instead of opening the app to the kiosk, Nina hands Home Assistant one URL
that carries its own permission:

    http://<host>:<KIOSK_PORT>/kiosk/<token>/index.html

`<token>` = base64url(json {"s": "route", "exp": <unix>}) + "." + HMAC-SHA256 hex, keyed by `kiosk_key` (its own secret in the private auth dir, so rotating it
revokes every kiosk link without logging anyone out). The token opens exactly one thing: the viewer files (kiosk-route/), the current route document and
two action endpoints, all under that path. Nothing else on the server is reachable with it, and it grants no admin or user data.

  GET  /kiosk/<token>/index.html | route.css | route.js | vendor/*   the viewer (a fixed whitelist read at startup, no path traversal)
  GET  /kiosk/<token>/route.json                                     the active route (204 when there is none), never cached
  GET  /kiosk/<token>/agenda/index.html | agenda.css | agenda.js     the agenda viewer (kiosk-agenda/); .../agenda/agenda.json = the day's agenda (204 when none)
  POST /kiosk/<token>/action/close | phone                           run Hooks.on_close / on_phone; 200 only if the hook reports success, else 502

Served over plain HTTP on the LAN by a SEPARATE kiosk-only listener (`serve`, env KIOSK_PORT; off when unset): an HA dashboard page is http and a
self-signed https iframe would be blocked, and a second site means none of the main app's endpoints are ever reachable over http. The listener has only
these routes and no login; the path token is the capability (visible to anyone sniffing the LAN, which is why it opens nothing but this viewer).
The server holds no routing credentials here; the document comes from `Hooks.document()` (the route store, later) and the actions from the hooks, which
may be None, in which case the action answers 501 and the viewer says it didn't go through. Nothing claims success that did not happen.

CLI (on the server):  python live/kiosk.py link --base http://<host>:<KIOSK_PORT> [--days 30]
"""
import argparse, base64, hmac, hashlib, json, os, sys, time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from aiohttp import web

DIR = os.path.expanduser(os.environ.get("NINA_AUTH_DIR", "~/.config/nina"))
KEY = os.path.join(DIR, "kiosk_key")
VIEWER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kiosk-route")
AGENDA_VIEWER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kiosk-agenda")
SCOPE = "route"
DEFAULT_DAYS = 30
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}


SERVED = {"key": None, "t": 0.0}                                            # the last route document a kiosk fetched: ("<id>|<updated>", monotonic time)


def served() -> tuple:
    """(key, age_seconds) of the last route document a viewer actually fetched; (None, inf) if none yet. The viewer polls every few seconds, so a key that
    matches the document just written is the closest thing to observing that the screen is showing it."""
    return SERVED["key"], (time.monotonic() - SERVED["t"]) if SERVED["key"] else float("inf")


SERVED_AGENDA = {"key": None, "t": 0.0}                                     # the same for the agenda document


def served_agenda() -> tuple:
    """(key, age_seconds) of the last agenda document a viewer fetched; (None, inf) if none yet."""
    return SERVED_AGENDA["key"], (time.monotonic() - SERVED_AGENDA["t"]) if SERVED_AGENDA["key"] else float("inf")


@dataclass
class Hooks:
    document: Callable[[], Optional[dict]]                                   # the viewer document, or None when no route is active
    on_close: Optional[Callable[[], Awaitable[bool]]] = None                 # close the route; True only when it was done
    on_phone: Optional[Callable[[], Awaitable[bool]]] = None                 # send the destination to the phone; True only when sent
    on_timer_stop: Optional[Callable[[str], Awaitable[bool]]] = None
    agenda: Optional[Callable[[], Optional[dict]]] = None                    # the day's agenda document for the agenda viewer, or None


# ---------------------------------------------------------------- token
def _key(path=None) -> bytes:
    path = path or KEY
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(os.urandom(32).hex())
    return open(path).read().strip().encode()


def make_token(days: float = DEFAULT_DAYS, now: float = None, key: bytes = None) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"s": SCOPE, "exp": int((now or time.time()) + days * 86400)}).encode()).decode().rstrip("=")
    return body + "." + hmac.new(key or _key(), body.encode(), hashlib.sha256).hexdigest()


def check_token(tok: str, now: float = None, key: bytes = None) -> bool:
    """True only for an untampered, unexpired token of the viewer scope."""
    try:
        body, sig = tok.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(key or _key(), body.encode(), hashlib.sha256).hexdigest()):
            return False
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return d.get("s") == SCOPE and d["exp"] > (now or time.time())
    except Exception:                                                        # noqa: BLE001 - any malformed token is simply invalid
        return False


def make_link(base: str, days: float = DEFAULT_DAYS) -> str:
    return "%s/kiosk/%s/index.html" % (base.rstrip("/"), make_token(days))


# ---------------------------------------------------------------- handlers
def _files(root: str, names=("index.html", "route.css", "route.js")) -> dict:
    """Whitelist of viewer files: {url-relative-name: absolute path}. Read once; a request can only ever name a key of this dict."""
    out = {}
    for name in names:
        if os.path.isfile(os.path.join(root, name)):
            out[name] = os.path.join(root, name)
    vend = os.path.join(root, "vendor")
    if os.path.isdir(vend):
        for n in os.listdir(vend):
            if n.endswith((".js", ".css")):
                out["vendor/" + n] = os.path.join(vend, n)
    return out


def _headers(extra=None):
    h = {"Referrer-Policy": "origin", "X-Content-Type-Options": "nosniff"}              # origin only: tile servers want a Referer, the token in the path must not leak
    h.update(extra or {})
    return h


def add_routes(app: web.Application, hooks: Hooks, root: str = VIEWER, key_path: str = None, agenda_root: str = AGENDA_VIEWER) -> None:
    files = _files(root)
    agenda_files = _files(agenda_root, ("index.html", "agenda.css", "agenda.js"))
    key = _key(key_path)

    def deny(status, text):
        return web.Response(status=status, text=text, headers=_headers({"Cache-Control": "no-store"}))

    async def get(request):
        if not check_token(request.match_info["token"], key=key):
            return deny(401, "link expired or invalid")
        name = request.match_info["name"]
        if name.startswith("agenda/"):                                       # the agenda viewer: its own whitelist and document, same signed link
            sub = name[len("agenda/"):]
            if sub == "agenda.json":
                doc = hooks.agenda() if hooks.agenda else None
                if not doc:
                    return web.Response(status=204, headers=_headers({"Cache-Control": "no-store"}))
                if doc.get("kind") == "timer" and hooks.on_timer_stop:
                    doc = dict(doc, stop_action="../action/timer-stop")
                SERVED_AGENDA["key"], SERVED_AGENDA["t"] = "%s|%s" % (doc.get("id"), doc.get("updated")), time.monotonic()
                return web.json_response(doc, headers=_headers({"Cache-Control": "no-store"}))
            apath = agenda_files.get(sub)
            if not apath:
                return deny(404, "not found")
            return web.Response(body=open(apath, "rb").read(), content_type=TYPES.get(os.path.splitext(sub)[1], "application/octet-stream").split(";")[0],
                                charset="utf-8", headers=_headers({"Cache-Control": "no-cache"}))
        if name == "route.json":
            doc = hooks.document()
            if not doc:
                return web.Response(status=204, headers=_headers({"Cache-Control": "no-store"}))
            SERVED["key"], SERVED["t"] = "%s|%s" % (doc.get("id"), doc.get("updated")), time.monotonic()
            doc = dict(doc)
            acts = {}
            if hooks.on_close:
                acts["close"] = "action/close"
            if hooks.on_phone:
                acts["phone"] = "action/phone"
            doc["actions"] = acts                                            # relative: resolves under this token's path, so it needs no second secret
            return web.json_response(doc, headers=_headers({"Cache-Control": "no-store"}))
        path = files.get(name)
        if not path:
            return deny(404, "not found")
        return web.Response(body=open(path, "rb").read(), content_type=TYPES.get(os.path.splitext(name)[1], "application/octet-stream").split(";")[0],
                            charset="utf-8" if name.endswith((".html", ".css", ".js")) else None, headers=_headers({"Cache-Control": "no-cache"}))

    async def post(request):
        if not check_token(request.match_info["token"], key=key):
            return deny(401, "link expired or invalid")
        fn = {"close": hooks.on_close, "phone": hooks.on_phone, "timer-stop": hooks.on_timer_stop}.get(request.match_info["action"])
        if request.match_info["action"] not in ("close", "phone", "timer-stop"):
            return deny(404, "not found")
        if fn is None:
            return deny(501, "not available")
        try:
            if request.match_info["action"] == "timer-stop":
                body = await request.json()
                ok = await fn(body.get("id"))
            else:
                ok = await fn()
        except Exception:                                                    # noqa: BLE001
            ok = False
        return web.json_response({"ok": bool(ok)}, status=200 if ok else 502, headers=_headers({"Cache-Control": "no-store"}))

    app.router.add_get("/kiosk/{token}/{name:.+}", get)
    app.router.add_post("/kiosk/{token}/action/{action}", post)


async def serve(hooks: Hooks, port: int, host: str = "0.0.0.0", root: str = VIEWER, key_path: str = None, agenda_root: str = AGENDA_VIEWER) -> web.AppRunner:
    """Start the kiosk-only plain-http listener; returns the runner (`runner.addresses[0][1]` is the bound port; await runner.cleanup() to stop). port 0 picks a free port (tests)."""
    app = web.Application()
    add_routes(app, hooks, root, key_path, agenda_root)
    runner = web.AppRunner(app, access_log=None)                             # no access log: the token is in the path
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner


def file_document(path: str) -> Callable[[], Optional[dict]]:
    """Hooks.document that reads the viewer document from a JSON file (whoever shows a route writes it). Missing, empty or unreadable = no active route."""
    def read():
        try:
            d = json.load(open(path))
            return d if isinstance(d, dict) and d.get("routes") else None
        except (OSError, ValueError):
            return None
    return read


def attach(app: web.Application, hooks: Hooks, port: int = None) -> None:
    """Start the kiosk listener with the main app (opt-in: KIOSK_PORT unset or 0 = off) and stop it with it."""
    port = int(os.environ.get("KIOSK_PORT", "0") or 0) if port is None else port
    if not port:
        return

    async def start(a):
        a["kiosk"] = await serve(hooks, port)

    async def stop(a):
        if a.get("kiosk"):
            await a["kiosk"].cleanup()
    app.on_startup.append(start)
    app.on_cleanup.append(stop)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("link", help="print a signed viewer URL")
    p.add_argument("--base", required=True, help="e.g. http://<host>:<KIOSK_PORT>")
    p.add_argument("--days", type=float, default=DEFAULT_DAYS)
    sub.add_parser("revoke", help="rotate kiosk_key: every existing link stops working")
    a = ap.parse_args(argv)
    if a.cmd == "link":
        print(make_link(a.base, a.days))
    else:
        if os.path.exists(KEY):
            os.remove(KEY)
        _key()
        print("kiosk_key rotated; all kiosk links are now invalid")


if __name__ == "__main__":
    main()
