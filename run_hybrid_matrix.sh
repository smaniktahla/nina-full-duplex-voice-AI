#!/bin/bash
# Hybrid v2 vs best prompt-only, with repeats. Sequential (single shared slot). Results preserved: new dirs only.
cd "$(dirname "$0")"
B="python3 bench.py --set unit --ticks 600"
# timing variance: deterministic sampling (temp 0), 3 runs
$B --modes hybrid --gates 0.3 --clf-mode inline --repeat 3 --temp 0 --seed 1 --out results/rep_hybrid_inline_t0
# behavioural variance: temp 0.3, seeds 1-5
$B --modes hybrid --gates 0.3 --clf-mode inline --repeat 5 --temp 0.3 --seed 1 --out results/rep_hybrid_inline_t03
$B --modes hybrid --gates 0.3 --clf-mode stateless --repeat 3 --temp 0.3 --seed 1 --out results/rep_hybrid_stateless_t03
# best prompt-only (after rescoring): append_state, gate lo 0.3 / hi 0.95
$B --modes append_state --gates 0.3/0.95 --repeat 5 --temp 0.3 --seed 1 --out results/rep_promptonly_best_t03
$B --modes append_state --gates 0.3/0.95 --repeat 3 --temp 0 --seed 1 --out results/rep_promptonly_best_t0
# soak
python3 bench.py --set soak --modes hybrid --gates 0.3 --clf-mode inline --ticks 600 --out results/soak_hybrid_inline
echo HYBRID_MATRIX_DONE
