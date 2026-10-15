#!/bin/bash
# Full benchmark matrix. Sequential: the shared llama-server has one slot. Idempotent per config (re-run overwrites).
cd "$(dirname "$0")"
B="python3 bench.py"
# 1. state-management modes x gate at 600 ms
$B --modes append,append_state,rebuild,snapshot --ticks 600 --gates none,0.3 --set unit
# 2. tick interval sweep
$B --modes append_state,snapshot --ticks 400,900 --gates 0.3 --set unit
# 3. gate threshold sweep
$B --modes append_state --ticks 600 --gates 0.15,0.5 --set unit
# 4. sentinel representation
$B --modes append_state --ticks 600 --gates none,0.3 --sentinel listen --set unit
$B --modes append_state --ticks 600 --gates none,0.3 --sentinel LISTEN --set unit
# 5. long-call soak: context growth, cache, rebuild
$B --modes append_state,rebuild,snapshot --ticks 600 --gates 0.3 --set soak
echo MATRIX_DONE
