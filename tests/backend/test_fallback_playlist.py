from __future__ import annotations

from pathlib import Path

from backend.config import Settings
from backend.database import connect, init_db, now_iso
from backend.fallback_playlist import FallbackPlaylistBuilder


def fallback_builder(
    tmp_path: Path,
    durations: list[float],
    *,
    include_missing: bool = False,
) -> FallbackPlaylistBuilder:
    settings = Settings(
        database_path=str(tmp_path / "data" / "radio.db"),
        music_dir=str(tmp_path / "media" / "stations" / "radiotedu-en" / "music"),
        static_dir=str(tmp_path / "data" / "public"),
        liquidsoap_queue_path=str(tmp_path / "data" / "liquidsoap" / "queue.m3u"),
        station_id="radiotedu-en",
    )
    init_db(settings)
    timestamp = now_iso()
    with connect(settings) as conn:
        for index, duration in enumerate(durations):
            path = settings.music_path / f"fallback-{index}.aac"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"station-local-audio")
            conn.execute(
                """
                insert into tracks (
                    title, artist, genre, duration_seconds, file_path,
                    play_count, created_at, updated_at
                ) values (?, 'RadioTEDU', 'pop', ?, ?, 0, ?, ?)
                """,
                (f"Fallback {index}", duration, str(path), timestamp, timestamp),
            )
        if include_missing:
            conn.execute(
                """
                insert into tracks (
                    title, artist, genre, duration_seconds, file_path,
                    play_count, created_at, updated_at
                ) values ('Missing', 'RadioTEDU', 'pop', 3600, ?, 0, ?, ?)
                """,
                (str(settings.music_path / "missing.aac"), timestamp, timestamp),
            )
        foreign = tmp_path / "media" / "stations" / "radiotedu-fr" / "music" / "foreign.aac"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_bytes(b"foreign-station-audio")
        conn.execute(
            """
            insert into tracks (
                title, artist, genre, duration_seconds, file_path,
                play_count, created_at, updated_at
            ) values ('Foreign', 'RadioTEDU', 'pop', 3600, ?, 0, ?, ?)
            """,
            (str(foreign), timestamp, timestamp),
        )
        conn.commit()
    return FallbackPlaylistBuilder(settings)


def add_track(builder: FallbackPlaylistBuilder, duration_seconds: float) -> None:
    timestamp = now_iso()
    path = builder.context.music_root / f"added-{timestamp.replace(':', '-')}.aac"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"additional-station-audio")
    with connect(builder.database_runtime) as conn:
        conn.execute(
            """
            insert into tracks (
                title, artist, genre, duration_seconds, file_path,
                play_count, created_at, updated_at
            ) values ('Added', 'RadioTEDU', 'pop', ?, ?, 0, ?, ?)
            """,
            (duration_seconds, str(path), timestamp, timestamp),
        )
        conn.commit()


def test_fallback_requires_six_hours_of_valid_local_audio(tmp_path: Path):
    builder = fallback_builder(tmp_path, durations=[3600] * 5)

    short = builder.rebuild()

    assert short.coverage_seconds == 5 * 3600
    assert short.air_ready is False
    add_track(builder, duration_seconds=3600)
    ready = builder.rebuild()
    assert ready.coverage_seconds >= 6 * 3600
    assert ready.air_ready is True


def test_fallback_contains_only_existing_station_local_files(tmp_path: Path):
    builder = fallback_builder(tmp_path, durations=[3600] * 6, include_missing=True)

    status = builder.rebuild()
    lines = status.playlist_path.read_text(encoding="utf-8").splitlines()

    assert lines
    assert all(Path(line).is_file() for line in lines)
    assert all(Path(line).is_relative_to(builder.context.music_root) for line in lines)
    assert not any("radiotedu-fr" in line for line in lines)
    assert not status.playlist_path.with_suffix(".m3u.tmp").exists()


def test_status_revalidates_playlist_when_a_file_disappears(tmp_path: Path):
    builder = fallback_builder(tmp_path, durations=[3600] * 6)
    ready = builder.rebuild()
    Path(ready.playlist_path.read_text(encoding="utf-8").splitlines()[0]).unlink()

    rechecked = builder.status()

    assert rechecked.coverage_seconds == 5 * 3600
    assert rechecked.air_ready is False


def test_failed_rebuild_preserves_the_last_valid_six_hour_playlist(
    tmp_path: Path,
    monkeypatch,
):
    builder = fallback_builder(tmp_path, durations=[3600] * 6)
    ready = builder.rebuild()
    previous = ready.playlist_path.read_bytes()
    monkeypatch.setattr(builder, "_eligible_tracks", lambda: [])

    rebuilt = builder.rebuild()

    assert rebuilt.playlist_path.read_bytes() == previous
