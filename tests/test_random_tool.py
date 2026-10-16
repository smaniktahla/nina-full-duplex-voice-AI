"""Nina's @random tool: it hands the request to Needle's random_ops and the RNG service (parser and wording are tested in needle-voice/tests)."""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import tools                                                                   # noqa: E402


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


def ask(http, text): return asyncio.run(tools.random_request(http, text))


def test_request_goes_to_the_service_and_is_spoken_naturally():
    h = Http({"ok": True, "result": "tails"})
    assert ask(h, "flip a coin") == "It's tails."
    assert h.calls[0][0].endswith("/v1/coin")
    h = Http({"ok": True, "result": 7})
    assert ask(h, "pick a number between 1 and 10") == "Your number is 7."
    assert h.calls[0][1] == {"min": 1, "max": 10}


def test_service_down_or_refusing_is_said_not_invented():
    out = ask(Http(boom=True), "flip a coin")
    assert "can't reach the random number generator" in out and "won't make one up" in out
    assert "couldn't do that" in ask(Http({"ok": False, "error": "roll between 1 and 100 dice"}), "roll 500d6")


def test_non_random_text_is_not_answered():
    assert "could not tell" in ask(Http({}), "what time is it")


def test_nina_has_no_parser_of_its_own_any_more():
    assert not hasattr(tools, "parse_random") and not hasattr(tools, "describe_random")
