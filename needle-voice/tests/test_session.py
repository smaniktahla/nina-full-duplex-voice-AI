from needle_voice.session import ACTIVE, DORMANT, IDLE, SUSPENDED, SessionConfig, SessionManager


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def adv(self, s): self.t += s


def mk(**kw):
    clk, ev = Clock(), []
    cfg = SessionConfig(active_timeout_s=25, idle_timeout_s=20, suspend_ttl_s=300, response_window_s=7, max_ignored=2, ambient_grace_s=6, **kw)
    return SessionManager(cfg, clk, ev.append), clk, ev


def names(ev): return [e["event"] for e in ev]


def test_wake_starts_session_and_lifecycle_chain():
    m, clk, ev = mk()
    assert m.state == DORMANT and m.session is None
    t = m.wake("wake_word")
    assert m.state == ACTIVE and t["new_session"] and m.session.id
    clk.adv(26); assert [x["to"] for x in m.tick()] == [IDLE]
    clk.adv(21); assert [x["to"] for x in m.tick()] == [DORMANT]
    assert m.session is None


def test_idle_revived_only_by_engagement():
    m, clk, _ = mk(); m.wake()
    clk.adv(26); m.tick(); assert m.state == IDLE
    m.on_user_utterance("blah blah"); clk.adv(3); m.tick(); assert m.state == IDLE   # ambient speech does not revive
    m.engage("assistant_spoke"); assert m.state == ACTIVE


def test_ambient_speech_cannot_keep_session_alive():
    m, clk, ev = mk(); m.wake()
    for _ in range(5):                       # kids chattering every 5 s, assistant never answers
        clk.adv(5); m.on_user_utterance("mommy can i have juice"); m.tick()
    assert m.state == IDLE                   # long before 5 x 5 s + 25 s of 'activity'
    assert "ambient_ignored" in names(ev)


def test_answered_conversation_stays_alive():
    m, clk, _ = mk(); m.wake()
    for _ in range(8):
        clk.adv(10); m.on_user_utterance("what is the weather"); m.engage("assistant_spoke"); m.tick()
    assert m.state == ACTIVE


def test_dismissal_phrase_and_close():
    m, clk, ev = mk(); m.wake()
    assert m.on_user_utterance("Thanks, that's all.").action == "dismiss"
    assert m.on_user_utterance("stop listening").action == "dismiss"
    assert m.on_user_utterance("what is two plus two").action == "participate"
    m.dismiss(); assert m.state == DORMANT and "dismissed" in names(ev)


def test_dismissal_with_pending_task_suspends_and_resumes():
    m, clk, _ = mk(); m.wake()
    t = m.add_task("hermes", "research x"); m.add_turn("user", "find x")
    m.dismiss(); assert m.state == SUSPENDED and m.session.tasks[t.id].status == "pending"
    sid = m.session.id
    m.finish_task(t.id); clk.adv(30)
    r = m.wake("wake_word")
    assert r["resumed"] and m.session.id == sid and m.state == ACTIVE and m.context()[0] == ("user", "find x")
    assert [x.id for x in m.pending_tasks()] == [t.id]       # finished but not told yet: still to deliver


def test_suspend_ttl_cancels_tasks_and_drops_session():
    m, clk, ev = mk(); m.wake(); t = m.add_task("hermes", "slow"); m.dismiss()
    assert m.state == SUSPENDED
    clk.adv(301); out = m.tick()
    assert out[0]["to"] == DORMANT and m.session is None and "task_cancelled" in names(ev)


def test_running_task_holds_active_session():
    m, clk, _ = mk(); m.wake(); m.add_task("hermes", "slow")
    clk.adv(100); m.tick(); assert m.state == ACTIVE
    clk.adv(250); m.tick(); assert m.state == IDLE            # max_task_s caps it


def test_always_on_mode_never_transitions_and_ignores_dismissal():
    m, clk, _ = mk(wake_enabled=False)
    assert m.state == ACTIVE and m.session
    clk.adv(10_000); assert m.tick() == [] and m.state == ACTIVE
    assert m.on_user_utterance("goodbye").action == "participate"


def test_configure_runtime_timeouts_and_mode_switch():
    m, clk, _ = mk(); m.wake()
    m.configure(active_timeout_s=5)
    clk.adv(6); m.tick(); assert m.state == IDLE
    m.configure(wake_enabled=False); assert m.state == ACTIVE
    m.configure(wake_enabled=True); assert m.state == DORMANT


def test_context_and_events_are_session_scoped():
    m, clk, ev = mk(); m.wake()
    m.add_turn("user", "a"); m.add_turn("user", "b"); m.add_turn("assistant", "c")
    assert m.context() == [("user", "a b"), ("assistant", "c")]
    m.note_interruption("keyword wait"); m.dismiss()
    m.wake(); assert m.context() == [] and {"activated", "interruption", "transition", "dismissed"} <= set(names(ev))


def test_ok_thanks_dismisses():
    m, clk, ev = mk(); m.wake()
    for phrase in ("ok thanks", "Okay, thanks.", "okay thank you", "OK thanks Nina"):
        assert m.check_dismissal(phrase), phrase
    for phrase in ("thanks for checking", "that's ok thank goodness the pool is warm", "ok then what about tomorrow"):
        assert not m.check_dismissal(phrase), phrase
