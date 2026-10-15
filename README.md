# Nina

A local, full-duplex voice assistant for the homelab: you talk to it from any browser, it can interrupt and be interrupted, look things up,
run Home Assistant requests through Needle's safety path, hand deep work to Hermes, and go to sleep until you say "Hey Jarvis" / "Hey Nina".

```
browser mic/speaker --WSS--> live/server.py on AI2
   STT (faster-whisper, streaming)   Needle session manager (wake / dismiss / timeouts / tasks)
   duplex controller (600 ms ticks)  --> llama-server (Qwen3.8-27B, 2 slots)
   tools: @ha (Needle) @weather @calendar @search @hermes @remember
   TTS (Kokoro on GPU, Piper fallback)
```

| Path | What |
|---|---|
| `live/` | the voice server (`server.py`), streaming STT, TTS engines, wake word, tools, browser client (`static/`), ops scripts, end-to-end checks |
| `duplex/` | the turn-taking engine pieces (prompt policy, gates, hybrid classifier mode) shared by the simulator and the live server |
| `needle-voice/` | Needle: the HA conversation component (router, guards, executor) **plus** the session lifecycle (`session.py`) and home requests over REST (`home_ops.py`) |
| `bench.py`, `run_*.sh`, `results/` | the duplex simulator and its benchmark results |
| `PIPELINE.md` | how it all works and why (read first) |
| `RESULTS.md` | simulator findings |
| `SPEAKER_ID_ASSESSMENT.md` | speaker recognition assessment (in progress) |

## Run it
```
live/voices/fetch_voices.sh      # once: Piper fallback voice
live/run.sh                      # https://<host>:8443/  (self-signed cert: live/gen_cert.sh)
```
Environment overrides are listed in `live/README.md`. The service shares AI2's llama-server with Hermes; see PIPELINE.md sections 9 and 12.

## Develop
`make test` (unit tests, runs anywhere), `make lint`, `make secrets`. Read **CONTRIBUTING.md** before opening a PR: several Claude Code sessions work on
this at once, and the rules below are what keeps them from colliding.
