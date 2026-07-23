from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.config import Settings
from backend.database import connect, init_db, now_iso
from backend.rundown import CoveragePolicy, RundownPlanner
from backend.scheduler import current_program


NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
MONDAY = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)


def seeded_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        database_path=str(tmp_path / "radio.db"),
        music_dir=str(tmp_path / "music"),
        static_dir=str(tmp_path / "static"),
        playback_backend="simulate",
    )
    init_db(settings)
    return settings


def insert_tracks(settings: Settings, durations: list[float]) -> None:
    timestamp = now_iso()
    with connect(settings) as conn:
        for index, duration in enumerate(durations):
            path = settings.music_path / f"Artist {index} - Track {index}.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"RIFF-ready")
            conn.execute(
                """
                insert into tracks (
                    title, artist, album, genre, mood, bpm, duration_seconds,
                    file_path, cover_path, last_played_at, play_count, created_at, updated_at
                ) values (?, ?, null, 'pop', 'bright', 110, ?, ?, null, null, 0, ?, ?)
                """,
                (f"Track {index}", f"Artist {index}", duration, str(path), timestamp, timestamp),
            )
        conn.commit()


def seeded_planner(tmp_path: Path, track_durations: list[float]) -> RundownPlanner:
    settings = seeded_settings(tmp_path)
    insert_tracks(settings, track_durations)
    return RundownPlanner(settings, fallback_seconds_provider=lambda: 21_600)


def test_coverage_policy_uses_exact_approved_windows():
    policy = CoveragePolicy()
    assert policy.planned_seconds == 4 * 60 * 60
    assert policy.rendered_seconds == 60 * 60
    assert policy.refill_seconds == 2 * 60 * 60
    assert policy.fallback_seconds == 6 * 60 * 60


def test_settings_load_exact_coverage_environment_names(tmp_path: Path):
    env_file = tmp_path / "station.env"
    env_file.write_text(
        "\n".join(
            (
                "RUNDOWN_PLANNED_SECONDS=14401",
                "RUNDOWN_RENDERED_SECONDS=3601",
                "RUNDOWN_REFILL_SECONDS=7201",
                "FALLBACK_COVERAGE_SECONDS=21601",
                "JINGLE_ENABLED=true",
                "JINGLE_INTERVAL_TRACKS=4",
                "IMAGING_RELEASE_ROOT=staged-imaging",
            )
        ),
        encoding="utf-8",
    )

    settings = Settings.from_env(env_file)

    assert settings.rundown_planned_seconds == 14_401
    assert settings.rundown_rendered_seconds == 3_601
    assert settings.rundown_refill_seconds == 7_201
    assert settings.fallback_coverage_seconds == 21_601
    assert settings.jingle_enabled is True
    assert settings.jingle_interval_tracks == 4
    assert settings.imaging_release_root == "staged-imaging"


def test_planner_builds_four_hours_and_refills_below_two(tmp_path: Path):
    planner = seeded_planner(tmp_path, track_durations=[1800] * 10)

    first = planner.maintain(NOW)

    assert first.planned_seconds >= 14_400
    assert first.rendered_seconds >= 3_600
    assert first.needs_refill is False
    assert first.air_ready is True
    with connect(planner.database_runtime) as conn:
        rows = conn.execute(
            "select id from rundown_items where state in ('planned', 'ready') order by planned_start"
        ).fetchall()
        for row in rows[:6]:
            conn.execute(
                "update rundown_items set state='completed', actual_end=?, updated_at=? where id=?",
                (NOW.isoformat(), NOW.isoformat(), row["id"]),
            )
        conn.commit()

    depleted = planner.coverage(NOW)

    assert depleted.planned_seconds < 7_200
    assert depleted.needs_refill is True
    refilled = planner.maintain(NOW)
    assert refilled.planned_seconds >= 14_400
    assert refilled.needs_refill is False


def test_planner_does_not_refill_while_more_than_two_hours_remain(tmp_path: Path):
    planner = seeded_planner(tmp_path, track_durations=[1800] * 10)
    planner.maintain(NOW)
    with connect(planner.database_runtime) as conn:
        rows = conn.execute(
            "select id from rundown_items where state in ('planned', 'ready') order by planned_start"
        ).fetchall()
        initial_count = len(rows)
        for row in rows[:2]:
            conn.execute(
                "update rundown_items set state='completed', actual_end=?, updated_at=? where id=?",
                (NOW.isoformat(), NOW.isoformat(), row["id"]),
            )
        conn.commit()

    before = planner.coverage(NOW)
    maintained = planner.maintain(NOW)
    with connect(planner.database_runtime) as conn:
        active_count = conn.execute(
            "select count(*) from rundown_items where state in ('planned', 'ready', 'queued', 'playing')"
        ).fetchone()[0]

    assert before.planned_seconds >= 2 * 60 * 60
    assert maintained.planned_seconds == before.planned_seconds
    assert active_count == initial_count - 2


