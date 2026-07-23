from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    from mutagen import File as MutagenFile
except Exception:  # pragma: no cover - exercised when mutagen is absent
    MutagenFile = None

from .config import Settings
from .database import connect, init_db, log_event, now_iso
from .audio.catalog_analyzer import analyze_audio
from .audio.models import AudioAnalysisError, AudioAnalyzerUnavailableError
from .stations.context import StationContext


AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg"}
NON_MUSIC_FILENAME_MARKERS = ("vlog review",)


@dataclass
class ScanResult:
    tracks_found: int
    tracks_indexed: int
    music_dir: str


def iter_audio_files(root: Path, limit: int | None = None) -> Iterator[Path]:
    root = root.resolve()
    if not root.is_dir():
        return
    count = 0
    stack = [root]
    visited = {root}
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            continue
        for entry in entries:
            try:
                resolved_entry = entry.resolve()
            except OSError:
                continue
            if not _is_within_root(resolved_entry, root):
                continue
            if entry.is_dir():
                if resolved_entry not in visited:
                    visited.add(resolved_entry)
                    stack.append(resolved_entry)
                continue
            if entry.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            if any(marker in entry.stem.casefold() for marker in NON_MUSIC_FILENAME_MARKERS):
                continue
            if not entry.is_file():
                continue
            yield resolved_entry
            count += 1
            if limit is not None and count >= limit:
                return


def infer_title_artist(path: Path) -> tuple[str, str]:
    stem = path.stem.strip()
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return title.strip() or stem, artist.strip() or "Unknown Artist"
    return stem or path.name, "Unknown Artist"


def _first_tag(audio, names: tuple[str, ...]) -> str | None:
    if not audio or not getattr(audio, "tags", None):
        return None
    for name in names:
        value = audio.tags.get(name)
        if isinstance(value, list) and value:
            return str(value[0])
        if value:
            return str(value)
    return None


def _duration_with_wave(path: Path) -> float | None:
    if path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            return round(frames / float(rate), 2) if rate else None
    except Exception:
        return None


def read_metadata(path: Path) -> dict:
    inferred_title, inferred_artist = infer_title_artist(path)
    audio = None
    if MutagenFile is not None:
        try:
            audio = MutagenFile(str(path), easy=True)
        except Exception:
            audio = None
    duration = None
    if audio is not None and getattr(audio, "info", None) is not None:
        duration = round(float(getattr(audio.info, "length", 0) or 0), 2) or None
    if duration is None:
        duration = _duration_with_wave(path)
    return {
        "title": _first_tag(audio, ("title",)) or inferred_title,
        "artist": _first_tag(audio, ("artist", "albumartist")) or inferred_artist,
        "album": _first_tag(audio, ("album",)),
        "genre": _first_tag(audio, ("genre",)),
        "mood": _first_tag(audio, ("mood",)),
        "bpm": None,
        "duration_seconds": duration,
        "file_path": str(path.resolve()),
    }


def scan_music(runtime: Settings | StationContext) -> ScanResult:
    """Persist only analyzer-validated assets beneath the configured music root."""

    music_root = _music_root(runtime)
    init_db(runtime)
    found = 0
    indexed = 0
    timestamp = now_iso()
    with connect(runtime) as conn:
        for audio_path in iter_audio_files(music_root):
            found += 1
            try:
                analysis = analyze_audio(audio_path)
            except AudioAnalyzerUnavailableError:
                continue
            except AudioAnalysisError:
                conn.execute("delete from tracks where file_path = ?", (str(audio_path),))
                continue
            metadata = read_metadata(audio_path)
            try:
                relative_parts = audio_path.resolve().relative_to(music_root.resolve()).parts
            except (OSError, ValueError):
                relative_parts = ()
            if relative_parts and relative_parts[0].casefold() == "rock":
                metadata["genre"] = "rock"
            metadata["duration_seconds"] = analysis.duration_seconds
            metadata["file_path"] = str(analysis.source_path)
            conn.execute(
                """
                insert into tracks (
                    title, artist, album, genre, mood, bpm, duration_seconds, file_path,
                    cover_path, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(file_path) do update set
                    title=excluded.title,
                    artist=excluded.artist,
                    album=excluded.album,
                    genre=excluded.genre,
                    mood=excluded.mood,
                    bpm=excluded.bpm,
                    duration_seconds=excluded.duration_seconds,
                    updated_at=excluded.updated_at
                """,
                (
                    metadata["title"],
                    metadata["artist"],
                    metadata["album"],
                    metadata["genre"],
                    metadata["mood"],
                    metadata["bpm"],
                    metadata["duration_seconds"],
                    metadata["file_path"],
                    None,
                    timestamp,
                    timestamp,
                ),
            )
            indexed += 1
            if indexed % 250 == 0:
                conn.commit()
        _remove_missing_station_tracks(conn, music_root)
        _remove_ineligible_station_tracks(conn, music_root)
        log_event(conn, "info", f"Music scan complete: {found} tracks found.", {"music_dir": str(music_root)})
        if found == 0:
            log_event(conn, "info", "Radio loop not started because no playable tracks exist.")
        conn.commit()
    return ScanResult(tracks_found=found, tracks_indexed=indexed, music_dir=str(music_root))


def _music_root(runtime: Settings | StationContext) -> Path:
    if isinstance(runtime, StationContext):
        return runtime.music_root
    return Path(runtime.music_dir)


def _remove_missing_station_tracks(conn, music_root: Path) -> None:
    resolved_root = music_root.resolve()
    if not resolved_root.is_dir():
        return
    rows = conn.execute("select file_path from tracks").fetchall()
    for (file_path,) in rows:
        candidate = Path(file_path).resolve()
        if _is_within_root(candidate, resolved_root) and not candidate.is_file():
            conn.execute("delete from tracks where file_path = ?", (str(candidate),))


def _remove_ineligible_station_tracks(conn, music_root: Path) -> None:
    resolved_root = music_root.resolve()
    rows = conn.execute("select file_path from tracks").fetchall()
    for (file_path,) in rows:
        candidate = Path(file_path).resolve()
        if _is_within_root(candidate, resolved_root) and any(
            marker in candidate.stem.casefold() for marker in NON_MUSIC_FILENAME_MARKERS
        ):
            conn.execute("delete from tracks where file_path = ?", (str(candidate),))


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True
