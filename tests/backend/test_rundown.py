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
            )
        ),
        encoding="utf-8",
    )

    settings = Settings.from_env(env_file)

    assert settings.rundown_planned_seconds == 14_401
    assert settings.rundown_rendered_seconds == 3_601
    assert settings.rundown_refill_seconds == 7_201
    assert settings.fallback_coverage_seconds == 21_601


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
        for row in rows[:5]:
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


def test_claim_next_ready_is_transactional_and_does_not_repeat(tmp_path: Path):
    planner = seeded_planner(tmp_path, track_durations=[1800] * 10)
    planner.maintain(NOW)

    first = planner.claim_next_ready(NOW)
    second = planner.claim_next_ready(NOW)

    assert first is not None and second is not None
    assert first.id != second.id
    assert first.state == "queued"
    assert second.state == "queued"


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

