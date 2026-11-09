import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))

try:                                                    # CI's pure-logic job does not install the Whisper runtime; _decode only needs a stand-in model
    import faster_whisper  # noqa: F401
except ImportError:
    sys.modules["faster_whisper"] = types.SimpleNamespace(WhisperModel=object)
import stt_stream


class Seg:
    def __init__(self, text, nsp, lp):
        self.text, self.no_speech_prob, self.avg_logprob = text, nsp, lp


class FakeModel:
    def __init__(self, segs):
        self.segs = segs

    def transcribe(self, *a, **k):
        return iter(self.segs), None


def stt(segs, listen_only):
    o = types.SimpleNamespace(model=FakeModel(segs), hotwords=None, listen_only=listen_only)
    return lambda: stt_stream.StreamingSTT._decode(o, None)


BORDERLINE = [Seg("Hi Nina", 0.64, -0.92)]            # the real miss: heard, then dropped by the confidence filter


def test_awake_decoding_still_filters_low_confidence(monkeypatch):
    monkeypatch.setattr(stt_stream, "RELAX_WAKE_FILTER", True)
    assert stt(BORDERLINE, False)() == []


def test_asleep_wake_check_keeps_borderline_segment(monkeypatch):
    monkeypatch.setattr(stt_stream, "RELAX_WAKE_FILTER", True)
    assert stt(BORDERLINE, True)() == ["Hi", "Nina"]


def test_env_switch_restores_old_behaviour(monkeypatch):
    monkeypatch.setattr(stt_stream, "RELAX_WAKE_FILTER", False)
    assert stt(BORDERLINE, True)() == []
