#!/bin/bash
# Go back to the previously deployed release (or to the legacy pre-migration tree).
#   scripts/rollback_voice.sh              -> the release recorded as `previous` in ~/.local/share/nina/DEPLOYED
#   scripts/rollback_voice.sh legacy       -> ~/claude-workspace/duplex-sim (exactly how it ran before the migration)
#   scripts/rollback_voice.sh <sha|path>   -> a specific release
set -u
STATE=$HOME/.local/share/nina; CUR=$HOME/nina-current; REL=$HOME/nina-releases; LEGACY=$HOME/claude-workspace/duplex-sim; PORT=${PORT:-8443}; UNIT=nina-voice.service
say() { echo "[$(date +%T)] $*"; }
case "${1:-}" in
  "") T=$(grep '^previous=' "$STATE/DEPLOYED" 2>/dev/null | cut -d= -f2-); [ -n "$T" ] || { say "no previous release recorded"; exit 1; };;
  legacy) T=$LEGACY;;
  /*) T=$1;;
  *) T=$REL/${1:0:7};;
esac
[ -x "$T/live/run.sh" ] || { say "$T has no live/run.sh"; exit 1; }
CURRENT=$(readlink "$CUR" 2>/dev/null || echo "?")
say "rolling back $CURRENT -> $T"
systemctl --user stop $UNIT 2>/dev/null
for p in $(pgrep -f "^/home/salil/venvs/duplex-live/bin/python server.py"); do kill "$p"; done
for i in $(seq 1 30); do pgrep -f "^/home/salil/venvs/duplex-live/bin/python server.py" >/dev/null || break; sleep 1; done; sleep 3
ln -sfn "$T" "$CUR.new" && mv -Tf "$CUR.new" "$CUR"
if systemctl --user cat $UNIT >/dev/null 2>&1; then systemctl --user start $UNIT; else setsid nohup bash -c 'cd "$1" && exec ./run.sh' _ "$CUR/live" >> "$STATE/server.log" 2>&1 < /dev/null & fi
for i in $(seq 1 60); do sleep 3; curl -sk -m3 -o /dev/null "https://localhost:$PORT/api/config" >/dev/null 2>&1 && { printf 'current=%s\nprevious=%s\nwhen=%s\n' "$T" "$CURRENT" "$(date -Is)" > "$STATE/DEPLOYED"; say "OK: $T is serving"; exit 0; }; done
say "!! $T did not come up; see $STATE/server.log"; exit 1
