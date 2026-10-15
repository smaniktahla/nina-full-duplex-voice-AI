"""Calibrated known/unknown/ambiguous decision. Raw cosine is never reported as a probability."""
import enum, os
from dataclasses import dataclass
from typing import Optional
import numpy as np
from . import embed, store

MIN_VOICED_MS = int(os.environ.get("SPEAKER_MIN_MS", "1000"))
DEFAULT_THRESH = 0.55   # placeholder until calibrated from local recordings
DEFAULT_MARGIN = 0.08


class Status(str, enum.Enum):
    IDENTIFIED = "identified"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"
    INSUFFICIENT_AUDIO = "insufficient_audio"


@dataclass
class SpeakerResult:
    status: Status
    speaker_id: Optional[str] = None
    score: float = 0.0       # best raw cosine (diagnostic only)
    margin: float = 0.0      # best - second best
    confidence: Optional[float] = None  # calibrated, None until calibration exists
    speech_ms: int = 0


def score_embedding(e, profiles):
    """Per-speaker score = mean of top-3 clip cosines (robust to a bad clip)."""
    sc = {}
    for sid, M in profiles.items():
        s = np.sort(M @ e)[::-1][:3]
        sc[sid] = float(s.mean())
    return sc


def decide(scores, speech_ms, thresh=DEFAULT_THRESH, margin=DEFAULT_MARGIN, calib=None):
    if speech_ms < MIN_VOICED_MS:
        return SpeakerResult(Status.INSUFFICIENT_AUDIO, speech_ms=speech_ms)
    if not scores:
        return SpeakerResult(Status.UNKNOWN, speech_ms=speech_ms)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best_id, best = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else -1.0
    m = best - second if len(ranked) > 1 else best
    conf = None
    if calib and "a" in calib:  # logistic fit: P(genuine | score)
        conf = float(1 / (1 + np.exp(-(calib["a"] * best + calib["b"]))))
    if best < thresh:
        st, sid = Status.UNKNOWN, None
    elif len(ranked) > 1 and m < margin:
        st, sid = Status.AMBIGUOUS, None
    else:
        st, sid = Status.IDENTIFIED, best_id
    return SpeakerResult(st, sid, best, m, conf, speech_ms)


class Identifier:
    """Fail-open: any error or SPEAKER_ID=off yields UNKNOWN."""

    def __init__(self):
        self.reload()

    def reload(self):
        self.profiles = store.load_all()
        c = store.thresholds()
        self.thresh = c.get("threshold", DEFAULT_THRESH)
        self.margin = c.get("margin", DEFAULT_MARGIN)
        self.calib = c.get("calibration")

    def identify(self, pcm) -> SpeakerResult:
        if os.environ.get("SPEAKER_ID", "on") == "off":
            return SpeakerResult(Status.UNKNOWN)
        try:
            x = embed.to_float(pcm)
            ms = embed.voiced_ms(x)
            if ms < MIN_VOICED_MS:
                return SpeakerResult(Status.INSUFFICIENT_AUDIO, speech_ms=ms)
            sc = score_embedding(embed.embed(x), self.profiles)
            return decide(sc, ms, self.thresh, self.margin, self.calib)
        except Exception:
            return SpeakerResult(Status.UNKNOWN)
