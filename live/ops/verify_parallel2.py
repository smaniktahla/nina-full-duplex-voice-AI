#!/usr/bin/env python3
"""After apply_parallel2.sh: slots, Hermes (AI2), HA voice, and the real test: voice tick latency while Hermes holds the other slot."""
import json, os, statistics as st, sys, threading, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from duplex.llama import Llama
from duplex.loop import chat, system_prompt, THINK_PREFIX

ll = Llama()
slots = ll.get_json("/slots"); print("slots:", len(slots), "n_ctx per slot:", slots[0]["n_ctx"], "| health:", ll.get_json("/health"))

def hermes():
    key = [l.split(":", 1)[1].strip() for l in open(os.path.expanduser("~/.hermes/config.yaml")) if l.strip().startswith("API_SERVER_KEY")][0]
    req = urllib.request.Request("http://localhost:8642/v1/chat/completions", data=json.dumps({"model": "hermes", "messages": [{"role": "user", "content": "In one sentence: what is the capital of Spain? Do not use tools."}]}).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t = time.time(); d = json.load(urllib.request.urlopen(req, timeout=300))
    return time.time() - t, d["choices"][0]["message"]["content"][:80], d["usage"]["prompt_tokens"]

def voice_ticks(n, slot=None):
    sysm = chat("system", system_prompt("~", 600, True)); hist, lat = "", []
    for i in range(n):
        u = '[%.1fs] (silence)' % (i * .6)
        t = time.perf_counter(); ll.complete(sysm + hist + chat("user", u) + THINK_PREFIX, n_predict=6, id_slot=slot); lat.append((time.perf_counter() - t) * 1000)
        hist += chat("user", u) + "<|im_start|>assistant\n<think>\n\n</think>\n\n~<|im_end|>\n"; time.sleep(max(0, 0.6 - lat[-1] / 1000))
    return lat

def heavy():
    """A Hermes-like load on the other slot: ~20K-token prompt (prefill) + 300 generated tokens."""
    body = ("The quick brown fox jumps over the lazy dog. " * 2200)
    t = time.time(); r = ll.complete(body + "\nSummarise the above at length:", n_predict=300, n_probs=0, temperature=0.7)
    return time.time() - t, r["prompt_n"] + r["cache_n"], r["predicted_n"]

voice_ticks(5)
base = voice_ticks(20); print("voice tick, idle server      : p50 %.0f ms  p95 %.0f ms" % (st.median(base), sorted(base)[int(.95 * len(base)) - 1]))
res = {}
th = threading.Thread(target=lambda: res.update(h=heavy())); th.start(); time.sleep(2)
busy = []
while th.is_alive() and len(busy) < 150:
    busy += voice_ticks(10)
th.join()
print("voice tick, WHILE Hermes ran : p50 %.0f ms  p95 %.0f ms  max %.0f ms  (%d ticks)" % (st.median(busy), sorted(busy)[int(.95 * len(busy)) - 1], max(busy), len(busy)))
print("heavy request on the other slot: %.1fs, %d prompt tokens, %d generated" % res["h"])
hr = hermes(); print("Hermes AI2 call: %.1fs, prompt_tokens=%s -> %r" % (hr[0], hr[2], hr[1]))
