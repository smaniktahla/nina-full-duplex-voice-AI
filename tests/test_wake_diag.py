import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live"))


def load(monkeypatch, tmp_path, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WAKE_DIAG_FILE", str(tmp_path / "d.jsonl"))
    import wake_diag
    return importlib.reload(wake_diag)


def test_off_by_default_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("WAKE_DIAG", raising=False)
    wd = load(monkeypatch, tmp_path)
    wd.record("wake_check", text="hey nina")
    assert not (tmp_path / "d.jsonl").exists()


def test_floating_match_finds_phrase_the_anchored_matcher_misses(monkeypatch, tmp_path):
    wd = load(monkeypatch, tmp_path)
    assert wd.floating_match("and then she said hey Nina what's the weather")
    assert not wd.floating_match("the weather is fine")


def test_text_is_not_stored_unless_opted_in(monkeypatch, tmp_path):
    monkeypatch.delenv("WAKE_DIAG_TEXT", raising=False)
    wd = load(monkeypatch, tmp_path, WAKE_DIAG="1")
    wd.record("wake_check", utt="a", text="private words hey nina")
    row = json.loads((tmp_path / "d.jsonl").read_text())
    assert "text" not in row and row["words"] == 4 and row["floating_match"] is True
    wd = load(monkeypatch, tmp_path, WAKE_DIAG="1", WAKE_DIAG_TEXT="1")
    wd.record("wake_check", text="x")
    assert json.loads((tmp_path / "d.jsonl").read_text().splitlines()[-1])["text"] == "x"
    assert oct(os.stat(tmp_path / "d.jsonl").st_mode & 0o777) == "0o600"
