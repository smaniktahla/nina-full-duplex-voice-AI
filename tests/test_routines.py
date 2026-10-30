import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import claims
import routines

CFG = {"morning": {"turn_on": ["light.a", "switch.b"], "report": ["weather", "calendar"]},
       "night": {"turn_off": ["light.a", "switch.b", "media_player.tv"]}}


class FakeIO:
    """states: entity -> state; `stuck` entities ignore commands; `refuse` entities fail the call."""
    def __init__(self, states, stuck=(), refuse=()):
        self.s, self.stuck, self.refuse, self.calls = dict(states), set(stuck), set(refuse), []

    async def state(self, e):
        return {"state": self.s[e], "attributes": {"friendly_name": e.split(".")[1].upper()}} if e in self.s else None

    async def call(self, domain, service, e):
        self.calls.append((domain, service, e))
        if e in self.refuse:
            return False
        if e not in self.stuck:
            self.s[e] = "on" if service == "turn_on" else "off"
        return True

    async def sleep(self, s):
        pass


def run(name, io):
    return asyncio.run(routines.run(name, CFG, io, settle_s=1.0))


def test_phrases_are_whole_utterance_and_need_config():
    for t in ("Good morning", "good morning!", "Hey Nina, good night", "goodnight", "good night Nina"):
        assert routines.parse(t) in ("morning", "night")
    for t in ("good morning what's the weather", "say good night to the kids", "good morning america"):
        assert routines.parse(t) == ""
    assert routines.owns("good morning", CFG) == "morning"
    assert routines.owns("good morning", {}) == "" and routines.owns("good morning", None) == ""
    assert claims.claim("good night", display=False, nav=False, routine_cfg=CFG) == "routine"
    assert claims.claim("good night", display=False, nav=False) == ""


def test_morning_turns_on_and_verifies():
    io = FakeIO({"light.a": "off", "switch.b": "off"})
    speech, ok = run("morning", io)
    assert ok and speech == "All 2 devices are on." and len(io.calls) == 2


def test_already_in_state_is_not_commanded():
    io = FakeIO({"light.a": "off", "switch.b": "off", "media_player.tv": "off"})
    speech, ok = run("night", io)
    assert ok and io.calls == []


def test_failures_are_named_not_hidden():
    io = FakeIO({"light.a": "on", "switch.b": "on", "media_player.tv": "playing"}, stuck={"switch.b"}, refuse={"media_player.tv"})
    speech, ok = run("night", io)
    assert not ok and "1 of 3" in speech and "B did not change" in speech
    assert "would not accept" in speech


def test_unavailable_device_is_reported_and_not_commanded():
    io = FakeIO({"light.a": "unavailable", "switch.b": "on"})
    speech, ok = run("night", io)
    assert not ok and "A unavailable" in speech and ("light", "turn_off", "light.a") not in io.calls


def test_media_player_playing_counts_as_on_and_standby_as_off():
    assert routines._is("media_player", "on", "playing") and not routines._is("media_player", "on", "off")
    assert routines._is("media_player", "off", "standby") and not routines._is("light", "off", "unavailable")


def test_config_problems():
    assert routines.problems(CFG) == []
    bad = {"night": {"turn_off": ["lock.front_door"], "report": ["news"], "explode": []}}
    assert len(routines.problems(bad)) == 3


def test_good_night_routine_wins_over_the_goodbye_but_only_when_configured(monkeypatch):
    import tools
    from needle_voice.session import DISMISS_RE
    assert DISMISS_RE.search("good night")                                  # the built-in goodbye still matches the words
    monkeypatch.setattr(tools, "ROUTINES", CFG)
    assert tools.routine_owns("Good night") and tools.routine_owns("hey nina good night")
    assert not tools.routine_owns("ok thanks good night") and not tools.routine_owns("good night everyone")
    monkeypatch.setattr(tools, "ROUTINES", {})
    assert not tools.routine_owns("good night")
