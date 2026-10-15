# Duplex voice assistant: the full pipeline (2026-10-14/15)

A live, interruptible voice conversation with the local Qwen3.8-27B on AI2, usable from any browser on the LAN, that can look
things up on the web, hand deep jobs to Hermes, and revise those jobs mid-flight. Built in one session, starting from a YouTube
idea ("Duplex Cascade": micro-turn loop over a plain text LLM) and ending in a working prototype.

Open it: `https://ai2.local:8443/` (self-signed cert, accept once). Start it: `~/claude-workspace/duplex-sim/live/run.sh`.

## 1. The idea

A text LLM can only read then write. Cut the user's speech into ~600 ms slices, give the model one turn per slice, and let it answer
with either words or a silence token (`~`). That loop looks full-duplex: the model decides when to talk, with no VAD and no
turn-taking model, and it keeps the intelligence of the big text model (the purpose-built duplex models are 7B and score far lower
on VoiceBench).

## 2. Architecture

```
 browser (Windows / Ubuntu / Android)                      AI2 (ai2.local)
 ┌───────────────────────────────┐   WSS :8443    ┌─────────────────────────────────────────────┐
 │ mic + echo cancellation (AEC) │ ─ 16 kHz PCM ─▶│ StreamingSTT  faster-whisper base.en (CPU)  │
 │ AudioWorklet downsampler      │                │   energy VAD, rolling decode, LocalAgreement│
 │                               │ ◀─ TTS PCM ────│                                             │
 │ gapless playback, instant     │ ◀─ cancel/duck │ Controller  (duplex-sim PromptPolicy)       │
 │ cancel, ducking, voice picker │                │   600 ms ticks, append-only history,        │
 └───────────────────────────────┘                │   state tags, silence gate 0.3, speak 0.95  │
                                                  │      │                │                     │
                                                  │      ▼                ▼                     │
                                                  │  llama-server :8080   Tool runner (async)   │
                                                  │  Qwen3.8-27B (shared) │  @search  @hermes   │
                                                  │                       │  @revise  @cancel   │
                                                  │ TTS  Kokoro-82M (GPU) │  result queue       │
                                                  └──────────────────────┬┴─────────────────────┘
                                         docker2 SearXNG / ddgs ◀────────┘     AI1 Hermes :8643
```

- **Audio in:** browser AEC/noise suppression, downsample to 16 kHz in an AudioWorklet, ~40 ms binary frames over a WebSocket.
- **STT:** energy VAD splits utterances; faster-whisper `base.en` (int8, CPU) re-decodes the growing utterance every ~0.45 s
  (0.15 s while the assistant talks, for fast barge-in). Words are only *committed* once two consecutive decodes agree
  (LocalAgreement-2) so the controller never sees text that later changes. VAD threshold is raised 2.2x while the assistant speaks.
- **Controller (reused from the simulator):** every 600 ms it sends the model one message `[12.6s] "words"` or `(silence)` plus
  state tags (`{speaking: ...}`, `{pending: ...}`), against an append-only history so llama-server's KV cache is reused
  (96-99% reuse, ~345 ms/tick). The model answers `~` or up to ~12 words.
