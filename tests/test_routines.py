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
    for t in ("Good morning", "good morning!", "Hey Nina, good night", "goodnight", "good night Nina", "Morning.", "hey nina morning"):
        assert routines.parse(t) in ("morning", "night")
    for t in ("good morning what's the weather", "say good night to the kids", "good morning america", "morning meeting", "night"):
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


# ---- script mode: the device list lives in an HA script, Nina runs it and verifies its simple on/off steps

SCRIPT_CFG = {"night": {"script": "script.good_night"}, "morning": {"script": "script.good_morning", "report": ["weather"]}}
NIGHT_SCRIPT = {"alias": "Good Night", "sequence": [
    {"action": "homeassistant.turn_off", "target": {"entity_id": ["light.a", "switch.b"]}},
    {"action": "media_player.turn_off", "target": {"entity_id": "media_player.tv"}},
    {"action": "homeassistant.turn_off", "target": {"entity_id": "switch.disabled"}, "enabled": False},
    {"delay": {"seconds": 1}},
    {"action": "notify.phone", "data": {"message": "x"}},
    {"action": "homeassistant.turn_off", "target": {"entity_id": ["lock.front_door"]}}]}


class ScriptIO(FakeIO):
    def __init__(self, states, conf=NIGHT_SCRIPT, runs=True, **kw):
        super().__init__(states, **kw)
        self.conf, self.runs, self.ran = conf, runs, []

    async def script_config(self, sid):
        return self.conf

    async def run_script(self, sid):
        self.ran.append(sid)
        if not self.runs:
            return False
        for a, e in routines.script_targets(self.conf):                  # the fake HA executes the script's steps
            if e not in self.stuck and self.s.get(e) != "unavailable":
                self.s[e] = "on" if a == "turn_on" else "off"
        return True


def run_script_mode(name, io):
    return asyncio.run(routines.run(name, SCRIPT_CFG, io, settle_s=1.0))


def test_script_targets_read_only_simple_on_off_steps():
    t = routines.script_targets(NIGHT_SCRIPT)
    assert t == [("turn_off", "light.a"), ("turn_off", "switch.b"), ("turn_off", "media_player.tv")]      # not disabled, delay, notify or lock
    assert routines.script_targets(None) == [] and routines.script_targets({}) == []
    assert routines.script_targets({"sequence": [{"service": "light.turn_on", "entity_id": "light.x, light.y"}]}) == [("turn_on", "light.x"), ("turn_on", "light.y")]


def test_script_runs_then_each_target_is_verified():
    io = ScriptIO({"light.a": "on", "switch.b": "on", "media_player.tv": "on"})
    speech, ok = run_script_mode("night", io)
    assert ok and io.ran == ["script.good_night"] and speech == "All 3 devices are off."


def test_script_stuck_or_unavailable_device_is_named():
    io = ScriptIO({"light.a": "on", "switch.b": "on", "media_player.tv": "unavailable"}, stuck={"switch.b"})
    speech, ok = run_script_mode("night", io)
    assert not ok and "1 of 3" in speech and "B did not change" in speech and "TV unavailable" in speech


def test_script_that_fails_to_run_says_so_and_checks_nothing():
    io = ScriptIO({"light.a": "on"}, runs=False)
    speech, ok = run_script_mode("night", io)
    assert not ok and "did not run" in speech


def test_script_with_nothing_verifiable_is_ok_but_honest():
    io = ScriptIO({}, conf={"sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.x"}}]})
    speech, ok = run_script_mode("night", io)
    assert ok and "can't check" in speech


def test_script_config_problems():
    assert routines.problems(SCRIPT_CFG) == []
    assert len(routines.problems({"night": {"script": "good_night"}})) == 1
    assert len(routines.problems({"night": {"script": "script.a", "turn_off": ["light.a"]}})) == 1
