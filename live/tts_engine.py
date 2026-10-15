"""TTS engines: Kokoro-82M on GPU (default, much more natural) with Piper CPU fallback. synth() yields int16 PCM bytes per sentence."""
import os
import time

import numpy as np

KOKORO_VOICES = ["af_heart", "af_bella", "af_nicole", "af_sarah", "bf_emma", "bf_isabella", "am_michael", "am_adam", "bm_george"]


class Tts:
    def __init__(self, kind=None, here=None):
        self.kind = kind or os.environ.get("TTS", "kokoro")
        self.here = here or os.path.dirname(os.path.abspath(__file__))
        self.speed = float(os.environ.get("KOKORO_SPEED", "1.0"))
        self.default_voice = os.environ.get("KOKORO_VOICE", "af_heart")
        self.voices = KOKORO_VOICES
        self.note = ""
        if self.kind == "kokoro":
            import time
            err = None
            for attempt in range(6):                                       # a restart races the old process for GPU memory: retry
                try:
                    import torch
                    from kokoro import KPipeline
                    dev = "cuda" if torch.cuda.is_available() else "cpu"
                    self.pipe = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device=dev)
                    self.rate = 24000
                    for _ in self.synth("Ready.", self.default_voice):      # warm-up (first call compiles kernels)
                        pass
                    self.device = dev
                    return
                except Exception as e:                                     # noqa: BLE001
                    err = e
                    print("kokoro load attempt %d failed: %s" % (attempt + 1, repr(e)[:120]), flush=True)
                    try:
                        import gc, torch
                        self.pipe = None
                        gc.collect()
                        torch.cuda.empty_cache()
                    except Exception:                                      # noqa: BLE001
                        pass
                    time.sleep(5)
            print("!! kokoro unavailable, FALLING BACK TO PIPER (one voice):", repr(err)[:160], flush=True)
            self.note = "Kokoro could not load (%s): using Piper" % type(err).__name__
            self.kind = "piper"
        from piper import PiperVoice
        self.voice = PiperVoice.load(os.path.join(self.here, "voices", "en_US-lessac-medium.onnx"))
        self.rate = self.voice.config.sample_rate
        self.voices = ["lessac"]
        self.default_voice = "lessac"
        self.device = "cpu"

    def _cpu_pipe(self):
        if getattr(self, "_cpu", None) is None:
            from kokoro import KPipeline
            self._cpu = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device="cpu")
        return self._cpu

    def _kokoro(self, pipe, text, v):
        for _gs, _ps, audio in pipe(text, voice=v, speed=self.speed):
            a = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            yield (np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes()

    def synth(self, text, voice=None):
        if self.kind == "kokoro":
            v = voice if voice in KOKORO_VOICES else self.default_voice
            # The GPU is shared with llama-server (it can leave only tens of MB free): a CUDA OOM must never mean silence. Retry once after
            # freeing the cache, then fall back to a CPU Kokoro (same voice, slower) for this request.
            for attempt in (0, 1, 2):
                done = []
                try:
                    pipe = self.pipe if attempt < 2 else self._cpu_pipe()
                    for pcm in self._kokoro(pipe, text, v):
                        done.append(1)
                        yield pcm
                    return
                except Exception as e:                                     # noqa: BLE001
                    oom = "out of memory" in str(e).lower()
                    print("TTS synth failed (attempt %d, %s): %s" % (attempt + 1, "OOM" if oom else type(e).__name__, str(e)[:100]), flush=True)
                    if done or attempt == 2:
                        raise                                              # audio already went out (or CPU failed too): don't repeat/hide it
                    try:
                        import gc, torch
                        gc.collect()
                        torch.cuda.empty_cache()
                    except Exception:                                      # noqa: BLE001
                        pass
        else:
            for ch in self.voice.synthesize(text):
                yield ch.audio_int16_bytes