- **Two gates on the first-token probability of `~`:** if it chose silence with P < 0.3 it is forced to speak (stops mid-sentence
  stalls, the video's trick); if the user has finished and P < 0.95 it is forced to speak (cures the silence bias, see section 4).
  Forced speech bans `~` and bare punctuation tokens.
- **TTS:** Kokoro-82M on GPU 0 (~320 MB), sentence-level streaming; first chunk spoken at once, later chunks joined into clauses so
  speech doesn't stutter. Piper is the CPU fallback.
- **Barge-in:** user speech over the assistant first ducks playback (~0.3 s); the keyword (wait / stop / hold on / hang on / pause /
  never mind ...) in the *streaming* transcript then cancels everything: browser playback, queued TTS, the in-flight LLM request
  and any response generated before the interruption (a generation counter discards it). Mic stays open. "mhm"/"yeah" never interrupt.
  A **Stop talking** button does the same; **End conversation** closes the socket and releases the mic.
- **Tools run in the background; the loop never waits.** The model writes a short spoken acknowledgement then a tag:
  - `@search <query>`: SearXNG wrapper on docker2 first, `ddgs` fallback (SearXNG's upstream engines are currently CAPTCHA'd or
    suspended, so ddgs answers today). 2.5-5 s.
  - `@hermes <task>`: AI1's Hermes (:8643, own llama-server) for multi-step thinking. Deliberately **not** AI2's Hermes: its runs
    carry ~29K prompt tokens (~50 s for a trivial call) and would stall the shared llama slot the voice loop runs on.
  - `@revise #id <change>` / `@cancel #id`: edit or drop a pending task. The task keeps its id; code owns the task record
    (original request + every refinement + the last 8 turns of conversation) and rebuilds the tool's prompt from it, so a revision
    never loses the original ask and never spawns an unrelated second search. A stale run's result is discarded.
  - **Result delivery is code, not the model:** finished results are queued and injected only when the user is quiet (>= 0.9 s,
    no utterance in progress) and the assistant isn't talking. The model is then forced to phrase it (up to 110 tokens). Pending
    tasks stay visible to the model as `{pending: #2 hermes "..." 12s rev1}`.

## 3. Verified end to end

- Spoken question, spoken answer, barge-in on "wait": cancel ~0.9-1.0 s after the user started the word; no audio sent after the
  cancel message (headless test with a synthetic microphone, `live/selftest.py`).
- `@search`: "weather in New York" acknowledged at 2.3 s, tool done 4.8 s, spoken at 5.9 s.
- Revision: "find out what NVIDIA announced about their new inference architecture" -> `On it` + hermes task #2; 5 s later
  "actually focus on whether it would help my dual 3080 setup" -> `Got it` + **#2 revised (rev 1)**, no new job; the single result
  (92 s on AI1) came back with the dual-3080 focus applied and was spoken at the next pause.
- Not verified yet: a real microphone and speakers (user reported the earlier build as "fantastic"); Kokoro voice choice by ear.

## 4. What the simulator taught us (`duplex-sim/RESULTS.md`, `results/`)

The simulator replays scripted conversations as timestamped 600 ms STT fragments against the real endpoint (virtual time, real
inference latency) and scores false interruptions, missed responses, mid-sentence stops, barge-in yield, delegation and Hermes
result delivery. 10 scenarios, n=1 per config unless noted.

- **Timing fits.** 600 ms ticks: p50 ~345 ms, p95 ~480 ms, ~3% overruns, cache reuse 96-99%, zero invalidations. 400 ms is marginal
  (10% overruns); the floor is ~340 ms per request (fixed overhead, not token count).
- **Probability gate works** as the video claims: mid-sentence stops 11 -> 3 with `P(silence) < 0.3` forced to speak.
- **A plain prompt-only loop drifts silent** (history of its own `~` replies biases it): after clear questions it stayed quiet in
  3-6 of 8 windows. The speak-gate (force speech when the user has paused and P(silence) < 0.95) fixes most of it and is the best
  prompt-only configuration found.
- **Scoring bug found and fixed:** a filler check counted delegation acknowledgements ("On it.") as fillers and hid every delegation.
  Old traces were re-scored (`rescore.py`, `results/rescored/`); originals kept.
- **Hybrid mode did not beat it.** A code-owned state machine (LISTENING / RESPONDING / INTERRUPTED / WAITING_FOR_HERMES) with a
  stateless Y/N turn classifier answered 6.8/8 windows vs 8/8 for the best prompt-only, with a slower p50 (1.33 s vs 1.17 s) and more
  budget overruns. Putting a second prompt on the single shared slot costs ~350 ms per switch in each direction (a classifier call
  followed by a generator call in one tick: ~1.5 s); appending the classifier question to the generator's own cached prefix removes
  the penalty (~395 ms). The classifier itself is accurate (8/8 utterance ends within a tick, ~3-4/86 false positives), the 0.8B CPU
  model is useless zero-shot. A follow-up hybrid (v2b) was aborted: its forced-continuation rule banned "." so answers never
  ended. Lesson kept in the live design: Hermes results are delivered by code, interruption handling is code, but the
  speak/stay-silent decision stays with the model plus the two probability gates.
- **Other measured facts:** `~` is one token on this model (`LISTEN` is two: `LIST`+`EN`, so a probability gate would key on a word
  prefix); append-only history grows ~65 tokens/s (98K context lasts ~25 min); Hermes-on-AI2 evicts/blocks the shared slot.

## 5. Choices and why

- **Browser client over WebSocket**: device independence (Windows, Ubuntu, Android) and free echo cancellation. HTTPS is required for
  mic access; a self-signed cert with the LAN IP SAN works after one click-through.
- **Whisper locally rather than AI1's Wyoming whisper**: that service returns one transcript per utterance (no partials) and is HA's.
- **Kokoro over Piper**: much more natural; GPU makes it ~0.01-0.03x real time after warm-up (CPU int8 was ~1.0x, too slow to stream).
  Piper `lessac-medium` was the first voice and remains the fallback. Voices selectable live.
- **Nothing in production was reconfigured.** New process, new port, new venv (`~/venvs/duplex-live`), read-only use of llama-server.
  The single llama slot is shared, so benchmarks and heavy agent work should not run during a voice session.

## 6. Known issues and next steps

- SearXNG (docker2) returns 0 results: upstream engines blocked (DuckDuckGo CAPTCHA, Startpage CAPTCHA, Brave suspended, Google
  denied, Wikidata timeout). ddgs fallback works; fixing SearXNG needs engine/config changes on docker2 (not done: production).
- Small models embellish: after a search the model once added details not in the snippets; a grounding rule and larger snippets
  (5 x 360 chars) reduced it ("I don't have the exact series score"), but facts beyond the snippet should go to `@hermes`.
- STT lags ~0.5-1 s; first spoken word ~1-1.5 s after you stop. Barge-in ~1 s (limited by recognising the word).
- Speaker echo relies on browser AEC; headphones are safest.
- Next: HA/device tools via a small registry, a Hermes tool-progress channel, a trained small turn-taking classifier to remove
  shared-slot contention, wake word / always-on mode, voice persona, persistent memory across sessions.

## 7. Files

`duplex-sim/live/` (`server.py`, `stt_stream.py`, `tts_engine.py`, `tools.py`, `static/index.html`, `run.sh`, `gen_cert.sh`,
`selftest.py`, `toolstest.py`, `README.md`); `duplex-sim/duplex/` (simulator components reused live); `duplex-sim/bench.py`,
`rescore.py`, `analyze_hybrid.py`, `RESULTS.md`, `results/`. Manifest section: "Duplex voice assistant" in the DocMost infrastructure manifest.

## 8. Session lifecycle: wake word, dismissal, timeouts (Needle as conversation manager)

Added 2026-10-15. Needle (`~/needle-voice`) was inspected first: it is a stateless per-utterance *router* (guards -> Needle tool model
-> Qwen fast tier -> Hermes) with HA-free modules, injected IO, JSONL telemetry and per-device loop-breaker state, but no sessions,
wake word or dismissal. The lifecycle was therefore added **inside Needle's package** as `needle_voice/session.py` (same style as
`router.py`: stdlib only, injected clock and event sink, 11 unit tests in `tests/test_session.py`; the whole Needle suite is 136 pass,
1 pre-existing failure unrelated: `voluptuous` missing on AI2). The duplex engine is untouched and is a client of it.

