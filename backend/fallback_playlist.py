from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .database import connect
from .stations.context import StationContext, coerce_station_context


@dataclass(frozen=True, slots=True)
class FallbackStatus:
    playlist_path: Path
    track_count: int
    coverage_seconds: int
    required_seconds: int
    air_ready: bool


class FallbackPlaylistBuilder:
    def __init__(
        self,
        runtime: Settings | StationContext,
        *,
        playlist_path: Path | None = None,
        required_seconds: int | None = None,
    ) -> None:
        self.context = coerce_station_context(runtime)
        self.settings = self.context.settings
        self._database_runtime: Settings | StationContext = (
            self.context if isinstance(runtime, StationContext) else self.settings
        )
        self.station_id = self.context.profile.station_id
        self.playlist_path = (
            Path(playlist_path)
            if playlist_path is not None
            else Path(self.settings.liquidsoap_queue_path).with_name("fallback.m3u")
        )
        self.required_seconds = int(
            self.settings.fallback_coverage_seconds
            if required_seconds is None
            else required_seconds
        )
        if self.required_seconds <= 0:
            raise ValueError("fallback coverage requirement must be positive")

    @property
    def database_runtime(self) -> Settings | StationContext:
        return self._database_runtime

    def rebuild(self) -> FallbackStatus:
        had_playlist = self.playlist_path.is_file()
        selected: list[Path] = []
        coverage = 0
        for track in self._eligible_tracks():
            selected.append(track["resolved_path"])
            coverage += int(float(track["duration_seconds"]))
            if coverage >= self.required_seconds:
                break
        if coverage < self.required_seconds and had_playlist:
            return self.status()
        self.playlist_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.playlist_path.with_suffix(".m3u.tmp")
        temporary.write_text(
            "".join(f"{path.as_posix()}\n" for path in selected),
            encoding="utf-8",
        )
        temporary.replace(self.playlist_path)
        return self._status(len(selected), coverage)

    def status(self) -> FallbackStatus:
        if not self.playlist_path.is_file():
            return self._status(0, 0)
        eligible = {
            str(track["resolved_path"]): int(float(track["duration_seconds"]))
            for track in self._eligible_tracks()
        }
        selected: set[str] = set()
        coverage = 0
        for raw_line in self.playlist_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            resolved = str(Path(line).expanduser().resolve())
            if resolved in selected or resolved not in eligible or not Path(resolved).is_file():
                continue
            selected.add(resolved)
            coverage += eligible[resolved]
        return self._status(len(selected), coverage)

    def _eligible_tracks(self) -> list[dict]:
        database_file = self.context.database_file
        if not database_file.is_file():
            return []
        try:
            with connect(self._database_runtime) as conn:
                rows = conn.execute(
                    """
                    select id, title, artist, genre, duration_seconds, file_path
                    from tracks
                    where duration_seconds is not null and duration_seconds > 0
                    order by coalesce(last_played_at, ''), play_count, title, id
                    """
                ).fetchall()
        except sqlite3.Error:
            return []
        music_root = self.context.music_root
        tracks: list[dict] = []
        seen: set[Path] = set()
        for row in rows:
            path = Path(str(row["file_path"] or "")).expanduser().resolve()
            if path in seen or not path.is_file() or not path.is_relative_to(music_root):
                continue
            seen.add(path)
            tracks.append({**dict(row), "resolved_path": path})
        return tracks

    def _status(self, track_count: int, coverage_seconds: int) -> FallbackStatus:
        return FallbackStatus(
            playlist_path=self.playlist_path,
            track_count=track_count,
            coverage_seconds=coverage_seconds,
            required_seconds=self.required_seconds,
            air_ready=coverage_seconds >= self.required_seconds,
        )
