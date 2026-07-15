from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import backend.database as database
from backend.config import Settings
from backend.stations.context import StationContext, build_station_context
from backend.stations.loader import load_station_profiles


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=str(tmp_path / "radiotedu.db"),
        music_dir=str(tmp_path / "music"),
        static_dir=str(tmp_path / "static"),
        rss_feeds_path=str(tmp_path / "rss_feeds.json"),
        playback_backend="simulate",
    )


def station_context(tmp_path: Path) -> StationContext:
    station_id = "radiotedu-en"
    profile = load_station_profiles(PROJECT_ROOT / "config" / "stations")[station_id]
    data_root = tmp_path / "data" / "stations" / station_id
    runtime = replace(
        profile.runtime,
        data_root=str(data_root),
        database=str(data_root / "radio.db"),
        music_root=str(tmp_path / "media" / "stations" / station_id / "music"),
        announcement_root=str(data_root / "announcements"),
        cache_root=str(data_root / "cache"),
        log_root=str(data_root / "logs"),
    )
    return build_station_context(
        Settings(static_dir=str(data_root / "public")),
        replace(profile, runtime=runtime),
    )


def migration_api():
    return database.Migration, database.MigrationError, database.apply_migrations


def test_initial_schema_migration_is_recorded_before_station_seeding(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    database.init_db(settings)

    with database.connect(settings) as conn:
        checkpoint = conn.execute(
            "select version, name, checksum, applied_at from schema_migrations order by version"
        ).fetchall()
        channel_count = conn.execute("select count(*) from channels").fetchone()[0]

    assert checkpoint
    assert checkpoint[0][0:2] == (1, "initial_station_schema")
    assert len(checkpoint[0][2]) == 64
    assert checkpoint[0][3]
    assert channel_count == 1


def test_repeating_startup_does_not_reapply_initial_migration(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database.init_db(settings)
    with database.connect(settings) as conn:
        first_checkpoint = conn.execute(
            "select version, name, checksum, applied_at from schema_migrations"
        ).fetchall()

    database.init_db(settings)

    with database.connect(settings) as conn:
        repeated_checkpoint = conn.execute(
            "select version, name, checksum, applied_at from schema_migrations"
        ).fetchall()
        program_count = conn.execute("select count(*) from programs").fetchone()[0]

    assert repeated_checkpoint == first_checkpoint
    assert program_count == 6


def test_editorial_fact_card_migration_is_versioned_and_constrained(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database.init_db(settings)

    with database.connect(settings) as conn:
        checkpoint = conn.execute(
            "select name from schema_migrations where version=7"
        ).fetchone()
        columns = {
            row["name"] for row in conn.execute("pragma table_info(editorial_fact_cards)")
        }
        indexes = {
            row["name"] for row in conn.execute("pragma index_list(editorial_fact_cards)")
        }

    assert tuple(checkpoint) == ("create_editorial_fact_cards",)
    assert {
        "track_id",
        "language",
        "fact",
        "fact_hash",
        "url",
        "source",
        "retrieved_at",
        "match_evidence",
        "created_at",
    } <= columns
    assert indexes


def test_rundown_migration_is_versioned_with_durable_transition_tables(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database.init_db(settings)

    with database.connect(settings) as conn:
        checkpoint = conn.execute(
            "select name from schema_migrations where version=8"
        ).fetchone()
        tables = {
            row["name"]
            for row in conn.execute(
                "select name from sqlite_master where type='table'"
            )
        }
        rundown_columns = {
            row["name"] for row in conn.execute("pragma table_info(rundown_items)")
        }

    assert tuple(checkpoint) == ("create_durable_rundowns",)
    assert {"rundown_items", "rundown_transitions", "liner_template_usage"} <= tables
    assert {
        "station_id",
        "planned_start",
        "planned_end",
        "actual_start",
        "actual_end",
        "measured_duration_seconds",
        "item_type",
        "track_id",
        "program_id",
        "state",
        "source_path",
        "rendered_path",
        "editorial_kind",
        "fact_card_id",
        "template_id",
        "transition_id",
        "attempts",
        "error_code",
    } <= rundown_columns


def test_migrations_are_applied_in_version_order() -> None:
    Migration, _, apply_migrations = migration_api()
    with sqlite3.connect(":memory:") as conn:
        apply_migrations(
            conn,
            [
                Migration(2, "insert_after_create", "insert into ordered_values(value) values ('second');"),
                Migration(1, "create_ordered_values", "create table ordered_values (value text not null);"),
            ],
        )

        values = [row[0] for row in conn.execute("select value from ordered_values")]
        versions = [row[0] for row in conn.execute("select version from schema_migrations order by version")]

    assert values == ["second"]
    assert versions == [1, 2]


def test_concurrent_startup_rechecks_checkpoint_after_acquiring_write_lock(tmp_path: Path) -> None:
    Migration, _, apply_migrations = migration_api()
    database_path = tmp_path / "concurrent.db"
    migrations = [
        Migration(1, "create_anchor", "create table migration_anchor (id integer);"),
        Migration(2, "create_target", "create table if not exists migration_target (id integer);"),
    ]
    with sqlite3.connect(database_path) as setup_connection:
        apply_migrations(setup_connection, migrations[:1])

    first_connection = sqlite3.connect(database_path, check_same_thread=False)
    second_connection = sqlite3.connect(database_path, check_same_thread=False)
    second_checkpoint_read = threading.Event()
    allow_second_to_continue = threading.Event()
    second_read_state = {"checkpoint_query": False, "paused": False, "write_lock": False}
    first_finished = threading.Event()
    first_errors: list[BaseException] = []
    second_errors: list[BaseException] = []

    def trace_second_connection(sql: str) -> None:
        normalized_sql = sql.strip().casefold()
        if normalized_sql.startswith("begin"):
            second_read_state["write_lock"] = True
        if normalized_sql.startswith("select version, name, checksum from schema_migrations"):
            second_read_state["checkpoint_query"] = True

    def pause_after_checkpoint_row(cursor, row):
        del cursor
        if second_read_state["checkpoint_query"] and not second_read_state["paused"]:
            second_read_state["paused"] = True
            second_checkpoint_read.set()
            assert allow_second_to_continue.wait(timeout=5)
        return row

    def run_second_startup() -> None:
        try:
            apply_migrations(second_connection, migrations)
        except BaseException as error:  # assertion reports the exact second-startup failure
            second_errors.append(error)

    def run_first_startup() -> None:
        try:
            apply_migrations(first_connection, migrations)
        except BaseException as error:  # assertion reports the exact competing-startup failure
            first_errors.append(error)
        finally:
            first_finished.set()

    second_connection.set_trace_callback(trace_second_connection)
    second_connection.row_factory = pause_after_checkpoint_row
    second_thread = threading.Thread(target=run_second_startup)
    first_thread = threading.Thread(target=run_first_startup)
    try:
        second_thread.start()
        assert second_checkpoint_read.wait(timeout=5)

        first_thread.start()
        if not second_read_state["write_lock"]:
            assert first_finished.wait(timeout=5)

        allow_second_to_continue.set()
        second_thread.join(timeout=5)
        first_thread.join(timeout=5)
        assert not second_thread.is_alive()
        assert not first_thread.is_alive()
        assert first_errors == []
        assert second_errors == []
        assert first_connection.execute(
            "select version from schema_migrations where version=2"
        ).fetchone() == (2,)
    finally:
        allow_second_to_continue.set()
        second_thread.join(timeout=5)
        first_thread.join(timeout=5)
        first_connection.close()
        second_connection.close()


def test_failing_migration_rolls_back_schema_and_checkpoint() -> None:
    Migration, MigrationError, apply_migrations = migration_api()
    with sqlite3.connect(":memory:") as conn:
        migration = Migration(
            7,
            "rollback_on_error",
            "create table must_be_rolled_back (id integer); insert into missing_table values (1);",
        )

        with pytest.raises(MigrationError, match="migration 7 failed"):
            apply_migrations(conn, [migration])

        table = conn.execute(
            "select name from sqlite_master where type='table' and name='must_be_rolled_back'"
        ).fetchone()
        checkpoint = conn.execute(
            "select version from schema_migrations where version=7"
        ).fetchone()

    assert table is None
    assert checkpoint is None


def test_checksum_drift_stops_migration_startup() -> None:
    Migration, MigrationError, apply_migrations = migration_api()
    with sqlite3.connect(":memory:") as conn:
        original = Migration(3, "checksum_target", "create table checksum_target (id integer);")
        apply_migrations(conn, [original])

        drifted = Migration(3, "checksum_target", "create table checksum_target (id text);")
        with pytest.raises(MigrationError, match="checksum drift"):
            apply_migrations(conn, [drifted])


def test_duplicate_migration_version_or_name_stops_startup() -> None:
    Migration, MigrationError, apply_migrations = migration_api()
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(MigrationError, match="duplicate migration version"):
            apply_migrations(
                conn,
                [
                    Migration(5, "first", "create table first_table (id integer);"),
                    Migration(5, "second", "create table second_table (id integer);"),
                ],
            )

        with pytest.raises(MigrationError, match="duplicate migration name"):
            apply_migrations(
                conn,
                [
                    Migration(5, "same_name", "create table one_table (id integer);"),
                    Migration(6, "same_name", "create table two_table (id integer);"),
                ],
            )


def test_incompatible_existing_feature_table_stops_initial_migration() -> None:
    _, MigrationError, apply_migrations = migration_api()
    with sqlite3.connect(":memory:") as conn:
        conn.execute("create table channels (id text primary key)")

        with pytest.raises(MigrationError, match="incompatible schema"):
            apply_migrations(conn)

        checkpoint = conn.execute("select version from schema_migrations where version=1").fetchone()

    assert checkpoint is None


def test_checkpointed_migration_revalidates_schema_on_later_startup() -> None:
    _, MigrationError, apply_migrations = migration_api()
    with sqlite3.connect(":memory:") as conn:
        apply_migrations(conn)
        conn.execute("drop table channels")

        with pytest.raises(MigrationError, match="incompatible schema"):
            apply_migrations(conn)


@pytest.mark.parametrize(
    "ledger_sql",
    [
        """
        create table schema_migrations (
            version integer primary key,
            name text unique,
            checksum text,
            applied_at text
        )
        """,
        """
        create table schema_migrations (
            version integer primary key,
            name text not null unique,
            checksum text not null,
            applied_at text not null,
            unexpected_column text
        )
        """,
    ],
)
def test_incompatible_migration_ledger_stops_startup(ledger_sql: str) -> None:
    _, MigrationError, apply_migrations = migration_api()
    with sqlite3.connect(":memory:") as conn:
        conn.execute(ledger_sql)

        with pytest.raises(MigrationError, match="incompatible schema_migrations table"):
            apply_migrations(conn)


def test_missing_table_constraint_stops_initial_migration() -> None:
    _, MigrationError, apply_migrations = migration_api()
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            """
            create table channels (
                id text,
                name text not null,
                description text not null,
                host_model text not null,
                status text not null,
                cover_path text,
                created_at text not null,
                updated_at text not null
            )
            """
        )

        with pytest.raises(MigrationError, match="incompatible schema"):
            apply_migrations(conn)

        checkpoint = conn.execute("select version from schema_migrations where version=1").fetchone()

    assert checkpoint is None


def test_initial_migration_preserves_legacy_program_column_upgrade(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with sqlite3.connect(settings.database_path) as conn:
        conn.executescript(
            """
            create table channels (
                id text primary key,
                name text not null,
                description text not null,
                host_model text not null,
                status text not null,
                cover_path text,
                created_at text not null,
                updated_at text not null
            );
            create table programs (
                id text primary key,
                channel_id text not null references channels(id),
                name text not null,
                description text not null,
                vibe text,
                start_time text not null,
                end_time text not null,
                days_of_week text not null,
                cover_path text,
                active integer not null default 1,
                created_at text not null,
                updated_at text not null
            );
            """
        )

    database.init_db(settings)

    with database.connect(settings) as conn:
        columns = {row["name"] for row in conn.execute("pragma table_info(programs)")}
        checkpoint = conn.execute(
            "select version from schema_migrations where version=1"
        ).fetchone()

    assert {"host_name", "host_gender", "voice", "personality"} <= columns
    assert checkpoint is not None
    assert checkpoint[0] == 1


def test_init_db_rejects_cross_station_path_before_migrations(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    context = station_context(tmp_path)
    expected_database = context.database_file
    foreign_database = tmp_path / "foreign-station.db"
    context.settings.database_path = str(foreign_database)

    with pytest.raises(ValueError, match="database .*path"):
        database.init_db(context)

    assert not expected_database.exists()
    assert not foreign_database.exists()
