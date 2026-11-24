"""Schema, validation and private-file writer for the setup wizard.

Two private files are managed (both chmod 600, outside the repo, timestamped backup before every change):
  household.json  non-secret household settings (household.DEFAULTS is the schema: the wizard adds no keys of its own)
  env             KEY=value lines read by live/run.sh; the wizard manages only the keys in ENV_KEYS and keeps everything else
Secret values (ENV_SECRETS) are never returned by the API, only whether they are set.
"""
import difflib, json, os, re, shutil, sys, time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import household                                                     # noqa: E402  (DEFAULTS only; the file path is resolved per call)

ENV_SECRETS = {"HA_TOKEN": "Home Assistant long-lived access token", "HERMES_API_KEY": "Hermes API server key"}
ENV_KEYS = dict(ENV_SECRETS)                                         # later phases add plain flags (TTS, WAKE, RESPONDER ...)

# Phase 1 edits the connection keys only; the other household keys pass through untouched.
URL_KEYS = ("ha_url", "searx_url", "hermes_url", "docmost_mcp_url")
CONNECTION_KEYS = URL_KEYS + ("hermes_ssh_host", "wake_host", "wake_port", "timezone", "region", "notes_default_user")
ENV_LINE = re.compile(r"^[A-Z][A-Z0-9_]*$")
HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9._@-]{0,64}$")


def household_path():
    return os.environ.get("NINA_HOUSEHOLD") or os.path.expanduser("~/.config/nina/household.json")


def env_path():
    return os.environ.get("NINA_ENV") or os.path.expanduser("~/.config/nina/env")


def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def read_household():
    txt = _read_text(household_path())
    return json.loads(txt) if txt.strip() else {}


def read_env_lines():
    return _read_text(env_path()).splitlines()


def parse_env(lines):
    out = {}
    for l in lines:
        if l.strip() and not l.lstrip().startswith("#") and "=" in l:
            k, v = l.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def effective():
    """household.json values over DEFAULTS, for the keys the wizard edits."""
    cur = read_household()
    return {k: cur.get(k, household.DEFAULTS[k]) for k in CONNECTION_KEYS}


def state():
    env = parse_env(read_env_lines())
    return {"values": effective(), "defaults": {k: household.DEFAULTS[k] for k in CONNECTION_KEYS},
            "secrets": {k: {"label": v, "set": bool(env.get(k) or os.environ.get(k))} for k, v in ENV_SECRETS.items()},
            "household_exists": os.path.exists(household_path()), "env_exists": os.path.exists(env_path())}


# ---------------------------------------------------------------- validation
def _url_ok(v):
    if v == "":
        return True
    u = urlparse(v)
    return u.scheme in ("http", "https") and bool(u.hostname) and not u.username and not u.password


def validate(values):
    """Return (clean, errors). Unknown keys are errors; secrets must not appear here (they go in `env`)."""
    clean, errors = {}, {}
    for k, v in (values or {}).items():
        if k not in CONNECTION_KEYS:
            errors[k] = "unknown setting"
        elif k in URL_KEYS:
            v = str(v).strip().rstrip("/")
            if _url_ok(v):
                clean[k] = v
            else:
                errors[k] = "must be an http(s) URL without credentials"
        elif k == "wake_port":
            try:
                p = int(v)
                if not 1 <= p <= 65535:
                    raise ValueError
                clean[k] = p
            except (TypeError, ValueError):
                errors[k] = "must be a port number (1-65535)"
        elif k == "wake_host":
            v = str(v).strip()
            if HOST_RE.match(v):
                clean[k] = v
            else:
                errors[k] = "must be a host name or IP address"
        elif k == "hermes_ssh_host":
            v = str(v).strip()
            if ALIAS_RE.match(v):
                clean[k] = v
            else:
                errors[k] = "must be an ssh alias (letters, digits, . _ @ -)"
        elif k == "timezone":
            v = str(v).strip()
            try:
                import zoneinfo
                zoneinfo.ZoneInfo(v)
                clean[k] = v
            except Exception:                                        # noqa: BLE001
                errors[k] = "unknown time zone (use e.g. America/New_York)"
        else:                                                        # region, notes_default_user: free short text
            v = str(v).strip()
            if len(v) <= 80 and "\n" not in v:
                clean[k] = v
            else:
                errors[k] = "too long"
    return clean, errors


