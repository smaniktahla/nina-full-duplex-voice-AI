"""Stand-in for speaker_id.worker used by tests: replies with a fixed identification, never loads a model."""
import json, struct, sys
sys.stdout.buffer.write(b'@@{"ready": true, "profiles": ["amy"]}\n'); sys.stdout.buffer.flush()
while True:
    h = sys.stdin.buffer.read(4)
    if len(h) < 4: break
    n = struct.unpack("<I", h)[0]; d = sys.stdin.buffer.read(n)
    if b"BOOM" == d[:4]: sys.exit(1)
    sys.stdout.buffer.write(("@@" + json.dumps({"status": "identified", "speaker_id": "amy", "name": "Amy", "score": 0.9, "margin": 0.3,
                                                "speech_ms": n // 32, "profiles": 1}) + "\n").encode()); sys.stdout.buffer.flush()
