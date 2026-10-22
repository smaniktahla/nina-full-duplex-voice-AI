"""Routing corpus: every labeled plain-English request must be claimed by the tool the label says (live/claims.py), or by nobody ('none').
Cases listed in KNOWN_GAPS are real misses we have not fixed yet: they are expected to FAIL, and the test fails if one starts passing (so the list only shrinks).
Run `python3 tests/routing_report.py` for per-route precision/recall and the list of misses."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))
import claims                                                                   # noqa: E402
import notes_tool                                                               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
KNOWN_GAPS = {                                                                  # {"text": "why"}: real misses, expected to fail until fixed
    "how long to sky zone springfield": "lowercase unknown name + time cue: no place signal (needs the NLR-3 model fallback or a known place)",
    "how many minutes to sky zone springfield": "same",
}


class Store:
    def __init__(self, route): self._r = route
    def get(self): return object() if self._r else None


def load():
    return [json.loads(l) for l in open(os.path.join(HERE, "routing_corpus.jsonl")) if l.strip()]


def claimed(case):
    c = case["ctx"]
    got = claims.claim(case["text"], store=Store(c.get("route")), offered=bool(c.get("offered")), agenda_fresh=bool(c.get("agenda")))
    if not got and notes_tool.voice_command(case["text"], bool(c.get("recording"))):
        return "notes"
    return got or "none"


def run_all():
    return [(c, claimed(c)) for c in load()]


def test_corpus_is_big_and_varied():
    cases = load()
    assert len(cases) >= 250
    routes = {}
    for c in cases:
        routes[c["route"]] = routes.get(c["route"], 0) + 1
    assert set(routes) == {"display", "nav", "calendar", "weather", "random", "notes", "none", "clock"} and min(routes.values()) >= 5, routes


def test_every_phrase_routes_as_labeled():
    wrong = [(c["text"], c["ctx"], c["route"], got) for c, got in run_all() if got != c["route"] and c["text"] not in KNOWN_GAPS]
    assert not wrong, "%d misrouted:\n" % len(wrong) + "\n".join("  %-55r %s expected %s got %s" % w for w in wrong)


def test_known_gaps_are_still_gaps():
    fixed = [c["text"] for c, got in run_all() if c["text"] in KNOWN_GAPS and got == c["route"]]
    assert not fixed, "fixed, remove from KNOWN_GAPS: %s" % fixed
