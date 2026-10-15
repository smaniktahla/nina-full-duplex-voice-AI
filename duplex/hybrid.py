"""Hybrid mode (v2): conversation state, interruption handling and Hermes delivery live in code.

The model is only asked for judgement where code has none:
  * generator (append-only history, silence-probability gate): continue an answer, cut in on danger, delegate, phrase text
  * stateless Y/N classifier: has the user finished their turn?

State machine (outside the model):
  LISTENING           assistant silent, user may be talking or idle
  RESPONDING          assistant speech is queued/playing
  INTERRUPTED         assistant speech was cancelled by a genuine user interruption; stays until the assistant speaks again
  WAITING_FOR_HERMES  idle with a delegated task outstanding (running, or finished and queued for delivery)
"""
import math
import re

from .llama import prob_of
from .loop import (HermesMock, PromptPolicy, Runner, SttReplay, THINK_PREFIX, Tick, TtsModel, chat, is_filler, parse_out)

LISTENING, RESPONDING, INTERRUPTED, WAITING = "LISTENING", "RESPONDING", "INTERRUPTED", "WAITING_FOR_HERMES"

BACKCHANNEL = {"mhm", "mm", "mmhm", "mmhmm", "mm-hm", "mm-hmm", "hm", "hmm", "yeah", "yep", "yes", "ok", "okay", "right", "uh-huh",
               "uhhuh", "uh", "huh", "sure", "i", "see", "got", "it", "go", "on", "really", "wow", "ah", "oh", "aha", "true",
               "exactly", "gotcha", "cool", "interesting"}
CONNECTIVES = {"um", "uh", "er", "and", "so", "but", "or", "because", "the", "a", "an", "to", "of", "about", "with", "then",
               "like", "if", "when", "my"}   # conservative: words that cannot end a finished request ("do about it", "what it is" can)

CLASSIFIER_SYS = chat("system",
    "You watch a live transcript of a person talking to a voice assistant. Decide whether the person has FINISHED their turn "
    "and is waiting for a reply right now. Answer N if they are mid-sentence, trailing off, hesitating (um, uh, so, and), "
    "pausing in the middle of a thought, or telling a story that is still going. Answer Y only if they asked a question or gave "
    "a complete request/statement and have gone quiet. Reply with exactly one letter: Y or N.")


def norm_words(text):
    return [w for w in (re.sub(r"[^\w'-]", "", x.lower()) for x in text.split()) if w]


def is_backchannel(text):
    ws = norm_words(text)
    return bool(ws) and all(w in BACKCHANNEL for w in ws)


def trailing_connective(text):
    ws = norm_words(text)
    return bool(ws) and ws[-1] in CONNECTIVES


class TurnManager:
    """Deterministic router. Returns one of: interrupt | gen | relay | classify | skip."""

    def __init__(self, cand_ms=300, pause_ms=1200):
        self.cand_ms, self.pause_ms = cand_ms, pause_ms
        self.state, self.interrupted = LISTENING, False
        self.answer_open = False        # last spoken chunk did not end a sentence: the answer is unfinished

    def step(self, text, user_buf, silent_ms, speaking, results_ready):
        if speaking and text and not is_backchannel(text):
            self.state, self.interrupted = INTERRUPTED, True
            return "interrupt"
        if speaking:
            self.state = RESPONDING
            return "gen"                                   # continue / finish the answer (backchannels don't stop it)
        if self.answer_open and text and not is_backchannel(text):
            self.answer_open = False                       # the user took the floor
        if self.answer_open:
            self.state = RESPONDING
            return "continue"                              # deterministic: never abandon an unfinished sentence
        if text and silent_ms < self.cand_ms:
            route = "gen"                                  # user is mid-speech: generator may still cut in on danger
        elif results_ready and silent_ms >= self.pause_ms:
            route = "relay"                                # deterministic delivery: user has been quiet long enough
        elif user_buf and silent_ms >= self.cand_ms:
            route = "classify"                             # candidate pause
        else:
            route = "skip"
        return route

    def settle(self, speaking_now, pending):
        if speaking_now:
            self.state, self.interrupted = RESPONDING, False
        elif self.interrupted:
            self.state = INTERRUPTED
        elif pending:
            self.state = WAITING
        else:
            self.state = LISTENING


