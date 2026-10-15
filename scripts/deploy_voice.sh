#!/bin/bash
# Deploy the voice server (and the Needle code it imports) from a git ref, with automatic rollback.
#
#   scripts/deploy_voice.sh [REF] [--trial] [--force] [--no-ci-check]
#
#   REF        branch / tag / sha to deploy (default: origin/main)
#   --trial    do NOT touch the running service: start this release on a spare port (8444, Piper TTS, no wake word) and smoke-test it
#   --force    deploy even if someone is connected to the live port
#   --no-ci-check   skip the "CI is green for this commit" check
#
# Layout (nothing here is inside the git tree):
#   ~/nina                 the clone (fetch only; edit in worktrees under ~/nina-wt/<topic>)
#   ~/nina-releases/<sha>  one detached worktree per deployed commit
#   ~/nina-current         symlink to the release that is running (or to the legacy tree ~/claude-workspace/duplex-sim)
#   ~/.local/share/nina/   state: cert.pem key.pem voices/ DEPLOYED server.log   (never committed)
set -u
REPO=${NINA_REPO:-$HOME/nina}; REL=$HOME/nina-releases; CUR=$HOME/nina-current; STATE=$HOME/.local/share/nina
LEGACY=$HOME/claude-workspace/duplex-sim; PORT=${PORT:-8443}; UNIT=nina-voice.service
REF=origin/main; TRIAL=0; FORCE=0; CICHECK=1
for a in "$@"; do case "$a" in --trial) TRIAL=1;; --force) FORCE=1;; --no-ci-check) CICHECK=0;; -*) echo "unknown flag $a"; exit 2;; *) REF=$a;; esac; done
say() { echo "[$(date +%T)] $*"; }
die() { say "ERROR: $*"; exit 1; }
mkdir -p "$REL" "$STATE"

# ---- 0. state files that must exist outside git
[ -f "$STATE/cert.pem" ] || { [ -f "$LEGACY/live/cert.pem" ] && cp -n "$LEGACY/live/cert.pem" "$LEGACY/live/key.pem" "$STATE/" && chmod 600 "$STATE/key.pem"; }
[ -d "$STATE/voices" ] || ln -s "$LEGACY/live/voices" "$STATE/voices"
[ -f "$STATE/cert.pem" ] || die "no TLS cert in $STATE (run live/gen_cert.sh and move cert.pem/key.pem there)"
[ -f "$HOME/.config/nina/household.json" ] || say "WARNING: ~/.config/nina/household.json missing: hosts/calendars fall back to discovery/placeholders"

# ---- 1. resolve the ref
git -C "$REPO" fetch -q origin || die "git fetch failed"
SHA=$(git -C "$REPO" rev-parse --verify "$REF^{commit}") || die "cannot resolve $REF"; S7=${SHA:0:7}
say "deploying $REF = $S7  ($(git -C "$REPO" log -1 --format=%s "$SHA"))"
if [ $CICHECK = 1 ] && command -v gh >/dev/null; then
  bad=$(gh api "repos/smaniktahla/nina-full-duplex-voice-AI/commits/$SHA/check-runs" --jq '[.check_runs[]|select(.conclusion!="success")]|length' 2>/dev/null || echo "?")
  [ "$bad" = "0" ] || die "CI is not green for $S7 (non-success runs: $bad). Use --no-ci-check to override."
fi

# ---- 2. build the release directory
R=$REL/$S7
if [ ! -d "$R" ]; then git -C "$REPO" worktree add -q --detach "$R" "$SHA" || die "worktree add failed"; fi
ln -sfn "$STATE/cert.pem" "$R/live/cert.pem"; ln -sfn "$STATE/key.pem" "$R/live/key.pem"
for f in "$STATE"/voices/*.onnx "$STATE"/voices/*.onnx.json "$STATE"/voices/*.bin; do [ -e "$f" ] && ln -sfn "$(readlink -f "$f")" "$R/live/voices/$(basename "$f")"; done
python3 -m compileall -q "$R/live" "$R/duplex" "$R/needle-voice" || die "release does not compile"

# ---- 3. trial mode: spare port, nothing else touched
if [ $TRIAL = 1 ]; then
  TP=8444; say "TRIAL on port $TP (Piper TTS, always-on, unpinned slot); the live service is not touched"
  (cd "$R/live" && PORT=$TP TTS=piper WAKE=off VOICE_SLOT=-1 setsid nohup ./run.sh > "$STATE/trial.log" 2>&1 < /dev/null &)
  for i in $(seq 1 60); do sleep 2; grep -q "TTS ready" "$STATE/trial.log" 2>/dev/null && break; done
  /home/salil/venvs/duplex-live/bin/python "$R/scripts/smoke_voice.py" --port $TP --tts-any; rc=$?
  pid=$(ss -ltnp 2>/dev/null | grep ":$TP " | sed -E 's/.*pid=([0-9]+).*/\1/' | head -1); [ -n "$pid" ] && kill "$pid"
  [ $rc = 0 ] && say "TRIAL PASSED for $S7" || say "TRIAL FAILED for $S7 (log: $STATE/trial.log)"; exit $rc
fi

# ---- 4. live deploy: wait until nobody is talking
conns() { ss -tn 2>/dev/null | grep ":$PORT " | grep -c ESTAB; }
if [ $FORCE = 0 ] && [ "$(conns)" != "0" ]; then die "$(conns) client(s) connected to :$PORT. Ask them to End the conversation, or use --force."; fi
PREV=$(readlink "$CUR" 2>/dev/null || echo "$LEGACY")

stop_service() {
  systemctl --user stop $UNIT 2>/dev/null
  for p in $(pgrep -f "^/home/salil/venvs/duplex-live/bin/python server.py"); do kill "$p"; done
  for i in $(seq 1 30); do pgrep -f "^/home/salil/venvs/duplex-live/bin/python server.py" >/dev/null || break; sleep 1; done
  sleep 3                                                    # GPU memory is released a moment after the process exits (Kokoro load race)
}
start_service() {
  if systemctl --user cat $UNIT >/dev/null 2>&1; then systemctl --user start $UNIT
  else setsid nohup bash -c 'cd "$1" && exec ./run.sh' _ "$CUR/live" >> "$STATE/server.log" 2>&1 < /dev/null & fi
}
switch_to() { ln -sfn "$1" "$CUR.new" && mv -Tf "$CUR.new" "$CUR"; }

say "stopping the running voice server (previous release: $PREV)"
stop_service
switch_to "$R"
start_service
ok=0
for i in $(seq 1 60); do sleep 3; curl -skf -m3 "https://localhost:$PORT/api/config" >/dev/null 2>&1 && { ok=1; break; }; done
if [ $ok = 1 ]; then
  /home/salil/venvs/duplex-live/bin/python "$R/scripts/smoke_voice.py" --port "$PORT" && ok=2
fi
if [ $ok != 2 ]; then
  say "NEW RELEASE FAILED its smoke test: rolling back to $PREV"
  stop_service; switch_to "$PREV"; start_service
  for i in $(seq 1 60); do sleep 3; curl -skf -m3 "https://localhost:$PORT/api/config" >/dev/null 2>&1 && { say "ROLLED BACK: $PREV is serving again"; exit 1; }; done
  die "rollback target did not come up either: check $STATE/server.log and journalctl --user -u $UNIT"
fi
printf 'current=%s\nprevious=%s\nwhen=%s\n' "$R" "$PREV" "$(date -Is)" > "$STATE/DEPLOYED"
say "DONE: $S7 is live (previous: $PREV). Roll back with scripts/rollback_voice.sh"
