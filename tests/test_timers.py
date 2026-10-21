import asyncio
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
from timers import Timer, duration


def test_duration():
    assert duration("set a timer for fifteen minutes") == 900
    assert duration("set a 15-minute timer") == 900
    assert duration("set a timer for ninety nine seconds") == 99
    assert duration("create a timer for 1 hour and thirty five minutes") == 5700
    assert duration("set a timer for half an hour") == 1800
    for text in ("set a timer", "zero minutes", "-5 minutes", "25 hours"):
        assert duration(text) is None


def test_lifecycle_and_naming():
    now = [100.0]
    timer = Timer(clock=lambda: now[0])
    answer, created = timer.handle("set a timer for 15 minutes")
    assert created and "15 minutes" in answer
    assert timer.owns("sure, this is for the turkey baking in the oven")
    timer.handle("sure, this is for the turkey baking in the oven")
    assert timer.current["label"] == "the turkey baking in the oven"
    assert not timer.owns("yeah, for something else")
    now[0] += 30
    assert "14 minutes and 30 seconds" in timer.handle("how much time is left on the timer")[0]
    assert timer.document()["remaining_s"] == 870
    assert not timer.handle("set a timer for 2 minutes")[1]
    assert timer.remaining() == 870
    now[0] += 900
    assert "finished" in timer.handle("check the timer")[0]
    assert timer.document()["remaining_s"] == 0
    assert not timer.handle("set a timer for 2 minutes")[1]
    timer.handle("cancel the timer")
    assert timer.current is None
    assert "no timer" in timer.handle("check the timer")[0]


def test_naming_window_expires():
    now = [0]
    timer = Timer(clock=lambda: now[0])
    timer.handle("set a timer for 2 minutes")
    now[0] = 61
    assert not timer.owns("yeah, for the kids reading before bedtime")


def test_request_schedules_expiry_and_cancellation(monkeypatch):
    import tools
    async def run():
        monkeypatch.setattr(tools, "_timer", Timer())
        monkeypatch.setattr(tools, "_timer_task", None)
        monkeypatch.setattr(tools, "_timer_ringing", False)
        sounds = []
        async def listener(**message):
            sounds.append(message["active"])
        monkeypatch.setattr(tools, "_timer_listeners", {listener})
        calls = []
        async def popup(http):
            calls.append(tools._timer.document())
        monkeypatch.setattr(tools, "timer_popup", popup)
        assert tools.claim("set a timer for one second") == "timer"
        answer = await tools.timer_request(None, "set a timer for one second")
        assert "I've set" in answer
        await asyncio.sleep(1.05)
        assert len(calls) == 2 and calls[-1]["remaining_s"] == 0
        assert sounds == [False, True] and tools._timer_ringing
        timer_id = tools._timer.current["id"]
        assert not await tools.kiosk_timer_stop(None, "stale-id")
        assert tools._timer_ringing
        assert await tools.kiosk_timer_stop(None, timer_id)
        assert sounds[-1] is False and not tools._timer_ringing
        await tools.timer_request(None, "set a timer for 2 minutes")
        task = tools._timer_task
        await tools.timer_request(None, "cancel the timer")
        await asyncio.sleep(0)
        assert task.cancelled() and tools._timer.current is None
    asyncio.run(run())


def test_transcribed_timer_for_duration_creates_instead_of_checking_status():
    for text in ("at timer for 5 minutes.", "a timer for five minutes", "timer for 5 minutes", "please timer for five minutes"):
        timer = Timer(clock=lambda: 0)
        answer, created = timer.handle(text)
        assert created and timer.remaining() == 300 and "I've set a timer for 5 minutes" in answer
        original = timer.current
        assert not timer.handle(text)[1] and timer.current is original


def test_status_questions_do_not_create_timers_from_mentioned_durations():
    for text in ("how much time is left on the timer", "is the timer for 5 minutes", "what happened to the timer for 5 minutes"):
        timer = Timer(clock=lambda: 0)
        assert not timer.handle(text)[1] and timer.current is None
    timer = Timer(clock=lambda: 0)
    answer, created = timer.handle("at timer for 25 hours")
    assert not created and timer.current is None and "How long" in answer
