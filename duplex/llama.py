"""Thin llama-server client: streaming /completion with logprobs, timings, cache stats.

Only reads/uses the public HTTP API; never touches server configuration.
"""
import http.client
import json
import math
import time
import urllib.parse


class Llama:
    def __init__(self, base="http://localhost:8080", timeout=120):
        u = urllib.parse.urlparse(base)
        self.host, self.port, self.timeout = u.hostname, u.port or 80, timeout

    def _conn(self):
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def get_json(self, path):
        c = self._conn()
        c.request("GET", path)
        return json.loads(c.getresponse().read())

    def tokenize(self, text):
        c = self._conn()
        c.request("POST", "/tokenize", json.dumps({"content": text, "add_special": False}),
                  {"Content-Type": "application/json"})
        return json.loads(c.getresponse().read())["tokens"]

    def task_id(self):
        try:
            return self.get_json("/slots")[0]["id_task"]
        except Exception:
            return -1

    def busy(self):
        try:
            return any(s.get("is_processing") for s in self.get_json("/slots"))
        except Exception:
            return False

    def complete(self, prompt, n_predict=24, n_probs=8, bias=None, temperature=0.0, seed=1, id_slot=None):
        body = {"prompt": prompt, "n_predict": n_predict, "n_probs": n_probs,
                "temperature": temperature, "seed": seed, "cache_prompt": True,
                "stream": True, "stop": ["<|im_end|>"]}
        if bias:
            body["logit_bias"] = bias
        if id_slot is not None:
            body["id_slot"] = id_slot
        c = self._conn()
        t0 = time.perf_counter()
        c.request("POST", "/completion", json.dumps(body), {"Content-Type": "application/json"})
        r = c.getresponse()
        ttft = None
        text, ids, first_top, last = [], [], None, {}
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            d = json.loads(line[5:])
            if ttft is None and d.get("content"):
                ttft = time.perf_counter() - t0
            if d.get("content"):
                text.append(d["content"])
            ids.extend(d.get("tokens") or [])
            if first_top is None and d.get("completion_probabilities"):
                cp = d["completion_probabilities"][0]
                first_top = [(t["id"], t["logprob"]) for t in cp.get("top_logprobs", [])]
            last = d
        total = time.perf_counter() - t0
        c.close()
        tm = last.get("timings", {})
        return {
            "text": "".join(text), "ids": ids, "first_top": first_top or [],
            "ttft": ttft if ttft is not None else total, "total": total,
            "cache_n": tm.get("cache_n", 0), "prompt_n": tm.get("prompt_n", 0),
            "prompt_ms": tm.get("prompt_ms", 0.0), "predicted_n": tm.get("predicted_n", 0),
            "draft_n": tm.get("draft_n", 0), "draft_ok": tm.get("draft_n_accepted", 0),
            "tokens_evaluated": last.get("tokens_evaluated", 0),
        }


def prob_of(first_top, token_id):
    for i, lp in first_top:
        if i == token_id:
            return math.exp(lp)
    return 0.0
