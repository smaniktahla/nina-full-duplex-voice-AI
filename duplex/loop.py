"""Tick loop. Components are deliberately separate:

  SttReplay    - audio capture stand-in: hands out timestamped words
  TtsModel     - playback stand-in: queue of spoken words, tells how long the assistant is still talking
  HermesMock   - async tool execution: accept, delay, deliver result
  PromptPolicy - conversational state: how history/state become a prompt (append / append_state /
                 rebuild / snapshot)
  Gate         - external token-probability rule on the silence sentinel
  Runner       - virtual-time scheduler; real inference latency advances the virtual clock, so a
                 tick that overruns its budget coalesces the next slices exactly like a live system.
"""
import math
import re
from dataclasses import dataclass, field

from .llama import prob_of

FILLERS = {"mhm", "mm-hm", "mmhm", "mm", "hm", "hmm", "yeah", "yes", "right", "okay", "ok", "uh-huh", "uhhuh",
           "i see", "sure", "got it", "go on", "yep", "mm-hmm", "i hear you"}
THINK_PREFIX = "<|im_start|>assistant\n<think>\n\n</think>\n\n"


def is_filler(text):
    """Pure backchannel/acknowledgement-of-listening, not a substantive reply ('On it.' is NOT a filler)."""
    t = re.sub(r"[^\w' -]", "", text.lower()).strip()
    return t in FILLERS


# ---------------------------------------------------------------- components
class SttReplay:
    def __init__(self, words):
        self.words, self.i = words, 0

    def take_until(self, t):
        out = []
        while self.i < len(self.words) and self.words[self.i][0] <= t:
            out.append(self.words[self.i])
            self.i += 1
        return out


class TtsModel:
    WPS = 2.8

    def __init__(self):
        self.end = 0.0
        self.words = []          # (word, scheduled_ms)

    def say(self, now, text):
        ws = text.split()
        start = max(now, self.end)
        for k, w in enumerate(ws):
            self.words.append((w, start + k * 1000 / self.WPS))
        self.end = start + len(ws) * 1000 / self.WPS

    def cancel(self, now):
        keep = [(w, t) for w, t in self.words if t <= now]
        dropped = len(self.words) - len(keep)
        self.words, self.end = keep, min(self.end, now)
        return dropped

    def speaking(self, now):
        return self.end > now

    def tag(self, now):
        if not self.speaking(now):
            return ""
        left = (self.end - now) / 1000
        tail = " ".join(w for w, _ in self.words[-6:])
        return '{speaking: "..%s", %.1fs left}' % (tail, left)


class HermesMock:
    def __init__(self, table):
        self.table, self.jobs, self.n = table, [], 0

    def accept(self, now, task):
        self.n += 1
        delay, res = 6000, "Done."
        for pat, (d, r) in self.table.items():
            if re.search(pat, task, re.I):
                delay, res = d, r
        self.jobs.append({"id": self.n, "task": task, "t_accept": now, "t_ready": now + delay, "res": res, "told": False,
                          "delivered": False})
        return self.n

    def deliver(self, now):
        out = []
        for j in self.jobs:
            if not j["delivered"] and j["t_ready"] <= now:
                j["delivered"] = True
                out.append(j)
        return out

    def ready(self, now):
        return [j for j in self.jobs if not j["delivered"] and j["t_ready"] <= now]

    def pending_tag(self, now):
        p = [j for j in self.jobs if not j["delivered"]]
        return " ".join('{pending: #%d "%s" %ds}' % (j["id"], j["task"][:40], (now - j["t_accept"]) / 1000) for j in p)


# ---------------------------------------------------------------- prompts
def system_prompt(S, T_ms, state_tags):
    tags = ("\nMessages may end with state tags. {user talking} / {user silent Ns} tell you whether the user is currently speaking and for how long they have been quiet. {speaking: ...} shows words your voice is still saying; "
            "{pending: ...} lists tasks delegated to hermes that are still running.") if state_tags else ""
    return (
        "You are the real-time voice front end of a home assistant. Speech is transcribed and delivered to you in short "
        "slices, one message about every %.1f seconds, as: [12.6s] \"what the user said in this slice\" or [12.6s] (silence).\n"
        "After EVERY message output exactly one of:\n"
        "1. %s  (just this one symbol) - say nothing this tick. Use it while the user is still talking, is pausing "
        "mid-thought (unfinished clause, trailing 'and', 'about', 'um', 'so'), is telling a long story, or you have "
        "nothing to add, and when your answer is finished.\n"
        "2. Up to 12 words of speech, the next words you say aloud. Speak when the user has finished a question or "
        "request, when the user is about to do something dangerous (cut in at once), or while you are in the middle of an "
        "answer: continue a longer answer across ticks with a full clause of 6-12 words each tick, and finish your sentence. Use normal "
        "punctuation and end every finished sentence with . ? or !\n"
        "3. A very short spoken acknowledgement followed by @hermes and a task, for anything needing devices, sensors, "
        "timers or lookups, e.g.: On it. @hermes check the garage door. Never claim to have done it yourself. When a "
        "message contains [hermes #n done: ...] tell the user the result at the next natural pause, never while they are "
        "mid-sentence.\n"
        "Rules: if the user says wait / stop / hold on / never mind while you are talking, stop at once (output %s). If they say "
        "mhm / yeah / ok while you talk, keep going. A short acknowledgement ('mhm', 'right') only rarely, during a long user "
        "monologue, never two ticks in a row. Never repeat yourself. Output nothing else.%s"
    ) % (T_ms / 1000, S, S, tags)


