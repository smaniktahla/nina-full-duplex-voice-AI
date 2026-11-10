"""Voice delegation to Paperclip: parsing, idempotency, honest speech (HTTP faked)."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from needle_voice import task_ops as t
from needle_voice.result import Status

CFG = t.TaskConfig(device="kitchen")


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
    async def post(p, b): return {"identifier": "NIN-90", "assigned": True, "agent_key": "systems_lead"}
    r = run(t.create(CFG, t.parse("ask Claude to review the latest commits"), post, "r1"))
    assert r.status is Status.SUCCESS and "NIN 90" in r.speech and "Systems Lead" in r.speech


def test_unassigned_response_does_not_claim_assignment():
    async def post(p, b): return {"identifier": "NIN-91", "assigned": False}
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


def test_secrets_redacted_and_payload():
    i = t.parse("ask claude to use key sk-abcdefghijklmnopqrstuv to deploy things")
    b = t.payload(CFG, i, "r1", 1000)
    assert "sk-abc" not in b["task"] + b["utterance"] and b["agent_key"] == "systems_lead"
    assert set(b) == {"task", "utterance", "agent_key", "request_id", "device", "idempotency_key"}      # no project/agent ids/status from the client


def test_agent_unavailable_from_facade():
    async def post(p, b): raise t.TaskApiError("agent_unavailable")
    r = run(t.create(CFG, t.parse("ask Claude to review the latest commits"), post, "r1"))
    assert r.status is Status.UNAVAILABLE and "haven't created anything" in r.speech


class FakeResp:
    def __init__(self, st, js): self.status, self._js = st, js
    async def json(self, content_type=None): return self._js
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class FakeSession:
    def __init__(self, resp=None, exc=None): self.resp, self.exc, self.seen = resp, exc, []
    def post(self, url, json=None, headers=None, timeout=None):
        self.seen.append((url, json, headers))
        if self.exc: raise self.exc
        return self.resp


def test_make_post_maps_status_and_never_leaks_token():
    def go(sess):
        return run(t.make_post(sess, "http://f:1/", "SECRETTOKEN")("/v1/tasks", {"request_id": "r9"}))
    s = FakeSession(FakeResp(200, {"identifier": "NIN-1"}))
    assert go(s)["identifier"] == "NIN-1" and s.seen[0][0] == "http://f:1/v1/tasks" and s.seen[0][2]["Authorization"] == "Bearer SECRETTOKEN"
    for st, kind in ((401, "auth"), (403, "auth"), (409, "agent_unavailable"), (422, "rejected"), (503, "unavailable")):
        try: go(FakeSession(FakeResp(st, {"error": "x"})))
        except t.TaskApiError as e: assert e.kind == kind and "SECRETTOKEN" not in str(e)
        else: raise AssertionError(st)
    try: go(FakeSession(exc=ConnectionRefusedError()))
    except t.TaskApiError as e: assert e.kind == "unavailable"


def test_stt_half_for_have_before_named_agent():
    it = t.parse("Half Hermes review the uptime monitoring of AI1")
    assert it and it.agent_key == "homelab_engineer" and it.task.startswith("review")
    assert t.parse("Half Hermes review the error handling in the needle project") is not None
    assert t.parse("half the team showed up late to the meeting") is None      # generic "half the team" is ordinary English
    assert t.parse("half of Hermes is broken today") is None


def test_lookups_stay_out_of_paperclip_unless_tracked():
    for s in ("ask Hermes to check the uptime of AI1", "have hermes report the hostname and uptime of ai1", "ask claude to show me the disk usage on docker2"):
        assert t.parse(s) is None, s
    assert t.parse("ask Hermes to check the uptime of AI1 and create a task if it is under a day") is not None
    assert t.parse("ask claude to investigate why the JSHQ matching is slow") is not None
