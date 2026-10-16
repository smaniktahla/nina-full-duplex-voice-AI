"""Voice-server side of speaker ID: talks to speaker_id.worker over pipes. Standard library only (the server's venv has no SpeechBrain).
Fail-open: any problem returns None (no label) and pauses attempts for a while; SPEAKER_ID=off disables it entirely."""
import asyncio, json, os, struct, time

PY = os.path.expanduser(os.environ.get("SPEAKER_PY", "~/venvs/speaker-eval/bin/python"))
LIVE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIX = b"@@"


class SpeakerClient:
    def __init__(self, log=print, cmd=None):
        self.log, self.cmd = log, cmd or [PY, "-m", "speaker_id.worker"]
        self.proc, self.lock, self.pause_until, self.profiles = None, asyncio.Lock(), 0.0, 0

    def enabled(self):
        return os.environ.get("SPEAKER_ID", "on") != "off" and time.monotonic() >= self.pause_until

    async def _start(self):
        env = dict(os.environ, PYTHONPATH=LIVE_DIR, PYTHONWARNINGS="ignore")
        self.proc = await asyncio.create_subprocess_exec(*self.cmd, cwd=LIVE_DIR, env=env, stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        ready = await self._read(60)                                   # first start loads the model (a few seconds)
        if not ready or not ready.get("ready"):
            raise RuntimeError("speaker worker did not become ready")
        self.profiles = len(ready.get("profiles", []))
        self.log("speaker worker ready, profiles:", ready.get("profiles"))

    async def _read(self, timeout):
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
            if not line:
                return None
            if line.startswith(PREFIX):
                return json.loads(line[2:])

    async def identify(self, pcm_int16: bytes):
        """-> dict(status, speaker_id, name, score, margin, speech_ms) or None"""
        if not self.enabled():
            return None
        async with self.lock:
            try:
                if self.proc is None or self.proc.returncode is not None:
                    await self._start()
                self.proc.stdin.write(struct.pack("<I", len(pcm_int16)) + pcm_int16)
                await self.proc.stdin.drain()
                r = await self._read(10)
                if r is None:
                    raise RuntimeError("worker closed")
                self.profiles = r.get("profiles", self.profiles)
                return r
            except Exception as e:                                      # noqa: BLE001  never let labelling hurt a conversation
                self.log("speaker id unavailable, pausing 60 s:", repr(e)[:100])
                self.pause_until = time.monotonic() + 60
                await self.close()
                return None

    async def close(self):
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            try:
                p.kill()
                await asyncio.wait_for(p.wait(), 2)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass
