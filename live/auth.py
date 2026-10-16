"""Login for the whole voice-server app, a first-run admin setup, and admin user management.

Deliberately small and replaceable: everything asks `current_user(request)`; to move to Authentik (OIDC / forward-auth) change that one
function (and the /login page + /api/auth/login|setup); the middleware, user model and UI gating keep working.

* First run: with no accounts the /login page offers "Create the admin account"; whoever completes it first becomes the admin (do it
  straight after deploying). After that /api/auth/setup is closed for good and only an admin can add users.
* Accounts: ~/.config/nina/users.json (chmod 600, outside the repo): {name: {"hash": "scrypt$...", "role": "admin"|"user"}}.
  Recovery on the server if the admin password is lost:  python -m auth passwd <name>   (also add|list|remove).
* Sessions: stateless signed cookie (HMAC-SHA256, key in ~/.config/nina/auth_secret, chmod 600), HttpOnly, SameSite=Strict, Secure over
  https, 12 h. Changing a password or deleting the account invalidates that user's sessions. Deleting auth_secret logs everyone out.
* Everything is protected except /login, /api/auth/*, /favicon.ico and /icons/*. Pages redirect to /login, APIs return 401, and the
  websocket also checks Origin. /api/speakers/* and /api/users* need role admin.
* Brute force: 5 failed logins from an address -> that address is locked for 60 s (in memory).
* CSRF: state-changing requests need the header `X-Nina-CSRF: 1` (cross-site forms cannot set it) on top of SameSite=Strict.
* Scripts on the server (deploy smoke test, selftests) send `X-Nina-Service: <token from ~/.config/nina/service_token>` (chmod 600,
  created at startup); that grants app access but never admin or user management.
"""
import argparse, base64, getpass, hashlib, hmac, json, os, re, secrets, sys, time
from urllib.parse import urlparse
from aiohttp import web

DIR = os.path.expanduser(os.environ.get("NINA_AUTH_DIR", "~/.config/nina"))
USERS, SECRET, SERVICE = (os.path.join(DIR, n) for n in ("users.json", "auth_secret", "service_token"))
COOKIE, TTL, MIN_PW = "nina_session", 12 * 3600, 10
ADMIN_ONLY = ("/api/speakers/", "/api/users")
NAME_RE = re.compile(r"^[a-z0-9_.-]{2,32}$")
N, R, P = 2 ** 14, 8, 1
_fails = {}   # ip -> (count, locked_until)
_DUMMY = None


# ---------------------------------------------------------------- storage / crypto
def _write_private(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
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


def save_users(u):
    _write_private(USERS, json.dumps(u, indent=2))


def _secret(path):
    if not os.path.exists(path):
        _write_private(path, secrets.token_hex(32))
    return open(path).read().strip()


def service_token():
    return _secret(SERVICE)


def _stamp(user):          # changes whenever the password hash changes, so old sessions die
    return hashlib.sha256(user["hash"].encode()).hexdigest()[:10]


def make_token(name, user, now=None):
    body = base64.urlsafe_b64encode(json.dumps({"u": name, "s": _stamp(user), "exp": int((now or time.time()) + TTL)}).encode()).decode()
    return body + "." + hmac.new(_secret(SECRET).encode(), body.encode(), hashlib.sha256).hexdigest()


def read_token(tok, now=None):
    """-> (name, stamp) or None"""
    try:
        body, sig = tok.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(_secret(SECRET).encode(), body.encode(), hashlib.sha256).hexdigest()):
            return None
        d = json.loads(base64.urlsafe_b64decode(body.encode()))
        return (d["u"], d["s"]) if d["exp"] > (now or time.time()) else None
    except Exception:
        return None


def current_user(request):
    """THE seam for Authentik: return {"name", "role"} for the request, or None."""
    svc = request.headers.get("X-Nina-Service")
    if svc and hmac.compare_digest(svc, service_token()):
        return {"name": "service", "role": "service"}
    t = read_token(request.cookies.get(COOKIE, ""))
    u = load_users().get(t[0]) if t else None
    if u and hmac.compare_digest(_stamp(u), t[1]):
        return {"name": t[0], "role": u.get("role", "user")}
    return None


def valid_new(name, pw):
    if not NAME_RE.match(name or ""):
        return "username: 2-32 characters, lowercase letters, digits, . _ -"
    if len(pw or "") < MIN_PW:
        return "password must be at least %d characters" % MIN_PW
    return None


def _json(status, **kw):
    return web.json_response(kw, status=status)


# ---------------------------------------------------------------- middleware
def _public(path):
    return path in ("/login", "/favicon.ico") or path.startswith(("/api/auth/", "/icons/"))


@web.middleware
async def middleware(request, handler):
    path = request.path
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Nina-CSRF") != "1" \
            and not request.headers.get("X-Nina-Service"):
        return _json(403, error="missing CSRF header")
    if _public(path):
        return await handler(request)
    user = current_user(request)
    if not user:
        if request.method == "GET" and (path == "/" or path.endswith(".html")):
            raise web.HTTPFound("/login")
        return _json(401, error="login required", setup_needed=not load_users())
    if path == "/ws":                                    # cross-site websocket hijack check (cookie is SameSite=Strict as well)
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return _json(403, error="bad origin")
    if path.startswith(ADMIN_ONLY) and user["role"] != "admin":
        return _json(403, error="admin only")
    return await handler(request)


# ---------------------------------------------------------------- handlers
def _login_ok(request, name, user):
    resp = _json(200, ok=True, name=name, role=user.get("role", "user"))
    resp.set_cookie(COOKIE, make_token(name, user), max_age=TTL, httponly=True, samesite="Strict", secure=request.secure, path="/")
    return resp


