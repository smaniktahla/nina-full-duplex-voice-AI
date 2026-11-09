"""Delegating work to the agent team by voice: "ask Claude to look into the JSHQ matching", "have someone review the latest commits".

Paperclip owns tasks, assignment, runs, budgets and approvals. This module is a thin, HA-free adapter and owns no task state:

  * `parse` recognises an explicit delegation phrase (deterministic regex; the models never decide to create work). Anything it does not
    match returns None and falls through to the normal pipeline untouched.
  * `create` builds the request envelope, POSTs one Paperclip issue with a stable `idempotencyKey` (a repeated delivery of the same
    utterance replays the original issue instead of creating a second), and speaks ONLY what the API confirmed: an identifier, and an
    assignee only if the response carries one.
  * Agents without a validated Paperclip agent (Codex, Gemini) are refused honestly; nothing is created for them.
  * Voice grants no privilege: issues are created read-only/propose by policy of the target project; approvals are never given here.

The HTTP call is injected (`post(path, body) -> dict`, raising `TaskApiError`), so the module is pure and testable. Credentials are the
caller's business and never appear in an issue. Phase 1 covers `task.create` only; status/cancel/results are later issues.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .result import Status, ToolResult

BUCKET_S = 300                      # a repeat of the same utterance within one bucket replays the same issue
MAX_TASK = 600
_SECRET = re.compile(r"\b(?:[A-Za-z0-9_\-]{24,}|sk-[A-Za-z0-9]{10,})\b")

# spoken name -> (label, key). `key` None = no validated Paperclip agent: refuse, do not create.
AGENT_ALIASES = {
    "claude": ("Claude", "systems_lead"), "claude code": ("Claude", "systems_lead"), "the systems lead": ("the Systems Lead", "systems_lead"),
    "systems lead": ("the Systems Lead", "systems_lead"),
    "hermes": ("Hermes", "homelab_engineer"), "the homelab engineer": ("the Homelab Engineer", "homelab_engineer"),
    "homelab engineer": ("the Homelab Engineer", "homelab_engineer"),
    "the ai infrastructure agent": ("the AI Infrastructure Agent", "ai_infra"), "ai infrastructure agent": ("the AI Infrastructure Agent", "ai_infra"),
    "codex": ("Codex", None), "gemini": ("Gemini", None), "antigravity": ("Gemini", None),
}
_NAMES = "|".join(sorted((re.escape(k) for k in AGENT_ALIASES), key=len, reverse=True))
_GENERIC = r"(?:someone|somebody|an agent|one of the agents|the team|the agents|the agent team)"
_ASK = re.compile(r"^(?:hey nina,?\s*)?(?:please\s+)?(?:can you\s+|could you\s+)?(?:ask|tell|have|get)\s+(?P<who>" + _NAMES + "|" + _GENERIC +
                  r")\s+(?:to\s+)?(?P<task>.{6,})$", re.I | re.S)
_ASSIGN = re.compile(r"^(?:please\s+)?(?:assign|delegate)\s+(?P<task>.{6,}?)\s+to\s+(?P<who>" + _NAMES + "|" + _GENERIC + r")\s*$", re.I | re.S)
_QUESTION_HEAD = re.compile(r"^(?:what|how|is|are|did|does|do|why)\b", re.I)


@dataclass
class TaskConfig:
    device: str = "voice"      # which terminal this is; project, agents and policy are decided by the facade, not here


@dataclass
class Intent:
    intent: str                 # task.create (Phase 1)
    utterance: str
    task: str
    agent_label: str | None     # what the speaker called the agent, None = unspecified
    agent_key: str | None
    refused: str | None = None  # label of an agent we cannot route to


class TaskApiError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__("%s %s" % (kind, detail))
        self.kind = kind        # unavailable | auth | rejected | agent_unavailable
        self.detail = detail


Post = Callable[[str, dict], Awaitable[dict]]


def _clean(task: str) -> str:
    t = re.sub(r"\s+", " ", task).strip().strip(".!? ")
    t = _SECRET.sub("[redacted]", t)
    return t[:MAX_TASK]


def parse(text: str) -> Intent | None:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    m = _ASK.match(t) or _ASSIGN.match(t)
    if not m:
        return None
    raw = m.group("task").strip()
    task = _clean(raw)
    if len(task) < 6 or _QUESTION_HEAD.match(raw) and raw.endswith("?"):
        return None                                     # "ask claude what time it is" style questions are not work orders
    who = m.group("who").lower()
    if who in AGENT_ALIASES:
        label, key = AGENT_ALIASES[who]
        return Intent("task.create", t, task, label, key, refused=label if key is None else None)
    return Intent("task.create", t, task, None, None)


def idempotency_key(device: str, utterance: str, now: float | None = None) -> str:
    norm = re.sub(r"[^a-z0-9 ]", "", utterance.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    bucket = int((now if now is not None else time.time()) // BUCKET_S)
    return "voice:%s:%s:%d" % (device, hashlib.sha1(norm.encode()).hexdigest()[:16], bucket)


def payload(cfg: TaskConfig, it: Intent, request_id: str, now: float | None = None) -> dict:
    """What the facade receives. No project id, agent id or status: the facade owns those by server-side policy."""
    return {"task": it.task, "utterance": _clean(it.utterance), "agent_key": it.agent_key, "request_id": request_id, "device": cfg.device,
            "idempotency_key": idempotency_key(cfg.device, it.utterance, now)}


def _fail(reason: str, speech: str, side_effect: bool = False, **tel) -> ToolResult:
    return ToolResult(Status.EXECUTION_FAILED, speech, reason=reason, domain="task", telemetry=tel, side_effect=side_effect)


_WHO = {"systems_lead": "the Systems Lead", "homelab_engineer": "the Homelab Engineer", "ai_infra": "the AI Infrastructure Agent"}


async def create(cfg: TaskConfig, it: Intent, post: Post, request_id: str, now: float | None = None) -> ToolResult:
    if it.refused:
        return ToolResult(Status.UNAVAILABLE, "I don't have %s set up as an agent yet, so I haven't created anything." % it.refused,
                          reason="agent:not_integrated", domain="task", request_id=request_id)
    try:
        issue = await post("/v1/tasks", payload(cfg, it, request_id, now))
    except TaskApiError as e:
        if e.kind == "agent_unavailable":
            return ToolResult(Status.UNAVAILABLE, "%s isn't available for that right now, so I haven't created anything." % (it.agent_label or "That agent"),
                              reason="agent:unavailable", domain="task", request_id=request_id)
        if e.kind == "auth":
            return _fail("facade:auth", "I couldn't create that. My access to the task system was refused.", request_id=request_id)
        if e.kind == "rejected":
            return _fail("facade:rejected", "The task system rejected that request, so nothing was created.", request_id=request_id)
        return _fail("facade:unavailable", "I can't reach the task system right now, so nothing was created. Please try again in a bit.", request_id=request_id)
    ident = (issue or {}).get("identifier")
    if not ident:                                       # accepted but unconfirmed: never claim success, never retry blindly
        return ToolResult(Status.UNVERIFIED, "I sent that, but I couldn't confirm it was created. Check the task board before asking again.",
                          reason="facade:no_identifier", domain="task", request_id=request_id, side_effect=True)
    spoken = ident.replace("-", " ")
    if issue.get("assigned"):
        who = _WHO.get(issue.get("agent_key")) or it.agent_label or "an agent"
        speech = "Got it. I've created %s and assigned it to %s. I'll let you know when it's finished." % (spoken, who)
    else:
        speech = "Got it. I've logged it as %s, but nobody is assigned yet." % spoken
    return ToolResult(Status.SUCCESS, speech, reason="task:created", domain="task", request_id=request_id, side_effect=True,
                      telemetry={"issue": ident, "assigned": bool(issue.get("assigned"))})


def make_post(session, base_url: str, token: str, timeout_s: float = 8.0) -> Post:
    """An aiohttp-backed `post` for the facade. `session` is any aiohttp.ClientSession (Nina's own, or Home Assistant's).
    The token lives only in this closure; it is sent as a bearer header and never logged or put in a payload."""
    import asyncio
    import uuid

    async def post(path: str, body: dict) -> dict:
        headers = {"Authorization": "Bearer " + token, "X-Correlation-ID": (body.get("request_id") or uuid.uuid4().hex[:10])}
        try:
            async with session.post(base_url.rstrip("/") + path, json=body, headers=headers, timeout=timeout_s) as r:
                st = r.status
                data = await r.json(content_type=None) if st == 200 else {}
                err = (await r.json(content_type=None) or {}).get("error", "") if st != 200 else ""
        except (asyncio.TimeoutError, OSError, ValueError, Exception) as e:     # noqa: BLE001  (connection refused, reset, bad body)
            raise TaskApiError("unavailable", type(e).__name__)
        if st == 200:
            return data
        if st in (401, 403):
            raise TaskApiError("auth")
        if st == 409:
            raise TaskApiError("agent_unavailable")
        if st in (400, 413, 422, 429, 502):
            raise TaskApiError("rejected", err)
        raise TaskApiError("unavailable", str(st))

    return post
