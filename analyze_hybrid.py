#!/usr/bin/env python3
"""Compare hybrid vs prompt-only across repeated runs; writes results/HYBRID_REPORT.md.
Usage: analyze_hybrid.py  (reads results/rep_* and the earlier single runs; no model calls)"""
import glob, json, os, statistics as st, sys
HERE = os.path.dirname(os.path.abspath(__file__))
R = os.path.join(HERE, "results")


def runs(pattern):
    return [json.load(open(f)) for f in sorted(glob.glob(os.path.join(R, pattern, "*.summary.json")))]


def ms(xs, fmt="%.1f"):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "-"
    if len(xs) == 1:
        return fmt % xs[0]
    return (fmt + " ±" + fmt + " [" + fmt + "–" + fmt + "]") % (st.mean(xs), st.pstdev(xs), min(xs), max(xs))


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else None


def collect(rs):
    c = {}
    def add(k, v):
        c.setdefault(k, []).append(v)
    for s in rs:
        p, b = s["perf"], s["behaviour"]
        e = [x for x in s["e2e"] if x]
        n_resp = len(s["e2e"])
        add("eou_detected", len(e)); add("eou_total", n_resp)
        add("e2e_p50", pct([x["e2e_ms"] for x in e], 50)); add("e2e_p95", pct([x["e2e_ms"] for x in e], 95))
        add("false_start", b["false_interruptions"] + b["premature_responses"])
        add("wrong_resp", b.get("wrong_responses", 0))
        add("junk", b.get("junk_utterances", 0))
        add("missed_int", b["missed_interruptions"]); add("yield_fail", b["yield_failures"])
        add("incomplete", b["incomplete_answers"]); add("rep_fill", b["repetitive_fillers"])
        add("relay_missing", b.get("relay_missing", 0)); add("relay_over_user", b.get("relay_during_user_speech", 0))
        add("relay_ms", b.get("relay_ms"))
        add("lat_p50", p["lat_p50"]); add("lat_p95", p["lat_p95"]); add("lat_p99", p["lat_p99"])
        add("overrun", p["miss_lat_pct"]); add("ttft_p95", p["ttft_p95"]); add("reuse", p["cache_reuse_mean"])
        add("inval", p["invalidations"]); add("late_p95", p["late_p95_ms"])
        add("clf_calls", p.get("clf_calls")); add("no_call", p.get("ticks_no_model_call"))
        add("gen_sw", p.get("gen_ms_p50_after_clf_switch")); add("gen_nosw", p.get("gen_ms_p50_no_switch"))
        add("seq_lat", p.get("seq_clf_gen_lat_p50")); add("seq_over", p.get("seq_clf_gen_overrun_pct"))
        add("ticks", p["ticks"])
    return c


CONFIGS = [
    ("hybrid / inline clf, temp 0.3 ×5", "rep_hybrid_inline_t03"),
    ("hybrid / inline clf, temp 0 ×3 (timing variance)", "rep_hybrid_inline_t0"),
    ("hybrid / stateless clf, temp 0.3 ×3", "rep_hybrid_stateless_t03"),
    ("prompt-only best (gate 0.3 + speak-gate 0.95), temp 0.3 ×5", "rep_promptonly_best_t03"),
    ("prompt-only best, temp 0 ×3", "rep_promptonly_best_t0"),
]
ROWS = [
    ("End-of-utterance: windows answered (of %d)", None),
    ("e2e response latency p50 (last word → first spoken word), ms", "e2e_p50"),
    ("e2e response latency p95, ms", "e2e_p95"),
    ("false starts (spoke while user mid-thought)", "false_start"),
    ("wrong-content responses", "wrong_resp"),
    ("degenerate utterances ('.', 'I', 'Want')", "junk"),
    ("missed interruptions (danger cut-in, of 2)", "missed_int"),
    ("barge-in yield failures", "yield_fail"),
    ("mid-sentence stops", "incomplete"),
    ("repetitive fillers", "rep_fill"),
    ("Hermes result never told (of 1)", "relay_missing"),
    ("Hermes result spoken over user", "relay_over_user"),
    ("Hermes result delivery delay after ready, ms", "relay_ms"),
    ("tick latency p50, ms", "lat_p50"), ("tick latency p95, ms", "lat_p95"), ("tick latency p99, ms", "lat_p99"),
    ("ticks over 600 ms budget, %", "overrun"), ("cache reuse (mean)", "reuse"), ("cache invalidations", "inval"),
    ("classifier calls / run", "clf_calls"), ("ticks with no model call", "no_call"),
    ("generator p50 right after classifier call, ms", "gen_sw"), ("generator p50 otherwise, ms", "gen_nosw"),
    ("classifier+generator in one tick: p50 ms", "seq_lat"), ("… of those over budget, %", "seq_over"),
]

out = ["# Hybrid v2 vs best prompt-only: report\n", "Mean ±sd [min–max] over repeated runs of the 10 unit scenarios (600 ms ticks, AI2 Qwen3.8-27B).\n"]
cols = [(n, collect(runs(d))) for n, d in CONFIGS if runs(d)]
out.append("| metric | " + " | ".join(n for n, _ in cols) + " |")
out.append("|---|" + "---|" * len(cols))
for label, key in ROWS:
    cells = []
    for n, c in cols:
        if key is None:
            cells.append("%s / %d" % (ms(c["eou_detected"], "%.1f"), c["eou_total"][0]))
        else:
            fmt = "%.2f" if key == "reuse" else "%.0f" if "lat" in key or "e2e" in key or key in ("relay_ms", "gen_sw", "gen_nosw", "seq_lat", "seq_over", "overrun") else "%.1f"
            cells.append(ms(c.get(key, []), fmt))
    out.append("| %s | %s |" % (label % 8 if "%d" in label else label, " | ".join(cells)))
txt = "\n".join(out) + "\n"
open(os.path.join(R, "HYBRID_REPORT.md"), "w").write(txt)
print(txt)
