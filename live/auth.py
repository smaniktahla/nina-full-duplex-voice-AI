"""Small local login for the admin parts of the voice server (voiceprint enrollment, /api/speakers/*).

Deliberately minimal and replaceable: everything else asks `current_user(request)`; to move to Authentik (OIDC / forward-auth)
change that one function (and drop /api/login), the middleware, routes and UI keep working.

* Accounts: ~/.config/nina/users.json (chmod 600, outside the repo): {name: {"hash": "scrypt$...", "role": "admin"}}.
  Create with:  python -m auth add <name> [--role admin]   (prompts for the password; run on the server).
* Sessions: stateless signed cookie (HMAC-SHA256, key in ~/.config/nina/auth_secret, chmod 600), HttpOnly, SameSite=Strict,
  Secure over https, 12 h. Deleting auth_secret logs everyone out.
* Brute force: 5 failed logins from an address -> that address is locked for 60 s (in memory).
* CSRF: state-changing requests need the header `X-Nina-CSRF: 1` (cross-site forms cannot set it) on top of SameSite=Strict.
* Fails closed: with no accounts, protected routes return 403.
"""
import argparse, base64, getpass, hashlib, hmac, json, os, secrets, sys, time
from aiohttp import web

DIR = os.path.expanduser(os.environ.get("NINA_AUTH_DIR", "~/.config/nina"))
USERS = os.path.join(DIR, "users.json")
SECRET = os.path.join(DIR, "auth_secret")
COOKIE, TTL = "nina_session", 12 * 3600
PROTECTED = ("/api/speakers/",)
N, R, P = 2 ** 14, 8, 1
_fails = {}   # ip -> (count, locked_until)
_DUMMY = None


def _write_private(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def hash_password(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (N, R, P, base64.b64encode(salt).decode(), base64.b64encode(h).decode())


def verify_password(pw, stored):
    try:
        _, n, r, p, s, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(s), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(h))
    except Exception:
        return False


def _dummy():
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("not-a-real-password")
    return _DUMMY


def load_users():
    try:
        return json.load(open(USERS))
    except Exception:
        return {}


def _key():
    if not os.path.exists(SECRET):
        _write_private(SECRET, secrets.token_hex(32))
    return open(SECRET).read().strip().encode()


def make_token(user, now=None):
    body = base64.urlsafe_b64encode(json.dumps({"u": user, "exp": int((now or time.time()) + TTL)}).encode()).decode()
    return body + "." + hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()


def read_token(tok, now=None):
    try:
        body, sig = tok.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()):
            return None
        d = json.loads(base64.urlsafe_b64decode(body.encode()))
        return d["u"] if d["exp"] > (now or time.time()) else None
    except Exception:
        return None


def current_user(request):
    """THE seam for Authentik: return {"name", "role"} for the request, or None."""
    name = read_token(request.cookies.get(COOKIE, ""))
    u = load_users().get(name) if name else None
    return {"name": name, "role": u.get("role", "admin")} if u else None


def _json(status, **kw):
    return web.json_response(kw, status=status)


@web.middleware
async def middleware(request, handler):
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.path.startswith(("/api/login", "/api/logout") + PROTECTED):
        if request.headers.get("X-Nina-CSRF") != "1":
            return _json(403, error="missing CSRF header")
    if request.path.startswith(PROTECTED):
        if not load_users():
            return _json(403, error="no accounts are set up: run `python -m auth add <name>` on the server")
        u = current_user(request)
        if not u:
            return _json(401, error="login required")
        if u["role"] != "admin":
            return _json(403, error="admin only")
    return await handler(request)


async def login(request):
    ip = request.remote or "?"
    cnt, until = _fails.get(ip, (0, 0))
    if until > time.time():
        return _json(429, error="too many attempts, try again in a minute")
    try:
        b = await request.json()
        name, pw = str(b.get("username", "")).strip().lower(), str(b.get("password", ""))
    except Exception:
        return _json(400, error="bad request")
    u = load_users().get(name)
    ok = verify_password(pw, u["hash"] if u else _dummy()) and bool(u)       # unknown user costs the same scrypt as a known one
    if not ok:
        cnt += 1
        _fails[ip] = (0, time.time() + 60) if cnt >= 5 else (cnt, 0)
        return _json(401, error="wrong username or password")
    _fails.pop(ip, None)
    resp = _json(200, ok=True, name=name, role=u.get("role", "admin"))
    resp.set_cookie(COOKIE, make_token(name), max_age=TTL, httponly=True, samesite="Strict", secure=request.secure, path="/")
    return resp


async def logout(request):
    resp = _json(200, ok=True)
    resp.del_cookie(COOKIE, path="/")
    return resp


async def me(request):
    u = current_user(request)
    return _json(200, user=u, accounts=bool(load_users()))


def add_routes(app):
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/me", me)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="auth", description="manage Nina login accounts")
    sub = ap.add_subparsers(dest="c", required=True)
    for c in ("add", "passwd"):
        s = sub.add_parser(c); s.add_argument("name"); s.add_argument("--role", default="admin", choices=["admin", "user"])
    sub.add_parser("list")
    s = sub.add_parser("remove"); s.add_argument("name")
    a = ap.parse_args(argv)
    users = load_users()
    if a.c == "list":
        for n, u in users.items():
            print(n, u.get("role", "admin"))
        return
    name = a.name.strip().lower()
    if a.c == "remove":
        users.pop(name, None)
    else:
        if a.c == "passwd" and name not in users:
            sys.exit("no such user")
        pw = getpass.getpass("Password: ")
        if len(pw) < 10 or pw != getpass.getpass("Again: "):
            sys.exit("passwords differ or are shorter than 10 characters")
        users[name] = {"hash": hash_password(pw), "role": a.role if a.c == "add" else users[name].get("role", "admin")}
    _write_private(USERS, json.dumps(users, indent=2))
    print("saved", name)


if __name__ == "__main__":
    main()
