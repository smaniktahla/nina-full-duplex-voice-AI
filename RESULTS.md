> **Update 2026-10-15: read PIPELINE.md first.** Corrections to the tables below: (1) a scoring bug counted delegation acks ("On it.") as
> fillers; re-scored numbers are in `results/rescored/` and are better than shown here (best prompt-only = append_state, gate 0.3 +
> speak-gate 0.95: 8/8 windows answered). (2) The hybrid mode (classifier + code state machine) was built and repeated: it did not beat
> that prompt-only config (6.8/8 answered, slower p50, more overruns); `results/HYBRID_REPORT.md`, `analyze_hybrid.py`. Hybrid v2b was
> aborted (forced continuation banned "." so answers never ended; `results/rep2_hybrid_inline_t03/ABORTED.txt`). (3) The live
> prototype in `live/` supersedes further benchmarking.

# Duplex front-end simulator: results and recommendation

Run 2026-10-14 against AI2's production `llama-server` (Qwen3.8-27B IQ3_XXS, 2×RTX 3080, MTP + ngram spec,
q8/q8 KV, `-np1`, 98K ctx). Nothing on the server was changed. Text-only: STT, TTS and Hermes are mocked in virtual time;
inference latency is real. Temperature 0, one run per config.

**Caveat on sample size:** 10 scripted conversations (~35 scored windows), n=1 per config. Differences of ±1 between configs
are noise. Large effects (latency floor, cache behaviour, the silence bias) are not.

## Answer

Yes for the *timing* budget, no for a prompt-only *decision* loop.

