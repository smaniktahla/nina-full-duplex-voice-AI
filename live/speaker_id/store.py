"""Profile metadata and embeddings stored separately, outside the repo, chmod 600."""
import json, os, shutil
import numpy as np

ROOT = os.path.expanduser(os.environ.get("SPEAKER_DIR", "~/.local/share/needle_voice/speakers"))


def _p(*a):
    return os.path.join(ROOT, *a)


def _secure(path, mode=0o600):
    os.chmod(path, mode)


def init():
    os.makedirs(_p("profiles"), exist_ok=True)
    os.makedirs(_p("embeddings"), exist_ok=True)
    for d in (ROOT, _p("profiles"), _p("embeddings")):
        os.chmod(d, 0o700)


def list_profiles():
    init()
    out = []
    for f in sorted(os.listdir(_p("profiles"))):
        if f.endswith(".json"):
            out.append(json.load(open(_p("profiles", f))))
    return out


def save_profile(speaker_id, display_name, role, embeddings, model="ecapa-tdnn"):
    init()
    prof = {"speaker_id": speaker_id, "display_name": display_name, "role": role,
            "embedding_model": model, "embedding_version": 1,
            "enrollment_samples": len(embeddings), "enabled": True, "created": int(__import__("time").time())}
    fp = _p("profiles", f"{speaker_id}.json")
    json.dump(prof, open(fp, "w"), indent=2); _secure(fp)
    ep = _p("embeddings", f"{speaker_id}.npy")
    np.save(ep, np.stack(embeddings).astype(np.float32)); _secure(ep)
    return prof


def load_embeddings(speaker_id):
    return np.load(_p("embeddings", f"{speaker_id}.npy"))


def load_all():
    """{speaker_id: (n,192) embeddings} for enabled profiles."""
    return {p["speaker_id"]: load_embeddings(p["speaker_id"])
            for p in list_profiles() if p.get("enabled", True)}


def remove(speaker_id):
    n = 0
    for f in (_p("profiles", f"{speaker_id}.json"), _p("embeddings", f"{speaker_id}.npy")):
        if os.path.exists(f):
            os.remove(f); n += 1
    shutil.rmtree(os.path.join(audio_root(), speaker_id), ignore_errors=True)
    return n


def thresholds():
    fp = _p("calibration.json")
    return json.load(open(fp)) if os.path.exists(fp) else {}


def save_thresholds(d):
    init()
    fp = _p("calibration.json")
    json.dump(d, open(fp, "w"), indent=2); _secure(fp)


# ---- settings (edited from the Settings page) ----
# Voice-sample storage: SPEAKER_AUDIO_DIR seeds the default; a value saved on the Settings page (settings.json) overrides it.
# Embeddings/profiles always stay under SPEAKER_DIR. Changing the folder does NOT move existing clips.
DEFAULT_SETTINGS = {"audio_dir": None, "keep_audio_after_enroll": True}


def settings():
    fp = _p("settings.json")
    d = dict(DEFAULT_SETTINGS)
    if os.path.exists(fp):
        try:
            d.update(json.load(open(fp)))
        except Exception:
            pass
    return d


def audio_root():
    return os.path.expanduser(settings().get("audio_dir") or os.environ.get("SPEAKER_AUDIO_DIR") or _p("audio"))


def audio_root_source():
    if settings().get("audio_dir"):
        return "settings"
    return "env SPEAKER_AUDIO_DIR" if os.environ.get("SPEAKER_AUDIO_DIR") else "default"


def validate_audio_dir(path):
    """Absolute path under the user's home (or an existing mount), creatable and writable. Returns the normalised path."""
    p = os.path.abspath(os.path.expanduser(path or ""))
    if not os.path.isabs(os.path.expanduser(path or "")) and not (path or "").startswith("~"):
        raise ValueError("use an absolute path (or one starting with ~)")
    if p in ("/", os.path.expanduser("~")) or p.startswith(("/etc", "/proc", "/sys", "/dev", "/boot", "/usr", "/bin", "/sbin", "/var/lib")):
        raise ValueError("not an allowed location")
    os.makedirs(p, mode=0o700, exist_ok=True)
    if not os.access(p, os.W_OK):
        raise ValueError("folder is not writable")
    os.chmod(p, 0o700)
    return p


def save_settings(**kw):
    init()
    d = settings()
    if "audio_dir" in kw:
        v = (kw["audio_dir"] or "").strip()
        d["audio_dir"] = validate_audio_dir(v) if v else None
    if "keep_audio_after_enroll" in kw:
        d["keep_audio_after_enroll"] = bool(kw["keep_audio_after_enroll"])
    fp = _p("settings.json")
    json.dump(d, open(fp, "w"), indent=2); _secure(fp)
    return d
