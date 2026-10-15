"""nina-speakers: enroll | list | test | remove | eval   (run: python -m speaker_id.cli ...)"""
import argparse, glob, json, os, sys, time
import numpy as np
from . import embed, store, identify

SEG_S = 4.0


def segments(x, seg_s=SEG_S):
    n = int(embed.SR * seg_s)
    segs = [x[i:i + n] for i in range(0, max(len(x) - n // 2, 1), n)]
    return [s for s in segs if embed.voiced_ms(s) >= identify.MIN_VOICED_MS]


def _wavs(paths):
    out = []
    for p in paths:
        out += sorted(glob.glob(os.path.join(p, "*.wav"))) if os.path.isdir(p) else [p]
    return out


def cmd_enroll(a):
    embs = []
    for f in _wavs(a.wav):
        x = embed.load_wav(f)
        embs += [embed.embed(s) for s in segments(x)] if len(x) > embed.SR * SEG_S * 1.5 else (
            [embed.embed(x)] if embed.voiced_ms(x) >= identify.MIN_VOICED_MS else [])
    if len(embs) < 3:
        sys.exit(f"only {len(embs)} usable clips; need >=3 (aim for 30-60 s of varied speech)")
    p = store.save_profile(a.speaker, a.name or a.speaker.capitalize(), a.role, embs)
    print(f"enrolled {a.speaker}: {p['enrollment_samples']} clips (audio not retained)")


def cmd_list(a):
    for p in store.list_profiles():
        print(json.dumps(p))


def cmd_remove(a):
    print("removed" if store.remove(a.speaker) else "nothing to remove", a.speaker)


def cmd_test(a):
    ident = identify.Identifier()
    for f in _wavs(a.wav):
        t = time.time(); r = ident.identify(embed.load_wav(f))
        print(f"{os.path.basename(f)}: {r.status.value} {r.speaker_id} score={r.score:.3f} "
              f"margin={r.margin:.3f} {int((time.time()-t)*1000)}ms")


def cmd_eval(a):
    """Layout: <dir>/<speaker>/<condition>/*.wav (condition optional). Leave-one-file-out against
    enrollment built from the other files; writes calibration.json."""
    files = {}
    for sp in sorted(os.listdir(a.dir)):
        fs = sorted(glob.glob(os.path.join(a.dir, sp, "**", "*.wav"), recursive=True))
        if fs: files[sp] = fs
    emb = {sp: [(f, [embed.embed(s) for s in segments(embed.load_wav(f))] or None) for f in fs]
           for sp, fs in files.items()}
    gen, imp, rows = [], [], []
    for sp, items in emb.items():
        for i, (f, es) in enumerate(items):
            if not es: continue
            prof = {}
            for s2, it2 in emb.items():
                M = [e for j, (_, e2) in enumerate(it2) if e2 and not (s2 == sp and j == i) for e in e2]
                if len(M) >= 3: prof[s2] = np.stack(M)
            for e in es:
                sc = identify.score_embedding(e, prof)
                rows.append((sp, f, sc))
                for s2, v in sc.items():
                    (gen if s2 == sp else imp).append(v)
    gen, imp = np.array(gen), np.array(imp)
    ths = np.linspace(-0.2, 0.9, 221)
    far = lambda t: float((imp >= t).mean()); frr = lambda t: float((gen < t).mean())
    eer_t = min(ths, key=lambda t: abs(far(t) - frr(t)))
    t_far1 = next((t for t in ths if far(t) <= 0.01), ths[-1])   # <=1% false accept
    # logistic calibration (score -> P(genuine)) by simple Newton fit
    X = np.concatenate([gen, imp]); y = np.concatenate([np.ones(len(gen)), np.zeros(len(imp))])
    a_, b_ = 10.0, -5.0
    for _ in range(200):
        p = 1 / (1 + np.exp(-(a_ * X + b_))); w = p * (1 - p) + 1e-6
        H = np.array([[ (w*X*X).sum(), (w*X).sum()], [(w*X).sum(), w.sum()]]) + 1e-6*np.eye(2)
        g = np.array([((p - y) * X).sum(), (p - y).sum()])
        a_, b_ = np.array([a_, b_]) - np.linalg.solve(H, g)
    # confusion at chosen threshold, and per-pair confusion
    thr = float(max(t_far1, eer_t)); conf = {}
    for sp, f, sc in rows:
        d = identify.decide(sc, 10**6, thr, a.margin)
        key = (sp, d.speaker_id or d.status.value); conf[key] = conf.get(key, 0) + 1
    print(f"genuine n={len(gen)} mean={gen.mean():.3f}  impostor n={len(imp)} mean={imp.mean():.3f}")
    print(f"EER thr={eer_t:.3f} (FAR={far(eer_t):.3f} FRR={frr(eer_t):.3f}); thr@FAR<=1%={t_far1:.3f}")
    print("chosen threshold", round(thr, 3), "margin", a.margin)
    for (s, r), n in sorted(conf.items()): print(f"  true={s:8s} -> {r:12s} {n}")
    if a.write:
        store.save_thresholds({"threshold": thr, "margin": a.margin, "calibration": {"a": float(a_), "b": float(b_)},
                               "n_genuine": len(gen), "n_impostor": len(imp)})
        print("calibration written")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nina-speakers"); sub = ap.add_subparsers(dest="c", required=True)
    e = sub.add_parser("enroll"); e.add_argument("speaker"); e.add_argument("wav", nargs="+")
    e.add_argument("--name"); e.add_argument("--role", default="adult", choices=["adult", "child"])
    e.set_defaults(f=cmd_enroll)
    sub.add_parser("list").set_defaults(f=cmd_list)
    r = sub.add_parser("remove"); r.add_argument("speaker"); r.set_defaults(f=cmd_remove)
    t = sub.add_parser("test"); t.add_argument("wav", nargs="+"); t.set_defaults(f=cmd_test)
    v = sub.add_parser("eval"); v.add_argument("dir"); v.add_argument("--margin", type=float, default=identify.DEFAULT_MARGIN)
    v.add_argument("--write", action="store_true"); v.set_defaults(f=cmd_eval)
    a = ap.parse_args(argv); a.f(a)


if __name__ == "__main__":
    main()
