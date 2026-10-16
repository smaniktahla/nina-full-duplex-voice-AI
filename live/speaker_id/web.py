"""HTTP routes for household voice enrollment, mounted on the live server (/api/speakers/*).

The web process only stores clips and metadata (numpy + stdlib). Anything that needs torch/SpeechBrain (building a profile,
evaluation) runs as a subprocess of the CLI in the separate speaker-eval venv ($SPEAKER_PY), so the live server never loads the
model. Clips live under store.audio_root() (settings page / $SPEAKER_AUDIO_DIR); embeddings and profiles under $SPEAKER_DIR.
"""
import asyncio, glob, os, re, secrets, shutil, sys, time, wave
import numpy as np
from aiohttp import web
from . import embed, store

PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # live/ (so `-m speaker_id.cli` resolves)
SPEAKER_PY = os.path.expanduser(os.environ.get("SPEAKER_PY", "~/venvs/speaker-eval/bin/python"))
SAFE = re.compile(r"^[a-z0-9_]{1,24}$")
CONDITIONS = ["normal", "quiet", "far", "noisy"]
PURPOSES = ["enroll", "test"]


def audio_dir(*a):
    return os.path.join(store.audio_root(), *a)


def counts():
    out = {}
    root = audio_dir()
    for sp in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        out[sp] = {}
        for f in glob.glob(audio_dir(sp, "*", "*", "*.wav")):
            k = "/".join(f.split(os.sep)[-3:-1])
            out[sp][k] = out[sp].get(k, 0) + 1
    return out


def check(sp):
    if not SAFE.match(sp or ""):
        raise web.HTTPBadRequest(text="bad speaker id (a-z, 0-9, _)")
    return sp


async def _cli(*args, timeout=600):
    """Run the speaker CLI in the speaker-eval venv; returns (returncode, combined output)."""
    if not os.path.exists(SPEAKER_PY):
        return 127, f"speaker-eval Python not found at {SPEAKER_PY} (set SPEAKER_PY)"
    env = dict(os.environ, PYTHONPATH=PKG_PARENT, PYTHONWARNINGS="ignore")
    p = await asyncio.create_subprocess_exec(SPEAKER_PY, "-m", "speaker_id.cli", *args, cwd=PKG_PARENT, env=env,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "timed out"
    text = "\n".join(l for l in out.decode(errors="replace").splitlines() if "warn" not in l.lower())
    return p.returncode, text.strip()


async def status(_):
    return web.json_response({"counts": counts(), "profiles": store.list_profiles(), "conditions": CONDITIONS})


async def clip(req):
    sp = check(req.query.get("speaker"))
    purpose, cond = req.query.get("purpose"), req.query.get("condition")
    if purpose not in PURPOSES or cond not in CONDITIONS:
        raise web.HTTPBadRequest(text="bad purpose/condition")
    pcm = np.frombuffer(await req.read(), dtype=np.int16)
    if len(pcm) < 16000 * 0.8:
        raise web.HTTPBadRequest(text="clip too short")
    d = audio_dir(sp, purpose, cond)
    os.makedirs(d, exist_ok=True)
    for p in (store.ROOT, audio_dir(), audio_dir(sp), audio_dir(sp, purpose), d):
        os.chmod(p, 0o700)
    fp = os.path.join(d, f"{int(time.time() * 1000)}_{secrets.token_hex(2)}.wav")
    with wave.open(fp, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm.tobytes())
    os.chmod(fp, 0o600)
    return web.json_response({"ok": True, "seconds": round(len(pcm) / 16000, 1),
                              "voiced_ms": embed.voiced_ms(embed.to_float(pcm))})


async def enroll(req):
    b = await req.json()
    sp, role = check(b.get("speaker")), b.get("role", "adult")
    if role not in ("adult", "child"):
        raise web.HTTPBadRequest(text="bad role")
    dirs = sorted(glob.glob(audio_dir(sp, "enroll", "*")))
    if not dirs:
        return web.json_response({"ok": False, "error": "no enrollment clips recorded for this person yet"}, status=400)
    args = ["enroll", sp, *dirs, "--role", role]
    if b.get("name"):
        args += ["--name", str(b["name"])[:40]]
    rc, out = await _cli(*args)
    if rc != 0:
        return web.json_response({"ok": False, "error": out[-400:] or f"exit {rc}"}, status=400)
    if not store.settings()["keep_audio_after_enroll"]:
        shutil.rmtree(audio_dir(sp, "enroll"), ignore_errors=True)
    return web.json_response({"ok": True, "message": out[-300:]})


async def evaluate(req):
    write = bool((await req.json()).get("write"))
    rc, out = await _cli("eval", audio_dir(), *(["--write"] if write else []))
    return web.json_response({"ok": rc == 0, "report": out})


async def delete(req):
    sp = check((await req.json()).get("speaker"))
    shutil.rmtree(audio_dir(sp), ignore_errors=True)
    return web.json_response({"ok": True, "removed_profile_files": store.remove(sp)})


def settings_view():
    s = store.settings()
    return {"audio_dir": store.audio_root(), "audio_dir_source": store.audio_root_source(),
            "env_audio_dir": os.environ.get("SPEAKER_AUDIO_DIR"), "keep_audio_after_enroll": s["keep_audio_after_enroll"],
            "profile_dir": store.ROOT}


async def get_settings(_):
    return web.json_response(settings_view())


async def put_settings(req):
    b = await req.json()
    try:
        store.save_settings(**{k: b[k] for k in ("audio_dir", "keep_audio_after_enroll") if k in b})
        os.makedirs(audio_dir(), exist_ok=True)
        os.chmod(audio_dir(), 0o700)
    except (ValueError, OSError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    return web.json_response({"ok": True, **settings_view()})


def add_routes(app, prefix="/api/speakers"):
    store.init()
    app.router.add_get(prefix + "/status", status)
    app.router.add_post(prefix + "/clip", clip)
    app.router.add_post(prefix + "/enroll", enroll)
    app.router.add_post(prefix + "/eval", evaluate)
    app.router.add_post(prefix + "/delete", delete)
    app.router.add_get(prefix + "/settings", get_settings)
    app.router.add_post(prefix + "/settings", put_settings)
