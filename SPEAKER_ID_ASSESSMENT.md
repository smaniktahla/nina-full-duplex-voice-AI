# Nina speaker recognition: Phase 0 architecture assessment (2026-10-15)

> **Status update 2026-10-16:** Phase 1 is done and in use (enrollment, calibrated identification, Settings UI, blank-clip handling) and transcript labels are live (display only). Phase 2 (per-speaker sessions in Needle) has **not** started. The "open decisions" below were answered by doing the work; current state, data locations, measurements and limits are in `PIPELINE.md` section 16.

Status when written (2026-10-15): inspection only. No production change, no restart, nothing installed.

## 1. What is actually deployed (differs from the brief)

- The voice stack runs **on AI2**, not on the Ubuntu laptop. The "laptop" is only the browser client (`https://ai2.local:8443/`).
  `live/server.py` (aiohttp, WSS) is the single process: audio in -> `StreamingSTT` -> controller ticks -> llama-server :8080 -> Kokoro TTS -> audio out.
- **Audio ingestion/VAD:** `ws_handler -> Session.run` receives 16 kHz int16 binary frames and calls `StreamingSTT.feed()`
  (`stt_stream.py`). VAD is an energy gate in `_frame()`; `self.buf` accumulates the whole utterance.
- **Utterance boundary:** `StreamingSTT.run()` fires `on_event("final", text)` when `end_ms` (700 ms) of silence elapses. At that instant
  `self.buf` holds the complete utterance audio. This is the smallest insertion point for speaker attribution.
- **Interruption state machine:** in `server.py` (`on_stt` partial -> `BARGE` keyword -> `interrupt()`; `duck_watch`; generation counter `gen`
  discards stale output). It is single-conversation: one `PromptPolicy` history, one task registry (`jobs`), one `user_buf` per WebSocket.
- **Needle** (`~/needle-voice/needle_voice`) is a library of HA-free modules: `router`, `guards`, `executor`, `context`, `home_ops`,
  `result`, and `session.py` (lifecycle: DORMANT/ACTIVE/IDLE/SUSPENDED). **There is no per-speaker anything.** `session.py` is one
  session per connection; `context.py` (follow-up pronouns, clarification candidates) is per `HomeAgent`, TTL 120 s; the long-term
  memory (`profile.json`, `@remember`) is **one global fact list injected into every session**, which is a privacy leak the moment a
  second person speaks. That file is the first thing that must become per-speaker.
- **HA satellites** (Voice PE) go through `needle_voice/conversation.py` and Wyoming; there is no raw audio in that path, so speaker ID
  there is out of scope for the prototype (Phase 5).
- **Compute (measured now):** AI2 has 16 cores, load ~0, 31 GB RAM with only ~6.7 GB available. **Both RTX 3080s are at 9.5/10 GB and 9.1/10 GB**, so
  there is no GPU headroom. Speaker ID should be **CPU only** (ECAPA is ~20M params; expected tens of ms per second of audio on CPU,
  to be measured). Sortformer (NeMo, GPU-oriented) is not viable on AI2 today; defer to Phase 4 and evaluate elsewhere.
- No speaker libraries are installed (`speechbrain` absent; `onnxruntime 1.27` and `torch 2.12+cu130` present in the system site-packages).
  Hugging Face is reachable.

## 2. Things to flag

1. **An empty venv `~/venvs/speaker-eval` appeared at 10:22 today** (no packages, no process). I did not create it. Another session or you
   may be starting this same work; I will reuse that path rather than collide, but please say if something else owns it.
2. Brief says the 4 outstanding controller bugs are being worked by another workstream. Section 10 of `PIPELINE.md` shows partial-transcript
   hold-back and mid-phrase continuation already landed, and the ambiguous-candidate and clarification-termination items are still open.
   Speaker work will touch the same `Session` class, so I will keep changes in new modules and a thin hook, not edit `tick()`.
3. The running server was restarted 10:17 and I must not test on the live port during a real conversation (lesson in PIPELINE.md s10). All
   evaluation will use a separate process and offline recordings.

## 3. Recommended design (smallest working prototype)

**Identification service = in-process library, not a daemon (for now).** New package `speaker_id/` beside `server.py`:
`embed.py` (ECAPA via SpeechBrain, CPU, model loaded once), `store.py` (profiles + embeddings), `identify.py` (calibrated decision),
`cli.py` (`nina-speakers`). A thread-pool executor runs it so the event loop is never blocked.

- **Insertion:** at `speech_start` + ~1.5 s of voiced audio (early guess, for routing) and again at `final` (full utterance, authoritative).
  Output `SpeakerResult(status, speaker_id, score, margin, speech_ms)`, attached to the utterance and nowhere else.
  Reuses `StreamingSTT.buf`; no second capture.
- **Statuses:** IDENTIFIED / UNKNOWN / AMBIGUOUS / INSUFFICIENT_AUDIO (< ~1.0 s voiced). Decision = best cosine score >= threshold **and**
  (best - second) >= margin. Thresholds from local held-out recordings per speaker pair, and "confidence" is reported as an empirical
  calibrated value (isotonic/logistic fit on genuine vs impostor scores from the household set), never raw cosine.
- **Enrollment:** `nina-speakers enroll|list|test|remove` records many short clips through the same 16 kHz path (mic via the browser page or
  `arecord`), stores per-clip embeddings (not raw audio unless `--keep-audio`), `chmod 600`, under `~/.local/share/needle_voice/speakers/`.
  Profile metadata and embeddings in separate files. Children's enrollment is explicit and `remove` deletes embeddings and any audio.
- **Fail-open:** if the module is absent or errors, every utterance gets `UNKNOWN` and Nina behaves exactly as today. A flag file/env
  `SPEAKER_ID=off` disables it.

**Needle side (Phase 2), new `needle_voice/speakers.py` + `session.py` extension:**
`SessionManager` becomes a registry keyed by `speaker_id` (plus `unknown`), each owning its own conversation turns, task registry,
`HomeAgent` context, and `profile.json` facts (`profile.<id>.json`, migrate existing facts to `salil`). Policy gate maps
`(speaker_id, status, capability)` -> allow/deny **before** tools run; identity is evidence only and sensitive intents keep the existing
`fallback_gate`/DENIED rules plus an out-of-band approval. The controller (one llama slot, one `PromptPolicy`) is rebuilt per speaker switch
from that speaker's stored turns; because history is append-only for KV reuse, a speaker switch costs one prompt re-prefill (~1-4 s at current
sizes) which I will measure before deciding whether to keep two warm histories on the two slots.

**Out of scope for first prototype:** diarization, overlap, multi-room, satellite audio.

## 4. Open decisions for you

1. Go-ahead for Phase 1 (enroll + identify + calibrate, no routing change), including installing `speechbrain` into `~/venvs/speaker-eval` only.
2. Recording the household set needs the adult and both children physically present; I will build the recorder and a short prompt script. Is the
   kids' enrollment OK to do now, and should clips be kept on disk after embedding (my default: no)?
3. Confirm the CPU budget: expect ~1-2 cores briefly per utterance on AI2 alongside Whisper (4 threads). OK, or pin affinity?

## 5. Next steps if approved
Phase 1 code and offline eval harness (no server change) -> record clean/noisy/short/distance sets -> calibrate -> report
confusion (child A vs child B), false accepts, latency -> then Phase 2 against a copy of the server on another port.