def fmt_user(t_ms, text, events, state):
    msg = '[%.1fs] %s' % (t_ms / 1000, ('"%s"' % text) if text else "(silence)")
    for j in events:
        msg += " [%s #%d done: %s]" % (j.get("kind", "hermes"), j["id"], j["res"])
    if state:
        msg += " " + state
    return msg


def chat(role, text):
    return "<|im_start|>%s\n%s<|im_end|>\n" % (role, text)


class PromptPolicy:
    """mode: append | append_state | rebuild | snapshot"""

    def __init__(self, mode, S, T_ms, rebuild_at=40, keep=12):
        self.mode, self.S, self.T = mode, S, T_ms
        self.state_tags = mode != "append"
        self.hint = mode == "append_hint"       # + STT-derived "user silent for X s" tag (free: no VAD model needed)
        self.sys = chat("system", system_prompt(S, T_ms, self.state_tags))
        self.turns = []                 # (user_msg, assistant_out)
        self.summary = ""
        self.rebuild_at, self.keep = rebuild_at, keep
        self.rebuilds = 0
        self.exchange, self.user_buf = [], ""     # snapshot mode
        self.meta = []

    # --- build prompt for this tick
    def build(self, t_ms, text, events, tts_tag, pend_tag, silent_ms=0):
        state = (tts_tag + " " + pend_tag).strip() if self.state_tags else ""
        if self.hint:
            state = ("{user silent %.1fs}" % (silent_ms / 1000) if not text and silent_ms >= 600 else "{user talking}") + " " + state
        if self.mode == "snapshot":
            hist = " | ".join(self.exchange[-8:])[-700:]
            cur = ("User so far (unanswered): \"%s\"\n" % self.user_buf[-300:]) if self.user_buf else ""
            body = "Earlier: %s\n%sLatest slice: %s" % (hist or "(nothing)", cur, fmt_user(t_ms, text, events, state))
            self.cur_user = fmt_user(t_ms, text, events, state)
            return self.sys + chat("user", body) + THINK_PREFIX
        user = fmt_user(t_ms, text, events, state)
        self.cur_user = user
        pre = self.sys
        if self.summary:
            pre += chat("user", "[earlier in this call] " + self.summary) + chat("assistant", "<think>\n\n</think>\n\n" + self.S)
        for u, a in self.turns:
            pre += chat("user", u) + "<|im_start|>assistant\n<think>\n\n</think>\n\n" + a + "<|im_end|>\n"
        return pre + chat("user", user) + THINK_PREFIX

    # --- record the tick's final decision
    def commit(self, text, user_words, out_text, kind):
        if self.mode == "snapshot":
            if user_words:
                self.user_buf = (self.user_buf + " " + user_words).strip()
            if kind != "silent" and not is_filler(out_text):
                if self.user_buf:
                    self.exchange.append("U: " + self.user_buf)
                    self.user_buf = ""
                self.exchange.append("A: " + out_text)
            return
        self.turns.append((self.cur_user, out_text if kind != "silent" else self.S))
        self.meta.append((user_words, out_text, kind))
        if self.mode == "rebuild" and len(self.turns) >= self.rebuild_at:
            # controlled reconstruction: fold dropped turns into one compact summary, keep a verbatim recent window
            for uw, ot, kd in self.meta[:-self.keep]:
                self._note(uw, ot, kd)
            self.turns, self.meta = self.turns[-self.keep:], self.meta[-self.keep:]
            self.rebuilds += 1

    _ub = ""

    def _note(self, user_words, out_text, kind):
        if user_words:
            self._ub += " " + user_words
        if kind != "silent" and not is_filler(out_text):
            if self._ub.strip():
                self.summary = (self.summary + " U: " + self._ub.strip())[-500:]
                self._ub = ""
            self.summary = (self.summary + " A: " + out_text)[-500:]


# ---------------------------------------------------------------- runner
@dataclass
class Tick:
    sc: str
    k: int
    t_sched: float
    t_start: float
    late: float
    user_text: str
    kind: str                  # silent | speech | delegate
    out: str                   # speech text (ack part for delegate)
    task: str
    t_dec: float               # absolute time the decision became visible
    lat: float
    ttft: float
    reqs: int
    cache_n: int
    prompt_n: int
    ctx: int
    p_s: float
    gate: str                  # "" | forced_speech | forced_silence
    was_speaking: bool
    spoken_filler: bool
    rebuilt: bool = False
    hermes_events: int = 0
    foreign: bool = False      # another client hit the shared slot since our previous tick
    route: str = "gen"         # hybrid: gen | gen_forced | skip
    clf_p: float = -1.0        # hybrid: P(user finished) from the stateless classifier
    result_ready: float = 0.0  # hybrid: when the delivered Hermes result became ready
    state: str = ""            # hybrid: LISTENING | RESPONDING | INTERRUPTED | WAITING_FOR_HERMES (held outside the model)
    clf_ms: float = 0.0        # hybrid: classifier call latency this tick
    gen_ms: float = 0.0        # hybrid: generator call latency this tick
    clf_since_gen: int = 0     # hybrid: classifier calls since the previous generator call (shared-slot cache swap)


