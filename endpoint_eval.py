#!/usr/bin/env python3
"""Stateless turn-completion classifier on the same Qwen endpoint (external-state alternative to the
history-biased silence sentinel). One short prompt per tick, no growing history, so nothing can poison it.
Scores P(user finished and expects a reply) per tick against the scripted ground truth."""
import json, math, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from duplex import scenarios
from duplex.llama import Llama
from duplex.loop import chat, THINK_PREFIX
from duplex.score import pct

SYS = chat("system",
    "You watch a live transcript of a person talking to a voice assistant. Decide whether the person has FINISHED their turn "
    "and is waiting for a reply right now. Answer N if they are mid-sentence, trailing off, hesitating (um, uh, so, and), "
    "pausing in the middle of a thought, or telling a story that is still going. Answer Y only if they asked a question or gave "
    "a complete request/statement and have gone quiet. Reply with exactly one letter: Y or N.")

def main(T=600):
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
    tag = sys.argv[2] if len(sys.argv) > 2 else "qwen27b"
    llm = Llama(base)
    if llm.busy():
        print("busy"); return
    ids = {c: llm.tokenize(c)[0] for c in "YN"}
    rows, lats = [], []
    for sc in scenarios.all_unit():
        resp = [w for w in sc.windows if w.kind == "respond"]
        if not resp:
            continue
        holds = [w for w in sc.windows if w.kind == "hold"]
        words, t = [], T
        while t < sc.end_ms:
            ws = [w for e, w in sc.words if e <= t]
            last = max([e for e, _ in sc.words if e <= t] or [0])
            silent = t - last
            prompt = SYS + chat("user", 'Transcript so far: "%s"\nThe person has been silent for %.1f seconds.\nFinished? (Y/N)' % (" ".join(ws), silent / 1000)) + THINK_PREFIX
            r = llm.complete(prompt, n_predict=1, n_probs=8)
            lp = dict(r["first_top"])
            py = math.exp(lp.get(ids["Y"], -20)); pn = math.exp(lp.get(ids["N"], -20))
            p = py / (py + pn + 1e-12)
            label = None
            if any(w.t0 <= t <= w.t1 for w in holds) and not any(w.t0 <= t for w in resp):
                label = 0
            elif any(w.t0 <= t <= w.t0 + 1800 for w in resp):
                label = 1
            rows.append((sc.name, t, silent, p, label)); lats.append(r["total"] * 1000)
            t += T
    pos = [r for r in rows if r[4] == 1]; neg = [r for r in rows if r[4] == 0]
    print("[%s] ticks classified: %d (pos %d, neg %d)   latency p50/p95 %.0f/%.0f ms (cache of system prefix only)" % (
        tag, len(rows), len(pos), len(neg), pct(lats, 50), pct(lats, 95)))
    print("thr   FP(neg called Y)  per-utterance detect (<=1 tick after end / <=2 ticks)  mean delay ms")
    resp_by_sc = {sc.name: [w for w in sc.windows if w.kind == "respond"] for sc in scenarios.all_unit()}
    for thr in (0.3, 0.5, 0.7, 0.8, 0.9, 0.95):
        fp = sum(1 for r in neg if r[3] >= thr)
        det1 = det2 = n = 0; delays = []
        for name, ws in resp_by_sc.items():
            for w in ws:
                n += 1
                hit = [r for r in rows if r[0] == name and r[1] >= w.t0 and r[3] >= thr]
                if hit:
                    d = hit[0][1] - w.t0; delays.append(d)
                    det1 += d <= T; det2 += d <= 2 * T
        print("%.2f  %3d/%d            %d/%d  %d/%d              %s" % (thr, fp, len(neg), det1, n, det2, n,
              "%.0f" % (sum(delays) / len(delays)) if delays else "-"))
    json.dump(rows, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "endpoint_eval_%s.json" % tag), "w"))
    print("\nhardest negatives (p high while user still mid-turn):")
    for r in sorted(neg, key=lambda r: -r[3])[:6]:
        print("  %-22s t=%5d silent=%4.1fs p=%.2f" % (r[0], r[1], r[2] / 1000, r[3]))
    print("weakest positives:")
    for r in sorted(pos, key=lambda r: r[3])[:6]:
        print("  %-22s t=%5d silent=%4.1fs p=%.2f" % (r[0], r[1], r[2] / 1000, r[3]))

main()
