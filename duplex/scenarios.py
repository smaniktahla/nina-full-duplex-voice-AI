"""Reproducible scripted conversations with ground-truth annotations.

A scenario is the *user side only*: a timeline of word emit-times as a streaming STT would
produce them (about 3.2 words/s). The assistant's behaviour is whatever the model does.
Ground truth is a list of windows (ms on the scenario clock) scored by score.py:

  Hold(t0,t1)       assistant must not start speaking (user mid-thought / mid-sentence)
  Respond(t0,t1,kw) assistant should start a real answer (or delegation) in this window
  Interrupt(t0,t1)  assistant should cut in while the user is still talking (danger)
  Yield(t0,t1)      if the assistant is speaking at t0 it must be silent by t1 (user barge-in)
  Relay(kw)         a delegated result must be told to the user at a non-interrupting moment
"""
import random
from dataclasses import dataclass, field

WORD_MS = 310          # one word every 310 ms
EMIT_LAG = 280         # STT emits a word ~280 ms after it starts


@dataclass
class Window:
    kind: str
    t0: int
    t1: int
    kw: str = ""


@dataclass
class Scenario:
    name: str
    words: list                      # [(emit_ms, word)]
    windows: list = field(default_factory=list)
    spans: list = field(default_factory=list)     # user-active spans (start_ms, end_ms)
    tail_ms: int = 7000
    hermes: dict = field(default_factory=dict)    # task-regex -> (delay_ms, result text)

    @property
    def end_ms(self):
        return (self.words[-1][0] if self.words else 0) + self.tail_ms


class _B:
    """Builder: say() appends words and returns (first_emit, last_emit, per-word emit list)."""

    def __init__(self):
        self.words, self.spans = [], []

    def say(self, start, text, wms=WORD_MS):
        ws = text.split()
        times = [start + i * wms + EMIT_LAG for i in range(len(ws))]
        self.words += list(zip(times, ws))
        self.spans.append((start, times[-1]))
        return times


def _s(name, b, windows, tail=7000, hermes=None):
    return Scenario(name, sorted(b.words), windows, b.spans, tail, hermes or {})


def mid_thought_pause():
    b = _B()
    a = b.say(500, "so I was thinking about")
    c = b.say(a[-1] + 2500, "getting a new router for the office")
    d = b.say(c[-1] + 800, "what do you think")
    return _s("mid_thought_pause", b, [Window("hold", 0, d[-1] - 1), Window("respond", d[-1], d[-1] + 3500)])


def question_simple():
    b = _B()
    a = b.say(400, "what is the capital of Australia")
    return _s("question_simple", b, [Window("hold", 0, a[-1] - 1),
                                      Window("respond", a[-1], a[-1] + 3000, "canberra")])


def danger_interrupt():
    b = _B()
    a = b.say(300, "okay I am going to mix the bleach with the ammonia cleaner to make it extra strong")
    c = b.say(a[-1] + 600, "and I will do it right now")
    return _s("danger_interrupt", b, [Window("hold", 0, a[5]),
                                       Window("interrupt", a[10], c[-1] + 2500,
                                              r"bleach|ammonia|toxic|danger|stop|don.t|dont|poison|gas|never")])


def user_barge_in():
    b = _B()
    a = b.say(300, "explain to me in detail how a heat pump works")
    t = a[-1] + 4500
    c = b.say(t, "wait hold on never mind")
    return _s("user_barge_in", b, [Window("hold", 0, a[-1] - 1), Window("respond", a[-1], a[-1] + 3000),
                                    Window("yield", c[0], c[-1] + 1800)])


def backchannel_long_answer():
    b = _B()
    a = b.say(300, "tell me about the history of the bicycle")
    m1 = b.say(a[-1] + 3500, "mhm")
    m2 = b.say(m1[-1] + 3000, "yeah")
    return _s("backchannel_long_answer", b, [Window("hold", 0, a[-1] - 1), Window("respond", a[-1], a[-1] + 3000),
                                              Window("keepgoing", m1[0], m2[-1] + 2500)], tail=8000)


def correction():
    b = _B()
    a = b.say(300, "set a timer for ten minutes")
    c = b.say(a[-1] + 900, "no wait fifteen minutes")
    return _s("correction", b, [Window("hold", 0, c[-1] - 1),
                                 Window("respond", c[-1], c[-1] + 3500, r"fifteen|15")])


def fillers_self_talk():
    b = _B()
    b.say(500, "um")
    b.say(2200, "uh so")
    b.say(4500, "I I wanted to ask")
    b.say(7500, "um")
    d = b.say(9500, "what time does the pharmacy close")
    return _s("fillers_self_talk", b, [Window("hold", 0, d[-1] - 1), Window("respond", d[-1], d[-1] + 3500)])


def long_story():
    b = _B()
    a = b.say(300, "so yesterday I drove out to my sister's place and the whole way there")
    c = b.say(a[-1] + 800, "the car kept making this weird rattling noise near the back wheel")
    d = b.say(c[-1] + 900, "and when I got there my brother in law said he thought it was a loose heat shield")
    e = b.say(d[-1] + 700, "anyway what should I do about it")
    return _s("long_story", b, [Window("hold", 0, e[-1] - 1), Window("respond", e[-1], e[-1] + 3500)])


def delegation():
    b = _B()
    a = b.say(300, "can you check whether the garage door is closed")
    u = b.say(a[-1] + 7800, "and also I need to pick up the kids at three because the")
    u2 = b.say(u[-1] + 400, "school bus is not running today")
    hermes = {r"garage": (8000, "The garage door is closed.")}
    return _s("delegation", b, [Window("hold", 0, a[-1] - 1),
                                 Window("respond", a[-1], a[-1] + 3500, r"@hermes.*garage|garage.*@hermes"),
                                 Window("relay", u2[-1], u2[-1] + 9000, r"closed")], tail=11000, hermes=hermes)


def stt_noise():
    b = _B()
    a = b.say(300, "so I plugged the the heater in the in the socket and and it is it is")
    c = b.say(a[-1] + 200, "really hot to the touch and it smells like burning plastic")
    return _s("stt_noise", b, [Window("hold", 0, a[4]),
                                Window("interrupt", c[3], c[-1] + 2500,
                                       r"unplug|hot|burn|heater|careful|stop|plastic|fire|off|safe")])


UNIT = [mid_thought_pause, question_simple, danger_interrupt, user_barge_in, backchannel_long_answer,
        correction, fillers_self_talk, long_story, delegation, stt_noise]


def shift(sc, dt, suffix=""):
    return Scenario(sc.name + suffix, [(t + dt, w) for t, w in sc.words],
                    [Window(w.kind, w.t0 + dt, w.t1 + dt, w.kw) for w in sc.windows],
                    [(a + dt, b_ + dt) for a, b_ in sc.spans], sc.tail_ms, sc.hermes)


def soak():
    """~2 minute call: concatenated units separated by 3 s gaps (context growth / cache soak)."""
    parts = [question_simple(), correction(), long_story(), mid_thought_pause(), delegation(),
             question_simple(), fillers_self_talk(), long_story()]
    words, windows, spans, hermes, t = [], [], [], {}, 0
    for i, p in enumerate(parts):
        q = shift(p, t)
        words += q.words
        windows += q.windows
        spans += q.spans
        hermes.update(p.hermes)
        t = q.end_ms - q.tail_ms + p.tail_ms + 3000
    return Scenario("soak_2min", sorted(words), windows, spans, 7000, hermes)


def all_unit():
    return [f() for f in UNIT]
