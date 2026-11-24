"""Setup wizard: validation, private-file writer (backups, modes, secrets hidden), routes and admin gating."""
import asyncio, json, os, stat, sys, tempfile
D = tempfile.mkdtemp()
os.environ["NINA_AUTH_DIR"] = D
os.environ["NINA_HOUSEHOLD"] = os.path.join(D, "household.json")
os.environ["NINA_ENV"] = os.path.join(D, "env")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import auth                                                                    # noqa: E402
from setup_wizard import core, web as sweb                                     # noqa: E402
from aiohttp import web                                                        # noqa: E402
from aiohttp.test_utils import TestClient, TestServer                          # noqa: E402

H = {"X-Nina-CSRF": "1"}
PW = "longenoughpw1"


def reset():
    for f in (os.environ["NINA_HOUSEHOLD"], os.environ["NINA_ENV"], auth.USERS):
        if os.path.exists(f):
            os.remove(f)
    for f in os.listdir(D):
        if ".bak-" in f:
            os.remove(os.path.join(D, f))


def test_validate_rejects_bad_values():
    _, e = core.validate({"ha_url": "ftp://x", "wake_port": "99999", "timezone": "Mars/Base", "wake_host": "a b", "nope": 1,
                          "searx_url": "http://u:p@h"})
    assert set(e) == {"ha_url", "wake_port", "timezone", "wake_host", "nope", "searx_url"}
    c, e = core.validate({"ha_url": "http://ha.local:8123/", "wake_port": "10400", "timezone": "America/New_York", "docmost_mcp_url": ""})
    assert not e and c["ha_url"] == "http://ha.local:8123" and c["wake_port"] == 10400 and c["docmost_mcp_url"] == ""


def test_env_secret_rules():
    _, e = core.validate_env({"HA_TOKEN": "has space", "OTHER": "x"})
    assert set(e) == {"HA_TOKEN", "OTHER"}


def test_apply_writes_private_files_with_backup_and_keeps_other_keys():
    reset()
    json.dump({"region": "VA", "places": {"x": "y"}}, open(core.household_path(), "w"))
    open(core.env_path(), "w").write("# flags\nRESPONDER=1\nHA_TOKEN=old\n")
    r = core.apply({"ha_url": "http://ha:8123", "region": "VA"}, {"HA_TOKEN": "newtoken", "HERMES_API_KEY": "k1"})
    assert r["ok"] and r["restart_needed"] and len(r["backups"]) == 2
    h = json.load(open(core.household_path()))
    assert h["ha_url"] == "http://ha:8123" and h["places"] == {"x": "y"}                         # untouched keys survive
    env = open(core.env_path()).read()
    assert "RESPONDER=1" in env and "# flags" in env and "HA_TOKEN=newtoken" in env and "HERMES_API_KEY=k1" in env and "old" not in env
    for p in (core.household_path(), core.env_path(), *r["backups"]):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert any("old" in open(b).read() for b in r["backups"] if b.startswith(core.env_path()))
    assert "newtoken" not in json.dumps(r)                                                        # secrets never echoed


def test_no_change_writes_nothing_and_blank_secret_keeps_value():
    reset()
    core.apply({"ha_url": "http://ha:8123"}, {"HA_TOKEN": "t1"})
    before = os.listdir(D)
    r = core.apply({"ha_url": "http://ha:8123"}, {"HA_TOKEN": ""})
    assert r["ok"] and not r["written"] and os.listdir(D) == before
    assert core.state()["secrets"]["HA_TOKEN"]["set"] and "t1" not in json.dumps(core.state())


def test_invalid_input_writes_nothing():
    reset()
    r = core.apply({"ha_url": "nope"}, {})
    assert not r["ok"] and not os.path.exists(core.household_path())


def _app():
    app = web.Application(middlewares=[auth.middleware])
    auth.add_routes(app)
    sweb.add_routes(app)
    return app


async def _login_as(c, name, role):
    users = auth.load_users()
    users[name] = {"hash": auth.hash_password(PW), "role": role}
    auth.save_users(users)
    r = await c.post("/api/auth/login", json={"username": name, "password": PW}, headers=H)
    assert r.status == 200


def test_routes_admin_only_and_roundtrip():
    reset()
    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await c.get("/setup", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/login"                           # anonymous page -> login
            assert (await c.get("/api/setup/state")).status == 401
            await _login_as(c, "bob", "user")
            assert (await c.get("/api/setup/state")).status == 403
            assert (await c.get("/setup")).status == 403
            await _login_as(c, "amy", "admin")
            st = await (await c.get("/api/setup/state")).json()
            assert st["values"]["timezone"] and not st["secrets"]["HA_TOKEN"]["set"]
            r = await c.post("/api/setup/preview", json={"values": {"ha_url": "bad"}}, headers=H)
            assert r.status == 400
            r = await c.post("/api/setup/apply", json={"values": {"ha_url": "http://ha:8123"}, "env": {"HA_TOKEN": "tok"}}, headers=H)
            assert r.status == 200 and (await r.json())["restart_needed"]
            assert (await c.post("/api/setup/apply", json={"values": {}}, headers={})).status == 403   # CSRF header required
            r = await c.post("/api/setup/probe", json={"probe": "wake", "values": {"wake_host": "127.0.0.1", "wake_port": 1}}, headers=H)
            assert (await r.json())["ok"] is False
    asyncio.run(go())
