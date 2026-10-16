"""Login for the admin API: hashing, signed sessions, lockout, CSRF header, fail-closed middleware."""
import asyncio, json, os, sys, tempfile
os.environ["NINA_AUTH_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import auth                                                                    # noqa: E402
from aiohttp import web                                                        # noqa: E402
from aiohttp.test_utils import TestClient, TestServer                          # noqa: E402

H = {"X-Nina-CSRF": "1"}


def test_password_roundtrip_and_tamper():
    h = auth.hash_password("correct horse battery")
    assert auth.verify_password("correct horse battery", h) and not auth.verify_password("nope", h)
    assert not auth.verify_password("x", "garbage")


def test_token_signed_expiring():
    t = auth.make_token("amy", now=1000)
    assert auth.read_token(t, now=1001) == "amy"
    assert auth.read_token(t, now=1000 + auth.TTL + 1) is None
    body, sig = t.rsplit(".", 1)
    assert auth.read_token(body + "." + "0" * len(sig)) is None
    assert auth.read_token("junk") is None


def _app():
    app = web.Application(middlewares=[auth.middleware])
    auth.add_routes(app)
    async def ok(_): return web.json_response({"ok": True})
    app.router.add_get("/api/speakers/status", ok)
    app.router.add_post("/api/speakers/delete", ok)
    app.router.add_get("/api/config", ok)
    return app


def run(coro):
    return asyncio.run(coro)


def test_flow():
    async def go():
        async with TestClient(TestServer(_app())) as c:
            assert (await c.get("/api/config")).status == 200                      # public stays public
            assert (await c.get("/api/speakers/status")).status == 403             # no accounts: fail closed
            os.makedirs(auth.DIR, exist_ok=True)
            auth._write_private(auth.USERS, json.dumps({"amy": {"hash": auth.hash_password("longenoughpw1"), "role": "admin"}}))
            assert (await c.get("/api/speakers/status")).status == 401             # accounts exist, not signed in
            assert (await c.post("/api/login", json={"username": "amy", "password": "longenoughpw1"})).status == 403   # CSRF header missing
            assert (await c.post("/api/login", json={"username": "amy", "password": "bad"}, headers=H)).status == 401
            r = await c.post("/api/login", json={"username": "AMY", "password": "longenoughpw1"}, headers=H)
            assert r.status == 200 and "HttpOnly" in r.headers["Set-Cookie"] and "SameSite=Strict" in r.headers["Set-Cookie"]
            assert (await c.get("/api/speakers/status")).status == 200
            assert (await c.post("/api/speakers/delete", json={})).status == 403   # signed in but no CSRF header
            assert (await c.post("/api/speakers/delete", json={}, headers=H)).status == 200
            assert (await (await c.get("/api/me")).json())["user"]["name"] == "amy"
            await c.post("/api/logout", headers=H)
            assert (await c.get("/api/speakers/status")).status == 401
    run(go())


def test_lockout():
    async def go():
        async with TestClient(TestServer(_app())) as c:
            auth._fails.clear()
            for _ in range(5):
                assert (await c.post("/api/login", json={"username": "amy", "password": "bad"}, headers=H)).status == 401
            assert (await c.post("/api/login", json={"username": "amy", "password": "longenoughpw1"}, headers=H)).status == 429
            auth._fails.clear()
    run(go())


def test_role_user_is_not_admin():
    async def go():
        auth._write_private(auth.USERS, json.dumps({"kid": {"hash": auth.hash_password("longenoughpw1"), "role": "user"}}))
        async with TestClient(TestServer(_app())) as c:
            auth._fails.clear()
            assert (await c.post("/api/login", json={"username": "kid", "password": "longenoughpw1"}, headers=H)).status == 200
            assert (await c.get("/api/speakers/status")).status == 403
    run(go())
