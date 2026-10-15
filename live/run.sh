#!/bin/bash
# One command: ./run.sh  ->  https://<this-host>:8443/
cd "$(dirname "$0")"
[ -f cert.pem ] || ./gen_cert.sh
PY=/home/salil/venvs/duplex-live/bin/python
# llama-server runs --parallel 2. The health gate pins all Hermes traffic to slot 0, so the voice
# server takes slot 1 (set VOICE_SLOT=-1 to go back to unpinned). Added 2026-10-15.
export VOICE_SLOT="${VOICE_SLOT:-1}"
echo "open: https://$(hostname -I | awk '{print $1}'):${PORT:-8443}/   (accept the self-signed certificate warning once)"
exec $PY server.py "$@"
