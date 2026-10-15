"""Home requests for the duplex voice assistant, through Needle's own guards -> retrieval -> plan -> execute path.

router.py decides one utterance for the HA satellite pipeline and talks to Home Assistant through the `hass` object. The live voice
assistant is a separate process that only has HA's REST/WebSocket API, so this module adds the missing piece instead of re-implementing
Needle's logic: executor.RestHass (a duck-typed `hass` over REST, exactly what executor.py needs) and a snapshot builder that
produces the same {"entities", "areas"} snapshot as ha_snapshot.build_entity_snapshot. Everything that decides what is safe is reused:

    guards.pre_guard / intent_signals  ->  candidates.rank / unique_top  ->  executor.plan (intent table, exposed-only, sensitive off)
    ->  executor.execute (availability checks, honest speech)

Nothing here lets a model pick a service or an entity. Every request ends in a result.ToolResult. Only UNSUPPORTED / NO_MATCH may fall back to
another agent, and only after fallback_gate() agrees (a DENIED request never reaches HA's own conversation agent). Writes are verified by an
independent HA read before anything says "done".
aiohttp is imported lazily; the pure parts are tested without it.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable

from . import candidates as cand_mod
from . import executor, guards
from .context import Context, Pending, Referent, is_correction, uses_pronoun
from .result import FALLBACK_CANDIDATES, Status, ToolResult, new_request_id  # noqa: F401
from .executor import ExecutionError, PlanError, RestHass, ValidatedIntent, _State  # noqa: F401  (RestHass lives in executor.py: the only module allowed to reach HA services)
from .intents import ALL_TARGET_DOMAINS, INTENTS

SNAPSHOT_TTL_S = 600.0
PLURAL = frozenset({"lights", "lamps", "switches", "fans", "doors", "windows", "sensors"})
GROUP_WRITES = frozenset({"turn_on", "turn_off", "toggle"})     # the only writes allowed on a household-defined group
MAX_GROUP = 4


async def build_snapshot(session, base: str, token: str, timeout: float = 20.0) -> dict | None:
    """Same snapshot ha_snapshot.build_entity_snapshot builds, from HA's WebSocket registries: entities EXPLICITLY exposed to Assist
    (options.conversation.should_expose == true), their names/aliases, and their area (own, else the device's)."""
    import aiohttp
    try:
        ws_url = base.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
        async with session.ws_connect(ws_url, max_msg_size=0, timeout=timeout) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": token})
            if (await ws.receive_json()).get("type") != "auth_ok":
                return None
            out = {}
            for i, kind in enumerate(("config/entity_registry/list", "config/area_registry/list", "config/device_registry/list",
                                      "get_states"), 1):
                await ws.send_json({"id": i, "type": kind})
                while True:
                    m = await asyncio.wait_for(ws.receive_json(), timeout)
                    if m.get("id") == i:
                        out[kind] = m.get("result") or []
                        break
        names = {s["entity_id"]: (s.get("attributes") or {}).get("friendly_name") for s in out["get_states"]}
        dev_area = {d["id"]: d.get("area_id") for d in out["config/device_registry/list"]}
        entities = []
        for e in out["config/entity_registry/list"]:
            if not ((e.get("options") or {}).get("conversation") or {}).get("should_expose"):
                continue
            eid = e["entity_id"]
            if eid not in names:
                continue
            entities.append({"entity_id": eid, "name": names[eid] or e.get("name") or e.get("original_name") or eid,
                             "aliases": sorted(a for a in (e.get("aliases") or []) if isinstance(a, str)),
                             "area_id": e.get("area_id") or dev_area.get(e.get("device_id"))})
        areas = [{"area_id": a["area_id"], "name": a["name"], "aliases": sorted(x for x in (a.get("aliases") or []) if isinstance(x, str))}
                 for a in out["config/area_registry/list"]]
        return {"entities": entities, "areas": areas}
    except Exception:  # noqa: BLE001 - caller falls back
        return None


# ---------------------------------------------------------------- policy
HomeOutcome = ToolResult          # back-compat name: .speech / .reason / .telemetry still exist
SENSITIVE_INTENTS = frozenset({name for name, spec in INTENTS.items() if spec.get("sensitive")})
# Nouns that make a WRITE request consequential no matter what verb is used. Needle declining such a request (any reason) must never hand it
# to another agent; reading their state ("is the garage door open") is fine.
SENSITIVE_NOUNS = frozenset({"door", "doors", "garage", "lock", "locks", "deadbolt", "alarm", "security", "siren", "valve", "valves",
                             "shutoff", "heater", "heaters", "furnace", "boiler", "gate", "oven", "stove"})
QUESTION_STARTERS = frozenset({"is", "are", "whats", "what", "whos", "does", "do", "status", "check", "tell"})
DENIED_SPEECH = "I can't do that by voice."


def _is_question(toks: list[str]) -> bool:
    return bool(toks) and toks[0] in QUESTION_STARTERS and "turn" not in toks


def fallback_gate(text: str, allow_sensitive: bool = False) -> tuple[bool, str]:
    """The common policy check that must pass before ANY other agent (HA's built-in, Hermes) is asked to act on `text`.
    Blocks write requests that are sensitive by verb (unlock, open) or by noun (door, garage, lock, alarm, valve, heater...).
    Independent of whether Needle could resolve the request: a safety denial must not become permission because another agent can do it."""
    toks = guards.tokens(text)
    sig = guards.intent_signals(text)
    writes = set() if _is_question(toks) else (sig & guards.WRITE_INTENTS)
    if not writes:
        return True, "read_or_no_write"
    if not allow_sensitive and writes & SENSITIVE_INTENTS:
        return False, "sensitive_intent:" + ",".join(sorted(writes & SENSITIVE_INTENTS))
    hit = set(toks) & SENSITIVE_NOUNS
    if hit and not allow_sensitive:
        return False, "sensitive_noun:" + ",".join(sorted(hit))
    return True, "ok"


def pick_intent(text: str) -> tuple[str | None, str]:
    """One intent the words clearly imply, else (None, reason)."""
    sig = guards.intent_signals(text)
    writes = sig & guards.WRITE_INTENTS
    if writes and _is_question(guards.tokens(text)):          # "is the garage door open" asks about state; it does not command it
        writes = set()
        sig = sig | {"get_state"}
    if len(writes) > 1:
        return None, f"conflicting_intent_words:{sorted(writes)}"
    if writes:
        return next(iter(writes)), "write"
    for r in ("get_weather", "get_temperature", "get_state"):
        if r in sig:
            return r, "read"
    return None, "no_intent_words"


def _args(intent: str, text: str) -> dict | None:
    nums = re.findall(r"\d+(?:\.\d+)?", text)
    if intent == "set_brightness":
        return {"brightness": int(float(nums[0]))} if nums else None
    if intent == "set_temperature":
        return {"temperature": float(nums[0])} if nums else None
    return {}


_PAST = {"turn_on": "turned on", "turn_off": "turned off", "toggle": "toggled", "set_brightness": "dimmed", "lock": "locked", "close": "closed",
         "open": "opened", "media_play": "played", "media_pause": "paused", "media_stop": "stopped"}


# ---------------------------------------------------------------- decision
async def handle(text: str, snapshot: dict | None, hass: Any, allow_sensitive: bool = False, *, ctx: Context | None = None,
                 is_current: Callable[[], bool] = lambda: True, write_lock: asyncio.Lock | None = None,
                 on_late: Callable[[ToolResult], None] | None = None, snapshot_stale: bool = False,
                 request_id: str | None = None) -> ToolResult:
    """Never raises (except CancelledError). Always returns a ToolResult; see result.py for what each status permits."""
    t0 = time.perf_counter()
    rid = request_id or new_request_id()
    tel: dict[str, Any] = {"input": text, "domain": "home", "executor": "rest", "request_id": rid}

    def done(status: Status, speech: str | None, reason: str = "", **kw) -> ToolResult:
        tel.update(status=status.value, reason=reason, ms=round((time.perf_counter() - t0) * 1000, 1), **kw)
        if status in FALLBACK_CANDIDATES and not kw.get("side_effect"):
            ok, why = fallback_gate(text, allow_sensitive)
            tel["fallback_gate"] = why
            if not ok:
                tel.update(status=Status.DENIED.value, reason=f"policy:{why}", fallback="blocked")
                return ToolResult(Status.DENIED, DENIED_SPEECH, f"policy:{why}", tel, request_id=rid)
        tel.setdefault("fallback", "allowed" if status in FALLBACK_CANDIDATES else "not_applicable")
        return ToolResult(status, speech, reason, tel, side_effect=bool(kw.get("side_effect")), request_id=rid)

    def unsupported(reason: str, **kw) -> ToolResult:
        return done(Status.UNSUPPORTED, None, reason, **kw)

    try:
        if not isinstance(snapshot, dict):
            return unsupported("no_snapshot")
        ref = ctx.fresh() if ctx is not None else None
        g = guards.pre_guard(text)
        if g:
            return unsupported(f"guard:{g}")
        picked = ctx.choose(text) if ctx is not None else None
        if picked is not None:
            pend, (pid, pname) = picked
            intent, why, args = pend.intent, "pending_choice", pend.args
            tel["pending_choice"] = pid
        else:
            intent, why = pick_intent(text)
        if intent is None:
            if ref is not None and is_correction(text):          # "actually just the pendant lights": a correction with no verb is never executed
                return _clarify_correction(text, snapshot, ref, done)
            return unsupported(why)
        spec = INTENTS[intent]
        tel["intent"] = intent
        if spec.get("sensitive") and not allow_sensitive:
            tel["fallback"] = "blocked"
            return done(Status.DENIED, DENIED_SPEECH, "policy:sensitive_intent")
        if spec["kind"] == "write" and snapshot_stale:
            return unsupported("stale_snapshot")
        if picked is None:
            args = _args(intent, text)
        if args is None:
            return unsupported("missing_argument")

        ids: list[str] = []
        labels: list[str] = []
        is_group = False
        from_context = False
        cands = [] if picked is not None else cand_mod.rank(text, snapshot, ALL_TARGET_DOMAINS)
        tel["candidates"] = [{"id": c.id, "score": c.score} for c in cands[:5]]
        if picked is not None:
            ids, labels = [pid], [pname]
            tel.update(route="needle_exec", target=pid)
        elif uses_pronoun(text) and not cands:
            if ref is None:
                return done(Status.AMBIGUOUS, "Which device do you mean?", "pronoun_without_context")
            if len(ref.entity_ids) > 1 and spec["kind"] == "write" and not (ref.group and intent in GROUP_WRITES):
                return done(Status.AMBIGUOUS, "Which one: " + ", ".join(ref.labels) + "?", "group_write_not_allowed")
            ids, labels, is_group, from_context = list(ref.entity_ids), list(ref.labels), len(ref.entity_ids) > 1, True
            tel.update(route="context", targets=ids)
        else:
            if not cands:
                return done(Status.NO_MATCH, None, "no_candidates")
            plural = bool(set(guards.tokens(text)) & PLURAL)
            ties = [c for c in cands if c.kind == "entity" and c.score >= cands[0].score - 0.01]
            if plural:
                nouns = {cand_mod._stem(t) for t in guards.tokens(text) if t in PLURAL}
                byid = {e["entity_id"]: e for e in snapshot.get("entities") or [] if isinstance(e, dict)}
                ties = [c for c in ties if any(nouns & set(p) for p in cand_mod._phrases(byid.get(c.id, {})))]
            group = ties if (plural and len(ties) > 1 and len({c.domain for c in ties}) == 1) else []
            if group:
                group = group[:MAX_GROUP]
                if spec["kind"] == "write" and intent not in GROUP_WRITES:
                    if ctx is not None:
                        ctx.ask(Pending(intent, args, [(c.id, c.name) for c in group]))
                    return done(Status.AMBIGUOUS, "I can only turn a group of devices on or off. Which one: " + ", ".join(c.name for c in group) + "?",
                                "group_write_not_allowed")
                ids, labels, is_group = [c.id for c in group], [c.name for c in group], True
                tel.update(route="group", targets=ids)
            else:
                top = cand_mod.unique_top(cands)
                if top is not None and plural and spec["kind"] == "write" and len([c for c in cands if c.kind == "entity"]) > 1:
                    top = None                                   # "the pool lights" with several look-alikes: ask, don't guess
                if top is None:
                    names = ", ".join(c.name for c in cands[:3])
                    if ctx is not None:
                        ctx.ask(Pending(intent, args, [(c.id, c.name) for c in cands[:3]]))
                    return done(Status.AMBIGUOUS, f"Did you mean {names}?", "ambiguous_target")
                ids, labels = [top.id], [top.name]
                tel.update(target=top.id, route="needle_exec", equiv=len(cand_mod.equivalent_candidates(top, snapshot)))

        plans = [executor.plan(ValidatedIntent(intent, i, args, "voice"), snapshot, allow_sensitive=allow_sensitive) for i in ids]
        tel["policy"] = "allow"
        remember = lambda wrote: ctx.remember(Referent(ids, labels, is_group and not from_context or (from_context and ref.group), intent, wrote)) if ctx is not None else None  # noqa: E731

        if spec["kind"] == "read":
            await hass.refresh()
            speeches = []
            for p in plans:
                speeches.append((await executor.execute(hass, p, None)).rstrip("."))
            remember(False)
            return done(Status.SUCCESS, ". ".join(speeches) + ".", "")

        # ---- write: refresh, re-check currency, execute under the write lock, verify by independent read ----
        async def write() -> ToolResult:
            await hass.refresh()
            tel["plan"] = [{"intent": p.intent, "calls": [{"service": f"{c.domain}.{c.service}", "data": c.data} for c in p.calls]} for p in plans]
            rep = await executor.execute_verified(hass, plans, None)
            tel["verify"] = [{"id": r.entity_id, "outcome": r.outcome, "before": r.before, "after": r.after} for r in rep.results]
            return done(rep.status, rep.speech, rep.reason, side_effect=rep.side_effect)

        async def guarded() -> ToolResult:
            if write_lock is not None:
                async with write_lock:
                    if not is_current():
                        tel["fallback"] = "not_applicable"
                        return done(Status.CANCELLED, None, "superseded_before_execution")
                    return await write()
            if not is_current():
                return done(Status.CANCELLED, None, "superseded_before_execution")
            return await write()

        task = asyncio.ensure_future(guarded())
        try:
            res = await asyncio.shield(task)
        except asyncio.CancelledError:
            # The caller was cancelled (barge-in). Cancelling the wait is NOT cancelling a command that may already be in flight: let it finish and
            # keep its true outcome so it can be reported, never silently assumed away.
            if on_late is not None:
                task.add_done_callback(lambda t: on_late(t.result()) if not t.cancelled() and t.exception() is None else None)
            raise
        if res.status != Status.CANCELLED:
            remember(True)
        return res
    except executor.PlanError as e:
        r = str(e)
        if r == "sensitive_intent_disabled":
            tel["fallback"] = "blocked"
            return done(Status.DENIED, DENIED_SPEECH, "policy:sensitive_intent")
        return unsupported(f"plan_refused:{r}")
    except ExecutionError as e:
        r = str(e)
        if r.startswith("state_unavailable:") or r.startswith("target_unavailable:"):
            return done(Status.UNAVAILABLE, "That device is unavailable right now.", r)
        return done(Status.EXECUTION_FAILED, "I couldn't reach Home Assistant, so I didn't change anything.", r)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        return done(Status.EXECUTION_FAILED, "Something went wrong and I didn't change anything.", f"exception:{type(e).__name__}")


_CORR_WORDS = frozenset({"actually", "just", "only", "instead", "rather", "the", "a", "one", "meant", "i", "lights", "light"})


def _clarify_correction(text: str, snapshot: dict, ref: Referent, done) -> ToolResult:
    named = cand_mod.unique_top(cand_mod.rank(text, snapshot, ALL_TARGET_DOMAINS))
    what = " and ".join("the " + x for x in ref.labels)
    if ref.wrote:
        words = {t for t in guards.tokens(text) if t not in _CORR_WORDS}
        hit = [i for i, l in zip(ref.entity_ids, ref.labels) if words and words <= {cand_mod._stem(t) for t in guards.tokens(l)} | set(guards.tokens(l))]
        if named is not None and named.id in ref.entity_ids:
            hit = [named.id]
        keep = hit[0] if len(hit) == 1 else None
        others = [l for i, l in zip(ref.entity_ids, ref.labels) if keep is not None and i != keep]
        hint = f" For example, say turn off the {others[0]}." if keep and others else ""
        return done(Status.AMBIGUOUS, f"A moment ago I {_PAST.get(ref.intent, 'changed')} {what}. I haven't changed anything since. "
                    f"Tell me exactly what to change.{hint}", "correction_needs_explicit_command")
    return done(Status.AMBIGUOUS, "Do you want me to do something with it? Tell me the full command.", "correction_needs_explicit_command")


class HomeAgent:
    """Snapshot cache + RestHass + conversation context + request lifecycle for a long-running process.

    Lifecycle: every request() takes a new epoch. A request whose epoch is no longer current when it reaches the write step is CANCELLED with
    no side effect. One that already sent its command keeps its real outcome: it is marked superseded and queued in `late` so the NEXT reply
    says what actually happened (drain_late() for a proactive report). Nothing is reversed automatically."""

    WRITE_SNAPSHOT_MAX_AGE_S = 60.0

    def __init__(self, session, base: str, token: str, lexicons=None):
        """lexicons: list of async callables returning {entity_id|area:<id>: [phrases]} (Needle's household-vocabulary format)."""
        self.session, self.base, self.token, self.lexicons = session, base, token, lexicons or []
        self.hass = RestHass(base, token)
        self.snapshot: dict | None = None
        self.snap_t = 0.0
        self.ctx = Context()
        self.epoch = 0
        self.write_lock = asyncio.Lock()
        self.late: list[ToolResult] = []

    def supersede(self) -> None:
        """A newer user turn exists (barge-in / correction): in-flight requests that have not sent a command yet will not."""
        self.epoch += 1

    def drain_late(self) -> list[ToolResult]:
        out, self.late = self.late, []
        return out

    async def _snapshot(self, want_write: bool) -> tuple[dict | None, bool]:
        age = time.monotonic() - self.snap_t
        if self.snapshot is None or age > (self.WRITE_SNAPSHOT_MAX_AGE_S if want_write else SNAPSHOT_TTL_S):
            snap = await build_snapshot(self.session, self.base, self.token)
            if snap:
                self.snapshot, self.snap_t, age = snap, time.monotonic(), 0.0
        return self.snapshot, want_write and age > SNAPSHOT_TTL_S

    async def request(self, text: str, request_id: str | None = None) -> ToolResult:
        self.epoch += 1
        mine = self.epoch
        want_write = pick_intent(text)[1] == "write"
        snap, stale = await self._snapshot(want_write)
        for load in self.lexicons:
            try:
                snap = cand_mod.merge_lexicon(snap, await load())
            except Exception:  # noqa: BLE001
                pass
        res = await handle(text, snap, self.hass, ctx=self.ctx, is_current=lambda: self.epoch == mine, write_lock=self.write_lock,
                           on_late=self.late.append, snapshot_stale=stale, request_id=request_id)
        res.telemetry["request_id"] = res.request_id
        if res.status == Status.UNSUPPORTED and res.reason == "no_intent_words":
            self.ctx.clear()                                      # unrelated conversation: don't carry device context into it
        if self.epoch != mine:
            res.superseded = True
            res.telemetry["superseded"] = True
            if res.side_effect:
                self.late.append(res)
        elif self.late and res.speech and res.status != Status.CANCELLED:
            earlier = " ".join(r.speech for r in self.drain_late() if r.speech)
            res.speech = f"Earlier request already ran: {earlier} {res.speech}"
        return res
