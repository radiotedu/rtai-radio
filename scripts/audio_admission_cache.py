"""Reuse admitted media facts while the exact file size and timestamp match.

This optional operator-generated cache avoids opening thousands of complete
audio files on a cold broadcaster start. Missing or changed files use the
normal Mutagen admission path. It contains no song choices or probabilities.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import threading

_LOCK = threading.Lock()
_RECORDS: dict[str, dict[str, object]] | None = None


def cache_path() -> Path:
    override = os.environ.get("RADIOTEDU_AUDIO_ADMISSION_CACHE")
    if override:
        return Path(override).expanduser().resolve()
    root = Path(os.environ.get("PROGRAMDATA", str(Path.home() / ".local" / "share")))
    return root / "RadioTEDU" / "cache" / "ai-audio-admission-v1.json"


def admitted_audio(path: Path) -> dict[str, object] | None:
    global _RECORDS
    with _LOCK:
        if _RECORDS is None:
            try:
                document = json.loads(cache_path().read_text(encoding="utf-8-sig"))
                rows = document.get("records") if document.get("version") == 1 else None
                _RECORDS = rows if isinstance(rows, dict) else {}
            except (OSError, ValueError, TypeError, AttributeError):
                _RECORDS = {}
    try:
        record = _RECORDS.get(str(path.resolve()).casefold())
        if not isinstance(record, dict):
            return None
        stat = path.stat()
        duration = record.get("duration_seconds")
        if (
            stat.st_size != record.get("bytes")
            or stat.st_mtime_ns != record.get("verified_mtime_ns")
            or not isinstance(record.get("title"), str)
            or not record["title"].strip()
            or not isinstance(record.get("artist"), str)
            or not record["artist"].strip()
            or not isinstance(record.get("sha256"), str)
            or len(record["sha256"]) != 64
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            return None
        return dict(record)
    except (OSError, ValueError, TypeError):
        return None
