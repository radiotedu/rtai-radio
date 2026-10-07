from __future__ import annotations

import json
import wave
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.transition_liner_library import (
    DAY_TONE,
    NIGHT_TONE,
    TransitionLinerLibrary,
    canonical_genre,
    tone_for_time,
)


def _write_liner(
    root: Path,
    *,
    language: str,
    tone: str,
    source: str,
    target: str,
    variant: int,
) -> None:
    directory = root / language / tone / f"{source}-to-{target}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"liner-{variant:03d}.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(b"\x01\x00" * 24_000)
    path.with_suffix(".json").write_text(
        json.dumps(
            {
                "lang": language,
                "tone": tone,
                "design": f"{language}-{'day' if tone == DAY_TONE else 'night'}",
                "from": source,
                "to": target,
                "idx": variant,
                "text": f"Radio TED U takes {source} into {target}.",
            }
        ),
        encoding="utf-8",
    )


def _complete_library(root: Path) -> None:
    genres = ("Lo-Fi", "Rock")
    for tone in (DAY_TONE, NIGHT_TONE):
        for source in genres:
            for target in genres:
                for variant in range(2):
                    _write_liner(
                        root,
                        language="en",
                        tone=tone,
                        source=source,
                        target=target,
                        variant=variant,
                    )


def test_library_validates_every_tone_pair_and_rotates_variants(tmp_path: Path) -> None:
    _complete_library(tmp_path)
    library = TransitionLinerLibrary(
        root=tmp_path,
        language="en",
        required_genres=("Lo-Fi", "Rock"),
        min_variants=2,
        seed=7,
    )

    snapshot = library.validate()
    morning = datetime(2026, 8, 30, 9, tzinfo=ZoneInfo("Europe/Istanbul"))
    first = library.choose("lofi", "rock", now=morning)
    second = library.choose("Lo-Fi", "Rock", now=morning)

    assert snapshot["ready"] == 16
    assert first.tone == DAY_TONE
    assert first.path.parent.name == "Lo-Fi-to-Rock"
    assert first.variant != second.variant


def test_library_uses_night_calm_for_overnight_transition(tmp_path: Path) -> None:
    _complete_library(tmp_path)
    library = TransitionLinerLibrary(
        root=tmp_path,
        language="en",
        required_genres=("Lo-Fi", "Rock"),
        min_variants=2,
    )
    library.validate()
    night = datetime(2026, 8, 30, 2, tzinfo=ZoneInfo("Europe/Istanbul"))

    selected = library.choose("Lo-Fi", "Rock", now=night)

    assert selected.tone == NIGHT_TONE
    assert tone_for_time(night) == NIGHT_TONE
    assert canonical_genre("hip hop") == "Hip-Hop"


def test_library_fails_closed_when_one_transition_is_missing(tmp_path: Path) -> None:
    _complete_library(tmp_path)
    missing = tmp_path / "en" / NIGHT_TONE / "Lo-Fi-to-Rock" / "liner-001.wav"
    missing.unlink()
    library = TransitionLinerLibrary(
        root=tmp_path,
        language="en",
        required_genres=("Lo-Fi", "Rock"),
        min_variants=2,
    )

    with pytest.raises(RuntimeError, match="library is incomplete"):
        library.validate()


def test_library_rejects_sidecar_text_for_the_wrong_target_genre(tmp_path: Path) -> None:
    _complete_library(tmp_path)
    sidecar = tmp_path / "en" / DAY_TONE / "Lo-Fi-to-Rock" / "liner-000.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["text"] = "Radio TED U takes that Lo-Fi haze into smooth jazz."
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    library = TransitionLinerLibrary(
        root=tmp_path,
        language="en",
        required_genres=("Lo-Fi", "Rock"),
        min_variants=2,
    )

    with pytest.raises(RuntimeError, match="target genre Rock"):
        library.validate()


def test_library_accepts_only_explicitly_declared_french_night_fallback(
    tmp_path: Path,
) -> None:
    genres = ("Lo-Fi", "Rock")
    for tone in (DAY_TONE, NIGHT_TONE):
        for source in genres:
            for target in genres:
                for variant in range(2):
                    _write_liner(
                        tmp_path,
                        language="fr",
                        tone=tone,
                        source=source,
                        target=target,
                        variant=variant,
                    )
    sidecar = tmp_path / "fr" / NIGHT_TONE / "Lo-Fi-to-Rock" / "liner-000.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload.update(
        {
            "design": "fr-day",
            "source_tone": DAY_TONE,
            "fallback_reason": "missing-fr-night-variant-in-downloaded-archive",
        }
    )
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    library = TransitionLinerLibrary(
        root=tmp_path,
        language="fr",
        required_genres=genres,
        min_variants=2,
    )
    assert library.validate()["ready"] == 16
