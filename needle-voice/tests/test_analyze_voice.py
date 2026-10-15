import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import analyze_voice as av

ROWS = [
    {"timestamp": "2026-09-24T10:00", "input": "Turn on the kitchen lights", "route": "needle", "total_latency_ms": 100, "final_success": True},
    {"timestamp": "2026-09-24T10:01", "input": "what's the weather", "route": "rules", "total_latency_ms": 20, "final_success": True},
    {"timestamp": "2026-09-24T10:02", "input": "Pause Plex", "route": "hermes", "fallback_reason": "no_candidates", "total_latency_ms": 60000, "final_success": True},
    {"timestamp": "2026-09-24T10:03", "input": "pause plex", "route": "hermes", "fallback_reason": "no_candidates", "total_latency_ms": 61000, "final_success": True},
    {"timestamp": "2026-09-24T10:04", "input": "why is it cold", "route": "hermes", "fallback_reason": "guard:reasoning_markers", "total_latency_ms": 90000, "final_success": True},
    {"timestamp": "2026-09-24T10:05", "input": "turn on the fan", "route": "hermes", "fallback_reason": "ambiguous_target", "total_latency_ms": 50000, "final_success": False},
]


def test_summary_and_teachable_phrases():
    s = av.summarize(ROWS)
    assert s["total"] == 6 and s["routes"] == {"needle": 1, "rules": 1, "hermes": 4}
    assert s["fast_path_share"] == round(2 / 6, 3)
    assert s["teachable"][0] == (("pause plex", "no_candidates"), 2)          # case-folded and counted
    assert ("turn on the fan", "ambiguous_target") in dict(s["teachable"])
    assert all("why is it cold" not in k[0] for k, _ in s["teachable"])       # deliberate guard is not a lexicon problem
    assert s["deliberate_escalations"] == 1
    assert s["failures"] == ["turn on the fan"]
    assert s["latency_ms"]["needle"]["p50"] == 100


def test_report_flags_small_samples_and_failures():
    text = av.report(av.summarize(ROWS))
    assert "anecdotes" in text and "FAILED FOR THE USER" in text and "'pause plex'" in text


def test_load_tolerates_truncated_line_and_since(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in ROWS) + '\n{"timestamp": "2026-09-24T')
    assert len(av.load(str(p))) == 6
    assert len(av.load(str(p), since="2026-09-24T10:03")) == 3


def test_empty_input_is_safe():
    assert av.summarize([])["fast_path_share"] is None
