#!/usr/bin/env python3
"""Duplex simulator benchmark. Text-only; talks to an existing llama-server over HTTP and changes nothing on it.

  python3 bench.py --probe
  python3 bench.py --modes append,append_state --ticks 600 --gates none,0.3 --set unit
  python3 bench.py --modes rebuild --ticks 600 --set soak
"""
import argparse
import dataclasses
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from duplex import scenarios
from duplex.llama import Llama, prob_of
from duplex.hybrid import HybridRunner
from duplex.loop import Runner, system_prompt, chat, THINK_PREFIX
from duplex.score import aggregate, e2e, perf, score_scenario

HERE = os.path.dirname(os.path.abspath(__file__))


def probe(llm):
    """Verify how candidate silence sentinels tokenise and how first-token probability behaves."""
    print("sentinel candidates (tokenisation on this model):")
    for s in ["~", "LISTEN", "listen", "<listen>", "...", "…", "-", "_"]:
        ids = llm.tokenize(s)
        print("  %-10r -> %d token(s) %s" % (s, len(ids), ids))
    for s in ["~", "listen", "LISTEN"]:
        sid = llm.tokenize(s)[0]
        sysm = chat("system", system_prompt(s, 600, False))
        for label, user in [("mid-thought", '[6.0s] "so I was thinking about"'), ("question", '[6.0s] "what is the capital of Australia"'),
                            ("silence", "[6.0s] (silence)")]:
            r = llm.complete(sysm + chat("user", user) + THINK_PREFIX, n_predict=12)
            top = ", ".join("%s=%.2f" % (i, 2.718281828 ** lp) for i, lp in r["first_top"][:4])
            print("  S=%-7r %-12s out=%-30r p(first tok == S first tok)=%.3f  top4 ids: %s" % (s, label, r["text"][:30], prob_of(r["first_top"], sid), top))


def capture_config(llm, a):
    """Exact configuration of a run: server (read-only queries), prompts, params, code hashes."""
    import hashlib, platform, subprocess
    def h(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()[:12]
    try:
        props = llm.get_json("/props")
        server = {k: props.get(k) for k in ("model_path", "model_alias", "total_slots") if k in props}
        server["n_ctx"] = props.get("default_generation_settings", {}).get("n_ctx")
    except Exception as e:
        server = {"error": str(e)}
    try:
        server["cmdline"] = subprocess.run("ps -eo args | grep '[l]lama-server' | grep -- '--port 8080'", shell=True,
                                           capture_output=True, text=True).stdout.strip()
    except Exception:
        pass
    code = {p: h(os.path.join(HERE, p)) for p in ["bench.py", "duplex/loop.py", "duplex/hybrid.py", "duplex/score.py", "duplex/scenarios.py", "duplex/llama.py"]}
    return {"date": time.strftime("%Y-%m-%d %H:%M:%S"), "host": platform.node(), "endpoint": a.base, "server": server,
            "args": vars(a), "code_sha256_12": code,
            "scenario_set": a.set, "scenarios": [s.name for s in (scenarios.all_unit() if a.set == "unit" else [scenarios.soak()])],
            "prompt_sha": {"generator_system_600": hashlib.sha256(system_prompt(a.sentinel, 600, True).encode()).hexdigest()[:12]}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--modes", default="append_state")
    ap.add_argument("--ticks", default="600")
    ap.add_argument("--gates", default="none")
    ap.add_argument("--sentinel", default="~")
    ap.add_argument("--set", default="unit", choices=["unit", "soak"])
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    ap.add_argument("--clf-thr", type=float, default=0.5)
    ap.add_argument("--clf-mode", default="stateless", choices=["stateless", "inline"])
    ap.add_argument("--cand-ms", type=int, default=300)
    ap.add_argument("--pause-ms", type=int, default=1200)
    ap.add_argument("--temp", type=float, default=0.0, help="generator temperature (classifier always 0)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--repeat", type=int, default=1, help="repeat each config N times with seed, seed+1, ...")
    ap.add_argument("--allow-busy", action="store_true")
    a = ap.parse_args()

    llm = Llama(a.base)
    if llm.busy() and not a.allow_busy:
        print("slot busy (production traffic); refusing. Re-run when idle or pass --allow-busy.")
        return 2
    if a.probe:
        probe(llm)
        return 0
    scs = scenarios.all_unit() if a.set == "unit" else [scenarios.soak()]
    if a.only:
        scs = [s for s in scs if s.name in a.only.split(",")]
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "config_%s.json" % time.strftime("%Y%m%d_%H%M%S")), "w") as f:
        json.dump(capture_config(llm, a), f, indent=1)
    for mode in a.modes.split(","):
        for T in [int(x) for x in a.ticks.split(",")]:
            for g in a.gates.split(","):
                for rep in range(a.repeat):
                    seed = a.seed + rep
                    lo, _, hi = g.partition("/")
                    gate = None if lo in ("none", "") else float(lo)
                    gate_hi = float(hi) if hi else None
                    name = "%s_%s_T%d_g%s_S%s%s" % (a.set, mode, T, g.replace("/", "-hi"), {"~": "tilde"}.get(a.sentinel, a.sentinel),
                                                  "" if a.repeat == 1 and a.temp == 0 else "_t%g_s%d" % (a.temp, seed))
                    if mode == "hybrid":
                        run = HybridRunner(llm, T, S=a.sentinel, gate=gate or 0.3, clf_thr=a.clf_thr, pause_ms=a.pause_ms,
                                           cand_ms=a.cand_ms, temp=a.temp, seed=seed, clf_mode=a.clf_mode)
                    else:
                        run = Runner(llm, mode, T, S=a.sentinel, gate=gate, gate_hi=gate_hi, temp=a.temp, seed=seed)
                    allticks, per, t0, lat_e2e = [], [], time.time(), []
                    for sc in scs:
                        tk = run.run(sc)
                        allticks += tk
                        per.append(score_scenario(sc, tk))
                        lat_e2e += e2e(sc, tk)
                        print("  %-24s %3d ticks" % (sc.name, len(tk)), flush=True)
                    with open(os.path.join(a.out, name + ".jsonl"), "w") as f:
                        for t in allticks:
                            f.write(json.dumps(dataclasses.asdict(t)) + "\n")
                    s = {"config": name, "mode": mode, "tick_ms": T, "gate": gate, "gate_hi": gate_hi, "sentinel": a.sentinel,
                         "set": a.set, "clf_mode": a.clf_mode if mode == "hybrid" else None, "temp": a.temp, "seed": seed, "clf_thr": a.clf_thr if mode == "hybrid" else None,
                         "wall_s": round(time.time() - t0), "perf": perf(allticks, T), "behaviour": aggregate(per),
                         "e2e": lat_e2e,
                         "per_scenario": {sc.name: {k: v for k, v in m.items() if v and not isinstance(v, list)} for sc, m in zip(scs, per)}}
                    with open(os.path.join(a.out, name + ".summary.json"), "w") as f:
                        json.dump(s, f, indent=1)
                    p, b = s["perf"], s["behaviour"]
                    print("%s  lat p50/p95/p99 %.0f/%.0f/%.0f ms  miss %.0f%%  reuse %.2f  inval %d | FI %d PR %d MR %d MI %d YF %d INC %d REP %d" % (
                        name, p["lat_p50"], p["lat_p95"], p["lat_p99"], p["miss_lat_pct"], p["cache_reuse_mean"], p["invalidations"],
                        b["false_interruptions"], b["premature_responses"], b["missed_responses"], b["missed_interruptions"],
                        b["yield_failures"], b["incomplete_answers"], b["repetitive_fillers"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
