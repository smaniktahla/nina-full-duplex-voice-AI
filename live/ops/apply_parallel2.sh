#!/bin/bash
# Give the voice assistant its own llama-server slot on AI2.   Run as:   sudo bash apply_parallel2.sh
# Changes ONE thing in /etc/systemd/system/llama-server.service:  --parallel 1  ->  --parallel 2 -kvu
# (-kvu = one unified KV pool shared by both slots: same VRAM, any slot may use up to the full 98304 ctx, so a 50K-token Hermes
#  prompt still fits). Waits for an idle moment, backs the unit up, restarts, verifies, and ROLLS BACK automatically on any failure.
# Manual rollback later:  sudo bash apply_parallel2.sh --rollback
set -u
UNIT=/etc/systemd/system/llama-server.service
BAK=/etc/systemd/system/llama-server.service.bak-pre-parallel2-voice-20261014
HERE="$(cd "$(dirname "$0")/.." && pwd)"
say() { echo "[$(date +%T)] $*"; }

wait_health() {   # $1 = max seconds
  for ((i=0; i<$1; i+=3)); do curl -sf -m3 localhost:8080/health >/dev/null && return 0; sleep 3; done; return 1
}
restart_unit() {
  systemctl daemon-reload
  systemctl reset-failed llama-server 2>/dev/null   # the unit has StartLimitBurst=2/120s: never let our restarts trip it
  systemctl restart llama-server
}
rollback() {
  say "ROLLING BACK to the previous unit"
  cp -f "$BAK" "$UNIT"
  sleep 25                                          # stay clear of the start-rate limit
  restart_unit
  if wait_health 270; then say "ROLLED BACK, llama-server healthy on the old config"; else say "!! llama-server NOT healthy after rollback: check 'journalctl -u llama-server -n 80'"; fi
}
start_voice() {
  say "starting the voice assistant again"
  sudo -u salil -H bash -c "cd '$HERE' && (setsid nohup ./run.sh > server.log 2>&1 < /dev/null &)"
}

if [ "${1:-}" = "--rollback" ]; then
  [ -f "$BAK" ] || { say "no backup at $BAK"; exit 1; }
  rollback; start_voice; exit 0
fi

grep -q -- "--parallel 1" "$UNIT" || { say "unit does not contain '--parallel 1' (already changed?)"; grep -n parallel "$UNIT"; exit 1; }

say "waiting for an idle moment (no generation for 20 s, max 10 min)"
quiet=0
for ((t=0; t<600; t+=5)); do
  busy=$(curl -s -m4 localhost:8080/slots | python3 -c "import sys,json;print(any(s['is_processing'] for s in json.load(sys.stdin)))" 2>/dev/null || echo True)
  if [ "$busy" = "False" ]; then quiet=$((quiet+5)); else quiet=0; fi
  [ $quiet -ge 20 ] && break
  sleep 5
done
[ $quiet -ge 20 ] || { say "never idle for 20 s: not touching production. Re-run later."; exit 1; }

say "stopping the voice assistant (frees ~0.3 GB GPU memory for the reload)"
for p in $(pgrep -f "^/home/salil/venvs/duplex-live/bin/python server.py"); do kill "$p"; done
sleep 4

cp -n "$UNIT" "$BAK" && say "backed up -> $BAK"
sed -i 's/^\(\s*\)--parallel 1 \\$/\1--parallel 2 \\\n\1-kvu \\/' "$UNIT"
grep -q -- "--parallel 2" "$UNIT" && grep -q -- "-kvu" "$UNIT" || { say "edit failed"; rollback; start_voice; exit 1; }
say "unit now:"; grep -nE "parallel|kvu|ctx-size" "$UNIT"

say "restarting llama-server (model load takes about a minute)"
restart_unit
if ! wait_health 270; then say "llama-server did not become healthy"; journalctl -u llama-server -n 40 --no-pager | tail -15; rollback; start_voice; exit 1; fi
sleep 2
slots=$(curl -s -m5 localhost:8080/slots | python3 -c "import sys,json;print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
say "slots reported: $slots"
smoke=$(curl -s -m60 localhost:8080/completion -d '{"prompt":"The capital of France is","n_predict":4,"temperature":0}' | python3 -c "import sys,json;print(json.load(sys.stdin)['content'])" 2>/dev/null)
say "smoke completion: '$smoke'"
if [ "$slots" != "2" ] || [ -z "$smoke" ]; then say "verification failed"; rollback; start_voice; exit 1; fi
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
journalctl -u llama-server --no-pager -n 300 | grep -iE "n_slots|kv_unified|n_ctx_slot" | tail -3
start_voice
say "DONE: llama-server now has 2 slots (unified KV). Tell Claude to run the verification."
