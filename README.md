# Nina

A local, full-duplex voice assistant you can run locally: you talk to it from any browser, run it as a service on a server or kiosk, and it can interrupt and be interrupted, look things up,
run Home Assistant requests through Needle's safety path, hand deep work to Hermes or other locally hosted agents, and go to sleep until you say "Hey Jarvis" / "Hey Nina".

```
browser mic/speaker --WSS--> live/server.py 
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

## What Nina can do

- **Browser and kiosk voice clients** — talk from the browser or run the native kiosk voice client as a service. On supported kiosk setups, the waveform indicates when Nina is listening or speaking. ([#47](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/47), [#81](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/81), [#85](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/85))
- **Agenda and timers** — ask for a day's agenda, follow up with “and tomorrow?”, or set a timer with a countdown and an alarm you can dismiss by voice or from the UI. Agenda and timer views can open on a configured kiosk. ([#48](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/48), [#50](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/50), [#54](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/54), [#55](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/55))
- **Calendar writes and routines** — enable voice scheduling for configured calendars and use “good morning” or “good night” to run configured Home Assistant routines. Routines can use device lists or Home Assistant scripts. ([#61](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/61), [#66](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/66), [#72](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/72))
- **Directions and camera views** — ask for an ETA or show a route on a configured screen, with alternate routes and traffic where available. Configured camera integrations can open a live feed, answer questions from a fresh snapshot, and look up person sightings. ([#35](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/35), [#42](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/42), [#83](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/83), [#84](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/84), [#86](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/86))
- **Voice profiles** — enroll samples in Settings and display speaker labels in transcripts. See the [speaker ID assessment](SPEAKER_ID_ASSESSMENT.md) for status and limitations. ([#19](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/19), [#23](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/23), [#25](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/25))
- **Optional team delegation** — explicitly delegate a task by voice through a configured, authenticated Paperclip facade. ([#78](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/78), [#79](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/79))
- **Dashboard transitions and display fades** — kiosk hosts can fade between dashboards and during screen blanking or wake. The host can follow per-kiosk settings from [HA Dashboard Manager](https://github.com/smaniktahla/ha-dashboard-manager). ([#87](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/87), [#88](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/88), [#89](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/89), [#90](https://github.com/smaniktahla/nina-full-duplex-voice-AI/pull/90))

## Run it
```
live/voices/fetch_voices.sh      # once: Piper fallback voice
live/run.sh                      # https://<host>:8443/  (self-signed cert: live/gen_cert.sh)
```
Environment overrides are listed in `live/README.md`. The service shares llama-server with Hermes; see PIPELINE.md sections 9 and 12.

## Develop
`make test` (unit tests, runs anywhere), `make lint`, `make secrets`. Read **CONTRIBUTING.md** before opening a PR: several Claude Code sessions work on
this at once, and the rules below are what keeps them from colliding.
