"""CI-safe tests for pieces of the voice server that need no GPU, models or network (the rest is tested on AI2: see CONTRIBUTING.md)."""
import os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "needle-voice"))
from duplex.loop import is_filler, parse_out, chat, system_prompt           # noqa: E402
from needle_voice.session import DISMISS_RE                                 # noqa: E402


def test_on_it_is_not_a_filler():
    assert not is_filler("On it.") and is_filler("mhm") and is_filler("Okay")


def test_parse_out_silence_speech_delegate():
    assert parse_out("~", "~")[0] == "silent"
    assert parse_out("Canberra.", "~")[:2] == ("speech", "Canberra.")
    assert parse_out("On it. @hermes check", "~")[0] == "delegate"


def test_system_prompt_mentions_sentinel_and_tags():
    p = system_prompt("~", 600, True)
    assert "~" in p and "@hermes" in p


def test_dismissal_phrases():
    for t in ("thanks, that's all", "goodbye", "stop listening", "go to sleep"):
        assert DISMISS_RE.search(t), t
    for t in ("what is the weather", "stop talking about that", "wait"):
        assert not DISMISS_RE.search(t), t


def test_tick_prompt_is_append_only():
    from duplex.loop import PromptPolicy
    pol = PromptPolicy("append_state", "~", 600)
    p1 = pol.build(600, "hello", [], "", "", 0); pol.commit("hello", "hello", "", "silent")
    p2 = pol.build(1200, "there", [], "", "", 0)
    assert p2.startswith(p1[: p1.rindex("<|im_start|>user")])               # history grows only at the tail: KV cache stays valid


def test_collapse_loops_caps_repeats():
    from live.claims import collapse_loops
    w = ("what's my agenda for a " + "f***ing " * 40).split()
    assert collapse_loops(w) == "what's my agenda for a f***ing f***ing f***ing".split()
    assert collapse_loops("no no no".split()) == ["no", "no", "no"]
    assert collapse_loops("what is the weather".split()) == "what is the weather".split()
