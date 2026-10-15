"""Scoring: behaviour against scripted ground truth + performance/cache metrics."""
import re
import statistics as st

from .loop import is_filler


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def user_active(sc, t, pad=400):
    return any(a <= t <= b + pad for a, b in sc.spans)


def real_speech(t):
    return t.kind in ("speech", "delegate") and not t.spoken_filler and (t.out or t.task)


def score_scenario(sc, ticks):
    m = dict(false_interruptions=0, premature_responses=0, missed_responses=0, wrong_responses=0,
             missed_interruptions=0, wrong_interruptions=0, yield_failures=0, yield_na=0, yield_ok=0,
             incomplete_answers=0, junk_utterances=0, incomplete_after_backchannel=0, repetitive_fillers=0, fillers=0,
             respond_latency_ms=[], interrupt_latency_ms=[], yield_latency_ms=[],
             delegations=0, delegation_missing=0, relay_ms=None, relay_during_user_speech=0, relay_missing=0)
    # -- starts of real speech
    starts = []
    for i, t in enumerate(ticks):
        prev = ticks[i - 1] if i else None
        if real_speech(t) and not t.was_speaking and not (prev and real_speech(prev) and t.t_dec - prev.t_dec < 2500):
            starts.append(t)
    for w in sc.windows:
        inw = [t for t in ticks if w.t0 <= t.t_dec <= w.t1]
        text = " ".join((t.out + " @hermes " + t.task) if t.task else t.out for t in inw if real_speech(t))
        if w.kind == "hold":
            for t in starts:
                if w.t0 <= t.t_dec <= w.t1:
                    if t.user_text:
                        m["false_interruptions"] += 1
                    else:
                        m["premature_responses"] += 1
        elif w.kind == "respond":
            hit = [t for t in inw if real_speech(t)]
            if not hit:
                m["missed_responses"] += 1
            else:
                m["respond_latency_ms"].append(hit[0].t_dec - w.t0)
                if w.kw and not re.search(w.kw, text, re.I | re.S):
                    m["wrong_responses"] += 1
        elif w.kind == "interrupt":
            hit = [t for t in inw if real_speech(t)]
            if not hit:
                m["missed_interruptions"] += 1
            else:
                m["interrupt_latency_ms"].append(hit[0].t_dec - w.t0)
                if w.kw and not re.search(w.kw, text, re.I):
                    m["wrong_interruptions"] += 1
        elif w.kind == "yield":
            before = [t for t in ticks if t.t_dec < w.t0]
            speaking = bool(before) and real_speech(before[-1]) or (inw and inw[0].was_speaking)
            if not speaking:
                m["yield_na"] += 1
                continue
            sil = [t for t in inw if t.kind == "silent"]
            late_speech = [t for t in ticks if w.t1 < t.t_dec <= w.t1 + 1500 and real_speech(t) and not t.user_text]
            if sil and not [t for t in inw if real_speech(t) and t.t_dec > sil[0].t_dec]:
                m["yield_ok"] += 1
                m["yield_latency_ms"].append(sil[0].t_dec - w.t0)
            else:
                m["yield_failures"] += 1
        elif w.kind == "relay":
            m["delegations"] += 1
            jobs_ready = [t for t in ticks if t.hermes_events]
            if not jobs_ready:
                m["relay_missing"] += 1
                continue
            t_ready = jobs_ready[0].result_ready or jobs_ready[0].t_start
            told = [t for t in ticks if t.t_dec >= t_ready and real_speech(t) and re.search(w.kw, t.out, re.I)]
            if not told:
                m["relay_missing"] += 1
            else:
                m["relay_ms"] = told[0].t_dec - t_ready
                if user_active(sc, told[0].t_dec, 0) or told[0].user_text:
                    m["relay_during_user_speech"] += 1
        if w.kind == "respond" and w.kw.startswith("@hermes") and not any(t.kind == "delegate" for t in inw):
            m["delegation_missing"] += 1
    # -- incomplete answers: speech without terminal punctuation followed by silence nobody asked for
    for i, t in enumerate(ticks[:-1]):
        n = ticks[i + 1]
        if t.kind == "speech" and not t.spoken_filler and not re.search(r"[.?!][\"')]*$", t.out) and n.kind == "silent":
            if not n.user_text:
                m["incomplete_answers"] += 1
            elif all(is_filler(x) or True for x in [n.user_text]) and len(n.user_text.split()) <= 2:
                m["incomplete_after_backchannel"] += 1
    # -- degenerate utterances: a 'response' that is punctuation or a lone function word (forced-speech artefacts)
    stop = {"i", "a", "the", "want", "it", "and", "so", "well", "um", "uh", "is", "to", "of", "that", "but", "or", "we", "you"}
    for t in starts:
        ws = [w for w in re.sub(r"[^\w' ]", " ", t.out.lower()).split()]
        if t.kind == "speech" and (not ws or (len(ws) == 1 and (ws[0] in stop or len(ws[0]) <= 1))):
            m["junk_utterances"] += 1
    # -- fillers
    last_f = -99
    for i, t in enumerate(ticks):
        if t.spoken_filler:
            m["fillers"] += 1
            if i - last_f <= 2:
                m["repetitive_fillers"] += 1
            last_f = i
    return m


