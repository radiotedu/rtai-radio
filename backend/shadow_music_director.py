"""Read-only, deterministic shadow music director for RadioTEDU."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


ALGORITHM_VERSION = "radiotedu-shadow-director-v1"
TRACK_REPEAT_HOURS = 12
ARTIST_REPEAT_MINUTES = 90
UNKNOWN_ARTISTS = {"", "unknown", "unknown artist", "various artists"}


def identity(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class DirectorTrack:
    track_id: int
    title: str
    artist: str
    genre: str | None
    mood: str | None
    bpm: int | None
    duration_seconds: float
    file_path: str
    metadata_status: str | None = None

    @property
    def title_key(self) -> str:
        return identity(self.title)

    @property
    def artist_key(self) -> str:
        return identity(self.artist)

    @property
    def known_artist(self) -> bool:
        return self.artist_key not in UNKNOWN_ARTISTS and not self.artist_key.isdigit()


@dataclass(frozen=True, slots=True)
class RecentPlay:
    title: str
    artist: str
    played_at: datetime

    @property
    def title_key(self) -> str:
        return identity(self.title)

    @property
    def artist_key(self) -> str:
        return identity(self.artist)


@dataclass(frozen=True, slots=True)
class Recommendation:
    position: int
    track_id: int
    title: str
    artist: str
    genre: str | None
    bpm: int | None
    score: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


PROGRAM_WEIGHTS = {
    "morning": {
        "pop": 1.5,
        "rock": 1.15,
        "jazz": 0.8,
        "classical": 0.65,
        "other": 1.0,
        "unknown": 1.0,
    },
    "day": {
        "pop": 1.35,
        "rock": 1.3,
        "jazz": 1.0,
        "classical": 0.75,
        "other": 1.05,
        "unknown": 1.0,
    },
    "evening": {
        "pop": 0.85,
        "rock": 1.0,
        "jazz": 1.55,
        "classical": 1.0,
        "other": 1.1,
        "unknown": 1.0,
    },
    "night": {
        "pop": 0.8,
        "rock": 0.8,
        "jazz": 1.45,
        "classical": 1.5,
        "other": 1.15,
        "unknown": 1.0,
    },
    "weekend": {
        "pop": 1.15,
        "rock": 1.2,
        "jazz": 1.1,
        "classical": 0.9,
        "other": 1.2,
        "unknown": 1.0,
    },
}


def program_profile(program: str) -> str:
    key = identity(program)
    if any(token in key for token in ("dawn", "aube")):
        return "morning"
    if any(token in key for token in ("jazz lab", "laboratoire jazz")):
        return "evening"
    if any(token in key for token in ("night", "nuit")):
        return "night"
    if "weekend" in key:
        return "weekend"
    return "day"


def deterministic_jitter(station_id: str, track_id: int, block: str, position: int) -> float:
    digest = hashlib.sha256(
        f"{ALGORITHM_VERSION}|{station_id}|{track_id}|{block}|{position}".encode()
    ).digest()
    return int.from_bytes(digest[:4], "big") / (2**32 - 1)


class ShadowMusicDirector:
    """Recommend a safe sequence without possessing any playout control."""

    def __init__(self, station_id: str) -> None:
        self.station_id = station_id

    def recommend(
        self,
        tracks: Iterable[DirectorTrack],
        recent_plays: Iterable[RecentPlay],
        *,
        program: str,
        now: datetime,
        current_title: str = "",
        current_artist: str = "",
        count: int = 6,
    ) -> list[Recommendation]:
        now = now.astimezone(timezone.utc)
        profile = program_profile(program)
        weights = PROGRAM_WEIGHTS[profile]
        recent = list(recent_plays)
        selected: list[Recommendation] = []
        selected_ids: set[int] = set()
        selected_artists: set[str] = set()
        current_title_key = identity(current_title)
        current_artist_key = identity(current_artist)
        block = now.strftime("%Y-%m-%dT%H")
        last_genre: str | None = None

        candidates = [
            track
            for track in tracks
            if track.track_id > 0
            and track.duration_seconds > 30
            and track.title_key
            and Path(track.file_path).is_file()
        ]
        for position in range(1, count + 1):
            scored: list[tuple[float, DirectorTrack, tuple[str, ...]]] = []
            for track in candidates:
                if track.track_id in selected_ids or track.title_key == current_title_key:
                    continue
                if track.known_artist and track.artist_key in selected_artists:
                    continue
                if self._recent_track(track, recent, now, TRACK_REPEAT_HOURS):
                    continue
                if track.known_artist and self._recent_artist(
                    track, recent, now, ARTIST_REPEAT_MINUTES
                ):
                    continue

                genre = identity(track.genre) or "unknown"
                score = 50.0
                reasons: list[str] = [
                    f"{TRACK_REPEAT_HOURS}h track separation",
                    f"{ARTIST_REPEAT_MINUTES}m artist separation",
                ]

                genre_weight = weights.get(genre, weights["unknown"])
                score += (genre_weight - 1.0) * 12
                if genre_weight > 1.1:
                    reasons.append(f"{profile} program fit")
                if last_genre and genre != "unknown" and genre != last_genre:
                    score += 3.5
                    reasons.append("genre variety")
                elif last_genre and genre == last_genre:
                    score -= 4

                if track.known_artist:
                    score += 4
                else:
                    score -= 5
                if track.metadata_status in {"accepted", "accepted_core"}:
                    score += 2.5
                    reasons.append("verified metadata")
                if 150 <= track.duration_seconds <= 330:
                    score += 2
                else:
                    score -= min(5.0, abs(track.duration_seconds - 240) / 120)

                bpm_score = self._bpm_score(profile, track.bpm)
                score += bpm_score
                if bpm_score >= 1:
                    reasons.append("daypart tempo fit")

                if track.artist_key == current_artist_key and track.known_artist:
                    score -= 20
                score += deterministic_jitter(
                    self.station_id, track.track_id, block, position
                ) * 3
                if math.isfinite(score):
                    scored.append((score, track, tuple(reasons[:4])))

            if not scored:
                break
            score, winner, reasons = max(
                scored,
                key=lambda item: (item[0], -item[1].track_id),
            )
            selected.append(
                Recommendation(
                    position=position,
                    track_id=winner.track_id,
                    title=winner.title,
                    artist=winner.artist,
                    genre=winner.genre,
                    bpm=winner.bpm,
                    score=round(score, 2),
                    reasons=reasons,
                )
            )
            selected_ids.add(winner.track_id)
            if winner.known_artist:
                selected_artists.add(winner.artist_key)
            last_genre = identity(winner.genre) or None
        return selected

    @staticmethod
    def _recent_track(
        track: DirectorTrack,
        recent: list[RecentPlay],
        now: datetime,
        hours: int,
    ) -> bool:
        threshold = now - timedelta(hours=hours)
        return any(
            play.played_at >= threshold and play.title_key == track.title_key
            for play in recent
        )

    @staticmethod
    def _recent_artist(
        track: DirectorTrack,
        recent: list[RecentPlay],
        now: datetime,
        minutes: int,
    ) -> bool:
        threshold = now - timedelta(minutes=minutes)
        return any(
            play.played_at >= threshold
            and play.artist_key == track.artist_key
            and play.artist_key not in UNKNOWN_ARTISTS
            for play in recent
        )

    @staticmethod
    def _bpm_score(profile: str, bpm: int | None) -> float:
        if not bpm or bpm < 40 or bpm > 240:
            return 0.0
        targets = {
            "morning": 105,
            "day": 118,
            "evening": 100,
            "night": 88,
            "weekend": 112,
        }
        difference = abs(bpm - targets[profile])
        return max(-3.0, 3.0 - difference / 12)


def status_payload(
    station_id: str,
    *,
    program: str,
    generated_at: datetime,
    recommendations: list[Recommendation],
    actual_next: list[dict],
    recent_play_count: int,
    track_count: int,
    current: dict,
) -> dict:
    return {
        "schema_version": 1,
        "algorithm_version": ALGORITHM_VERSION,
        "station_id": station_id,
        "state": "shadow",
        "read_only": True,
        "controls_live_playout": False,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "program": program,
        "program_profile": program_profile(program),
        "current": current,
        "actual_next": actual_next,
        "recommendations": [item.to_dict() for item in recommendations],
        "constraints": {
            "track_repeat_hours": TRACK_REPEAT_HOURS,
            "artist_repeat_minutes": ARTIST_REPEAT_MINUTES,
            "file_must_exist": True,
            "minimum_duration_seconds": 30,
            "generative_model_can_override": False,
        },
        "statistics": {
            "tracks_considered": track_count,
            "recent_plays_considered": recent_play_count,
            "recommendations_ready": len(recommendations),
        },
    }


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)
