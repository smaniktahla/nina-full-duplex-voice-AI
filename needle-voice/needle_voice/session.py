"""Session lifecycle: Needle as the conversation manager for the always-in-the-room duplex voice assistant.

Needle already decides *how* one utterance is routed (router.py: guards -> Needle -> Qwen -> Hermes). This module decides
*whether the assistant is in a conversation at all* and owns everything scoped to that conversation. The duplex engine
(live turn-taking: when to speak / listen / yield) and Hermes (heavy reasoning, tools) are clients of it, not owners.

Home Assistant-free, stdlib only, clock + event sink injected (same style as router.IO), so it is unit-testable and the
HA-satellite path is untouched.

States
  DORMANT    no session. Only the wake-word detector listens.
  ACTIVE     engaged. Full duplex.
  IDLE       grace period after inactivity: engine still hears you, but nothing you say keeps the session alive unless the
             assistant itself answers it (ambient-speech protection).
  SUSPENDED  asleep with the session (context + pending tasks) retained for `suspend_ttl_s`; a wake word resumes it.

Transitions
  DORMANT/SUSPENDED --wake--> ACTIVE (new session / resumed)
  ACTIVE --no engagement for active_timeout_s--> IDLE            (or sooner after `max_ignored` unanswered utterances)
  IDLE --engagement--> ACTIVE ;  IDLE --idle_timeout_s--> SUSPENDED (tasks pending) | DORMANT
  ACTIVE/IDLE --dismissal--> SUSPENDED (tasks pending) | DORMANT
  SUSPENDED --suspend_ttl_s--> DORMANT (pending tasks are cancelled, context dropped)

"Engagement" is only: wake / tap / typed input, the assistant speaking, a tool result delivered. Ambient speech the assistant
ignores is *not* engagement, so a noisy room cannot keep a session alive.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

DORMANT, ACTIVE, IDLE, SUSPENDED = "DORMANT", "ACTIVE", "IDLE", "SUSPENDED"
ASLEEP = (DORMANT, SUSPENDED)

DISMISS_RE = re.compile(
    r"\b(?:that'?s (?:all|it|enough|everything)|that will be all|nothing else|we'?re (?:all )?done|i'?m (?:all )?done|stop listening|"
    r"go to sleep|good ?night|good ?bye|bye(?: bye)?|see you(?: later| tomorrow)?|thanks?,? (?:that'?s|bye|good)|you can go|dismissed|"
    r"never ?mind (?:all )?that|cancel (?:the )?(?:conversation|session))\b", re.I)


@dataclass(frozen=True)
class SessionConfig:
    wake_enabled: bool = True              # False = always-on (state pinned ACTIVE, no timeouts): the pre-existing behaviour
    wake_word: str = "hey_jarvis"
    active_timeout_s: float = 25.0
    idle_timeout_s: float = 20.0
    suspend_ttl_s: float = 300.0
    response_window_s: float = 7.0         # an utterance the assistant hasn't answered within this is 'ignored'
    max_ignored: int = 2                   # this many consecutive ignored utterances => ambient room, cut the grace short
    ambient_grace_s: float = 6.0
    max_task_s: float = 300.0              # a pending task never holds a session open longer than this

    @classmethod
    def from_dict(cls, d: dict | None) -> "SessionConfig":
        d = d or {}
        f = cls.__dataclass_fields__
        return cls(**{k: type(f[k].default)(d[k]) if k in d else f[k].default for k in f})


@dataclass
class Task:
    id: int
    kind: str
    goal: str
    status: str = "pending"                # pending | done | cancelled
    told: bool = False                     # result delivered to the user
    t0: float = 0.0


@dataclass
class Session:
    id: str
    started: float
    turns: list = field(default_factory=list)      # [(who, text)]: session-scoped context
    tasks: dict = field(default_factory=dict)      # id -> Task: session-scoped pending / finished Hermes+tool tasks


@dataclass
class Decision:
    action: str                            # participate | dismiss | ignore
    reason: str = ""


class WakeDetector(Protocol):
    """Pluggable wake-word detector. Implementations live with the audio front end (e.g. live/wake.py -> HA openWakeWord)."""
    async def start(self) -> None: ...
    def feed(self, pcm: bytes) -> None: ...
    async def stop(self) -> None: ...


class SessionManager:
    def __init__(self, cfg: SessionConfig | None = None, clock: Callable[[], float] = time.monotonic,
                 emit: Callable[[dict], None] | None = None):
        self.cfg, self.clock = cfg or SessionConfig(), clock
        self._emit = emit or (lambda e: None)
        self.state = ACTIVE if not self.cfg.wake_enabled else DORMANT
        self.session: Optional[Session] = Session(self._sid(), clock()) if not self.cfg.wake_enabled else None
        self.t_state = clock()
        self.last_engagement = clock()
        self.pending_utts: list[float] = []            # user utterances awaiting an answer (timestamps)
        self.ignored_streak = 0
        self.task_seq = 0

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _sid() -> str:
        return uuid.uuid4().hex[:8]

    def emit(self, event: str, **kw) -> None:
        self._emit({"event": event, "state": self.state, "session": self.session.id if self.session else None,
                    "ts": time.time(), **kw})

    def _go(self, new: str, reason: str) -> dict:
        old, self.state, self.t_state = self.state, new, self.clock()
        t = {"from": old, "to": new, "reason": reason, "session": self.session.id if self.session else None}
        self.emit("transition", **{k: v for k, v in t.items() if k != "session"})
        return t

    def configure(self, **kw) -> None:
        """Change timeouts / wake word at runtime. wake_enabled flips the always-on mode."""
        from dataclasses import replace
        was = self.cfg.wake_enabled
        self.cfg = replace(self.cfg, **{k: type(getattr(self.cfg, k))(v) for k, v in kw.items() if hasattr(self.cfg, k)})
        if not self.cfg.wake_enabled and self.state != ACTIVE:
            self.wake("always-on")
        elif self.cfg.wake_enabled and not was and self.state != DORMANT:
            self._close("reconfigured")
        self.emit("configured", **{k: v for k, v in kw.items()})

    @property
    def asleep(self) -> bool:
        return self.state in ASLEEP

    # ------------------------------------------------------------------ session data
    def add_turn(self, who: str, text: str) -> None:
        if not self.session or not text:
            return
        t = self.session.turns
        if t and t[-1][0] == who:
            t[-1] = (who, (t[-1][1] + " " + text).strip())
        else:
            t.append((who, text))
        del t[:-24]

    def context(self, n: int = 8) -> list:
        return list(self.session.turns[-n:]) if self.session else []

    def add_task(self, kind: str, goal: str) -> Task:
        self.task_seq += 1
        t = Task(self.task_seq, kind, goal, t0=self.clock())
        if self.session:
            self.session.tasks[t.id] = t
        self.emit("task_added", task=t.id, kind=kind)
        return t

    def finish_task(self, tid: int) -> None:
        t = self.session.tasks.get(tid) if self.session else None
        if t and t.status == "pending":
            t.status = "done"
            self.engage("tool_result")
            self.emit("task_finished", task=tid)

    def task_told(self, tid: int) -> None:
        t = self.session.tasks.get(tid) if self.session else None
        if t:
            t.told = True

    def cancel_task(self, tid: int) -> None:
        t = self.session.tasks.get(tid) if self.session else None
        if t and t.status == "pending":
            t.status, t.told = "cancelled", True
            self.emit("task_cancelled", task=tid)

    def pending_tasks(self) -> list:
        """Running, or finished-but-not-yet-told: either keeps a session worth suspending instead of dropping."""
        if not self.session:
            return []
        now = self.clock()
        return [t for t in self.session.tasks.values()
                if (t.status == "pending" and now - t.t0 < self.cfg.max_task_s) or (t.status == "done" and not t.told)]

    # ------------------------------------------------------------------ events from the audio / duplex side
    def wake(self, source: str = "wake_word") -> Optional[dict]:
        """Wake word, tap, typed input. Returns the transition or None if already ACTIVE."""
        if self.state == ACTIVE:
            self.engage(source)
            return None
        resumed = self.state == SUSPENDED
        new_session = self.state == DORMANT
        if new_session:
            self.session, self.task_seq = Session(self._sid(), self.clock()), 0
        t = self._go(ACTIVE, ("resumed:" if resumed else "engaged:" if self.state == IDLE else "wake:") + source)
        t["resumed"], t["new_session"] = resumed, new_session
        self.engage(source, quiet=True)
        self.emit("activated", source=source, resumed=resumed)
        return t

    def engage(self, kind: str, quiet: bool = False) -> None:   # quiet: caller already moved the state
        """Real engagement (assistant spoke, result delivered, wake/tap/typing). Resets the inactivity clock."""
        self.last_engagement = self.clock()
        self.ignored_streak = 0
        self.pending_utts.clear()
        if self.state == IDLE:
            self._go(ACTIVE, "engaged:" + kind)

    def check_dismissal(self, text: str) -> bool:
        """Cheap, repeatable check on rolling transcript text (dismissal is meaningless in always-on mode)."""
        return bool(self.cfg.wake_enabled and self.state not in ASLEEP and DISMISS_RE.search(text or ""))

    def on_user_utterance(self, text: str) -> Decision:
        """Called once per complete user utterance. Does NOT extend the session: only an answer from the assistant does."""
        if self.state in ASLEEP:
            return Decision("ignore", "asleep")
        if self.check_dismissal(text):
            return Decision("dismiss", "dismissal phrase")
        self.pending_utts.append(self.clock())
        return Decision("participate")

    def note_interruption(self, why: str) -> None:
        self.emit("interruption", why=why)

    def dismiss(self, reason: str = "spoken") -> Optional[dict]:
        if self.state in ASLEEP:
            return None
        self.emit("dismissed", reason=reason)
        return self._close("dismissed")

    # ------------------------------------------------------------------ time
    def tick(self, busy: bool = False) -> list[dict]:
        """Advance timers. busy = assistant speaking / user mid-utterance (counts as engagement). Returns transitions."""
        out: list[dict] = []
        if not self.cfg.wake_enabled:
            return out
        now = self.clock()
        if busy:
            self.last_engagement = now
        # ambient protection: unanswered utterances are counted, they never refresh the clock
        while self.pending_utts and now - self.pending_utts[0] > self.cfg.response_window_s:
            self.pending_utts.pop(0)
            self.ignored_streak += 1
            self.emit("ambient_ignored", streak=self.ignored_streak)
        quiet_for = now - self.last_engagement
        if self.state == ACTIVE:
            limit = self.cfg.active_timeout_s
            if self.ignored_streak >= self.cfg.max_ignored:
                limit = min(limit, self.cfg.ambient_grace_s)
            if quiet_for > limit and not self.pending_tasks_running():
                out.append(self._go(IDLE, "ambient" if self.ignored_streak >= self.cfg.max_ignored else "inactivity"))
        elif self.state == IDLE:
            if now - self.t_state > self.cfg.idle_timeout_s and not self.pending_tasks_running():
                out.append(self._close("idle timeout"))
        elif self.state == SUSPENDED and now - self.t_state > self.cfg.suspend_ttl_s:
            out.append(self._expire("suspend ttl"))
        return out

    def pending_tasks_running(self) -> bool:
        return any(t.status == "pending" for t in self.pending_tasks())

    # ------------------------------------------------------------------ closing
    def _close(self, reason: str) -> dict:
        if self.pending_tasks():
            return self._go(SUSPENDED, reason + " (tasks pending)")
        t = self._go(DORMANT, reason)
        self.session = None
        return t

    def _expire(self, reason: str) -> dict:
        for t in list(self.session.tasks.values()) if self.session else []:
            self.cancel_task(t.id)
        t = self._go(DORMANT, reason)
        self.session = None
        return t
