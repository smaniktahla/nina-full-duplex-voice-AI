"""Spoken randomness requests -> RNG service calls (parser, wording, honest failure). No network: the service is faked."""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import tools                                                                   # noqa: E402
from tools import parse_random as pr, spoken_number                            # noqa: E402


def test_spoken_numbers():
    assert [spoken_number(x) for x in ("7", "ten", "twenty five", "one hundred", "two hundred fifty", "a thousand", "banana", "")] == [7, 10, 25, 100, 250, 1000, None, None]


def test_coin():
    for t in ("flip a coin", "Toss a coin for me", "heads or tails?", "can you flip a coin please", "do a coin flip"):
        assert pr(t) == ("coin", {}), t


def test_dice():
    assert pr("roll a d20") == ("dice", {"expr": "d20"})
    assert pr("roll 2d6+3") == ("dice", {"expr": "2d6+3"})
    assert pr("roll a die") == ("dice", {"expr": "1d6"})
    assert pr("roll two dice") == ("dice", {"expr": "2d6"})
    assert pr("roll three six sided dice") == ("dice", {"expr": "3d6"})
    assert pr("roll a twenty sided die") == ("dice", {"expr": "1d20"})
    assert pr("roll 4d6kh3") == ("dice", {"expr": "4d6kh3"})


def test_number_ranges():
    assert pr("pick a number between 1 and 10") == ("integer", {"min": 1, "max": 10})
    assert pr("give me a random number between five and fifty") == ("integer", {"min": 5, "max": 50})
    assert pr("choose a number from 1 to 100") == ("integer", {"min": 1, "max": 100})
    assert pr("pick a random number between 10 and 1") == ("integer", {"min": 1, "max": 10})          # reversed bounds are fixed
    assert pr("give me a random number") == ("integer", {"min": 1, "max": 100})


def test_picks():
    assert pr("pick between pizza and tacos") == ("choice", {"items": ["pizza", "tacos"]})
    assert pr("choose pizza, tacos or sushi") == ("choice", {"items": ["pizza", "tacos", "sushi"]})
    assert pr("decide between going to the park or staying home for me") == ("choice", {"items": ["going to park", "staying home"]})


def test_not_randomness():
    for t in ("what's the weather tomorrow", "what time is it", "I can't decide whether to go or stay", "choose the red one",
              "what is the capital of Italy", "turn on the lights", "my favorite number is seven", "how many sides does a die have"):
        assert pr(t) is None, t


def test_wording_and_service_call():
    assert tools.describe_random("coin", {}, {"result": "heads"}) == "Coin flip: heads."
    assert tools.describe_random("dice", {}, {"result": 6, "expr": "2d6", "rolls": [4, 2], "kept": [4, 2]}) == "Rolled 2d6: 4 and 2, total 6."
    assert tools.describe_random("dice", {}, {"result": 14, "expr": "1d20", "rolls": [14], "kept": [14]}) == "Rolled a d20: 14."
    assert "kept 6 and 5" in tools.describe_random("dice", {}, {"result": 11, "expr": "4d6kh2", "rolls": [6, 5, 2, 1], "kept": [6, 5]})
    assert tools.describe_random("integer", {"min": 1, "max": 10}, {"result": 7}) == "Random number between 1 and 10: 7."
    assert tools.describe_random("choice", {"items": ["a", "b"]}, {"result": "b"}) == "Picked at random from a and b: b."

    class R:
        def __init__(s, j): s.j = j
        async def __aenter__(s): return s
        async def __aexit__(s, *a): return False
        async def json(s): return s.j
    class Http:
        def __init__(s, j=None, boom=False): s.j, s.boom, s.calls = j, boom, []
        def post(s, url, json=None, timeout=None):
            if s.boom: raise ConnectionError("down")
            s.calls.append((url, json)); return R(s.j)
    h = Http({"ok": True, "result": "tails"})
    assert asyncio.run(tools.random_request(h, "flip a coin")) == "Coin flip: tails."
    assert h.calls[0][0].endswith("/v1/coin")
    out = asyncio.run(tools.random_request(Http(boom=True), "flip a coin"))
    assert "not reachable" in out and "do not make up" in out                                    # never invents a result
    assert "refused" in asyncio.run(tools.random_request(Http({"ok": False, "error": "bad"}), "flip a coin"))
    assert "could not tell" in asyncio.run(tools.random_request(Http({}), "what time is it"))
