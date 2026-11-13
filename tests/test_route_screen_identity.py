"""A kiosk names its own screen (X-Nina-Screen), so route maps survive an IP change; the IP map stays the fallback."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import route_screen as r  # noqa: E402

CFG = json.dumps({"peers": {"10.0.0.5": "old_kiosk", "10.0.0.9": "laptop"}, "named": {"kitchen": "old_kiosk", "laptop": "laptop"}})


def test_declared_screen_wins_over_a_changed_ip(monkeypatch):
    monkeypatch.setenv("NINA_ROUTE_SCREENS", CFG)
    screen = r.declared_screen({"X-Nina-Service": "t", "X-Nina-Screen": "old_kiosk"})
    assert r.target("10.0.0.77", screen=screen) == "old_kiosk"          # new IP, still resolved
    assert r.target("10.0.0.9") == "laptop"                              # browser clients keep the IP map
    assert r.target("10.0.0.77") is None


def test_declared_screen_is_ignored_without_service_token_or_unknown_or_malformed(monkeypatch):
    monkeypatch.setenv("NINA_ROUTE_SCREENS", CFG)
    assert r.declared_screen({"X-Nina-Screen": "old_kiosk"}) is None
    assert r.declared_screen({"X-Nina-Service": "t", "X-Nina-Screen": "other"}) is None
    assert r.declared_screen({"X-Nina-Service": "t", "X-Nina-Screen": "a b/../"}) is None
    assert r.declared_screen({"X-Nina-Service": "t"}) is None


def test_spoken_screen_name_still_overrides(monkeypatch):
    monkeypatch.setenv("NINA_ROUTE_SCREENS", CFG)
    assert r.target("10.0.0.77", "show it on the laptop", "old_kiosk") == "laptop"
