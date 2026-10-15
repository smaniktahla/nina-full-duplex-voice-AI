"""Wake-word detection through Home Assistant's existing openWakeWord service (Wyoming protocol, default port 10400).

No model is run in this process: 16 kHz int16 PCM is streamed to the service and `detection` events come back. Available words
on this HA: okay_nabu (the household satellites' word), hey_jarvis, hey_mycroft, alexa, hey_rhasspy.
"""
import asyncio
import json
import os
import time

import household
HOST = household.get("wake_host", "WAKE_HOST")
PORT = int(household.get("wake_port", "WAKE_PORT"))
ACOUSTIC_WORDS = ["hey_jarvis", "okay_nabu", "alexa", "hey_mycroft", "hey_rhasspy"]      # HA openWakeWord models
# Phrase wake words have no acoustic model: while asleep the recogniser decodes the first ~1.3 s of each utterance and the text is
# matched here (Whisper writes 'Hey Nina' as Nina / Nia / Neena ... depending on the voice). Everything else is dropped unlogged.
PHRASE_WAKE = {"hey_nina": r"^\W*(?:hey|hay|he|hi|okay|ok)\W*(?:nina|nia|neena|nena|ninna|nine a|nin a)\b"}
WORDS = ACOUSTIC_WORDS + list(PHRASE_WAKE)
WAKE_TOKENS = {"hey", "hay", "he", "hi", "okay", "ok", "jarvis", "nabu", "alexa", "mycroft", "rhasspy", "nina", "nia", "neena", "nena", "ninna"}


class WakeDetector:
    def __init__(self, name, on_detect, log=print):
        self.name, self.on_detect, self.log = name, on_detect, log
        self.w = self.r = self.task = None
        self.cool_until = 0.0
        self.t0 = time.time()

    async def start(self):
        await self.stop()
        self.r, self.w = await asyncio.wait_for(asyncio.open_connection(HOST, PORT), 5)
        self._send({"type": "detect", "data": {"names": [self.name]}})
        self._send({"type": "audio-start", "data": {"rate": 16000, "width": 2, "channels": 1, "timestamp": 0}})
        self.task = asyncio.ensure_future(self._read())
        self.log("wake detector listening for", self.name)

    def _send(self, header, payload=b""):
        if payload:
            header = dict(header, payload_length=len(payload))
        self.w.write((json.dumps(header) + "\n").encode() + payload)

    def feed(self, pcm: bytes):
        if self.w is None or self.w.is_closing():
            return
        try:
            self._send({"type": "audio-chunk", "data": {"rate": 16000, "width": 2, "channels": 1, "timestamp": int((time.time() - self.t0) * 1000)}}, pcm)
        except Exception:                                                    # noqa: BLE001
            pass

    async def _read(self):
        try:
            while True:
                line = await self.r.readline()
                if not line:
                    break
                h = json.loads(line)
                data = h.get("data") or {}
                if h.get("data_length"):
                    data = {**data, **json.loads(await self.r.readexactly(h["data_length"]))}
                if h.get("payload_length"):
                    await self.r.readexactly(h["payload_length"])
                if h.get("type") == "detection" and time.time() > self.cool_until:
                    self.cool_until = time.time() + 2.0
                    self.log("WAKE WORD detected:", data.get("name"))
                    self.on_detect(data.get("name"))
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as e:                                               # noqa: BLE001
            self.log("wake reader error", repr(e)[:100])

    async def stop(self):
        if self.task:
            self.task.cancel()
        if self.w:
            try:
                self.w.close()
            except Exception:                                                # noqa: BLE001
                pass
        self.r = self.w = self.task = None
