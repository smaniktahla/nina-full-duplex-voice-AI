"""The kiosk client's pure parts: framing, warm-up, playback queue, backoff, config, URLs. No audio hardware, no network."""
import json, os, struct, sys
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kiosk-client"))
import nina_kiosk as k                                                           # noqa: E402


def test_framer_recuts_any_block_size_into_exact_frames():
    f = k.Framer(); out = []
    for n in (320, 320, 100, 1000, 7):
        out += list(f.feed(np.arange(n, dtype=np.int16)))
    assert all(len(b) == 640 * 2 for b in out) and len(out) == (320 + 320 + 100 + 1000 + 7) // 640
    f = k.Framer(); a = np.arange(1500, dtype=np.int16)
    assert b"".join(f.feed(a)) == a[:1280].tobytes()                              # nothing lost or reordered, the remainder waits


def test_warmup_drops_exactly_the_configured_samples_then_passes_audio():
    w = k.Warmup(0.01)                                                           # 160 samples
    a = np.ones(100, np.int16); b = w(a); assert len(b) == 0
    b = w(np.arange(100, dtype=np.int16)); assert list(b[:3]) == [60, 61, 62] and len(b) == 40
    assert len(w(np.ones(50, np.int16))) == 50
    w.restart(0.01); assert len(w(np.ones(400, np.int16))) == 240


def test_playback_queue_fill_cancel_duck_and_resample():
    p = k.Playback(24000)
    p.push(24000, np.arange(1, 101, dtype=np.int16).tobytes())
    assert list(p.fill(40)[:3]) == [1, 2, 3] and p.pending_s() == 60 / 24000
    rest = p.fill(100); assert list(rest[:2]) == [41, 42] and rest[59] == 100 and not rest[60:].any()      # silence once the queue is empty
    p.push(24000, np.full(200, 1000, np.int16).tobytes()); p.duck(True)
    assert abs(int(p.fill(10)[0]) - 120) <= 1                                       # ducked to 12%
    p.cancel(); assert p.pending_s() == 0 and not p.fill(5).any() and not p.ducked
    p.push(12000, np.ones(100, np.int16).tobytes()); assert 190 <= p.n <= 210        # 12 kHz speech is resampled to the 24 kHz stream
    p.cancel(); p.push(24000, b"\x01"); assert p.n == 0                              # a stray odd byte is ignored, not a crash


def test_earcon_tones_are_audible_and_bounded():
    p = k.Playback(24000); p.tone(k.EARCONS["wake"])
    a = p.fill(p.n); assert 0.05 * 32767 < np.abs(a).max() < 0.25 * 32767 and len(a) == 2 * int(0.09 * 24000)


def test_backoff_doubles_to_a_cap():
    assert [k.backoff(i, 2.0, 30.0) for i in range(6)] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0] and k.backoff(-3, 2.0, 30.0) == 2.0


def test_config_and_urls_and_token(tmp_path):
    p = tmp_path / "c.json"; p.write_text(json.dumps({"server": "https://h:8443/", "wake": "off", "bogus": 1}))
    c = k.load_config(str(p)); assert c["server"] == "https://h:8443/" and c["wake"] == "off" and "bogus" not in c and c["warmup_s"] == 2.5
    assert k.load_config(str(tmp_path / "missing.json"))["wake"] == "hey_nina"
    assert k.ws_url("https://h:8443/") == "wss://h:8443/ws" and k.ws_url("http://h:8080") == "ws://h:8080/ws"
    t = tmp_path / "tok"; t.write_text("  secret \n"); assert k.read_token(str(t)) == "secret" and k.read_token(str(tmp_path / "none")) == ""


def test_server_messages_drive_the_playback():
    c = k.Client(dict(k.DEFAULTS))
    c.play.push(24000, np.ones(2400, np.int16).tobytes())
    c.on_json({"t": "duck", "on": True}); assert c.play.ducked
    c.on_json({"t": "cancel", "why": "barge-in"}); assert c.play.pending_s() == 0 and not c.play.ducked
    c.on_json({"t": "earcon", "kind": "wake"}); assert c.play.pending_s() > 0.1
    c.on_json({"t": "earcon", "kind": "nonsense"}); c.on_json({"t": "mgr", "state": "ACTIVE"}); c.on_json({"t": "hello", "tts": "x"}); c.on_json({"t": "unknown"})
