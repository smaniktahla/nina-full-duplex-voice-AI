import json, re, sys, time, os, resource
import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio

MODELS = sys.argv[1].split(",")
VAD = len(sys.argv) > 2 and "vad" in sys.argv[2].split("+")
PROMPT = open("prompt.txt").read() if len(sys.argv) > 2 and "prompt" in sys.argv[2].split("+") else None
man = json.load(open("manifest.json"))
rng = np.random.default_rng(7)

def pink(n):
    w = rng.standard_normal(n); f = np.fft.rfft(w); k = np.arange(1, len(f) + 1); f = f / np.sqrt(k); x = np.fft.irfft(f, n)
    return x / (np.std(x) + 1e-9)

def mix(sig, snr_db):
    n = pink(len(sig)); ps = np.mean(sig ** 2) + 1e-12
    n = n * np.sqrt(ps / (10 ** (snr_db / 10)))
    return (sig + n).astype(np.float32)

def reverb(sig, rt60=0.6, sr=16000):
    L = int(rt60 * sr); t = np.arange(L) / sr
    ir = rng.standard_normal(L) * np.exp(-6.9 * t / rt60); ir[0] = 1.0
    out = np.convolve(sig, ir / np.sqrt(np.sum(ir ** 2)) * 0.6)[: len(sig) + L // 3]
    return out.astype(np.float32)

def variants(sig):
    quiet = sig * 0.35
    pad = np.zeros(8000, dtype=np.float32)                       # half a second of lead/trail room noise
    def wrap(x): return np.concatenate([pad, x, pad])
    return {
        "clean": wrap(sig),
        "noisy": mix(wrap(quiet), 10),
        "far":   mix(wrap(reverb(quiet)), 5),
    }

NUM = {"30": "thirty", "70": "seventy"}
def norm(s):
    s = s.lower().replace("'", "")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return [NUM.get(w, w) for w in s.split()]

def wer(ref, hyp):
    r, h = norm(ref), norm(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))

clips = [(m["text"], decode_audio(m["file"], sampling_rate=16000)) for m in man]
noise_only = {"fan_noise_8s": (pink(16000 * 8) * 0.05).astype(np.float32), "quiet_room_6s": (pink(16000 * 6) * 0.004).astype(np.float32)}
allv = [(t, k, v) for t, s in clips for k, v in variants(s).items()]

for name in MODELS:
    t0 = time.time()
    m = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=6)
    load_s = time.time() - t0
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    m.transcribe(clips[0][1], language="en", beam_size=5)      # warm-up
    res = {"clean": [], "noisy": [], "far": []}; times = []; worst = []
    for text, kind, audio in allv:
        t = time.time()
        segs, _ = m.transcribe(audio, language="en", beam_size=5, vad_filter=VAD, condition_on_previous_text=False, initial_prompt=PROMPT)
        hyp = " ".join(s.text for s in segs).strip()
        dt = time.time() - t; times.append(dt)
        w = wer(text, hyp); res[kind].append(w)
        if kind == "far" and w >= 0.4: worst.append((text, hyp))
    hall = {}
    for k, a in noise_only.items():
        segs, _ = m.transcribe(a, language="en", beam_size=5, vad_filter=VAD, condition_on_previous_text=False, initial_prompt=PROMPT)
        hall[k] = " ".join(s.text for s in segs).strip()
    print(json.dumps({"model": name, "vad": VAD, "prompt": bool(PROMPT), "load_s": round(load_s, 1), "rss_mb": round(rss),
        "wer": {k: round(float(np.mean(v)), 3) for k, v in res.items()},
        "sec_per_clip_mean": round(float(np.mean(times)), 2), "sec_max": round(float(np.max(times)), 2),
        "noise_only_output": hall, "worst_far": worst[:3]}), flush=True)
