# Live duplex voice prototype

Browser (Windows / Ubuntu / Android) ⇄ WebSocket ⇄ service on AI2 ⇄ AI2's existing `llama-server` (:8080, untouched).

```
./run.sh        # then open  https://ai2.local:8443/  and accept the self-signed certificate warning once
```

Click **Start conversation**, allow the microphone, talk. Say **"wait"**, **"stop"** or **"hold on"** to interrupt; **Stop talking**
does the same from the screen. The terminal running `run.sh` (and `server.log`) prints live STT partials/finals, state changes,
every controller tick (`words → SPEAK/SILENT, p_silence, gate, ms`) and interruptions.

## What runs where
| piece | where | notes |
|---|---|---|
| mic capture, echo cancellation, playback | browser | `getUserMedia({echoCancellation, noiseSuppression})`; 16 kHz int16 over WS; TTS audio scheduled gaplessly, cancelled instantly on `cancel` |
| streaming STT | AI2 CPU | faster-whisper `base.en` int8 (`STT_MODEL=tiny.en` is faster). Energy VAD + rolling decode every ~0.45 s, LocalAgreement-2 word commit |
| controller | AI2 | `duplex/loop.py` PromptPolicy (append-only history + state tags), 600 ms ticks, silence-probability gate 0.3, speak-gate 0.95 |
| LLM | AI2 llama-server :8080 | shared single slot with Hermes: don't run other heavy agents at the same time |
| TTS | AI2 GPU | Kokoro-82M (`af_heart` default, picker in the UI, ~0.01-0.03x real time); `TTS=piper` selects the CPU Piper fallback |
| tools | background | `@search` (SearXNG wrapper -> ddgs fallback), `@hermes` (AI1 Hermes :8643), `@revise #id` / `@cancel #id`; results delivered when you're quiet. See `../PIPELINE.md` |

Barge-in: user speech over the assistant first **ducks** playback (≈0.3 s), the keyword in the streaming transcript then
**cancels** everything: playback, queued TTS, the in-flight LLM request and any response generated before the interruption
(discarded, never spoken). The mic stays open throughout.

## Wake word, sessions, sleeping
Needle (`~/needle-voice/needle_voice/session.py`) owns the session lifecycle: DORMANT -> (wake) ACTIVE -> IDLE -> DORMANT/SUSPENDED.
Say **"Hey Jarvis"** (HA's openWakeWord) or **"Hey Nina"** (phrase match on the recogniser, see PIPELINE.md; choose another word or "Always listening" in the UI), talk, and end with "goodbye" / "that's all"
/ "stop listening" or just stop: it goes IDLE after 25 s without the assistant being engaged, asleep 20 s later. Ambient chatter the
assistant doesn't answer never keeps it awake. Timeouts are editable in the UI; events land in `~/.local/share/needle_voice/sessions.jsonl`.
`WAKE=off ./run.sh` = always listening (old behaviour). Tests: `wakeflow_test.py` (full lifecycle, ~4 min), Needle: `cd ~/needle-voice/tests && pytest test_session.py`.

## Dependencies (already installed on AI2)
`/home/salil/venvs/duplex-live` (system site-packages + `piper-tts`, `faster-whisper`); Piper voice in `voices/`.
Rebuild: `python3 -m venv --without-pip --system-site-packages ~/venvs/duplex-live && python3 -m pip --python ~/venvs/duplex-live/bin/python install piper-tts faster-whisper`, then download
`en_US-lessac-medium.onnx(.json)` from `rhasspy/piper-voices` into `voices/`. `./gen_cert.sh [ip]` regenerates the certificate.

## Checks
`/home/salil/venvs/duplex-live/bin/python selftest.py` drives the whole loop with a synthetic microphone (ask → answer → barge-in):
cancel arrives ~0.9 s after "wait" starts and nothing is sent after it.

## Known limits (first test)
- STT is ~0.5-1 s behind speech (commit lag); first spoken word ≈ 1.5 s after you stop. Barge-in ≈ 1 s (limited by recognising the word).
- Use headphones if speaker echo triggers false barge-ins (the VAD threshold is raised while the assistant talks).
- Tools are new: grounding is imperfect on thin search snippets; ask for `hermes` research for detail. SearXNG itself is currently blocked upstream (ddgs fallback answers).
- One conversation per connection; history grows (≈65 tokens/s); use **Reset** if it gets odd. Browser needs HTTPS for the mic.
