"""Whole-app login: first-run admin setup, middleware, user management, sessions, lockout, CSRF, service token."""
import asyncio, json, os, sys, tempfile
os.environ["NINA_AUTH_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import auth                                                                    # noqa: E402
from aiohttp import web                                                        # noqa: E402
from aiohttp.test_utils import TestClient, TestServer                          # noqa: E402

H = {"X-Nina-CSRF": "1"}
PW = "longenoughpw1"


def _app():
    app = web.Application(middlewares=[auth.middleware])
    auth.add_routes(app)
    async def ok(_): return web.json_response({"ok": True})
    for p in ("/", "/api/config"):
        app.router.add_get(p, ok)
    app.router.add_get("/api/speakers/status", ok)
    app.router.add_post("/api/speakers/delete", ok)
    app.router.add_get("/ws", ok)
    return app


def run(coro):
    return asyncio.run(coro)


def reset():
    for f in (auth.USERS,):
        if os.path.exists(f):
            os.remove(f)
    auth._fails.clear()


def test_password_roundtrip_and_token():
    h = auth.hash_password(PW)
    assert auth.verify_password(PW, h) and not auth.verify_password("nope", h) and not auth.verify_password("x", "garbage")
    u = {"hash": h}
    t = auth.make_token("amy", u, now=1000)
    assert auth.read_token(t, now=1001) == ("amy", auth._stamp(u))
    assert auth.read_token(t, now=1000 + auth.TTL + 1) is None
    body, sig = t.rsplit(".", 1)
    assert auth.read_token(body + "." + "0" * len(sig)) is None and auth.read_token("junk") is None


def test_everything_is_protected_until_signed_in_and_first_run_setup():
    async def go():
        reset()
        async with TestClient(TestServer(_app())) as c:
            r = await c.get("/", allow_redirects=False); assert r.status == 302 and r.headers["Location"] == "/login"
            r = await c.get("/api/config"); assert r.status == 401 and (await r.json())["setup_needed"] is True
            assert (await c.get("/ws")).status == 401
            assert (await c.get("/api/auth/me")).status == 200                          # public: lets /login decide setup vs sign-in
            assert (await c.post("/api/auth/setup", json={"username": "amy", "password": "short"}, headers=H)).status == 400
            assert (await c.post("/api/auth/setup", json={"username": "amy", "password": PW})).status == 403          # CSRF header
            r = await c.post("/api/auth/setup", json={"username": "Amy", "password": PW}, headers=H)
            assert r.status == 200 and (await r.json())["role"] == "admin" and "HttpOnly" in r.headers["Set-Cookie"]
            assert (await c.get("/api/config")).status == 200 and (await c.get("/", allow_redirects=False)).status == 200
            assert (await c.post("/api/auth/setup", json={"username": "evil", "password": PW}, headers=H)).status == 403   # closed for good
    run(go())


def test_users_admin_and_roles():
    async def go():
        reset()
        async with TestClient(TestServer(_app())) as admin, TestClient(TestServer(_app())) as kid:
            await admin.post("/api/auth/setup", json={"username": "amy", "password": PW}, headers=H)
            assert (await admin.post("/api/users/add", json={"username": "kid", "password": PW, "role": "user"}, headers=H)).status == 200
            assert (await admin.post("/api/users/add", json={"username": "kid", "password": PW}, headers=H)).status == 400   # exists
            assert (await kid.post("/api/auth/login", json={"username": "kid", "password": PW}, headers=H)).status == 200
            assert (await kid.get("/api/config")).status == 200                          # a normal user can use the app
            assert (await kid.get("/api/users")).status == 403 and (await kid.get("/api/speakers/status")).status == 403
            assert (await admin.get("/api/speakers/status")).status == 200
            # last-admin protection
            assert (await admin.post("/api/users/delete", json={"username": "amy"}, headers=H)).status == 400
            assert (await admin.post("/api/users/update", json={"username": "amy", "role": "user"}, headers=H)).status == 400
            # reset password kills the kid's old session
            assert (await admin.post("/api/users/update", json={"username": "kid", "password": "anotherpass99"}, headers=H)).status == 200
            assert (await kid.get("/api/config")).status == 401
            # delete a user
            assert (await admin.post("/api/users/delete", json={"username": "kid"}, headers=H)).status == 200
            assert (await kid.post("/api/auth/login", json={"username": "kid", "password": "anotherpass99"}, headers=H)).status == 401
    run(go())


def test_change_own_password_keeps_this_session_kills_others():
    async def go():
        reset()
        async with TestClient(TestServer(_app())) as a, TestClient(TestServer(_app())) as b:
            await a.post("/api/auth/setup", json={"username": "amy", "password": PW}, headers=H)
            await b.post("/api/auth/login", json={"username": "amy", "password": PW}, headers=H)
            assert (await a.post("/api/auth/password", json={"current": "wrong", "new": "brandnewpass1"}, headers=H)).status == 403
            assert (await a.post("/api/auth/password", json={"current": PW, "new": "brandnewpass1"}, headers=H)).status == 200
            assert (await a.get("/api/config")).status == 200 and (await b.get("/api/config")).status == 401
    run(go())


def test_csrf_logout_lockout_service_token_and_ws_origin():
    async def go():
        reset()
        async with TestClient(TestServer(_app())) as c:
            await c.post("/api/auth/setup", json={"username": "amy", "password": PW}, headers=H)
            assert (await c.post("/api/speakers/delete", json={})).status == 403        # no CSRF header
            assert (await c.post("/api/speakers/delete", json={}, headers=H)).status == 200
            assert (await c.get("/ws", headers={"Origin": "https://evil.example"})).status == 403
            assert (await c.get("/ws", headers={"Origin": f"http://{c.server.host}:{c.server.port}"})).status == 200
            await c.post("/api/auth/logout", headers=H)
            assert (await c.get("/api/config")).status == 401
            svc = {"X-Nina-Service": auth.service_token()}
            assert (await c.get("/api/config", headers=svc)).status == 200               # scripts: app access, no cookie
            assert (await c.get("/api/speakers/status", headers=svc)).status == 403      # ...but never admin
            assert (await c.get("/api/config", headers={"X-Nina-Service": "wrong"})).status == 401
            for _ in range(5):
                assert (await c.post("/api/auth/login", json={"username": "amy", "password": "bad"}, headers=H)).status == 401
            assert (await c.post("/api/auth/login", json={"username": "amy", "password": PW}, headers=H)).status == 429
            auth._fails.clear()
    run(go())


def test_audit_log_has_events_but_never_secrets():
    async def go():
        reset()
        lines = []
        auth.LOG = lines.append
        try:
            async with TestClient(TestServer(_app())) as a, TestClient(TestServer(_app())) as b:
                await a.post("/api/auth/setup", json={"username": "amy", "password": PW}, headers=H)
                await a.post("/api/users/update", json={"username": "amy", "password": "reset-pass-9876"}, headers=H)
                await b.post("/api/auth/login", json={"username": "amy", "password": PW}, headers=H)                    # old: refused
                await b.post("/api/auth/login", json={"username": "amy", "password": "reset-pass-9876"}, headers=H)    # new: ok
                await b.post("/api/auth/logout", headers=H)
        finally:
            auth.LOG = lambda *x: None
        text = "\n".join(lines)
        for needle in ("setup: first admin created", "user updated", "password_reset=True", "login FAILED", "login ok", "logout"):
            assert needle in text, (needle, text)
        assert PW not in text and "reset-pass-9876" not in text and "scrypt" not in text and "hash" not in text
    run(go())
