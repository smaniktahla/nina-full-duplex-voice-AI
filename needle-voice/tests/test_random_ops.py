"""Randomness category: parser, spoken answers, honest failure, and its place in the router (RNG service faked)."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from needle_voice import random_ops as ro, router as r
from needle_voice.result import Status
from test_router import Rig, SNAP


def run(c): return asyncio.run(c)


def test_spoken_numbers():
    assert [ro.spoken_number(x) for x in ("7", "ten", "twenty five", "one hundred", "two hundred fifty", "a thousand", "banana", "")] == [7, 10, 25, 100, 250, 1000, None, None]


def test_coin():
    for t in ("flip a coin", "Toss a coin for me", "heads or tails?", "can you flip a coin please", "do a coin flip"):
        assert ro.parse(t) == ("coin", {}), t
    assert ro.parse("flip a coin three times") == ("coin", {"count": 3})
    assert ro.parse("flip two coins") == ("coin", {"count": 2})


def test_dice():
    assert ro.parse("roll a d20") == ("dice", {"expr": "d20"})
    assert ro.parse("roll 2d6+3") == ("dice", {"expr": "2d6+3"})
    assert ro.parse("roll a die") == ("dice", {"expr": "1d6"})
    assert ro.parse("roll two dice") == ("dice", {"expr": "2d6"})
    assert ro.parse("roll three six sided dice") == ("dice", {"expr": "3d6"})
    assert ro.parse("roll a twenty sided die") == ("dice", {"expr": "1d20"})
    assert ro.parse("roll 4d6kh3") == ("dice", {"expr": "4d6kh3"})


def test_number_ranges():
    assert ro.parse("pick a number between 1 and 10") == ("integer", {"min": 1, "max": 10})
    assert ro.parse("give me a random number between five and fifty") == ("integer", {"min": 5, "max": 50})
    assert ro.parse("choose a number from 1 to 100") == ("integer", {"min": 1, "max": 100})
    assert ro.parse("pick a random number between 10 and 1") == ("integer", {"min": 1, "max": 10})
    assert ro.parse("give me a random number") == ("integer", {"min": 1, "max": 100})


def test_picks():
    assert ro.parse("pick between pizza and tacos") == ("choice", {"items": ["pizza", "tacos"]})
    assert ro.parse("choose pizza, tacos or sushi") == ("choice", {"items": ["pizza", "tacos", "sushi"]})
    assert ro.parse("decide between going to the park or staying home for me") == ("choice", {"items": ["going to park", "staying home"]})


def test_not_randomness():
    for t in ("what's the weather tomorrow", "what time is it", "I can't decide whether to go or stay", "choose the red one", "what is the capital of Italy",
              "turn on the kitchen lights", "my favorite number is seven", "how many sides does a die have", "turn on the lights and the fan"):
        assert ro.parse(t) is None, t


def test_speech():
    sp = ro.speak
    assert sp("coin", {}, {"result": "heads"}) == "It's heads."
    assert sp("coin", {}, {"result": ["heads", "tails", "heads"]}) == "You got heads, tails and heads."
    assert sp("dice", {}, {"result": 14, "expr": "1d20", "rolls": [14], "kept": [14], "modifier": 0}) == "You rolled 14."
    assert sp("dice", {}, {"result": 6, "expr": "2d6", "rolls": [4, 2], "kept": [4, 2], "modifier": 0}) == "You rolled 4 and 2, for a total of 6."
    assert sp("dice", {}, {"result": 9, "expr": "2d6+3", "rolls": [4, 2], "kept": [4, 2], "modifier": 3}) == "You rolled 4 and 2, plus 3, for a total of 9."
    assert sp("dice", {}, {"result": 13, "expr": "4d6kh3", "rolls": [6, 5, 2, 1], "kept": [6, 5, 2], "modifier": 0}) == \
        "You rolled 6, 5, 2 and 1. Keeping the highest 3, 6, 5 and 2, for a total of 13."
    assert sp("integer", {}, {"result": 7}) == "Your number is 7."
    assert sp("choice", {"items": ["a", "b"]}, {"result": "b"}) == "I pick b."


class Svc:
    def __init__(self, out): self.out, self.calls = out, []
    async def __call__(self, tool, params):
        self.calls.append((tool, params))
        if isinstance(self.out, Exception): raise self.out
        return self.out


def test_handle_contract():
    s = Svc({"ok": True, "result": "tails"})
    res = run(ro.handle("flip a coin", s)); assert res.status is Status.SUCCESS and res.speech == "It's tails." and s.calls == [("coin", {})]
    assert res.telemetry == {"code": "coin"} and not res.side_effect and res.domain == "random"          # no drawn value in telemetry
    res = run(ro.handle("what time is it", s)); assert res.status is Status.UNSUPPORTED and res.speech is None and len(s.calls) == 1
    for bad in (None, ConnectionError("down")):
        res = run(ro.handle("flip a coin", Svc(bad))); assert res.status is Status.UNAVAILABLE and "won't make one up" in res.speech and res.handled
    res = run(ro.handle("flip a coin", None)); assert res.status is Status.UNAVAILABLE and res.handled
    res = run(ro.handle("roll 500d6", Svc({"ok": False, "error": "roll between 1 and 100 dice"})))
    assert res.status is Status.EXECUTION_FAILED and "between 1 and 100 dice" in res.speech


def _go(text, rig, rng, **cfg):
    io = r.IO(rig.needle_route, rig.qwen_post, rig.execute, lambda: rig.pt, rng)
    return asyncio.run(r.handle(text, SNAP, r.RouterConfig.from_dict(cfg), io))


def test_router_answers_randomness_without_any_model():
    rig, s = Rig(), Svc({"ok": True, "result": 7})
    out = _go("pick a number between 1 and 10", rig, s)                  # contains "and": would trip the multi-target guard if it came later
    assert out.route == "random" and out.speech == "Your number is 7." and out.fallback_reason is None
    assert rig.needle_calls == 0 and rig.qwen_calls == 0 and not rig.executed
    assert "7" not in str(out.telemetry["random"]) and out.telemetry["random"]["code"] == "integer"


def test_router_service_down_is_spoken_not_delegated():
    out = _go("flip a coin", Rig(), Svc(None))
    assert out.route == "random" and out.speech and "won't make one up" in out.speech and out.fallback_reason is None


def test_router_leaves_everything_else_alone_and_can_be_switched_off():
    s = Svc({"ok": True, "result": "heads"})
    out = _go("turn on the kitchen lights", Rig(needle=None), s)
    assert out.route != "random" and s.calls == []
    assert _go("flip a coin", Rig(), s, random_enabled=False).route != "random" and s.calls == []
    assert _go("flip a coin", Rig(), None).route != "random"                                       # no rng wired: behaves as before
    assert _go("flip a coin", Rig(), s, enabled=False).fallback_reason == "disabled"
    assert _go("flip a coin", Rig(passthrough=True), s).fallback_reason == "passthrough_flag"
