"""Blank-recording detection and clip review/delete routes for voice enrollment (numpy + aiohttp only; no models)."""
import asyncio, os, sys, tempfile
import pytest
np = pytest.importorskip("numpy")
os.environ["SPEAKER_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
from speaker_id import embed, store                                             # noqa: E402
from speaker_id import web as W                                                 # noqa: E402
from aiohttp import web                                                         # noqa: E402
from aiohttp.test_utils import TestClient, TestServer                           # noqa: E402

SR = 16000
rng = np.random.default_rng(1)


def speechish(amp, secs=4.0, noise=0.0):
    """word-like bursts (0.4 s on, 0.15 s off) with harmonic content, over optional room noise"""
    t = np.arange(int(SR * secs)) / SR
    gate = ((t % 0.55) < 0.4).astype(float) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t) ** 2)
    ph = 2 * np.pi * np.cumsum(140 * (1 + 0.08 * np.sin(2 * np.pi * 0.7 * t))) / SR
    x = sum(np.sin(k * ph) / k for k in range(1, 10)) * gate
    return (x / np.abs(x).max() * amp + rng.normal(0, noise, len(t))).astype(np.float32)


def pcm16(x):
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def test_quality_rules():
    q = lambda x: embed.clip_quality(x)
    assert q(np.zeros(SR * 3, np.float32))["blank"]                                   # digital silence / muted mic
    assert q(rng.normal(0, 0.0008, SR * 3).astype(np.float32))["blank"]               # near-silent noise
    assert q((0.05 * np.sin(2 * np.pi * 50 * np.arange(SR * 3) / SR)).astype(np.float32))["blank"]   # steady hum
    assert q(rng.normal(0, 0.03, SR * 3).astype(np.float32))["blank"]                 # steady loud noise, no speech
    assert not q(speechish(0.3))["blank"]                                             # normal
    assert not q(speechish(0.03, noise=0.0005))["blank"]                              # quiet / far-field
    assert q(speechish(0.3, secs=0.4))["blank"]                                       # too short to be a phrase


def _app():
    app = web.Application(client_max_size=20 * 1024 * 1024)
    W.add_routes(app)
    return app


H = {"X-Nina-CSRF": "1"}


def test_routes_reject_blank_and_manage_clips():
    async def go():
        async with TestClient(TestServer(_app())) as c:
            r = await c.post("/api/speakers/clip?speaker=amy&purpose=enroll&condition=normal", data=pcm16(np.zeros(SR * 3)))
            j = await r.json(); assert r.status == 422 and j["blank"] and "not saved" in j["error"]
            assert (await (await c.get("/api/speakers/clips")).json())["clips"] == []                         # nothing stored
            for _ in range(2):
                r = await c.post("/api/speakers/clip?speaker=amy&purpose=enroll&condition=normal", data=pcm16(speechish(0.3)))
                assert r.status == 200 and not (await r.json())["quality"]["blank"]
            cl = (await (await c.get("/api/speakers/clips")).json())["clips"]
            assert len(cl) == 2 and not any(x["blank"] for x in cl)
            # a blank file that got in some other way (older version, manual copy) is found and purged
            d = W.audio_dir("amy", "test", "far"); os.makedirs(d)
            import wave
            with wave.open(os.path.join(d, "123_abcd.wav"), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm16(np.zeros(SR * 2)))
            cl = (await (await c.get("/api/speakers/clips")).json())["clips"]; assert sum(x["blank"] for x in cl) == 1
            assert (await (await c.get("/api/speakers/status")).json())["seconds"]["amy"]["enroll"] > 7
            r = await c.post("/api/speakers/clips/purge_blank", json={}, headers=H); assert (await r.json())["removed"] == 1
            good = (await (await c.get("/api/speakers/clips")).json())["clips"][0]
            q = {k: good[k] for k in ("speaker", "purpose", "condition", "file")}
            assert (await c.get("/api/speakers/clips/audio", params=q)).headers["Content-Type"] == "audio/wav"
            assert (await c.post("/api/speakers/clips/delete", json=q, headers=H)).status == 200
            assert len((await (await c.get("/api/speakers/clips")).json())["clips"]) == 1
            bad = dict(q, file="../../etc/passwd")                                                             # path traversal is refused
            assert (await c.post("/api/speakers/clips/delete", json=bad, headers=H)).status == 400
            assert (await c.get("/api/speakers/clips/audio", params=bad)).status == 400
    asyncio.run(go())