def validate_env(env):
    clean, errors = {}, {}
    for k, v in (env or {}).items():
        v = "" if v is None else str(v).strip()
        if k not in ENV_KEYS:
            errors[k] = "unknown setting"
        elif not ENV_LINE.match(k) or not v or re.search(r"\s", v) or len(v) > 4096:     # run.sh exports the raw line: no spaces or newlines
            errors[k] = "must be non-empty with no spaces or line breaks (leave blank to keep the current value)"
        else:
            clean[k] = v
    return clean, errors


# ---------------------------------------------------------------- planning and writing
def _household_text(merged):
    return json.dumps(merged, indent=2, ensure_ascii=False) + "\n"


def _env_text(lines, updates):
    """Replace existing KEY= lines in place, append new keys at the end, keep every other line (comments, other flags)."""
    out, seen = [], set()
    for l in lines:
        k = l.split("=", 1)[0].strip() if "=" in l and not l.lstrip().startswith("#") else None
        if k in updates:
            out.append("%s=%s" % (k, updates[k]))
            seen.add(k)
        else:
            out.append(l)
    for k, v in updates.items():
        if k not in seen:
            out.append("%s=%s" % (k, v))
    return "\n".join(out).rstrip("\n") + "\n"


def plan(values, env):
    """Validate and compute what would be written. Returns {errors, changes, diff, files:{path:text}}; never touches disk.
    The diff and `changes` only name secret keys, never their values."""
    clean, errors = validate(values)
    cenv, eerr = validate_env({k: v for k, v in (env or {}).items() if str(v or "").strip()})
    errors.update({k: m for k, m in eerr.items()})
    if errors:
        return {"errors": errors, "changes": [], "diff": "", "files": {}}
    cur = read_household()
    merged = dict(cur)
    changes = []
    for k, v in clean.items():
        if cur.get(k, household.DEFAULTS[k]) != v or k not in cur and v != household.DEFAULTS[k]:
            merged[k] = v
            changes.append({"key": k, "from": cur.get(k, household.DEFAULTS[k]), "to": v})
    files, diff = {}, ""
    if merged != cur:
        old, new = _household_text(cur) if cur else "", _household_text(merged)
        files[household_path()] = new
        diff += "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "household.json (current)", "household.json (new)"))
    if cenv:
        lines = read_env_lines()
        cur_env = parse_env(lines)
        real = {k: v for k, v in cenv.items() if cur_env.get(k) != v}
        if real:
            files[env_path()] = _env_text(lines, real)
            changes += [{"key": k, "secret": True, "from": "set" if cur_env.get(k) else "unset", "to": "new value"} for k in real]
            diff += "env: will update " + ", ".join(sorted(real)) + " (values hidden)\n"
    return {"errors": {}, "changes": changes, "diff": diff, "files": files}


def _write_private(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def apply(values, env):
    """Write what plan() computed. A timestamped backup of each existing file is taken first (chmod 600)."""
    p = plan(values, env)
    if p["errors"] or not p["files"]:
        return {"ok": not p["errors"], "errors": p["errors"], "changes": p["changes"], "written": [], "backups": [], "restart_needed": False}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backups = []
    for path, text in p["files"].items():
        if os.path.exists(path):
            b = "%s.bak-%s" % (path, stamp)
            shutil.copy2(path, b)
            os.chmod(b, 0o600)
            backups.append(b)
        _write_private(path, text)
    return {"ok": True, "errors": {}, "changes": p["changes"], "written": sorted(p["files"]), "backups": backups, "restart_needed": True}
