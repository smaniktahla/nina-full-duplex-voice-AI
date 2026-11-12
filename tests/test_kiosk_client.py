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


def test_watchdog_settings_and_config_example_name_the_echo_cancelled_devices(monkeypatch):
    assert k.DEFAULTS["mic_stall_s"] >= 5
    ex = json.load(open(os.path.join(os.path.dirname(__file__), "..", "kiosk-client", "config.example.json")))
    assert ex["input_device"] == "nina_ec_source" and ex["output_device"] == "nina_ec_sink"
    c = k.Client(dict(k.DEFAULTS)); t0 = c.last_mic
    monkeypatch.setattr(k.time, "monotonic", lambda: t0 + 1)
    c.loop = type("L", (), {"call_soon_threadsafe": lambda *a: None})(); c.q = None
    c.connected = False; c._mic_cb(np.zeros(320, np.int16).tobytes(), 320, None, None); assert c.last_mic > t0        # every callback feeds the watchdog


def test_timer_alarm_loops_until_dismissed_and_survives_speech_cancel():
    c = k.Client(dict(k.DEFAULTS))
    c.on_json({"t": "timer_alarm", "active": True})
    cycle = c.play.fill(len(c.play.alarm_pcm))
    assert np.abs(cycle).max() > 5000 and not cycle[-2400:].any()
    for _ in range(25):
        assert np.array_equal(cycle, c.play.fill(len(cycle)))
    c.on_json({"t": "cancel", "why": "barge-in"})
    assert c.play.alarm_active and c.play.fill(2400).any()
    c.on_json({"t": "timer_alarm", "active": False})
    assert not c.play.alarm_active and not c.play.fill(2400).any()


def test_alarm_respects_volume_ducking_and_does_not_overflow():
    p = k.Playback(volume=0.5)
    p.alarm(True)
    a = p.fill(len(p.alarm_pcm))
    p.duck(True)
    b = p.fill(len(p.alarm_pcm))
    assert np.abs(a).max() < 3300
    assert np.abs(b).max() < np.abs(a).max() * 0.13
    p.duck(False)
    p.push(24000, np.full(2400, 32767, np.int16).tobytes())
    mixed = p.fill(2400)
    assert mixed.min() > 0 and mixed.max() <= 16384


def test_indicator_shows_only_while_active():
    assert k.indicator_wanted("ACTIVE") and k.indicator_wanted("active")
    assert not any(k.indicator_wanted(s) for s in ("IDLE", "DORMANT", "SUSPENDED", "", None))


def test_level_of_maps_loudness_to_0_1():
    assert k.level_of(np.zeros(320, np.int16)) == 0.0 and k.level_of(np.array([], np.int16)) == 0.0
    quiet = k.level_of(np.full(320, 30, np.int16)); mid = k.level_of(np.full(320, 1500, np.int16)); loud = k.level_of(np.full(320, 30000, np.int16))
    assert 0.0 <= quiet < mid < loud <= 1.0 and loud > 0.95                         # monotonic, speech-level audio lands mid-scale
    assert k.push_js("speak", 2.0).endswith("push('speak',1.000)") and "push('listen',0.000)" in k.push_js("x", -1)


def test_indicator_injects_pushes_live_levels_switches_colour_and_hides():
    """A fake DevTools endpoint: the overlay is injected into page tabs only, then live levels stream in, the mode changes, and hiding removes it."""
    import asyncio
    from aiohttp import web, ClientSession
    seen = []

    async def go():
        async def lst(request):
            host = request.host
            return web.json_response([{"id": "p1", "type": "page", "webSocketDebuggerUrl": "ws://%s/p1" % host},
                                      {"id": "u", "type": "browser_ui", "webSocketDebuggerUrl": "ws://%s/ui" % host}])

        async def page(request):
            ws = web.WebSocketResponse(); await ws.prepare(request)
            async for m in ws:
                seen.append(json.loads(m.data)["params"]["expression"])
                await ws.send_str(json.dumps({"id": 1, "result": {}}))
            return ws
        app = web.Application(); app.router.add_get("/json/list", lst); app.router.add_get("/p1", page)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]

        async def until(pred):
            for _ in range(100):
                if pred(): return True
                await asyncio.sleep(0.05)
            return False
        async with ClientSession() as http:
            ind = k.Indicator("http://127.0.0.1:%d" % port); ind.start(http)
            ind.set("listen", 0.4)
            assert await until(lambda: any(e.startswith("window.__ninaWave&&") and "'listen',0.400" in e for e in seen))
            assert seen[0] == k.WAVE_JS                                                  # injected first, once, before any push
            ind.set("speak", 0.9)
            assert await until(lambda: any("'speak',0.900" in e for e in seen))
            ind.set(None)
            assert await until(lambda: seen[-1] == k.OVERLAY_OFF)
            n = len(seen); await asyncio.sleep(0.3); assert len(seen) == n                # hidden = silent: nothing more is sent
            assert all(e in (k.WAVE_JS, k.OVERLAY_OFF) or e.startswith("window.__ninaWave&&") for e in seen)        # only the page tab, never browser_ui
            ind.stop()
            dead = k.Indicator("http://127.0.0.1:1"); dead.start(http); dead.set("listen", 0.5)
            await asyncio.sleep(0.4); dead.stop()                                      # unreachable: logged, no exception escapes
        await runner.cleanup()
    asyncio.run(go())
