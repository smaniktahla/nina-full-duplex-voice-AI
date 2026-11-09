# Wake-phrase test protocol ("Hey Nina")

Measures first-attempt wake reliability before and after any change. Observation tooling: `WAKE_DIAG=1` (`live/wake_diag.py`) and `scripts/wake_diag_report.py`.
Never commit results that contain transcripts (`WAKE_DIAG_TEXT=1` output) or recordings; summaries (counts, rates, latencies) are fine.

## How the wake phrase works (so the numbers make sense)
"Hey Nina" has no acoustic model. While asleep, `StreamingSTT` (listen-only) opens an utterance on energy, decodes the first ~1.3 s with Whisper
`base.en`, and again on the whole utterance when it ends (700 ms of quiet or 14 s). `server.py:on_stt` regex-matches the transcript, anchored to its start.
Consequences to test for: late activation (waits for the utterance to end), phrase hidden behind earlier speech/TV (`floating_match` true, `matched` false),
Whisper mis-spellings outside the regex, and decode queueing.

## Setup (every run)
1. Same device position, same speaking distance (state it: e.g. 2 m), same orientation. Mic not muted. Device idle/dim-green, session DORMANT.
2. Confirm `WAKE_DIAG=1` is live (`ls -la ~/.local/share/nina/wake_diag.jsonl` grows). Optional `WAKE_DIAG_TEXT=1` for a debugging run only, then remove it.
3. Note the start time (local, to the minute).
4. Say the phrase at a normal conversational volume, **one attempt at a time**, ~15 s apart. After each attempt, wait: if Nina wakes, say "goodbye" and wait for sleep; if
   she does not wake within 5 s, record a **miss**, then say it again once (record whether the **second** attempt woke her). Keep a tally: `attempt, woke-first (y/n), woke-second (y/n/NA)`.
5. Note the end time. Run `scripts/wake_diag_report.py --start ... --end ... --attempts N`.

## Conditions (20 attempts each)
| Run | Condition |
| --- | --- |
| A | Quiet room, no media |
| B | Typical household background (TV at conversation level, normal talking in the room) |
| C | Noisy (louder TV, several people, activity) |
Also, separately, with a short note of what you did: **cold start** (right after a service restart), **post-playback** (within 2 s, and 5 s, after Nina finished speaking and went back to sleep), and **steady state** (after an hour idle).

## False positives
A separate **idle listening run**: no one says the phrase. Run 60 minutes in condition B (TV on, normal talk). Report activations (all are false) and `non-wake decodes with speech per hour`.
Include radio/TV lines that sound close ("Hey Nina", "Nia", "I'm Nina").

## Recording a result
For each run: condition, distance, N attempts, first-attempt activations, second-attempt rescues, misses never rescued, median activation latency (stop talking -> chime;
note it by stopwatch or from `utt_age_s` of the matching `wake_check final`), near misses from the report, clipped-utterance count, and anything that changed on the device or server.

## Success criteria (engineering targets, not assumed capability)
- >= 95% first-attempt in A; clear improvement over the recorded baseline in B and C.
- False activations in the idle run no worse than baseline (do not buy recall with false wakes).

## Order of work
1. Baseline A, B, C and the idle run with **no** change. Save the report output.
2. Apply one reversible change. Rerun the same runs. Compare.
3. Rollback = revert the PR / `scripts/rollback_voice.sh`; diagnostics off = remove `WAKE_DIAG=1` from `~/.config/nina/env` and restart.
