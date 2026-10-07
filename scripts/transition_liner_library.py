from __future__ import annotations

import hashlib
import json
import re
import threading
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


DAY_TONE = "day-energetic"
NIGHT_TONE = "night-calm"
SUPPORTED_TONES = (DAY_TONE, NIGHT_TONE)

# Every accepted liner must describe both sides of its declared transition.
# Cues include the natural phrases used by the pre-generated EN/FR voices, so
# the validator catches a swapped or misfiled clip without forcing robotic
# repetition of the canonical genre label in every announcement.
GENRE_TEXT_CUES: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "Blues": ("blues",),
        "Classical": ("classical", "elegant strings"),
        "Electronic": ("electronic", "electro", "pulse"),
        "Folk": ("folk", "storytelling"),
        "Hip-Hop": ("hip-hop", "hip hop"),
        "Jazz": ("jazz", "swing"),
        "Lo-Fi": ("lo-fi", "lo fi", "chill, relaxed haze"),
        "Pop": ("pop",),
        "Rock": ("rock", "guitars"),
    },
    "fr": {
        "Blues": ("blues",),
        "Classical": ("classique", "cordes classiques"),
        "Electronic": ("électronique", "electronique", "électro", "electro"),
        "Folk": ("folk",),
        "Hip-Hop": ("hip-hop", "hip hop"),
        "Jazz": ("jazz", "swing"),
        "Lo-Fi": ("lo-fi", "lo fi"),
        "Pop": ("pop",),
        "Rock": ("rock", "guitares"),
    },
}


def canonical_genre(value: str | None) -> str:
    token = re.sub(r"[ _]+", "-", str(value or "").strip()).casefold()
    aliases = {
        "blues": "Blues",
        "classical": "Classical",
        "electronic": "Electronic",
        "folk": "Folk",
        "hip-hop": "Hip-Hop",
        "hiphop": "Hip-Hop",
        "jazz": "Jazz",
        "lo-fi": "Lo-Fi",
        "lofi": "Lo-Fi",
        "pop": "Pop",
        "rock": "Rock",
    }
    return aliases.get(token, "")


def tone_for_time(
    now: datetime | None = None,
    *,
    timezone_name: str = "Europe/Istanbul",
) -> str:
    zone = ZoneInfo(timezone_name)
    local = now.astimezone(zone) if now is not None else datetime.now(zone)
    return DAY_TONE if 6 <= local.hour < 18 else NIGHT_TONE


@dataclass(frozen=True)
class LinerSelection:
    path: Path
    language: str
    tone: str
    from_genre: str
    to_genre: str
    variant: int
    text: str


