"""Voice delegation to Paperclip: parsing, idempotency, honest speech (HTTP faked)."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from needle_voice import task_ops as t
from needle_voice.result import Status

CFG = t.TaskConfig("proj1", {"systems_lead": "agent-sl", "homelab_engineer": "agent-he"}, device="kitchen")


def run(c): return asyncio.run(c)


def test_parse_create():
    i = t.parse("Hey Nina, ask Claude to investigate the JSHQ research pipeline.")
    assert (i.intent, i.agent_key, i.task) == ("task.create", "systems_lead", "investigate the JSHQ research pipeline")
    assert t.parse("have someone review the latest commits").agent_key is None
    assert t.parse("assign the pool sensor problem to Hermes").agent_key == "homelab_engineer"
    assert t.parse("ask the team to improve JSHQ's matching algorithm").task.startswith("improve")


def test_parse_ignores_other_speech():
    for s in ("turn off the kitchen lights", "what's the team working on", "ask claude what time it is?", "have a nice day", "tell me a joke"):
        assert t.parse(s) is None, s


def test_unintegrated_agent_refused_without_post():
    calls = []
    async def post(p, b): calls.append(b); return {}
    r = run(t.create(CFG, t.parse("ask Codex to fix the login bug"), post, "r1"))
    assert r.status is Status.UNAVAILABLE and not calls and "Codex" in r.speech


def test_success_speaks_only_confirmed():
    async def post(p, b): return {"identifier": "NIN-90", "assigneeAgentId": "agent-sl"}
    r = run(t.create(CFG, t.parse("ask Claude to review the latest commits"), post, "r1"))
    assert r.status is Status.SUCCESS and "NIN 90" in r.speech and "Claude" in r.speech


def test_unassigned_response_does_not_claim_assignment():
    async def post(p, b): return {"identifier": "NIN-91", "assigneeAgentId": None}
    r = run(t.create(CFG, t.parse("have someone review the latest commits"), post, "r1"))
    assert "assigned it" not in r.speech and "nobody is assigned" in r.speech


def test_idempotency_key_stable_per_bucket():
    k1 = t.idempotency_key("kitchen", "Ask Claude to review!", 1000)
    assert k1 == t.idempotency_key("kitchen", "ask claude to  review", 1010)
    assert k1 != t.idempotency_key("kitchen", "ask claude to review", 1000 + t.BUCKET_S)
    assert k1 != t.idempotency_key("den", "ask claude to review", 1000)


def test_errors_never_claim_success():
    for kind in ("unavailable", "auth", "rejected"):
        async def post(p, b, kind=kind): raise t.TaskApiError(kind)
        r = run(t.create(CFG, t.parse("ask Claude to review the latest commits"), post, "r1"))
        assert r.status is Status.EXECUTION_FAILED and "nothing was created" in r.speech or kind == "auth"
    async def noid(p, b): return {}
    assert run(t.create(CFG, t.parse("ask Claude to review the latest commits"), noid, "r1")).status is Status.UNVERIFIED


def test_secrets_redacted_and_envelope():
    i = t.parse("ask claude to use key sk-abcdefghijklmnopqrstuv to deploy things")
    b = t.envelope(CFG, i, "r1", 1000)
    assert "sk-abc" not in b["description"] and b["assigneeAgentId"] == "agent-sl" and b["status"] == "todo"
    assert t.envelope(t.TaskConfig("p"), t.parse("have someone review the logs"), "r2", 1)["status"] == "backlog"
