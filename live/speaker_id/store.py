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
            "enrollment_samples": len(embeddings), "enabled": True}
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
    shutil.rmtree(_p("audio", speaker_id), ignore_errors=True)
    return n


def thresholds():
    fp = _p("calibration.json")
    return json.load(open(fp)) if os.path.exists(fp) else {}


def save_thresholds(d):
    init()
    fp = _p("calibration.json")
    json.dump(d, open(fp, "w"), indent=2); _secure(fp)