| Question | Finding |
|---|---|
| Can the existing deployment hit a 600 ms tick? | **Yes.** p50 345 ms, p95 ~480 ms, ~3% of ticks overrun 600 ms. Cache reuse 96-99%, zero invalidations in append-only mode. |
| 400 ms tick? | **Marginal.** Per-request floor is ~340 ms (fixed overhead, not tokens), so 10% of ticks miss. Stateless prompts miss 45%. |
| 900 ms tick? | Comfortable (1% misses) but response latency p50 balloons to ~2.8 s; not worth it. |
| Does the video's probability gate work? | **Yes for what it claims.** Mid-sentence stops 11 → 3 with `P(silence) < 0.3` forced to speak. Thresholds 0.15 / 0.3 / 0.5 behave similarly. |
| Does a prompt-only LISTEN/SPEAK loop work? | **Not reliably.** The model stays silent after clear questions in 3-6 of the 8 windows that require a response, in every history/state variant. Silence bias grows with history, as the video describes. |
| Can an external rule fix that? | Partly. Forcing speech when the user paused and `P(silence) < 0.9-0.95` recovers 1-2 responses but emits junk fragments ("I", ".", "Want") and adds deadline misses (p95 ~620 ms). A patch, not a design. |
| Does an external turn-taking classifier fix it? | **Yes, in a stateless Y/N call on the 27B:** 8/8 utterance ends detected within one tick at threshold 0.5-0.7, 3-4 false positives of 86 hold ticks (most from "set a timer for ten minutes", which is arguably already complete), 344 ms per call. The Qwen3.5-0.8B CPU model (Needle's `qwen-fast`) is **not** usable zero-shot (no separation). |
| Does the sentinel need LoRA? | Not for the sentinel itself. Silence-vs-speech *timing* is where a LoRA (or the classifier) would be needed. |

## What was measured

- **Per-tick floor ≈ 340 ms**, independent of new-token count (21-token and 100-token ticks are within ~10%). A 31-token
  prefill costs ~300 ms and a 552-token prefill ~830 ms (fixed overhead of ~250 ms plus ~1 ms/token). Decode is 2 tokens for a
  silent tick, so almost all time is prefill setup. `-ub 64` and MTP are not the bottleneck here; I did not test them.
- **Append-only history is cheap while it lasts.** 2-minute soak: context 8.5K tokens, latency p50 361 / p95 416 ms, cache reuse
  0.99, no invalidations. Growth is ~65 tokens/s, so 98K fills in ~25 minutes.
- **Controlled rebuild** (summary + last 12 turns every 40 ticks) works but each rebuild re-prefills (~500 tokens, 0.65-0.85 s,
  one deadline miss). Soak: 8 invalidations, p95 486 ms. Needed only for calls longer than ~20 min.
- **Stateless snapshot** prompts cost ~+70 ms per tick (cache reuse 0.86), p95 ~600 ms, and behaved *worse* (3-4 false
  interruptions: no history means it cannot tell a pause from a finished thought).
- **Sentinel tokenisation.** `~` is 1 token (id 93); `listen` is 1; `LISTEN` is 2 (`LIST`+`EN`); `<listen>` is 3. A probability
  gate reads the first token, so a multi-token sentinel gates on a word prefix (`LIST`) that also starts real words. Use a
  single-token sentinel. `~` and lowercase `listen` both worked; `LISTEN` had a missed interruption.
- **Interruption.** 0 missed in nearly all configs; cut-in latency p50 ~820 ms after the dangerous word. Barge-in yield: 0
  failures, p50 ~550 ms.
- **Delegation.** The model emits `@hermes <task>` with a one-word acknowledgement ("On it.") reliably (missing in 2 of 23 unit runs).
  Relaying the result is **not** reliable: in 14 of 23 runs it never told the user, and in 8 of the 9 where it did it spoke over the user
  (`relay_during_user_speech`). Result delivery must be policy code, not model judgement.
- **Cache sharing with a second prompt.** Interleaving a different prompt on the single slot did not drop the cache (the
  server restores it) but cost +280 ms on the next generator call (350 → 631 ms). A second prompt type on the same server must
  not run in the same tick as generation.

## Architecture recommendation

Split the responsibilities and keep each component swappable:

```
mic → streaming STT (partial words, silence timer)
        │  600 ms ticks
        ▼
  Turn manager (code) ── state: speaking / user_talking / pending tasks / result queue
        │
        ├─ TurnClassifier  (stateless Y/N, P(done)) ─ run only on pause ticks, not while speaking
        ├─ Generator (27B, append-only history, gate lo=0.3)  ─ runs when classifier says Y, while
        │       speaking (to continue or yield), or when user words arrive mid-answer (barge-in/danger)
        ├─ TTS queue (cancellable; yield on barge-in is code, model confirms)
        └─ HermesBridge: delegate → ack; results held in a queue and injected only at a pause
```

Rules that fall out of the data:
1. **Never ask the history-bearing prompt to decide "should I speak now" on its own.** It drifts silent. Decide with the
   stateless classifier; give the generator a forced-speak option (`logit_bias` on the sentinel) when the classifier says Y.
2. **Keep the probability gate at 0.3** on the generator for mid-answer continuation.
3. **Use a single-token sentinel** and read first-token logprobs (`n_probs`, `top_logprobs`); this endpoint exposes them
   and the values are consistent with the sampled token.
4. **Append-only history, budget 600 ms ticks, rebuild only past ~20 minutes.** Don't clean silent turns out of history
   (that is what invalidates the cache).
5. **Hermes results are delivered by code at a pause.** The model phrases them, the harness chooses the moment.
6. **Run classifier ticks and generator ticks in different ticks**, never both in one (280 ms swap penalty). A dedicated
   second `llama-server` is the better fix but does not fit: both 3080s have ~1.5-2 GB free.

Not yet established (next experiments, in order):
- Hybrid mode wired into the loop (classifier trigger + generator): needs the harness change from rule 6 and a rerun.
- Barge-in/echo handling needs real audio; text-only mocks assume perfect cancellation.
- A small fine-tuned classifier (0.8B on AI1 CPU) to move turn-taking off the 27B entirely.
- Statistical power: multiple seeds / more conversations, and real STT output (this script's STT is clean).

## Files

- `bench.py` runner, `run_matrix.sh` the matrix executed here, `endpoint_eval.py` the classifier test.
- `duplex/` components: `llama.py` client, `scenarios.py` ground truth, `loop.py` (STT replay, TTS model, Hermes mock,
  prompt policies, gate, scheduler), `score.py`.
- `results/*.summary.json` + `*.jsonl` per-tick traces for every config; `RESULTS_TABLE.md` all configs;
  `endpoint_eval_*.json` classifier ticks.