async def me(request):
    return _json(200, user=current_user(request), setup_needed=not load_users())


async def setup(request):
    """First run only: create the admin. Closed once any account exists."""
    if load_users():
        return _json(403, error="already set up")
    try:
        b = await request.json()
        name, pw = str(b.get("username", "")).strip().lower(), str(b.get("password", ""))
    except Exception:
        return _json(400, error="bad request")
    err = valid_new(name, pw)
    if err:
        return _json(400, error=err)
    users = {name: {"hash": hash_password(pw), "role": "admin", "created": int(time.time())}}
    save_users(users)
    return _login_ok(request, name, users[name])


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
    ok = verify_password(pw, u["hash"] if u else _dummy()) and bool(u)    # unknown user costs the same scrypt as a known one
    if not ok:
        cnt += 1
        _fails[ip] = (0, time.time() + 60) if cnt >= 5 else (cnt, 0)
        return _json(401, error="wrong username or password")
    _fails.pop(ip, None)
    return _login_ok(request, name, u)


async def logout(request):
    resp = _json(200, ok=True)
    resp.del_cookie(COOKIE, path="/")
    return resp


async def change_own_password(request):
    user = current_user(request)
    if not user or user["role"] == "service":
        return _json(401, error="login required")
    b = await request.json()
    users = load_users()
    u = users.get(user["name"])
    if not u or not verify_password(str(b.get("current", "")), u["hash"]):
        return _json(403, error="current password is wrong")
    err = valid_new(user["name"], str(b.get("new", "")))
    if err:
        return _json(400, error=err)
    u["hash"] = hash_password(str(b["new"]))
    save_users(users)
    return _login_ok(request, user["name"], u)          # old sessions die with the old hash; this one stays signed in


def _view(users):
    return [{"name": n, "role": u.get("role", "user"), "created": u.get("created")} for n, u in sorted(users.items())]


def _admins(users):
    return [n for n, u in users.items() if u.get("role") == "admin"]


async def users_list(request):
    return _json(200, users=_view(load_users()))


async def users_add(request):
    b = await request.json()
    name, pw, role = str(b.get("username", "")).strip().lower(), str(b.get("password", "")), b.get("role", "user")
    err = valid_new(name, pw) or (None if role in ("admin", "user") else "bad role")
    users = load_users()
    if not err and name in users:
        err = "that username exists"
    if err:
        return _json(400, error=err)
    users[name] = {"hash": hash_password(pw), "role": role, "created": int(time.time())}
    save_users(users)
    return _json(200, ok=True, users=_view(users))


async def users_update(request):
    """{username, role?, password?}: change a role and/or reset a password."""
    b = await request.json()
    name = str(b.get("username", "")).strip().lower()
    users = load_users()
    if name not in users:
        return _json(404, error="no such user")
    if "role" in b:
        if b["role"] not in ("admin", "user"):
            return _json(400, error="bad role")
        if users[name].get("role") == "admin" and b["role"] != "admin" and _admins(users) == [name]:
            return _json(400, error="cannot demote the last admin")
        users[name]["role"] = b["role"]
    if b.get("password"):
        err = valid_new(name, str(b["password"]))
        if err:
            return _json(400, error=err)
        users[name]["hash"] = hash_password(str(b["password"]))
    save_users(users)
    return _json(200, ok=True, users=_view(users))


async def users_delete(request):
    name = str((await request.json()).get("username", "")).strip().lower()
    users = load_users()
    if name not in users:
        return _json(404, error="no such user")
    if users[name].get("role") == "admin" and _admins(users) == [name]:
        return _json(400, error="cannot delete the last admin")
    del users[name]
    save_users(users)
    return _json(200, ok=True, users=_view(users))


async def login_page(request):
    return web.FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "login.html"))


def add_routes(app):
    service_token()                                                    # make sure the script token exists before anything connects
    app.router.add_get("/login", login_page)
    app.router.add_get("/api/auth/me", me)
    app.router.add_post("/api/auth/setup", setup)
    app.router.add_post("/api/auth/login", login)
    app.router.add_post("/api/auth/logout", logout)
    app.router.add_post("/api/auth/password", change_own_password)
    app.router.add_get("/api/users", users_list)
    app.router.add_post("/api/users/add", users_add)
    app.router.add_post("/api/users/update", users_update)
    app.router.add_post("/api/users/delete", users_delete)


# ---------------------------------------------------------------- recovery CLI
def main(argv=None):
    ap = argparse.ArgumentParser(prog="auth", description="recovery tool for Nina login accounts (normal use is the web UI)")
    sub = ap.add_subparsers(dest="c", required=True)
    for c in ("add", "passwd"):
        s = sub.add_parser(c); s.add_argument("name"); s.add_argument("--role", default="admin", choices=["admin", "user"])
    sub.add_parser("list")
    s = sub.add_parser("remove"); s.add_argument("name")
    a = ap.parse_args(argv)
    users = load_users()
    if a.c == "list":
        for n, u in users.items():
            print(n, u.get("role", "user"))
        return
    name = a.name.strip().lower()
    if a.c == "remove":
        users.pop(name, None)
    else:
        if a.c == "passwd" and name not in users:
            sys.exit("no such user")
        pw = getpass.getpass("Password: ")
        err = valid_new(name, pw)
        if err or pw != getpass.getpass("Again: "):
            sys.exit(err or "passwords differ")
        if a.c == "add":
            users[name] = {"hash": hash_password(pw), "role": a.role, "created": int(time.time())}
        else:
            users[name]["hash"] = hash_password(pw)
    save_users(users)
    print("saved", name)


if __name__ == "__main__":
    main()