class TransitionLinerLibrary:
    """Validated, reusable day/night host audio for exact genre transitions."""

    def __init__(
        self,
        *,
        root: Path,
        language: str,
        required_genres: tuple[str, ...],
        min_variants: int = 5,
        timezone_name: str = "Europe/Istanbul",
        seed: int = 20260813,
    ) -> None:
        self.root = root.resolve()
        self.language = language.strip().casefold()
        self.required_genres = tuple(
            dict.fromkeys(canonical_genre(value) for value in required_genres)
        )
        self.min_variants = max(1, int(min_variants))
        self.timezone_name = timezone_name
        self.seed = int(seed)
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str, str], tuple[LinerSelection, ...]] = {}
        self._cursor: dict[tuple[str, str, str], int] = {}
        self._last: LinerSelection | None = None

        if self.language not in {"en", "fr"}:
            raise ValueError("transition liner language must be en or fr")
        if not self.required_genres or any(not value for value in self.required_genres):
            raise ValueError("transition liner genres must use supported canonical names")
        ZoneInfo(self.timezone_name)

    @staticmethod
    def _validate_wav(path: Path) -> None:
        if path.stat().st_size <= 1_024:
            raise RuntimeError(f"transition liner is empty or truncated: {path}")
        try:
            with wave.open(str(path), "rb") as handle:
                if (
                    handle.getnchannels() not in {1, 2}
                    or handle.getframerate() < 16_000
                    or handle.getnframes() <= 0
                    or handle.getsampwidth() not in {2, 3, 4}
                ):
                    raise RuntimeError(f"transition liner has invalid PCM format: {path}")
        except (EOFError, OSError, wave.Error) as exc:
            raise RuntimeError(f"transition liner is unreadable: {path}: {exc}") from exc

    def _read_selection(
        self,
        path: Path,
        *,
        tone: str,
        from_genre: str,
        to_genre: str,
    ) -> LinerSelection:
        self._validate_wav(path)
        sidecar = path.with_suffix(".json")
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8-sig"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"transition liner sidecar is invalid: {sidecar}") from exc
        expected = {
            "lang": self.language,
            "tone": tone,
            "from": from_genre,
            "to": to_genre,
        }
        for field, value in expected.items():
            if str(payload.get(field) or "").strip() != value:
                raise RuntimeError(
                    f"transition liner sidecar mismatch for {field}: {sidecar}"
                )
        expected_design = f"{self.language}-{'day' if tone == DAY_TONE else 'night'}"
        actual_design = str(payload.get("design") or "").strip()
        declared_fr_night_fallback = (
            self.language == "fr"
            and tone == NIGHT_TONE
            and actual_design == "fr-day"
            and str(payload.get("source_tone") or "").strip() == DAY_TONE
            and str(payload.get("fallback_reason") or "").strip()
            == "missing-fr-night-variant-in-downloaded-archive"
        )
        if actual_design != expected_design and not declared_fr_night_fallback:
            raise RuntimeError(
                f"transition liner sidecar mismatch for design: {sidecar}"
            )
        text = re.sub(r"\s+", " ", str(payload.get("text") or "")).strip()
        if not text or "radio ted u" not in text.casefold():
            raise RuntimeError(f"transition liner text is not station-grounded: {sidecar}")
        folded_text = text.casefold()
        for role, genre in (("source", from_genre), ("target", to_genre)):
            cues = GENRE_TEXT_CUES[self.language][genre]
            if not any(cue.casefold() in folded_text for cue in cues):
                raise RuntimeError(
                    f"transition liner text does not identify {role} genre {genre}: "
                    f"{sidecar}"
                )
        try:
            variant = int(payload.get("idx"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"transition liner variant is invalid: {sidecar}") from exc
        match = re.fullmatch(r"liner-(\d+)\.wav", path.name, flags=re.IGNORECASE)
        if match is None or int(match.group(1)) != variant:
            raise RuntimeError(f"transition liner filename/variant mismatch: {sidecar}")
        return LinerSelection(
            path=path.resolve(),
            language=self.language,
            tone=tone,
            from_genre=from_genre,
            to_genre=to_genre,
            variant=variant,
            text=text,
        )

    def validate(self) -> dict[str, object]:
        language_root = self.root / self.language
        missing: list[str] = []
        loaded: dict[tuple[str, str, str], tuple[LinerSelection, ...]] = {}
        for tone in SUPPORTED_TONES:
            for from_genre in self.required_genres:
                for to_genre in self.required_genres:
                    pair = f"{from_genre}-to-{to_genre}"
                    pair_root = language_root / tone / pair
                    selections: list[LinerSelection] = []
                    if pair_root.is_dir():
                        for path in sorted(
                            pair_root.glob("liner-*.wav"),
                            key=lambda item: item.name.casefold(),
                        ):
                            selections.append(
                                self._read_selection(
                                    path,
                                    tone=tone,
                                    from_genre=from_genre,
                                    to_genre=to_genre,
                                )
                            )
                    if len(selections) < self.min_variants:
                        missing.append(
                            f"{self.language}/{tone}/{pair} "
                            f"({len(selections)}/{self.min_variants})"
                        )
                    loaded[(tone, from_genre, to_genre)] = tuple(selections)
        if missing:
            preview = "; ".join(missing[:12])
            suffix = f"; plus {len(missing) - 12} more" if len(missing) > 12 else ""
            raise RuntimeError(f"transition liner library is incomplete: {preview}{suffix}")
        with self._lock:
            self._items = loaded
            self._cursor.clear()
            self._last = None
        return self.snapshot()

    def _selection_index(
        self,
        key: tuple[str, str, str],
        count: int,
    ) -> int:
        cursor = self._cursor.get(key, 0)
        digest = hashlib.sha256(
            f"{self.seed}|{self.language}|{'|'.join(key)}".encode("utf-8")
        ).digest()
        start = int.from_bytes(digest[:8], "big") % count
        self._cursor[key] = cursor + 1
        return (start + cursor) % count

    def choose(
        self,
        from_genre: str | None,
        to_genre: str | None,
        *,
        now: datetime | None = None,
    ) -> LinerSelection:
        source = canonical_genre(from_genre)
        target = canonical_genre(to_genre)
        if not source or not target:
            raise RuntimeError(
                f"cannot choose transition liner for genres {from_genre!r} -> {to_genre!r}"
            )
        tone = tone_for_time(now, timezone_name=self.timezone_name)
        key = (tone, source, target)
        with self._lock:
            candidates = self._items.get(key, ())
            if not candidates:
                raise RuntimeError(
                    f"no validated transition liner for {self.language}/{tone}/"
                    f"{source}-to-{target}"
                )
            selected = candidates[self._selection_index(key, len(candidates))]
            self._last = selected
            return selected

    def describe_transition(
        self,
        from_genre: str | None,
        to_genre: str | None,
        *,
        now: datetime | None = None,
    ) -> dict[str, object]:
        source = canonical_genre(from_genre)
        target = canonical_genre(to_genre)
        tone = tone_for_time(now, timezone_name=self.timezone_name)
        candidates = self._items.get((tone, source, target), ())
        return {
            "from_genre": source or None,
            "to_genre": target or None,
            "tone": tone,
            "ready_variants": len(candidates),
        }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            ready = sum(len(items) for items in self._items.values())
            expected = (
                len(SUPPORTED_TONES)
                * len(self.required_genres)
                * len(self.required_genres)
                * self.min_variants
            )
            last = self._last
            return {
                "mode": "prebaked-transition-host",
                "language": self.language,
                "root": str(self.root),
                "ready": ready,
                "expected": expected,
                "pairs": len(self._items),
                "genres": list(self.required_genres),
                "min_variants": self.min_variants,
                "timezone": self.timezone_name,
                "current_tone": tone_for_time(timezone_name=self.timezone_name),
                "ollama_required": False,
                "fallback_tts": False,
                "last_selection": (
                    {
                        "tone": last.tone,
                        "from_genre": last.from_genre,
                        "to_genre": last.to_genre,
                        "variant": last.variant,
                        "file": last.path.name,
                    }
                    if last is not None
                    else None
                ),
            }