- **States:** DORMANT (no session; only the wake detector listens), ACTIVE (full duplex), IDLE (grace period; STT still on but nothing
  you say keeps the session alive unless the assistant answers it), SUSPENDED (asleep but the session, its context and pending tasks
  are retained for `suspend_ttl_s`).
- **Transitions:** wake word / tap / typed input -> ACTIVE (new session from DORMANT, resumed from SUSPENDED, revived from IDLE);
  no *engagement* for `active_timeout_s` (25) -> IDLE; `idle_timeout_s` (20) -> DORMANT, or SUSPENDED if tasks are pending;
  spoken dismissal ("goodbye", "that's all", "stop listening", "go to sleep", "thanks, that's it" ...) -> farewell + the same rule;
  `suspend_ttl_s` (300) -> DORMANT with pending tasks cancelled and context dropped.
- **Ambient-speech protection:** only engagement resets the clock: wake/tap/typing, the assistant speaking, a tool result delivered.
  User utterances do not. An utterance unanswered after `response_window_s` (7) counts as ignored; `max_ignored` (2) of them shorten
  the grace to `ambient_grace_s` (6). STT also drops distant voices (level gate relative to your own, slider in the UI), low-confidence
  Whisper segments and one/two-word scraps ("Okay."), and the model is told to stay silent when speech isn't meant for it.
  A running Hermes task holds a session open for at most 5 minutes.
- **Session-scoped state:** conversation turns (used to give tools context) and the task registry (`@search/@hermes/@ha/@weather/@calendar`,
  `@revise`, `@cancel`) live in the Needle session; closing it to DORMANT cancels the jobs and resets the model's history.