class HybridRunner(Runner):
    def __init__(self, llm, tick_ms, S="~", gate=0.3, clf_thr=0.5, pause_ms=1200, cand_ms=300, temp=0.0, seed=1, clf_mode="stateless"):
        super().__init__(llm, "append_state", tick_ms, S, gate, temp=temp, seed=seed)
        self.clf_thr, self.pause_ms, self.cand_ms, self.clf_mode = clf_thr, pause_ms, cand_ms, clf_mode
        self.yid, self.nid = llm.tokenize("Y")[0], llm.tokenize("N")[0]
        # forced speech must be words: also ban the bare-punctuation tokens the model falls back to when it 'wants' silence
        self.ban = sorted({self.sid} | {llm.tokenize(t)[0] for t in [".", ",", "!", "?", "...", "\u2026", "-", "_", "~"] if len(llm.tokenize(t)) == 1})

    def classify(self, transcript, silent_ms, gen_prompt=None):
        if self.clf_mode == "inline":
            # same prefix as the generator (system + history + this tick's message), control question appended as a suffix:
            # no unrelated prompt on the shared slot, so the cached prefix should survive the call
            base = gen_prompt[:-len(THINK_PREFIX)]
            prompt = base + chat("user", "[control] Has the user finished their turn and expects a reply now? "
                                         "Answer with one letter, Y or N, nothing else.") + THINK_PREFIX
        else:
            prompt = CLASSIFIER_SYS + chat("user", 'Transcript so far: "%s"\nThe person has been silent for %.1f seconds.\nFinished? (Y/N)'
                                           % (transcript[-400:], silent_ms / 1000)) + THINK_PREFIX
        r = self.llm.complete(prompt, n_predict=1, n_probs=8)
        lp = dict(r["first_top"])
        py, pn = math.exp(lp.get(self.yid, -20)), math.exp(lp.get(self.nid, -20))
        return py / (py + pn + 1e-12), r

    def _gen(self, prompt, forced):
        bias = [[i, False] for i in self.ban] if forced else None
        r = self.llm.complete(prompt, n_predict=26, bias=bias, temperature=self.temp, seed=self.seed)
        return r

    def run(self, sc):
        stt, tts, hm = SttReplay(sc.words), TtsModel(), HermesMock(sc.hermes)
        pol = PromptPolicy("append_state", self.S, self.T)
        tm = TurnManager(self.cand_ms, self.pause_ms)
        ticks, k, T = [], 1, self.T
        t_sched = t_start = float(T)
        last_word, user_buf, last_ctx, clf_since_gen, note_once = 0.0, "", 0, 0, ""
        last_clf = ("", -1)
        while True:
            if t_start > sc.end_ms and not any(not j["delivered"] for j in hm.jobs):
                break
            if t_start > sc.end_ms + 30000:
                break
            words = stt.take_until(t_start)
            text = " ".join(w for _, w in words)
            if words:
                last_word = words[-1][0]
            silent_ms = t_start - last_word
            user_buf = (user_buf + " " + text).strip()
            was_speaking = tts.speaking(t_start)
            ready = hm.ready(t_start)
            route = tm.step(text, user_buf, silent_ms, was_speaking, bool(ready))
            events, result_ready = [], 0.0
            clf_p, clf_lat, sub = -1.0, 0.0, route
            if route == "relay":
                events = hm.deliver(t_start)
                result_ready = min(j["t_ready"] for j in events)
                note_once = (note_once + " {tell the user the hermes result now, briefly}").strip()
            if route == "interrupt":
                tts.cancel(t_start)
                note_once = "{you were interrupted and your speech was cancelled}"
            tag = (tts.tag(t_start) + " " + note_once).strip()
            prompt = pol.build(t_start, text, events, tag, hm.pending_tag(t_start), silent_ms)
            if route == "classify":
                bucket = sum(silent_ms >= b for b in (300, 900, 1800, 3000))
                if trailing_connective(user_buf):
                    route, sub = "skip", "skip_connective"
                elif last_clf[0] == user_buf and bucket <= last_clf[1]:
                    route, sub = "skip", "skip_cached"          # same words, no new silence bucket: nothing new to ask
                else:
                    last_clf = (user_buf, bucket)
                    clf_p, cr = self.classify(user_buf, silent_ms, prompt)
                    clf_lat = cr["total"]
                    clf_since_gen += 1
                    route = "gen_forced" if clf_p >= self.clf_thr else "skip"
                    sub = "classify_y" if route == "gen_forced" else "classify_n"
            elif route == "relay":
                route, sub = "gen_forced", "relay"
            elif route == "continue":
                route, sub = "gen_forced", "continue"
            kind, out, task, gate_note, p_s = "silent", "", "", "", 1.0
            gen_lat, ttft, reqs = 0.0, 0.0, (1 if clf_lat else 0)
            cache_n = prompt_n = 0
            ctx = last_ctx
            dec_dt = clf_lat
            if route in ("gen", "gen_forced"):
                forced = route == "gen_forced"
                r = self._gen(prompt, forced)
                kind, out, task = parse_out(r["text"], self.S)
                p_s = prob_of(r["first_top"], self.sid)
                gen_lat, reqs = r["total"], reqs + 1
                dec_dt = clf_lat + r["ttft"]
                if not forced:
                    if kind == "silent" and p_s < self.gate:           # the video's gate: split vote must not stop speech
                        r2 = self._gen(prompt, True)
                        kind, out, task = parse_out(r2["text"], self.S)
                        dec_dt = clf_lat + r["total"] + r2["ttft"]
                        gen_lat, reqs, gate_note = r["total"] + r2["total"], reqs + 1, "forced_speech"
                    elif kind != "silent" and p_s >= self.gate:
                        kind, out, task, gate_note = "silent", "", "", "forced_silence"
                ttft, cache_n, prompt_n, ctx = r["ttft"], r["cache_n"], r["prompt_n"], r["tokens_evaluated"]
                note_once = ""
                gen_since = clf_since_gen
                clf_since_gen = 0
            else:
                gen_since = 0
            last_ctx = ctx
            lat = clf_lat + gen_lat
            t_dec = t_start + dec_dt * 1000 if reqs else t_start
            free = t_start + lat * 1000
            if kind != "silent":
                if out:
                    tts.say(t_dec, out)
                if kind == "delegate":
                    hm.accept(t_dec, task)
                if not (kind == "speech" and is_filler(out)):
                    user_buf = ""
            if kind == "speech" and not is_filler(out):
                tm.answer_open = not re.search(r"[.?!][\"')]*$", out)
            elif kind != "silent":
                tm.answer_open = False
            elif route == "interrupt":
                tm.answer_open = False
            elif kind == "silent" and sub == "continue":
                tm.answer_open = False                      # model produced nothing even when forced: give up
            tm.settle(tts.speaking(t_dec), any(not j["delivered"] for j in hm.jobs))
            pol.commit(text, text, (out + (" @hermes " + task if task else "")).strip(), kind)
            ticks.append(Tick(sc.name, k, t_sched, t_start, t_start - t_sched, text, kind, out, task, t_dec, lat * 1000,
                              ttft * 1000, reqs, cache_n, prompt_n, ctx, p_s, gate_note, was_speaking,
                              kind == "speech" and bool(out) and is_filler(out), False, len(events), False, sub, clf_p, result_ready,
                              tm.state, clf_lat * 1000, gen_lat * 1000, gen_since))
            k += 1
            nxt = k * T
            if nxt >= free:
                t_sched = t_start = float(nxt)
            else:
                k = int(free // T) + 1
                t_sched, t_start = float(nxt), free
        return ticks
