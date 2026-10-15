import asyncio, json, sys, time
import numpy as np
from faster_whisper.audio import decode_audio
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
sys.path.insert(0, ".")
import importlib.util
spec = importlib.util.spec_from_file_location("b", "bench_stt.py")
src = open("bench_stt.py").read().split("clips = [")[0].replace("MODELS = sys.argv[1].split(\",\")", "MODELS=[]").replace('man = json.load(open("manifest.json"))', "man=[]")
ns = {}; exec(src, ns)
variants, pink = ns["variants"], ns["pink"]
HOST, PORT = sys.argv[1], int(sys.argv[2])
async def transcribe(audio16k):
    pcm = (np.clip(audio16k, -1, 1) * 32767).astype(np.int16).tobytes()
    t = time.time()
    async with AsyncTcpClient(HOST, PORT) as c:
        await c.write_event(Transcribe(language="en").event())
        await c.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        for i in range(0, len(pcm), 2048):
            await c.write_event(AudioChunk(rate=16000, width=2, channels=1, audio=pcm[i:i+2048]).event())
        await c.write_event(AudioStop().event())
        while True:
            ev = await c.read_event()
            if ev is None: return None, time.time() - t
            if Transcript.is_type(ev.type): return Transcript.from_event(ev).text, time.time() - t
man = json.load(open("manifest.json"))
async def main():
    await transcribe(decode_audio(man[0]["file"], sampling_rate=16000))          # warm-up
    for m in man[:8]:
        sig = decode_audio(m["file"], sampling_rate=16000)
        for kind, audio in variants(sig).items():
            text, dt = await transcribe(audio)
            print(f"{dt:4.2f}s {kind:5s} | {m['text']!r:52s} -> {text!r}")
    for name, a in {"fan noise only": (pink(16000*8)*0.05).astype(np.float32), "quiet room only": (pink(16000*6)*0.004).astype(np.float32)}.items():
        text, dt = await transcribe(a); print(f"{dt:4.2f}s {name:16s} -> {text!r}   (must be empty)")
asyncio.run(main())
