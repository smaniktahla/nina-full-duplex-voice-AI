"""Append-only JSONL telemetry for the live router. One line per request;
never raises (a telemetry failure must never affect a voice request)."""

from __future__ import annotations

import json
import os


def write(path: str, record: dict) -> bool:
    try:
        line = json.dumps(record, separators=(",", ":"), default=str)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception:  # noqa: BLE001
        return False
