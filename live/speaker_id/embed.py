"""ECAPA-TDNN embeddings, CPU only, model loaded once."""
import os, threading
import numpy as np

MODEL_SRC = "speechbrain/spkrec-ecapa-voxceleb"
MODEL_DIR = os.path.expanduser(os.environ.get("SPEAKER_MODEL_DIR", "~/.cache/nina-speaker-eval/ecapa"))
SR = 16000
_lock = threading.Lock()
_model = None


def _load():
    global _model
    with _lock:
        if _model is None:
            import torch
            torch.set_num_threads(int(os.environ.get("SPEAKER_THREADS", "2")))
            from speechbrain.inference.speaker import EncoderClassifier
            _model = EncoderClassifier.from_hparams(
                source=MODEL_SRC, savedir=MODEL_DIR, run_opts={"device": "cpu"})
    return _model


def voiced_ms(x: np.ndarray, frame_ms=20, thresh=0.01) -> int:
    """Rough voiced duration: frames whose RMS exceeds thresh (float32 in [-1,1])."""
    n = int(SR * frame_ms / 1000)
    if len(x) < n:
        return 0
    f = x[: len(x) // n * n].reshape(-1, n)
    return int((np.sqrt((f ** 2).mean(1)) > thresh).sum() * frame_ms)


def to_float(pcm) -> np.ndarray:
    a = np.asarray(pcm)
    return a.astype(np.float32) / 32768.0 if a.dtype == np.int16 else a.astype(np.float32)


def embed(pcm) -> np.ndarray:
    """16 kHz mono int16/float audio -> L2-normalised 192-d embedding."""
    import torch
    m = _load()
    x = torch.from_numpy(to_float(pcm)).unsqueeze(0)
    with torch.no_grad():
        e = m.encode_batch(x).squeeze().cpu().numpy()
    return (e / (np.linalg.norm(e) + 1e-9)).astype(np.float32)


def load_wav(path) -> np.ndarray:
    import soundfile as sf
    from math import gcd
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = x.mean(1)
    if sr != SR:
        from scipy.signal import resample_poly
        g = gcd(sr, SR)
        x = resample_poly(x, SR // g, sr // g).astype(np.float32)
    return x