def perf(ticks, T):
    called = [t for t in ticks if t.reqs > 0]       # hybrid skip ticks make no model call
    lat = [t.lat for t in called]
    ttft = [t.ttft for t in called]
    reuse = [t.cache_n / (t.cache_n + t.prompt_n) for t in ticks if t.cache_n + t.prompt_n]
    inval = sum(1 for i, t in enumerate(ticks) if i and not t.foreign and t.prompt_n > 120 and t.cache_n < 0.6 * (t.cache_n + t.prompt_n))
    late = [t.late for t in ticks]
    gen_t = [t for t in ticks if t.gen_ms > 0]
    sw = [t.gen_ms for t in gen_t if t.clf_since_gen > 0]
    nosw = [t.gen_ms for t in gen_t if t.clf_since_gen == 0]
    seq = [t.lat for t in ticks if t.clf_ms > 0 and t.gen_ms > 0]
    hyb = {
        "clf_calls": sum(1 for t in ticks if t.clf_ms > 0), "gen_calls": len(gen_t),
        "ticks_no_model_call": sum(1 for t in ticks if t.reqs == 0), "interrupt_cancels": sum(1 for t in ticks if t.route == "interrupt"),
        "skip_connective": sum(1 for t in ticks if t.route == "skip_connective"),
        "clf_ms_p50": pct([t.clf_ms for t in ticks if t.clf_ms > 0], 50),
        "gen_ms_p50_after_clf_switch": pct(sw, 50), "gen_ms_p50_no_switch": pct(nosw, 50), "gen_after_switch_n": len(sw),
        "seq_clf_gen_ticks": len(seq), "seq_clf_gen_lat_p50": pct(seq, 50), "seq_clf_gen_overrun_pct": 100 * sum(1 for x in seq if x > T) / max(1, len(seq)),
        "gen_prompt_new_after_switch_p50": pct([t.prompt_n for t in gen_t if t.clf_since_gen > 0], 50),
    }
    return {
        **hyb, "ticks": len(ticks), "lat_p50": pct(lat, 50), "lat_p95": pct(lat, 95), "lat_p99": pct(lat, 99), "lat_max": max(lat) if lat else 0,
        "ttft_p50": pct(ttft, 50), "ttft_p95": pct(ttft, 95),
        "miss_lat_pct": 100 * sum(1 for x in lat if x > T) / max(1, len(lat)),
        "miss_ttft_pct": 100 * sum(1 for x in ttft if x > T) / max(1, len(ttft)),
        "late_p95_ms": pct(late, 95), "late_max_ms": max(late) if late else 0,
        "prompt_new_p50": pct([t.prompt_n for t in ticks], 50), "prompt_new_p95": pct([t.prompt_n for t in ticks], 95),
        "cache_reuse_mean": st.mean(reuse) if reuse else 0, "ctx_max": max((t.ctx for t in ticks), default=0),
        "invalidations": inval, "foreign_evictions": sum(1 for t in ticks if t.foreign), "rebuilds": sum(1 for t in ticks if t.rebuilt),
        "gate_forced_speech": sum(1 for t in ticks if t.gate == "forced_speech"),
        "gate_forced_silence": sum(1 for t in ticks if t.gate == "forced_silence"),
    }


def aggregate(per):
    agg = {}
    for m in per:
        for k, v in m.items():
            if isinstance(v, list):
                agg.setdefault(k, []).extend(v)
            elif v is None:
                continue
            else:
                agg[k] = agg.get(k, 0) + v
    for k in ("respond_latency_ms", "interrupt_latency_ms", "yield_latency_ms"):
        xs = agg.pop(k, [])
        agg[k.replace("_ms", "_p50_ms")] = round(pct(xs, 50)) if xs else None
        agg[k.replace("_ms", "_n")] = len(xs)
    return agg


def e2e(sc, ticks):
    """End-to-end response latency per respond window: user's last word -> first spoken word (virtual ms), decomposed."""
    out = []
    for w in sc.windows:
        if w.kind != "respond":
            continue
        hit = [t for t in ticks if w.t0 <= t.t_dec and real_speech(t)]
        if not hit or hit[0].t_dec > w.t1:
            out.append(None)
            continue
        t = hit[0]
        out.append({"scenario": sc.name, "e2e_ms": t.t_dec - w.t0, "tick_wait_ms": t.t_start - w.t0, "clf_ms": t.clf_ms,
                    "gen_ttft_ms": t.ttft, "route": t.route, "late_ms": t.late})
    return out
