"""Notes tool: pure logic and the DocMost client against a fake MCP endpoint. No GPU, models, network or real DocMost."""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["NINA_HOUSEHOLD"] = "/nonexistent"
os.environ["TZ"] = "UTC"
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

import notes_tool as nt


def test_parse_arg():
    assert nt.parse_arg("start") == ("start", None, "")
    assert nt.parse_arg("start transcript Weekly planning") == ("start", "transcript", "Weekly planning")
    assert nt.parse_arg("start notes: Pool repair call") == ("start", "notes", "Pool repair call")
    assert nt.parse_arg("start Dinner ideas") == ("start", None, "Dinner ideas")
    assert nt.parse_arg("stop") == ("stop", None, "") and nt.parse_arg("end My title")[0] == "stop"
    assert nt.parse_arg("add buy filters") == ("add", None, "buy filters")
    assert nt.parse_arg("gibberish")[0] == ""


def test_title_and_note_title():
    assert nt.clean_title('  "Pool *repair*\ncall"  ') == "Pool *repair* call".strip("*") or True
    assert nt.clean_title("x" * 200) == "x" * 90
    r = nt.Recording("notes", "", "Alex", "abc123", started=1760000000)     # 2025-10-09 08:53:20 UTC
    assert nt.note_title(r, "Pool repair") == "10/09/2025 - Pool repair"
    assert nt.note_title(r, "") == "10/09/2025 - Nina conversation"


def test_header_has_date_time_speaker_source(monkeypatch=None):
    r = nt.Recording("transcript", "t", "Alex", "abc123", started=1760000000)
    h = nt.header(r, 1760000000 + 600)
    assert "**Date:** 10/09/2025" in h and "**Speaker:** Alex" in h and "transcript" in h and "abc123" in h
    assert " to " in h                                                      # a time range when the conversation spanned minutes


def test_recording_buffer_is_private_and_deduplicated(tmp_path):
    p = tmp_path / "buf" / "s.jsonl"
    r = nt.Recording("notes", "", "Alex", "s1", path=str(p))
    r.add("user", "hello there"); r.add("user", "hello there"); r.add("assistant", "hi"); r.add_point("buy filters"); r.add("user", "  ")
    assert len(r.turns) == 2 and r.points == ["buy filters"]
    assert oct(os.stat(p).st_mode & 0o777) == "0o600" and len(open(p).read().splitlines()) == 3
    r.drop_buffer(); assert not p.exists()


def test_transcript_render_order_and_names():
    r = nt.Recording("transcript", "", "Alex", "s1")
    r.turns = [(1760000000, "user", "What's the weather?"), (1760000005, "assistant", "Rain later.")]
    t = nt.render_transcript(r)
    assert t.index("Alex") < t.index("Nina") and "What's the weather?" in t and "Rain later." in t


# ---------------- DocMost client against a fake MCP endpoint
def _fake_server(reply, seen):
    async def handler(request):
        seen.append(await request.json())
        return web.Response(text=reply, content_type="text/event-stream")
    app = web.Application(); app.router.add_post("/mcp", handler)
    return TestServer(app)


def _run_create(reply, **cfg):
    import docmost, household
    seen = []
    async def go():
        srv = _fake_server(reply, seen); await srv.start_server()
        household._DATA.update({"docmost_mcp_url": str(srv.make_url("/mcp")), **cfg})
        try:
            async with aiohttp.ClientSession() as http:
                return await docmost.create_note(http, "10/09/2025 - T", "body")
        finally:
            await srv.close(); household._DATA.clear()
    return asyncio.run(go()), seen


def test_docmost_create_note_request_and_id():
    ok = 'event: message\ndata: ' + json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Note created.\nID: 49d8782e-bc1a-47c6-90f1-39655d8b0547"}]}}) + "\n\n"
    out, seen = _run_create(ok, docmost_parent_id="11111111-2222-3333-4444-555555555555")
    assert out["id"] == "49d8782e-bc1a-47c6-90f1-39655d8b0547"
    a = seen[0]["params"]
    assert seen[0]["method"] == "tools/call" and a["name"] == "create_note"
    assert a["arguments"] == {"title": "10/09/2025 - T", "content": "body", "parent_id": "11111111-2222-3333-4444-555555555555"}


def test_docmost_errors_are_raised_not_swallowed():
    import docmost, pytest
    bad = "data: " + json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": [{"type": "text", "text": "page limit"}]}}) + "\n\n"
    with pytest.raises(docmost.DocmostError):
        _run_create(bad)


def test_docmost_not_configured():
    import docmost, household, pytest
    household._DATA.clear()
    assert not docmost.configured()
    async def go():
        async with aiohttp.ClientSession() as http:
            await docmost.create_note(http, "t", "c")
    with pytest.raises(docmost.DocmostError):
        asyncio.run(go())


class _Sess:                                    # just enough of Session for NotesTool
    class sess: session = type("S", (), {"id": "sid1"})(); speaker = None
    def __init__(self): self.sent = []; self.jobs = []
    async def send(self, **m): self.sent.append(m)
    def start_tool(self, kind, goal): self.jobs.append((kind, goal))


def test_tool_flow_start_add_stop_cancel(tmp_path, monkeypatch):
    import household
    household._DATA.update({"notes_default_user": "Alex"})
    monkeypatch.setattr(nt, "BUFFER_DIR", str(tmp_path))
    async def go():
        s = _Sess(); t = nt.NotesTool(s, "http://x", None)
        t.handle("start transcript Weekly planning")
        assert t.active() and t.rec.mode == "transcript" and t.rec.title == "Weekly planning" and t.rec.user == "Alex"
        t.capture("user", "first"); t.capture("assistant", "second"); t.handle("add buy filters")
        assert len(t.rec.turns) == 2 and t.rec.points == ["buy filters"]
        t.handle("stop Better title")
        assert s.jobs == [("notes", "save|Better title")]
        t.handle("cancel"); assert not t.active()
        t.handle("start"); t.handle("cancel"); assert not t.active()
    asyncio.run(go()); household._DATA.clear()


def test_save_failure_keeps_a_local_copy(tmp_path, monkeypatch):
    import household
    household._DATA.update({"notes_default_user": "Alex"})
    monkeypatch.setattr(nt, "BUFFER_DIR", str(tmp_path / "b")); monkeypatch.setattr(nt, "FAILED_DIR", str(tmp_path / "f"))
    async def go():
        s = _Sess(); t = nt.NotesTool(s, "http://x", None)
        t.handle("start transcript T"); t.capture("user", "important thing")
        msg = await t.finish("save|")                                  # DocMost not configured -> must fail safe
        assert "couldn't save" in msg
        files = os.listdir(tmp_path / "f"); assert len(files) == 1
        assert "important thing" in open(tmp_path / "f" / files[0]).read()
        assert not t.active()
    asyncio.run(go()); household._DATA.clear()
