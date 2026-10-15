"""Summarize needle_voice telemetry and list what to teach it.

Usage: python3 analyze_voice.py path/to/telemetry.jsonl [--since 2026-09-24T12:00]
Read-only. Pull the file from HAOS first: /config/needle_voice/telemetry.jsonl
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict

# Fallbacks that mean "it didn't know this household's words" -> fixable with the lexicon / HA aliases.
TEACHABLE = ("no_candidates", "ambiguous_target", "needle_policy", "qwen:unknown_target", "qwen:target_disagrees_with_retrieval")
# Fallbacks that are deliberate (guards) or infrastructure; not lexicon problems.
DELIBERATE = ("guard:", "passthrough_flag", "disabled")


def load(path: str, since: str | None = None) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a truncated final line
            if since and str(r.get("timestamp", "")) < since:
                continue
            rows.append(r)
    return rows


def pct(vals: list[float], p: float):
    if not vals:
        return None
    s = sorted(vals)
    return round(s[min(len(s) - 1, int(round((len(s) - 1) * p)))], 1)


def summarize(rows: list[dict]) -> dict:
    routes = Counter(r.get("route") for r in rows)
    lat: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if isinstance(r.get("total_latency_ms"), (int, float)):
            lat[r.get("route")].append(r["total_latency_ms"])
    reasons = Counter(r.get("fallback_reason") for r in rows if r.get("route") == "hermes")
    teach: Counter = Counter()
    for r in rows:
        fr = r.get("fallback_reason") or ""
        if r.get("route") == "hermes" and fr.startswith(TEACHABLE):
            teach[(r.get("input", "").strip().lower(), fr)] += 1
    fast = sum(routes[k] for k in ("needle", "qwen_fast", "rules"))
    return {
        "total": len(rows),
        "routes": dict(routes),
        "fast_path_share": round(fast / len(rows), 3) if rows else None,
        "latency_ms": {k: {"n": len(v), "p50": pct(v, 0.5), "p95": pct(v, 0.95), "max": round(max(v), 1)} for k, v in lat.items()},
        "hermes_reasons": dict(reasons),
        "teachable": teach.most_common(),
        "deliberate_escalations": sum(v for k, v in reasons.items() if str(k).startswith(DELIBERATE)),
        "failures": [r.get("input") for r in rows if r.get("final_success") is False],
    }


def report(s: dict) -> str:
    out = [f"requests: {s['total']}   routes: {s['routes']}   fast-path share: {s['fast_path_share']}"]
    for route, l in s["latency_ms"].items():
        out.append(f"  {route:10s} n={l['n']:<4} p50={l['p50']}ms p95={l['p95']}ms max={l['max']}ms")
    out.append(f"fell to Hermes, by reason: {s['hermes_reasons']}")
    out.append(f"  (of which deliberate guards/flags: {s['deliberate_escalations']})")
    if s["failures"]:
        out.append(f"REQUESTS THAT FAILED FOR THE USER: {s['failures']}")
    out.append("")
    out.append("Phrases worth teaching (fell to Hermes because a name wasn't recognized):")
    if not s["teachable"]:
        out.append("  none yet")
    for (text, why), n in s["teachable"]:
        out.append(f"  x{n}  {text!r}   [{why}]")
    if s["total"] < 30:
        out.append(f"\nNOTE: only {s['total']} requests; treat shares/latencies as anecdotes, not measurements.")
    return "\n".join(out)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else None
    if since in args:
        args.remove(since)
    print(report(summarize(load(args[0], since))))
