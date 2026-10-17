"""Signed kiosk links: token sign/verify, what a valid link can and cannot reach, document and action endpoints, listener isolation."""
import asyncio, json, os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import aiohttp, yarl                                                                 # noqa: E402
import kiosk                                                                   # noqa: E402

KEYF = os.path.join(tempfile.mkdtemp(), "kiosk_key")
KEY = kiosk._key(KEYF)
DOC = {"id": "r1", "updated": "t", "selected": 0, "destination": {"label": "X"}, "routes": [{"id": "r0", "duration_s": 60, "distance_m": 1, "steps": []}]}


def run(coro):
    return asyncio.run(coro)


def test_token_roundtrip_and_failures():
    t = kiosk.make_token(1, key=KEY)
    assert kiosk.check_token(t, key=KEY)
    assert not kiosk.check_token(t, now=time.time() + 2 * 86400, key=KEY)               # expired
    assert not kiosk.check_token(t, key=b"another key")                                  # signed by someone else / key rotated
    body, sig = t.rsplit(".", 1)
    assert not kiosk.check_token(body + "x." + sig, key=KEY) and not kiosk.check_token(body + "." + "0" * 64, key=KEY)
    for bad in ("", "x", ".", "a.b", "..", None):
        assert not kiosk.check_token(bad, key=KEY)


def test_wrong_scope_is_rejected():
    import base64, hmac, hashlib
    body = base64.urlsafe_b64encode(json.dumps({"s": "admin", "exp": int(time.time()) + 999}).encode()).decode().rstrip("=")
    assert not kiosk.check_token(body + "." + hmac.new(KEY, body.encode(), hashlib.sha256).hexdigest(), key=KEY)


def test_key_file_is_private_and_stable():
    assert oct(os.stat(KEYF).st_mode & 0o777) == "0o600" and kiosk._key(KEYF) == KEY


async def _with(hooks, fn):
    r = await kiosk.serve(hooks, 0, host="127.0.0.1", key_path=KEYF)
    try:
        async with aiohttp.ClientSession() as s:
            return await fn(s, "http://127.0.0.1:%d" % r.addresses[0][1])
    finally:
        await r.cleanup()


def test_viewer_files_and_isolation():
    async def go(s, base):
        tok = kiosk.make_token(1, key=KEY)
        for name, ctype in (("index.html", "text/html"), ("route.js", "text/javascript"), ("route.css", "text/css"), ("vendor/leaflet.js", "text/javascript")):
            async with s.get("%s/kiosk/%s/%s" % (base, tok, name)) as r:
                assert r.status == 200 and r.content_type == ctype and r.headers["Referrer-Policy"] == "no-referrer", name
        for name in ("..%2flive%2fserver.py", "vendor%2f..%2f..%2flive%2fauth.py", "test_viewer.py", "make_fixture.py", "route.example.json", "vendor/LICENSE-leaflet"):
            async with s.get(yarl.URL("%s/kiosk/%s/%s" % (base, tok, name), encoded=True)) as r:
                assert r.status == 404, name                                             # whitelist only: no traversal, no test or fixture files
        port = int(base.rsplit(":", 1)[1])
        rd, wr = await asyncio.open_connection("127.0.0.1", port)                        # raw request: a client library would normalise the dots away
        wr.write(("GET /kiosk/%s/../../live/server.py HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n" % tok).encode())
        head = (await rd.read(200)).split(b"\r\n")[0]; wr.close()
        assert b" 404 " in head or b" 401 " in head, head                                 # never the file
        for path in ("/", "/api/config", "/api/users", "/login", "/ws", "/kiosk/", "/icons/x"):
            async with s.get(base + path) as r:
                assert r.status == 404, path                                             # the listener has nothing but the kiosk routes
        async with s.get("%s/kiosk/%s/index.html" % (base, "bad.token")) as r:
            assert r.status == 401
        async with s.get("%s/kiosk/%s/index.html" % (base, kiosk.make_token(1, now=time.time() - 3 * 86400, key=KEY))) as r:
            assert r.status == 401
    run(_with(kiosk.Hooks(document=lambda: None), go))


def test_route_json_204_document_and_actions():
    state = {"doc": None}
    kiosk.SERVED.update(key=None, t=0.0); assert kiosk.served()[0] is None and kiosk.served()[1] == float("inf")

    async def go(s, base):
        tok = kiosk.make_token(1, key=KEY); url = "%s/kiosk/%s/route.json" % (base, tok)
        async with s.get(url) as r:
            assert r.status == 204 and r.headers["Cache-Control"] == "no-store"
        state["doc"] = DOC
        async with s.get(url) as r:
            d = await r.json()
            assert r.status == 200 and d["id"] == "r1" and d["actions"] == {"close": "action/close"} and r.headers["Cache-Control"] == "no-store"   # no phone hook
        assert "actions" not in DOC                                                      # the stored document is not mutated
        assert kiosk.served()[0] == "r1|t" and kiosk.served()[1] < 5                      # display_ops verifies "the screen has this document" through this
        async with s.get("%s/kiosk/bad.token/route.json" % base) as r:
            assert r.status == 401
    run(_with(kiosk.Hooks(document=lambda: state["doc"], on_close=lambda: _ret(True)), go))


async def _ret(v):
    return v


def test_actions_report_the_truth():
    calls = []

    async def close():
        calls.append("close"); return True

    async def phone():
        calls.append("phone"); return False

    async def boom():
        raise RuntimeError("x")

    async def go(s, base):
        t = kiosk.make_token(1, key=KEY)
        post = lambda a, tok=t: s.post("%s/kiosk/%s/action/%s" % (base, tok, a))
        async with post("close") as r:
            assert r.status == 200 and (await r.json()) == {"ok": True}
        async with post("phone") as r:
            assert r.status == 502 and (await r.json()) == {"ok": False}                 # hook says it did not happen: never a 200
        async with post("open_the_pod_bay_doors") as r:
            assert r.status == 404
        async with post("close", "bad.token") as r:
            assert r.status == 401 and calls == ["close", "phone"]                       # a bad link runs nothing
    run(_with(kiosk.Hooks(document=lambda: DOC, on_close=close, on_phone=phone), go))

    async def go2(s, base):
        t = kiosk.make_token(1, key=KEY)
        async with s.post("%s/kiosk/%s/action/close" % (base, t)) as r:
            assert r.status == 502                                                       # a crashing hook is a failure, not a success
        async with s.post("%s/kiosk/%s/action/phone" % (base, t)) as r:
            assert r.status == 501                                                       # no hook wired
    run(_with(kiosk.Hooks(document=lambda: DOC, on_close=boom), go2))


def test_file_document(tmp_path):
    p = tmp_path / "doc.json"
    f = kiosk.file_document(str(p))
    assert f() is None
    p.write_text("{not json"); assert f() is None
    p.write_text(json.dumps({"id": "x"})); assert f() is None                            # no routes = no active route
    p.write_text(json.dumps(DOC)); assert f()["id"] == "r1"


def test_attach_is_opt_in(monkeypatch):
    from aiohttp import web
    monkeypatch.delenv("KIOSK_PORT", raising=False)
    base = len(web.Application().on_startup)
    a = web.Application(); kiosk.attach(a, kiosk.Hooks(document=lambda: None)); assert len(a.on_startup) == base
    monkeypatch.setenv("KIOSK_PORT", "8"); b = web.Application(); kiosk.attach(b, kiosk.Hooks(document=lambda: None)); assert len(b.on_startup) == base + 1