- **Wake word:** pluggable detector protocol; the implementation streams the browser's 16 kHz audio to **HA's existing openWakeWord**
  (Wyoming, homeassistant.local:10400): `hey_jarvis` (default, to avoid colliding with the satellites' `okay_nabu`), `okay_nabu`, `alexa`,
  `hey_mycroft`, `hey_rhasspy`. While asleep, audio reaches only the detector (no STT, no LLM ticks, no llama slot use). The wake phrase
  itself is stripped from the first transcript. "Always listening" is selectable and reproduces the previous behaviour exactly.
- **Events:** `transition, activated, dismissed, interruption, ambient_ignored, task_added/finished/cancelled, configured` are appended to
  `~/.local/share/needle_voice/sessions.jsonl` (Needle's telemetry writer) and pushed to the browser; the UI shows a session chip
  (DORMANT / ACTIVE / IDLE / SUSPENDED), the engine chip, task count, manual Wake / Sleep buttons, Mute, and the three timeouts.
- **Verified headless (synthetic mic, real openWakeWord / Whisper / LLM / Hermes):** speech while asleep ignored; "Hey Jarvis" wakes
  (~0.8 s); answered; silence -> IDLE (8 s) -> DORMANT (6 s) with a new session id on the next wake; a Hermes task started, "thanks,
  that's all" spoken -> farewell and **SUSPENDED (task pending)**; task finished while suspended; "Hey Jarvis" -> **resumed the same
  session** and the result was spoken. Not tested: a real noisy room.
- **Not done:** Needle's own tiers are not yet consulted for "is this addressed to me?" (the seam is `SessionManager.on_user_utterance`);
  speaker verification for "with whom"; the HA Voice PE satellite is not wired to this service (see below).

## 9. Own llama slot for voice (2026-10-14, applied)

AI2's llama-server now runs `--parallel 2 -kvu` (two slots sharing one 98304-token KV pool: no extra VRAM, any slot may use the whole context). The voice server pins its requests to slot 1; Hermes keeps slot 0 by prompt-cache affinity. Applied with an idle-wait + automatic-rollback script (`live/ops/apply_parallel2.sh`); MTP/ngram speculation started fine with two slots. Verified afterwards: idle voice tick p50 344 ms; during a Hermes AI2 call (prompt now ~7.8K tokens after the separate Hermes slimming work) voice p50 692 ms, p95 3.2 s, max 8.6 s over 10 ticks: queueing is gone but GPU compute is still shared, so a Hermes prefill still stretches individual voice ticks. HA satellite pipeline: weather (rules tier) 0.09 s; a Hermes-routed question 52.9 s (separate latency work in ATQ 2c232783). Services healthy (llama-server, hermes, health gate, GPU watchdog HEALTHY).

## 9. AI2 llama-server now has two slots (2026-10-14)

Problem: a Hermes agent session (40-50K-token prompts, many sequential calls) held the single slot for minutes and the voice loop's
600 ms ticks queued behind it (first tick 16.8 s; a pinned-slot test later showed 55 s). Fix (approved, applied with a self-rolling-back
script, `live/ops/apply_parallel2.sh`): `--parallel 1` -> `--parallel 2 -kvu` (unified KV: one 98304-token pool shared by both slots, no extra
VRAM, any slot can hold a 50K Hermes prompt). Result, measured by `live/ops/verify_parallel2.py`:
- health ok, 2 slots, kv_unified=true, n_ctx_slot=98304; MTP + ngram speculation started fine with two slots; HA satellite pipeline still
  works ("what's the weather?" 0.06 s via rules; "how many lights are currently on" 35.8 s via Needle -> Hermes AI2, correct answer); Hermes AI2 API call ok.
- Voice tick, idle server: p50 349 ms. With a **cache-warm** 22K-token Hermes-like request decoding on the other slot (the normal Hermes case,
  its calls hit 99-100% prompt cache): p50 485 ms, p95 1.2 s, max 4.4 s, versus waiting out the whole request before. The Hermes request ran ~2x slower
  (19 s vs 10 s) while sharing the GPUs.
- Limit: a **cold** 20K+-token prefill on the other slot starved the voice slot for ~29 s (the server fills each batch from the lowest slot first).
  Rare for Hermes (prefix cache) but possible after a restart or a new Hermes session.
- Pinning voice to a fixed slot (`id_slot`) is worse: if another client already holds that slot the request queues behind it. Left unpinned.
- Rollback: `sudo bash ~/claude-workspace/duplex-sim/live/ops/apply_parallel2.sh --rollback`. The unit restarts at most twice per 120 s (StartLimitBurst=2).

## Phrase wake word "hey Nina" (2026-10-15)
HA's openWakeWord has no "Nina" model, so `hey_nina` is a *phrase* wake word: while asleep the recogniser decodes the first ~1.3 s of each utterance (Whisper base.en, CPU) and matches `hey/hi/okay + nina/nia/neena/nena` at the start; the text of anything else is dropped without being logged or kept ("hey Dina" does not wake). Wake and command can be one breath ("Hey Nina, what's the capital of France?" woke and answered Paris). Trade-offs vs an acoustic model: ~1.5 s wake latency, CPU decode for every utterance in the room while asleep, and Whisper's spelling of the name decides detection. Selectable in the UI wake-word list or `WAKE=hey_nina ./run.sh`; hey_jarvis and the other HA words still work. A properly trained openWakeWord model (synthetic Piper/Kokoro data, deployable into HA's openWakeWord add-on so the satellites can use it too) would remove those trade-offs.

## 10. Fixes after the first real conversation (2026-10-15) and long-term memory

Found by reading the live log of a real conversation ("do you know my home address?" ... "My home address is <street address>"):
- **Answered before the user finished:** the model saw partial words ("My home address") and replied "I don't have your home address stored".
  Now a response (or tool call) that would *start* while the user's voice is still active is held back and re-decided once they stop;
  exception: danger words (bleach, ammonia, fire, knife, ...) still cut in.
- **Mid-phrase cut-offs** ("Got it, <number> <street>", "Got"): forced speech banned `. , ! ?` so a forced sentence could never end, and nothing continued
  an unfinished sentence. Forced speech now bans only the sentinel and junk glyphs; if the last chunk had no sentence end and the model goes silent,
  the next tick is forced to continue (max 3 ticks, then it stops: this is the v2b runaway guard); a "response" of pure punctuation counts as silence.
- **`@ha` used for a personal question** returned the time ("what's the home address" -> "12:05 AM"): the prompt now restricts `@ha` to devices,
  sensors, the time and current weather.
- **No memory:** added `@remember <fact>` / `@forget <words>`. Facts live in `~/.local/share/needle_voice/profile.json` (mode 0600, plaintext, last 60)
  and are put in the system prompt of every new session. Verified across two separate sessions (taught "my dog is Biscuit" -> next session answered
  "Your dog's name is Biscuit"; test fact then deleted).
- **Self-inflicted latency:** my headless tests ran against the live server during that conversation (ticks of 6-7 s in the log). Don't test on the live
  port while someone is using it.
- Regression after the changes (`selftest.py`): answer 1.1 s after the transcript, barge-in cancel 0.97 s, nothing sent after cancel.

## 11. Home requests go through Needle (2026-10-15)

The `@ha` tool no longer relies on HA's built-in matcher alone (it failed on "swimming pool lights": the devices are `switch.pool_pendant_lights`
and `switch.pool_high_bay_lights`). It now uses **Needle's own path**, extended inside the Needle package rather than re-implemented:
- `needle_voice/home_ops.py` (new, HA-free logic): `guards.pre_guard / intent_signals` -> household vocabulary (`merge_lexicon`) ->
  `candidates.rank / unique_top` -> `executor.plan` (fixed intent table, exposed entities only, sensitive intents off) -> `executor.execute`
  (availability check, honest speech). Plus `build_snapshot` (same snapshot as `ha_snapshot.build_entity_snapshot`, built from HA's WebSocket registries:
  entities explicitly exposed to Assist, aliases, areas) and `HomeAgent` (snapshot cache).
- `executor.RestHass` (new): a duck-typed `hass` over HA's REST API (`states.get` from a per-request cache, `services.async_call` -> `/api/services`).
  It lives in executor.py because Needle's invariant (tests/test_static.py) says only executor.py may reach HA services.
- Group vocabulary: when the household vocabulary names several devices for one phrase ("pool lights" -> both pool switches) and the user said a plural
  noun, reads answer for each device and writes are allowed for turn_on / turn_off / toggle only; dimming or anything else on a group is refused.
  Look-alikes without a group alias are never guessed: the assistant is told "That matches several devices: A, B, C" and asks you.
- Vocabulary sources, merged: HA's `/config/needle_voice/lexicon.json` (the satellites' file, read over ssh, cached 5 min) and the voice-only
  `~/.local/share/needle_voice/lexicon_voice.json` (pool lights group lives here; promote to HA's lexicon if you want the satellites to know it).
- Superseded by section 12: fallback to HA's agent is now gated and results are structured.
- Tests: `~/needle-voice/tests/test_home_ops.py` (9 tests on the real 196-entity snapshot, incl. group on/off, no guessing, unavailable target not
  claimed done, guards, sensitive intents); superseded by section 12 (now 182 pass, 0 fail). Requests are logged to
  `~/.local/share/needle_voice/home_requests.jsonl` (Needle telemetry writer).
- Live check (read-only): "Do you know if the swimming pool lights are on?" -> "Let me check." -> tool 0.4 s -> "No, the pool lights are off."; "What about the pool fan?" answered.
  Not yet exercised live: writes (turn on/off).


## 12. Needle action contract: verified writes, fallback safety, context (2026-10-15)

**Tool-result contract** (`needle_voice/result.py`, shared by every future domain). `ToolResult(status, speech, reason, telemetry, request_id, superseded, side_effect)`:

| Status | Meaning | Fallback to another agent |
|---|---|---|
| SUCCESS | done and *observed* (or already true, or a read) | n/a |
| UNSUPPORTED / NO_MATCH | Needle doesn't implement it / found nothing | only if `home_ops.fallback_gate()` passes |
| AMBIGUOUS | several candidates; `speech` is the question | never |
| DENIED | safety policy refused | **never** |
| UNAVAILABLE | target unavailable / vanished | never |
| EXECUTION_FAILED | HA refused / unreachable, nothing believed changed | never |
| UNVERIFIED | command sent, outcome not confirmed (timeout, state unchanged, unobservable intent) | never; never auto-retried |
| CANCELLED | superseded by a newer user turn *before* any side effect | never |

`speech` is derived from the structured result; Nina's tool text is `[DONE] ...` only for SUCCESS, `[NOT DONE] ...` for everything else, and the prompt
tells the controller to say only that sentence.

**Safety boundary.** `fallback_gate(text)` is the common check before ANY other agent acts: write requests that are sensitive by verb (unlock, open) or noun
(door, garage, lock, alarm, security, valve, heater, furnace, gate, oven, stove...) are DENIED even when Needle merely declined (guards, no snapshot, no match), so a denial
can't become permission through HA's agent. Questions about state ("is the garage door open") pass. Known limit: HA's built-in agent has its own exposure rules we don't control;
the gate only narrows what is handed to it. Needle itself still treats `heater` etc. as ordinary switches when resolved (only unlock/open are intent-sensitive).

**Verified execution** (`executor.execute_verified`, the only mutating code): skips the call if already in the requested state; refuses unavailable/vanished targets; after the call
re-reads each entity from HA (`RestHass.read_state`, independent GET) for up to 2.5 s and compares to the expected state (on/off/lock/close/open/media/brightness/temperature).
Script/scene intents have no observable state, so they are UNVERIFIED. REST errors are classified (`rest_auth_failed`, `rest_timeout`, `rest_unreachable`, `rest_http_N`,
`rest_malformed_response`); a timeout after the POST is UNVERIFIED (may have run), an HTTP error is EXECUTION_FAILED. Writes use a snapshot no older than 60 s and are refused if it can't be refreshed.

**Context** (`context.py`, per `HomeAgent`, TTL 120 s): follow-up pronouns ("turn them on") resolve to the last referent, group writes only if it was a household group, and every follow-up goes
through the same plan/exposure checks. A verb-less correction ("actually just the pendant lights") is never executed: Needle says what it last did and asks for an explicit command.
A "Did you mean A, B or C?" question stores its candidate set; the answer ("the basement one", "the second") binds to it. Unrelated utterances clear the context.

**Lifecycle.** Every `request()` takes an epoch; a request superseded before its write step is CANCELLED with no side effect; one whose command was already sent is *not* assumed
cancelled: its real outcome is kept (also if the caller is cancelled mid-flight) and reported in the next reply ("Earlier request already ran: ..."). Nothing is reversed automatically.
Concurrent writes serialise on a lock; a duplicate request becomes "already on".

**Mutation audit mode** (flag file `~/.local/share/needle_voice/mutation_audit`; delete to turn off). `@ha` logs a `proposed` record (tool text + transcript) and an `executed`
record (plan, services actually sent, per-entity before/after) with one shared `request_id`, and rejects tool text containing both on and off words, several actions, or polarity not present in
the committed transcript. Independently of the flag, server.py replaces the model's @ha text with the user's own words when it adds on/off/and/then/open/close/lock words the user never said.

**The 09:54 failure ("turn off the voice test lamp" -> "...and turn on the test lamp")**: ASR was correct; the controller LLM composed the corrupted tool text from a trailing fragment. Needle
refused it (multi-target guard) and the *old* fallback then went to HA and the model improvised narration. Fixed by the polarity/words repair, the gate, `[NOT DONE]` replies. Not fixed (follow-ups):
the controller still ticks on partial transcripts with n_predict 30 (truncated narration); "turn off the living room lights" still returns the media players as candidates (candidate ranking
should filter by the noun's domain); the server's session end during an unresolved clarification.

**Live validation 2026-10-15** (approved devices only: `input_boolean.needle_voice_test`, `switch.pool_pendant_lights`; both ended in their original state): read -> ON (verified off->on, independent GET) -> ON again ("already on", no call) -> OFF (verified);
pendant on->verify->off->verify. "unlock the front door" -> DENIED, gate blocks fallback. "turn on the living room lights" -> AMBIGUOUS. Not live-tested: unavailable device, UNVERIFIED path, REST timeouts/auth
(covered by mocks), and the running voice server has NOT been restarted onto this code yet.

**Calendar / navigation plan (not started; same contract).** `calendar_ops.py` / `navigation_ops.py` in needle_voice, each `handle(text, ctx) -> ToolResult`, no new daemons.
Facts found: HA already has several calendars, `tools.calendar()` already reads them via `/api/calendars` (read-only);
no Google Maps Travel Time entity exists in HA; notify targets `notify.mobile_app_<phone>` targets exist for phone handoff (`data: {message: "navigate", data: {...}}` / geo: URI); `zone.home` holds the home location (keep address there, not in logs).
Steps: (1) calendar_ops read-only on HA calendars: next event / day, remember the last event + its location in Context, UNSUPPORTED for create/modify (DENIED until explicitly authorised); (2) navigation_ops: add Google Routes API (needs an API key; separate from HA)
or HA Google Maps Travel Time integration; resolve place via Places text search, AMBIGUOUS on several, `routingPreference=TRAFFIC_AWARE` for now, `departureTime` for future, say "typical" for predictions; (3) "when should I leave" = start - ETA - buffer (config, default 10 min);
(4) "send it to my phone" = notify.mobile_app_* navigation intent, requires the pending destination in Context and is UNVERIFIED (no read-back). Requests share `request_id` with home_requests.jsonl; log no event descriptions or street addresses.

## 12. Why results came out in odd chunks, and the fix (2026-10-15)

Session 315963c3 ("It's sixty-two degrees and raining right / now.", "You have a tour at / twelve-thirty, an S / BA / payment / at / two / PM")
had two compounding causes, both in the speak-the-tool-result path:
1. `loop.parse_out` caps any speech at 16 words (right for 600 ms tick chunks). Tool results were spoken through it, so every result longer than
   16 words was cut mid-sentence.
2. The leftover was then "finished" by the tick-format history, which has taught the model to say a few words and end its turn: the
   silence-probability gate forced one word per tick (about 0.6-1.2 s each), and my forced "continue the sentence" call also ended after one word
   because the model can end its turn after any token.
Fixes: tool results are now spoken by a clean one-shot request (`say_result`: persona prompt + "The user asked ... The tool returned ...", up to two short sentences,
temperature 0.2, no tick history), kept whole, spoken as one utterance; the forced continuation call now bans end-of-turn and stops at the sentence end, so it
finishes the sentence in one call. Replay of the same weather / calendar questions: one coherent utterance each, no fragments. Also noticed: another session
pinned the voice server to llama slot 1 (`run.sh`, `VOICE_SLOT=1`) after making the health gate pin Hermes to slot 0, which resolves the queue-behind-Hermes
problem that unpinned/pinned-both-ways had.

## 13. Reply generation decoupled from the tick loop (2026-10-15, built, OFF by default)

Problem (11:33 ET and 09:55 sessions): the tick model emitted one word then the sentinel, so replies came out as "Sorry", "It", "It" and the turn was over; TTS chunking followed whatever few words each 600 ms tick produced.

Behind `RESPONDER=1` (env, read by `run.sh`; default 0 = old behavior exactly):
- The tick loop only DECIDES (STT drain, p_s turn-taking gate, speak-gate, hold-back while the user is mid-utterance, barge-in, dismissal). When the decision is "speak" with no tool tag, no tool result and a non-filler chunk, the tick's own words are discarded.
- `Session.start_responder` -> `Session.respond` (server.py) runs ONE streamed `/completion` from a clean prompt (`responder.py`: Nina persona + long-term facts + `self.convo` last 10 turns + the user's words; n_predict 120, temp 0.4, no tick format, no sentinel). `responder.generate` splits the stream into sentences (1-2 word sentences are joined to the next, max 4) and `speak(sentence, final=True)` sends each as its own TTS unit; sentence 1 goes out as soon as it completes. Tool tags/markdown are stripped from the text.
- While a reply is in progress, ticks keep draining STT and accumulating `user_buf`, but make no LLM call and take no tool results (they are delivered after). `interrupt()` cancels the responder (`llm_task` is the responder task) and `self.gen` stops any further sentence, so barge-in/duck/dismissal behave as before.
- Unchanged: tool-call ticks (`On it. @ha ...` speak the tick's words and start the tool), `say_result()` for tool results, Needle `[DONE]`/`[NOT DONE]` handling, @ha polarity repair and mutation audit.
- Logging: each reply gets `rid`; server.log has `RESPOND rid=... decision-tick=... user=... final-stt=...` and `RESPOND rid=... done: N sentence(s), first audio queued X ms, total Y ms`; `tools._home["parent_rid"]` is written as `reply_id` into home_requests.jsonl rows, linking tool calls to the reply that triggered them. STT-final text and decision tick are in the first line.
- Tested: `live/responder_test.py` (9 offline tests: first sentence before stream end, no one-word replies, short-sentence joining, decimals, interruption stops speech, tag/markdown stripping, cap, trailing fragment) and one real call through `Session.respond` against the live llama: "can you hear me clearly right now" -> 2 sentences, first queued at 942 ms (no pinned slot used). NOT tested with real audio/browser.
- Known gaps: the tick model's own history (`pol`) still records the discarded fragment as what Nina said; a reply is not re-checked against the user continuing to talk except through barge-in/gen; only plain-speech turns go through the responder (tool turns still speak the tick's short acknowledgement).
- Enable: `cd ~/claude-workspace/duplex-sim/live && RESPONDER=1 VOICE_SLOT=<as before> setsid nohup ./run.sh >> server.log 2>&1 < /dev/null &` (Salil restarts). Back out: restart without `RESPONDER`; the pre-change file is `server.py.pre-responder`.

## 14. GPU headroom for Kokoro (2026-10-15)

Kokoro (TTS) shares GPU 0 with llama-server; at ctx 98304 the cards were 9.8 / 9.1 GB used of 10 GB and a long sentence hit CUDA OOM (silent reply). llama-server ctx reduced 98304 -> 65536 (`/etc/systemd/system/llama-server.service`, backup `.bak-1015-1153`), Hermes `context_length` (`~/.hermes/config.yaml` line 5, backup `.bak-1015-1153`) reduced to match; llama + Hermes restarted. Result: GPU0 ~7.4 GB llama (was 8.6), GPU1 ~7.7 (was 9.1). Roll back: restore both backups, `sudo systemctl daemon-reload && sudo systemctl restart llama-server hermes`. Also tts_engine.py now retries on OOM then falls back to a CPU Kokoro (`tts_engine.py.pre-oomfix` is the old file).

## 15. Notes and transcripts saved to DocMost

Ask Nina to "take notes", "write down this conversation" or "make a transcript". The model only emits a tag; the code does the recording, so it never depends on the model remembering:

| Tag | Effect |
|---|---|
| `@notes start [notes\|transcript] [title]` | begin recording what is said from now on (a red "notes" chip shows in the browser) |
| `@notes add <text>` | put one explicit point into the notes |
| `@notes stop [title]` / `save` | stop and write the DocMost note; the result is spoken back |
| `@notes cancel` | discard |

- **Modes:** `notes` (default) = concise bullets, decisions and action items written from the conversation by one clean LLM request (plus any explicit points); `transcript` = verbatim turns with times.
  A title is generated if none was given.
- **Every note** is titled `MM/DD/YYYY - <title>`, filed under the configured parent page, and starts with a header: date, time (in the household time zone), speaker, source (mode, session id).
  The speaker is `household.json` -> `notes_default_user` until speaker recognition can supply `session.speaker` (the notes code already prefers it).
- **Nothing is lost:** the recording is flushed to a private local file as it grows (`~/.local/share/nina/notes_buffer/`, mode 0600); it is saved automatically (and the note says so) if the conversation ends
  or the browser disconnects first; if DocMost is unreachable the note is parked as markdown in `~/.local/share/nina/notes_failed/` and the user is told.
- **Never on its own:** recording starts only when asked, and Nina says so. Transcripts can contain other people in the room; keep this in mind before enabling it for the whole household.
- **Config (private, not in the repo):** `docmost_mcp_url` (the DocMost MCP endpoint), optional `docmost_parent_id`, `notes_default_user`, `timezone` in `~/.config/nina/household.json`
  (template in `live/household.example.json`). With no `docmost_mcp_url` the feature is off and the prompt does not mention it.
- **Code:** `live/docmost.py` (client), `live/notes_tool.py` (recording, rendering, summarising, saving), small hooks in `live/server.py`; tests in `tests/test_notes.py` (fake DocMost endpoint, no network).
- **Verified live (trial instance, real DocMost):** notes mode (asked to name it, said two facts, asked to save: note created with header + bullets + explicit points) and transcript mode ended by the browser disconnecting (autosaved).
