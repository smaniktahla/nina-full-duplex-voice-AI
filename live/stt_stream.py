"""Streaming STT: energy VAD + rolling faster-whisper decode with LocalAgreement-2 word commit.

feed() takes 16 kHz int16 PCM. Words are *committed* once two consecutive decodes agree on them (or when the utterance ends),
so the controller never sees text that later gets rewritten. The raw hypothesis is exposed for the UI and barge-in keywords.
"""
import asyncio
import collections
import re
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from faster_whisper import WhisperModel

SR = 16000
FRAME = 320                      # 20 ms
HALLUCINATIONS = {"you", "thank you", "thanks for watching", "thank you for watching", "bye", "the end", "so"}
# one- or two-word scraps that background chatter / room noise turn into when Whisper has nothing real to transcribe
TRIVIAL = HALLUCINATIONS | {"okay", "ok", "yeah", "yes", "no", "uh", "um", "hmm", "mm", "huh", "oh", "ah", "the", "a", "i", "and", "right", "well"}


def norm(w):
    return re.sub(r"[^\w']", "", w.lower())


class StreamingSTT:
    def __init__(self, model="base.en", threads=4, vad_thr=0.010, end_ms=700, max_utt_s=14, decode_every=0.45,
                 shared_model=None, executor=None, on_event=None, sens=0.4):
        self.model = shared_model or WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)
        self.pool = executor or ThreadPoolExecutor(max_workers=1)
        self.vad_thr, self.end_ms, self.max_utt, self.decode_every = vad_thr, end_ms, max_utt_s, decode_every
        self.on_event = on_event or (lambda *a, **k: None)      # on_event(kind, text) kind in partial|final|speech_start
        self.boost = 1.0                                         # raise while the assistant is speaking (echo residue)
        self.listen_only = False                                 # asleep + phrase wake word: one short decode per utterance, no commits
        self.sens = sens                                         # 'ignore distant voices': min level relative to the user's own
        self.ref = None                                          # running level of the user's real utterances
        self.ignored = 0
        self.noise = 0.004
        self.pre = collections.deque(maxlen=15)
        self.rem = np.zeros(0, np.float32)
        self.reset_utt()
        self.pending = []                                        # committed words awaiting the controller
        self.speech_ms = 0
        self.level = 0.0

    def reset_utt(self):
        self.buf = np.zeros(0, np.float32)
        self.in_utt, self.silence_ms, self.speech_ms = False, 0, 0
        self.lvl_sum, self.lvl_n, self.ignore = 0.0, 0, False
        self.committed, self.prev_hyp, self.hyp = [], [], []
        self.need_final, self.decoded_len = False, 0
        self.wake_checked = False

    # ---------------------------------------------------------------- audio in
    def feed(self, pcm: bytes):
        x = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        x = np.concatenate([self.rem, x])
        n = len(x) // FRAME
        for i in range(n):
            self._frame(x[i * FRAME:(i + 1) * FRAME])
        self.rem = x[n * FRAME:]

    def _frame(self, f):
        rms = float(np.sqrt(np.mean(f * f)) + 1e-9)
        self.level = 0.8 * self.level + 0.2 * rms
        thr = max(self.vad_thr, self.noise * 3.0) * self.boost
        speech = rms > thr
        if not speech:
            self.noise = min(0.02, 0.97 * self.noise + 0.03 * rms)
        if speech:
            if not self.in_utt:
                self.in_utt = True
                self.buf = np.concatenate(list(self.pre) + [f]) if self.pre else f.copy()
                self.on_event("speech_start", "")
            else:
                self.buf = np.concatenate([self.buf, f])
            self.silence_ms = 0
            self.speech_ms += 20
            self.lvl_sum += rms
            self.lvl_n += 1
        elif self.in_utt:
            self.buf = np.concatenate([self.buf, f])
            self.silence_ms += 20
            if self.silence_ms >= self.end_ms or len(self.buf) > self.max_utt * SR:
                self.need_final = True
        else:
            self.pre.append(f.copy())

    # ---------------------------------------------------------------- decode loop
    def _decode(self, audio):
        segs, _ = self.model.transcribe(audio, language="en", beam_size=1, temperature=0.0, without_timestamps=True,
                                        condition_on_previous_text=False, vad_filter=False)
        keep = []
        for s in segs:
            # Whisper's own confidence: drop segments it thinks are not speech / are low-confidence guesses (room noise, far voices)
            if (s.no_speech_prob > 0.6 and s.avg_logprob < -0.8) or s.avg_logprob < -1.4:
                continue
            keep.append(s.text.strip())
        return " ".join(keep).split()

    async def run(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(0.08)
            if not self.in_utt:
                continue
            if self.listen_only and not self.need_final and (self.wake_checked or self.speech_ms < 1300):
                continue
            fast = self.boost > 1.0                                    # assistant is talking: decode sooner so barge-in keywords land fast
            fresh = len(self.buf) - self.decoded_len
            if not self.need_final and fresh < (0.10 if fast else 0.25) * SR:
                continue
            if not self.need_final and (time.monotonic() - getattr(self, "_last", 0)) < (0.15 if fast else self.decode_every):
                continue
            final, audio = self.need_final, self.buf[-int(self.max_utt * SR):].copy()
            if final:
                audio = np.concatenate([audio, np.zeros(int(0.2 * SR), np.float32)])
            self.decoded_len = len(self.buf)
            hyp = await loop.run_in_executor(self.pool, self._decode, audio)
            self._last = time.monotonic()
            if not self.in_utt:
                continue
            if len(hyp) == 1 and norm(hyp[0]) in HALLUCINATIONS and self.speech_ms < 600:
                hyp = []
            if self.listen_only:
                self.wake_checked = True
                self.on_event("wake_check", " ".join(hyp))
                if final or not self.listen_only:                        # decided by the callback (it clears listen_only on a match)
                    pass
                if final:
                    self.reset_utt()
                continue
            self.hyp = hyp
            level = self.lvl_sum / max(1, self.lvl_n)
            if (not self.ignore and self.sens > 0 and self.ref and self.speech_ms >= 300 and level < self.sens * self.ref):
                self.ignore = True                                       # too quiet to be the person at the mic: background / other room
                self.ignored += 1
                print("STT ignoring distant voice (level %.4f < %.2f x %.4f): %r" % (level, self.sens, self.ref, " ".join(hyp)), flush=True)
            if self.ignore:
                if final:
                    self.reset_utt()
                continue
            if final:
                if len(hyp) <= 2 and all(norm(w) in TRIVIAL for w in hyp) and self.boost <= 1.0:
                    print("STT dropped trivial scrap: %r" % " ".join(hyp), flush=True)
                    self.reset_utt()
                    continue
                if len(hyp) >= 3:
                    self.ref = level if self.ref is None else 0.7 * self.ref + 0.3 * level
                new = hyp[len(self.committed):]
                self._commit(new)
                self.final_audio = audio[:-int(0.2 * SR)]                   # the utterance (without the 0.2 s pad) for speaker identification
                self.on_event("final", " ".join(hyp))
                self.reset_utt()
            else:
                m = 0
                while m < min(len(hyp), len(self.prev_hyp)) and norm(hyp[m]) == norm(self.prev_hyp[m]):
                    m += 1
                if m > len(self.committed):
                    self._commit(hyp[len(self.committed):m])
                self.prev_hyp = hyp
                self.on_event("partial", " ".join(hyp))

    def _commit(self, words):
        self.committed += words
        self.pending += words

    def drain(self):
        w, self.pending = self.pending, []
        return w

    @property
    def live_text(self):
        return " ".join(self.hyp) if self.in_utt else ""
