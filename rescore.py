#!/usr/bin/env python3
"""Re-score stored per-tick traces with the current scoring code (no model calls).
Used after fixing is_filler ('On it.' was wrongly a filler, which hid every delegation ack). Originals are untouched;
rescored summaries go to <results>/rescored/."""
import dataclasses, glob, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from duplex import scenarios
from duplex.loop import Tick, is_filler
from duplex.score import aggregate, e2e, perf, score_scenario

src = sys.argv[1] if len(sys.argv) > 1 else "results"
dst = os.path.join(src, "rescored")
os.makedirs(dst, exist_ok=True)
fields = {f.name for f in dataclasses.fields(Tick)}
for jl in sorted(glob.glob(os.path.join(src, "*.jsonl"))):
    name = os.path.basename(jl)[:-6]
    old = json.load(open(jl[:-6] + ".summary.json"))
    scs = scenarios.all_unit() if old["set"] == "unit" else [scenarios.soak()]
    rows = [json.loads(l) for l in open(jl)]
    per, ticks_all, lat_e2e = [], [], []
    for sc in scs:
        tk = []
        for r in rows:
            if r["sc"] != sc.name:
                continue
            r = {k: v for k, v in r.items() if k in fields}
            r["spoken_filler"] = r["kind"] == "speech" and bool(r["out"]) and is_filler(r["out"])
            tk.append(Tick(**r))
        per.append(score_scenario(sc, tk)); ticks_all += tk; lat_e2e += e2e(sc, tk)
    new = dict(old, perf=perf(ticks_all, old["tick_ms"]), behaviour=aggregate(per), e2e=lat_e2e, rescored="is_filler fix 2026-10-14",
               per_scenario={sc.name: {k: v for k, v in m.items() if v and not isinstance(v, list)} for sc, m in zip(scs, per)})
    json.dump(new, open(os.path.join(dst, name + ".summary.json"), "w"), indent=1)
print("rescored", len(glob.glob(os.path.join(dst, "*.json"))), "summaries ->", dst)
