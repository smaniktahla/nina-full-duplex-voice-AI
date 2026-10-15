#!/bin/bash
# One command: ./run.sh  ->  https://<this-host>:8443/
cd "$(dirname "$0")"
[ -f cert.pem ] || ./gen_cert.sh
PY=/home/salil/venvs/duplex-live/bin/python
# Private runtime flags that must survive redeploys (one KEY=value per line, e.g. RESPONDER=1). Never in git. Trial instances read it too
# (so a trial runs the live configuration); explicit environment variables on the command line still win.
NINA_ENV_FILE="${NINA_ENV:-$HOME/.config/nina/env}"
if [ -f "$NINA_ENV_FILE" ]; then
  while IFS= read -r l; do case "$l" in ""|\#*) ;; *=*) k=${l%%=*}; [ -z "${!k+x}" ] && export "$l";; esac; done < "$NINA_ENV_FILE"
fi
# llama-server runs --parallel 2. The health gate pins all Hermes traffic to slot 0, so the voice
# server takes slot 1 (set VOICE_SLOT=-1 to go back to unpinned). Added 2026-10-15.
export VOICE_SLOT="${VOICE_SLOT:-1}"
echo "open: https://$(hostname -I | awk '{print $1}'):${PORT:-8443}/   (accept the self-signed certificate warning once)"
exec $PY server.py "$@"