def test_planner_promotes_earliest_planned_rows_before_claiming_tail(tmp_path: Path):
    planner = seeded_planner(tmp_path, track_durations=[1800] * 10)
    planner.maintain(NOW)
    with connect(planner.database_runtime) as conn:
        ready = conn.execute(
            "select id from rundown_items where state='ready' order by planned_start, id"
        ).fetchall()
        for row in ready:
            conn.execute(
                "update rundown_items set state='completed', actual_end=?, updated_at=? where id=?",
                (NOW.isoformat(), NOW.isoformat(), row["id"]),
            )
        earliest_planned = conn.execute(
            "select id from rundown_items where state='planned' order by planned_start, id limit 1"
        ).fetchone()[0]
        conn.commit()

    planner.maintain(NOW)
    claimed = planner.claim_next_ready(NOW)

    assert claimed is not None
    assert claimed.id == earliest_planned


def test_planner_targets_three_pop_compatible_tracks_per_rotation_block(tmp_path: Path):
    settings = seeded_settings(tmp_path)
    insert_tracks(settings, [1800] * 8)
    with connect(settings) as conn:
        conn.execute("update tracks set genre='jazz' where id % 2 = 0")
        conn.commit()
    planner = RundownPlanner(settings, fallback_seconds_provider=lambda: 21_600)

    planner.maintain(NOW)

    with connect(settings) as conn:
        genres = [
            row["genre"]
            for row in conn.execute(
                """
                select tracks.genre from rundown_items
                join tracks on tracks.id = rundown_items.track_id
                order by rundown_items.planned_start, rundown_items.id
                """
            ).fetchall()
        ]
    compatible = sum(genre == "pop" for genre in genres)
    ratio = compatible / len(genres)
    assert 0.70 <= ratio <= 0.80

    with connect(settings) as conn:
        completed_ids = [
            row["id"]
            for row in conn.execute(
                "select id from rundown_items order by planned_start, id limit 6"
            ).fetchall()
        ]
        for item_id in completed_ids:
            conn.execute(
                "update rundown_items set state='completed', actual_end=?, updated_at=? where id=?",
                (NOW.isoformat(), NOW.isoformat(), item_id),
            )
        conn.commit()

    planner.maintain(NOW)

    with connect(settings) as conn:
        active_genres = [
            row["genre"]
            for row in conn.execute(
                """
                select tracks.genre from rundown_items
                join tracks on tracks.id = rundown_items.track_id
                where rundown_items.state in ('planned', 'ready', 'queued', 'playing')
                order by rundown_items.planned_start, rundown_items.id
                """
            ).fetchall()
        ]
    active_ratio = sum(genre == "pop" for genre in active_genres) / len(active_genres)
    assert 0.70 <= active_ratio <= 0.80


def test_claim_next_ready_is_transactional_and_does_not_repeat(tmp_path: Path):
    planner = seeded_planner(tmp_path, track_durations=[1800] * 10)
    planner.maintain(NOW)

    first = planner.claim_next_ready(NOW)
    second = planner.claim_next_ready(NOW)

    assert first is not None and second is not None
    assert first.id != second.id
    assert first.state == "queued"
    assert second.state == "queued"


def test_maintenance_never_expires_an_item_that_is_still_playing(tmp_path: Path):
    planner = seeded_planner(tmp_path, track_durations=[1800] * 10)
    planner.maintain(NOW)
    item = planner.claim_next_ready(NOW)
    assert item is not None
    planner.mark_playing(
        item.id,
        actual_start=NOW,
        expected_duration_seconds=1800,
        metadata={"track_seconds": 1800, "transition": "none"},
    )
    with connect(planner.database_runtime) as conn:
        conn.execute(
            "update rundown_items set planned_end=? where id=?",
            ((NOW + timedelta(seconds=1)).isoformat(), item.id),
        )
        conn.commit()

    planner.maintain(NOW + timedelta(seconds=2))

    with connect(planner.database_runtime) as conn:
        state = conn.execute(
            "select state from rundown_items where id=?", (item.id,)
        ).fetchone()[0]
    assert state == "playing"


def test_micro_clips_are_not_planned_as_music_tracks(tmp_path: Path):
    planner = seeded_planner(tmp_path, track_durations=[0.1])

    status = planner.maintain(NOW)

    assert status.planned_seconds == 0
    with connect(planner.database_runtime) as conn:
        assert conn.execute("select count(*) from rundown_items").fetchone()[0] == 0


def test_every_week_hour_has_a_named_or_overnight_program(tmp_path: Path):
    settings = seeded_settings(tmp_path)
    assert current_program(settings, MONDAY)["id"] == "overnight_signal"
    assert current_program(settings, MONDAY + timedelta(days=5))["id"] == "weekend_overnight"
    for day_offset in range(7):
        for hour in range(24):
            program = current_program(
                settings,
                MONDAY + timedelta(days=day_offset, hours=hour),
            )
            assert program["id"]
            assert program["name"]
