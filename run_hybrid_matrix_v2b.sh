#!/bin/bash
# Hybrid v2b (connective-list fix, deterministic answer continuation, punctuation ban on forced speech).
# v2a results (rep_hybrid_*, hybrid_v2*) are preserved untouched. Prompt-only baselines are reused (code unchanged).
cd "$(dirname "$0")"
B="python3 bench.py --set unit --ticks 600 --modes hybrid --gates 0.3"
$B --clf-mode inline --repeat 5 --temp 0.3 --seed 1 --out results/rep2_hybrid_inline_t03
$B --clf-mode inline --repeat 3 --temp 0 --seed 1 --out results/rep2_hybrid_inline_t0
$B --clf-mode stateless --repeat 3 --temp 0.3 --seed 1 --out results/rep2_hybrid_stateless_t03
python3 bench.py --set soak --modes hybrid --gates 0.3 --clf-mode inline --ticks 600 --out results/soak2_hybrid_inline
echo V2B_DONE
