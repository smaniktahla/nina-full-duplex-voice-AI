"""Speaker-ID client: pipe protocol, fail-open and pause on a dying worker, SPEAKER_ID=off (fake worker, no models)."""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
from speaker_id.client import SpeakerClient                                    # noqa: E402

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_speaker_worker.py")]


def test_identify_roundtrip_and_failopen():
    async def go():
        c = SpeakerClient(log=lambda *a: None, cmd=FAKE)
        r = await c.identify(b"\x00\x01" * 16000)
        assert r["name"] == "Amy" and r["status"] == "identified" and r["speech_ms"] == 1000
        assert (await c.identify(b"\x00\x01" * 8000))["speech_ms"] == 500            # same worker, second request
        assert await c.identify(b"BOOM" + b"\x00" * 60) is None                       # worker dies: no label, no exception...
        assert not c.enabled()                                                        # ...and attempts pause
        assert await c.identify(b"\x00\x01" * 100) is None
        c.pause_until = 0
        assert (await c.identify(b"\x00\x01" * 100))["name"] == "Amy"                  # recovers with a fresh worker
        await c.close()
    asyncio.run(go())


def test_disabled_by_env_and_missing_worker():
    async def go():
        os.environ["SPEAKER_ID"] = "off"
        try:
            assert await SpeakerClient(log=lambda *a: None, cmd=FAKE).identify(b"\x00" * 64) is None
        finally:
            del os.environ["SPEAKER_ID"]
        c = SpeakerClient(log=lambda *a: None, cmd=["/nonexistent/python", "-m", "x"])
        assert await c.identify(b"\x00" * 64) is None and not c.enabled()
        await c.close()
    asyncio.run(go())
