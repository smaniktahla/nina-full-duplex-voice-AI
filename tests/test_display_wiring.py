"""The glue between the voice server and Needle's display_ops: reply convention, config gating, referent rule, the viewer's Close button. The logic itself is
tested in needle-voice/tests/test_display_ops.py; here the HA and viewer I/O is a fake."""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "needle-voice"))
import tools                                                                   # noqa: E402
from needle_voice import display_ops as d, route_store as rs                    # noqa: E402

SENSOR = "sensor.test_kiosk_browser_path"
CFG = d.DisplayConfig(targets={"kitchen": SENSOR}, verify_timeout_s=2.0)


class Fake:
    def __init__(self, online=True, accept=True):
        self.state = {SENSOR: "/dashboard-news/0" if online else "unavailable", CFG.active_flag: "off", CFG.pause_flag: "off"}
        self.accept, self.key, self.t, self.pending, self.acts = accept, None, 0.0, None, []
        self.store = rs.RouteStore(); self.store.set("X", rs.parse_routes({"routes": [{"duration": "600s", "distanceMeters": 3000, "legs": []}]})[0])
        self.io = d.DisplayIO(self.act, self.get_state, self.write, lambda: (self.key, self.t - self.kt if self.key else float("inf")), self.sleep); self.kt = 0.0

    async def act(self, action, entity):
        self.acts.append((action, entity))
        if not self.accept:
            return False
        if action == "show":
            self.state[CFG.active_flag], self.state[SENSOR] = "on", CFG.route_path; self.pending = self.t + 1
        if action == "close":
            self.state[CFG.active_flag], self.state[SENSOR] = "off", "/dashboard-news/0"
        return True

    async def get_state(self, e): return self.state.get(e)
    async def write(self, doc): return True

    async def sleep(self, s):
        self.t += s
        if self.pending is not None and self.t >= self.pending:
            doc = self.store.to_document(); self.key, self.kt, self.pending = "%s|%s" % (doc["id"], doc["updated"]), self.t, None

    @property
    def env(self): return self.store, self.io, CFG


def run(c): return asyncio.run(c)


def test_reply_convention(monkeypatch):
    monkeypatch.setattr(tools, "KIOSK_TARGETS", {"kitchen": SENSOR})
    f = Fake(); assert run(tools.display_request(None, "show me the route on the kitchen screen", env=f.env)) == "[DONE] It's on the kitchen screen."
    f = Fake(online=False); r = run(tools.display_request(None, "show me the route on the kitchen screen", env=f.env)); assert r.startswith("[NOT DONE] ") and f.acts == []
    f = Fake(accept=False); assert run(tools.display_request(None, "show me the route", env=f.env)).startswith("[NOT DONE] ")


def test_unconfigured_display_says_so_and_owns_nothing(monkeypatch):
    monkeypatch.setattr(tools, "KIOSK_TARGETS", {})
    assert run(tools.display_request(None, "show me the route")).startswith("[NOT DONE] The kitchen display isn't set up")
    assert tools.display_owns("show me the route on the kitchen screen") is False


def test_owns_needs_a_referent(monkeypatch):
    monkeypatch.setattr(tools, "KIOSK_TARGETS", {"kitchen": SENSOR})
    empty = rs.RouteStore(); monkeypatch.setitem(tools._disp, "store", empty)
    assert tools.display_owns("put that on the kitchen display") is False                      # no route: 'that' is not ours
    assert tools.display_owns("show me the route") is True and tools.display_owns("what's the weather") is False
    monkeypatch.setitem(tools._disp, "store", Fake().store)
    assert tools.display_owns("put that on the kitchen display") is True


def test_viewer_close_button_is_true_only_when_seen_to_close():
    f = Fake(); f.state[CFG.active_flag], f.state[SENSOR] = "on", CFG.route_path
    assert run(tools.kiosk_close(None, env=f.env)) is True and f.acts == [("close", CFG.close_script)]
    n = Fake()                                                                                  # nothing on the screen: already closed, no call sent
    assert run(tools.kiosk_close(None, env=n.env)) is True and n.acts == []
    g = Fake(accept=False); g.state[CFG.active_flag], g.state[SENSOR] = "on", CFG.route_path
    assert run(tools.kiosk_close(None, env=g.env)) is False                                    # HA refused: the button must not report success


def test_server_routes_display_requests_deterministically():
    src = open(os.path.join(os.path.dirname(__file__), "..", "live", "server.py")).read()
    assert 'tools_mod.display_owns(user_said' in src and 'start_tool("display"' in src and 'k == "display"' in src and 'on_close=kiosk_close' in src
    assert 'replaced by @display' in src                                   # a model-chosen tool tag must not take a route display request
