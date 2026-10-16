"""Long-lived speaker-ID helper (runs in the speaker-eval venv, CPU only). Protocol on stdin/stdout, so the voice server never loads
torch/SpeechBrain: request = 4-byte little-endian byte length + int16 16 kHz mono PCM; reply = one line  @@{json}\\n .
Profiles/thresholds are re-read whenever their files change, so enrolling or calibrating needs no restart."""
import json, os, struct, sys
import numpy as np
from . import store
from .identify import Identifier

PREFIX = "@@"


def _stamp():
    root = store.ROOT
    out = []
    for sub in ("embeddings", "profiles"):
        d = os.path.join(root, sub)
        out.append(max([os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d)] or [0]) if os.path.isdir(d) else 0)
    cal = os.path.join(root, "calibration.json")
    out.append(os.path.getmtime(cal) if os.path.exists(cal) else 0)
    return tuple(out)


def main():
    rd, wr = sys.stdin.buffer, sys.stdout.buffer
    from . import embed
    embed.embed(np.zeros(16000, np.float32))                           # load the model now, not on the first utterance
    ident, stamp = Identifier(), _stamp()
    names = {p["speaker_id"]: p.get("display_name") or p["speaker_id"] for p in store.list_profiles()}
    wr.write((PREFIX + json.dumps({"ready": True, "profiles": sorted(names)}) + "\n").encode()); wr.flush()
    while True:
        head = rd.read(4)
        if len(head) < 4:
            return
        n = struct.unpack("<I", head)[0]
        data = rd.read(n)
        if len(data) < n:
            return
        if _stamp() != stamp:
            ident.reload(); stamp = _stamp()
            names = {p["speaker_id"]: p.get("display_name") or p["speaker_id"] for p in store.list_profiles()}
        r = ident.identify(np.frombuffer(data, dtype=np.int16))
        reply = {"status": r.status.value, "speaker_id": r.speaker_id, "name": names.get(r.speaker_id), "score": round(r.score, 3),
                 "margin": round(r.margin, 3), "speech_ms": r.speech_ms, "profiles": len(ident.profiles)}
        wr.write((PREFIX + json.dumps(reply) + "\n").encode()); wr.flush()


if __name__ == "__main__":
    main()
