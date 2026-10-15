#!/bin/bash
# Piper fallback voice (used only if Kokoro cannot load).
cd "$(dirname "$0")"
for f in en_US-lessac-medium.onnx en_US-lessac-medium.onnx.json; do
  [ -s "$f" ] || curl -sL -o "$f" "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/$f"
done
ls -la
