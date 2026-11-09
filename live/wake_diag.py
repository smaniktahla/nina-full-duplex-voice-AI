"""Opt-in diagnostics for the 'Hey Nina' phrase wake word (observation only: nothing here changes behaviour).

WAKE_DIAG=1        write one JSON line per wake check to $WAKE_DIAG_FILE (default ~/.local/share/nina/wake_diag.jsonl)
WAKE_DIAG_TEXT=1   additionally store the transcript (off by default; transcripts of non-wake speech are otherwise never kept)
Never records audio. Each line carries the utterance id (`utt`) so a check, its final decode and the activation correlate.
"""
import json
import os
import re
import time

ENABLED = os.environ.get("WAKE_DIAG") == "1"
KEEP_TEXT = os.environ.get("WAKE_DIAG_TEXT") == "1"
PATH = os.environ.get("WAKE_DIAG_FILE", os.path.expanduser("~/.local/share/nina/wake_diag.jsonl"))
_MAX_BYTES = 20 * 1024 * 1024                                     # stop writing past this; the file is a bounded diagnostic, not a log

# The production matcher is anchored (^) to the start of the whole transcript. This one is not: it answers "was the phrase
# somewhere in the text, but not at the start?", which is the hypothesis that long noisy utterances hide the phrase.
_FLOATING = re.compile(r"\b(?:hey|hay|he|hi|okay|ok)\W*(?:nina|nia|neena|nena|ninna|nine a|nin a)\b", re.I)
_state = {"n": 0, "last_activation": None, "last_play_end": None}


def floating_match(text):
    return bool(_FLOATING.search(text or ""))


def note(key, value=None):
    """Record a state timestamp (e.g. note('last_activation')) so later lines can report time-since."""
    _state[key] = value if value is not None else time.time()


def record(event, **fields):
    if not ENABLED:
        return
    try:
        if os.path.exists(PATH) and os.path.getsize(PATH) > _MAX_BYTES:
            return
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        now = time.time()
        row = {"ts": round(now, 3), "event": event, **fields}
        text = row.pop("text", None)
        if text is not None:
            row["words"] = len(text.split())
            row["floating_match"] = floating_match(text)
            if KEEP_TEXT:
                row["text"] = text
        for k in ("last_activation", "last_play_end"):
            if _state.get(k):
                row["since_" + k + "_s"] = round(now - _state[k], 1)
        fd = os.open(PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:                                              # noqa: BLE001  diagnostics must never break audio
        pass
