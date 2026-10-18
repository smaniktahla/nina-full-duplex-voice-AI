#!/usr/bin/env python3
"""Per-route precision/recall over tests/routing_corpus.jsonl and the list of misses.   python3 tests/routing_report.py"""
import collections, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_routing_corpus as t

rows = t.run_all()
routes = sorted({c["route"] for c, _ in rows})
print("%-9s %5s %9s %9s" % ("route", "n", "recall", "precision"))
for r in routes:
    n = sum(1 for c, g in rows if c["route"] == r); tp = sum(1 for c, g in rows if c["route"] == r and g == r); claimed = sum(1 for c, g in rows if g == r)
    print("%-9s %5d %8.0f%% %8.0f%%" % (r, n, 100 * tp / n if n else 0, 100 * tp / claimed if claimed else 100))
miss = [(c, g) for c, g in rows if g != c["route"]]
print("\n%d of %d misrouted" % (len(miss), len(rows)))
for c, g in miss:
    print("  %-58r %-22s expected %-8s got %s%s" % (c["text"], c["ctx"] or "", c["route"], g, "   [known gap]" if c["text"] in t.KNOWN_GAPS else ""))