def parse_out(text, S):
    t = text.strip()
    if "@hermes" in t:
        ack, task = t.split("@hermes", 1)
        return "delegate", ack.strip(), task.strip(" :")
    if not t or t.startswith(S):
        return "silent", "", ""
    return "speech", " ".join(t.split()[:16]), ""


class Runner:
    def __init__(self, llm, mode, tick_ms, S="~", gate=None, rebuild_at=40, gate_hi=None, temp=0.0, seed=1):
        # gate (lo): silence veto/force threshold from the video; gate_hi: force speech when the user paused and P(silence) < hi
        self.llm, self.mode, self.T, self.S, self.gate, self.gate_hi = llm, mode, tick_ms, S, gate, gate_hi
        self.temp, self.seed = temp, seed
        toks = llm.tokenize(S)
        self.sid = toks[0]
        self.S_ntok = len(toks)
        self.rebuild_at = rebuild_at

    def run(self, sc):
        stt, tts, hm = SttReplay(sc.words), TtsModel(), HermesMock(sc.hermes)
        pol = PromptPolicy(self.mode, self.S, self.T, self.rebuild_at)
        ticks, k = [], 1
        prev_tid, prev_reqs = None, 0
        T = self.T
        t_sched = float(T)
        t_start = t_sched
        last_word = 0.0
        while True:
            if t_start > sc.end_ms and not any(not j["delivered"] for j in hm.jobs):
                break
            if t_start > sc.end_ms + 30000:
                break
            words = stt.take_until(t_start)
            text = " ".join(w for _, w in words)
            events = hm.deliver(t_start)
            was_speaking = tts.speaking(t_start)
            if words:
                last_word = words[-1][0]
            silent_ms = t_start - last_word
            prompt = pol.build(t_start, text, events, tts.tag(t_start), hm.pending_tag(t_start), silent_ms)
            tid = self.llm.task_id()
            foreign = prev_tid is not None and tid - prev_tid > 9 * prev_reqs + 4   # heuristic: our request costs ~5-7 task ids
            r = self.llm.complete(prompt, n_predict=26, temperature=self.temp, seed=self.seed)
            lat, reqs, dec_dt, gate_note = r["total"], 1, r["ttft"], ""
            first_id = r["ids"][0] if r["ids"] else None
            p_s = prob_of(r["first_top"], self.sid)
            kind, out, task = parse_out(r["text"], self.S)
            if self.gate is not None or self.gate_hi is not None:
                lo = self.gate if self.gate is not None else -1
                force = p_s < lo or (self.gate_hi is not None and not text and p_s < self.gate_hi)
                if kind == "silent" and force:       # sentinel won a split vote: force a real word
                    r2 = self.llm.complete(prompt, n_predict=26, bias=[[self.sid, False]], temperature=self.temp, seed=self.seed)
                    k2, o2, t2 = parse_out(r2["text"], self.S)
                    dec_dt = lat + r2["ttft"]
                    lat += r2["total"]
                    reqs = 2
                    kind, out, task, gate_note = k2, o2, t2, "forced_speech"
                    r = dict(r, cache_n=r["cache_n"])
                elif kind != "silent" and self.gate is not None and p_s >= self.gate:     # model almost wanted silence: honour it
                    kind, out, task, gate_note = "silent", "", "", "forced_silence"
            prev_tid, prev_reqs = tid, reqs
            t_dec = t_start + dec_dt * 1000
            free = t_start + lat * 1000
            if kind != "silent":
                if out:
                    tts.say(t_dec, out)
                if kind == "delegate":
                    hm.accept(t_dec, task)
            total = r["cache_n"] + r["prompt_n"]
            rebuilt = pol.rebuilds
            pol.commit(text, text, (out + (" @hermes " + task if task else "")).strip(), kind)
            rebuilt = pol.rebuilds != rebuilt
            ticks.append(Tick(sc.name, k, t_sched, t_start, t_start - t_sched, text, kind, out, task, t_dec,
                              lat * 1000, r["ttft"] * 1000, reqs, r["cache_n"], r["prompt_n"],
                              r["tokens_evaluated"] or total, p_s, gate_note, was_speaking, kind == "speech" and bool(out) and is_filler(out),
                              rebuilt, len(events), foreign))
            k += 1
            nxt = k * T
            if nxt >= free:
                t_sched = t_start = float(nxt)
            else:                       # overran the next slot: start as soon as the engine is free, slices coalesce
                k = int(free // T) + 1
                t_sched, t_start = float(nxt), free
        return ticks

