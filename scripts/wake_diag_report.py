#!/usr/bin/env python3
"""Summarise live/wake_diag.jsonl (written when WAKE_DIAG=1) for one test run.

  scripts/wake_diag_report.py --start "2026-11-08 20:00" --end "2026-11-08 20:12" --attempts 20 [--file PATH]

Times are local. --attempts is how many times the tester said the wake phrase in the window. Reports activations, first-attempt
rate, near misses (phrase heard somewhere but not at the start, so the anchored matcher refused it), decode latency and levels.
"""
import argparse
import datetime as dt
import json
import os
import statistics as st


def ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))] if v else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=os.path.expanduser("~/.local/share/nina/wake_diag.jsonl"))
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--attempts", type=int, default=0)
    a = ap.parse_args()
    t0, t1 = ts(a.start), ts(a.end)
    rows = [r for r in map(json.loads, open(a.file)) if t0 <= r["ts"] <= t1]
    checks = [r for r in rows if r["event"] == "wake_check"]
    dec = [r for r in rows if r["event"] == "wake_decision"]
    woke = [r for r in dec if r["matched"]]
    near = [r for r in dec if r["floating_match"] and not r["matched"]]
    speech = [r for r in checks if r["words"] > 0]
    print("window %s .. %s" % (a.start, a.end))
    print("wake checks: %d (%d with words, %d first-check, %d final)" % (len(checks), len(speech), sum(not r["final"] for r in checks), sum(r["final"] for r in checks)))
    print("activations: %d" % len(woke))
    if a.attempts:
        print("attempts said: %d -> activation rate %.0f%%" % (a.attempts, 100.0 * len(woke) / a.attempts))
    print("near misses (phrase present, not at start of transcript): %d" % len(near))
    dm = [r["decode_ms"] for r in checks]
    if dm:
        print("decode ms: median %d  p95 %d  max %d" % (st.median(dm), pct(dm, 0.95), max(dm)))
    fin = [r["audio_s"] for r in checks if r["final"]]
    if fin:
        print("final utterance length s: median %.1f  p95 %.1f  max %.1f  (>5 s: %d)" % (st.median(fin), pct(fin, 0.95), max(fin), sum(x > 5 for x in fin)))
    pk = [r["peak"] for r in checks]
    if pk:
        print("utterance peak: median %.2f  p95 %.2f  clipped (>=0.99): %d" % (st.median(pk), pct(pk, 0.95), sum(x >= 0.99 for x in pk)))
    hours = max((t1 - t0) / 3600.0, 1e-9)
    print("non-wake decodes with speech per hour (false-positive exposure): %.0f" % ((len(speech) - len(woke)) / hours))


if __name__ == "__main__":
    main()
