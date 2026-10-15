"""Standalone household voice recorder (separate process/port from the live voice server).

Run from live/:  SPEAKER_BIND=0.0.0.0 ~/venvs/speaker-eval/bin/python -m speaker_id.recorder
Prints a one-time URL containing an access token; every request needs the token (biometric data).
Default bind is 127.0.0.1. Clips are saved under $SPEAKER_DIR/audio/<speaker>/<purpose>/<condition>/
(chmod 600). Enrolling keeps only embeddings; "delete" removes a person's clips and profile.
"""
import contextlib, glob, io, os, re, secrets, shutil, ssl, time, wave
import numpy as np
from aiohttp import web
from . import cli, embed, identify, store

HERE = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.path.expanduser(os.environ.get("SPEAKER_CERT_DIR", "~/nina-current/live"))
SAFE = re.compile(r"^[a-z0-9_]{1,24}$")
CONDITIONS = ["normal", "quiet", "far", "noisy"]
PURPOSES = ["enroll", "test"]
TOKEN = os.environ.get("RECORDER_TOKEN") or secrets.token_urlsafe(12)


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


@web.middleware
async def auth(req, handler):
    t = req.query.get("t") or req.headers.get("X-Token", "")
    if not secrets.compare_digest(t, TOKEN):
        raise web.HTTPForbidden(text="missing or bad token")
    return await handler(req)


async def page(_):
    return web.FileResponse(os.path.join(HERE, "static", "record.html"))


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
    embs = []
    for f in sorted(glob.glob(audio_dir(sp, "enroll", "*", "*.wav"))):
        x = embed.load_wav(f)
        embs += [embed.embed(s) for s in cli.segments(x)] or ([embed.embed(x)] if embed.voiced_ms(x) >= identify.MIN_VOICED_MS else [])
    if len(embs) < 3:
        return web.json_response({"ok": False, "error": f"only {len(embs)} usable enrollment clips"}, status=400)
    p = store.save_profile(sp, b.get("name") or sp.capitalize(), role, embs)
    if not store.settings()["keep_audio_after_enroll"]:
        shutil.rmtree(audio_dir(sp, "enroll"), ignore_errors=True)
    return web.json_response({"ok": True, "profile": p})


async def evaluate(req):
    write = bool((await req.json()).get("write"))
    args = type("A", (), {"dir": audio_dir(), "margin": identify.DEFAULT_MARGIN, "write": write})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_eval(args)
    return web.json_response({"ok": True, "report": buf.getvalue()})


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
        os.makedirs(audio_dir(), exist_ok=True); os.chmod(audio_dir(), 0o700)
    except (ValueError, OSError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    return web.json_response({"ok": True, **settings_view()})


async def delete(req):
    sp = check((await req.json()).get("speaker"))
    shutil.rmtree(audio_dir(sp), ignore_errors=True)
    return web.json_response({"ok": True, "removed_profile_files": store.remove(sp)})


def main():
    store.init()
    os.makedirs(audio_dir(), exist_ok=True)
    os.chmod(audio_dir(), 0o700)
    print(f"recorder: clips -> {audio_dir()} ({store.audio_root_source()})", flush=True)
    app = web.Application(client_max_size=20 * 1024 * 1024, middlewares=[auth])
    app.add_routes([web.get("/", page), web.get("/status", status), web.post("/clip", clip),
                    web.post("/enroll", enroll), web.post("/eval", evaluate), web.post("/delete", delete),
                    web.get("/settings", get_settings), web.post("/settings", put_settings)])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(CERT_DIR, "cert.pem"), os.path.join(CERT_DIR, "key.pem"))
    host, port = os.environ.get("SPEAKER_BIND", "127.0.0.1"), int(os.environ.get("RECORDER_PORT", "8444"))
    print(f"recorder: https://<this-host>:{port}/?t={TOKEN}   (bound to {host})", flush=True)
    web.run_app(app, host=host, port=port, ssl_context=ctx, print=None)


if __name__ == "__main__":
    main()
