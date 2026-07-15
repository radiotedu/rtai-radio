from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import Settings, ensure_runtime_dirs
from .stations.context import StationContext, coerce_station_context, ensure_station_runtime_dirs


Runtime = Settings | StationContext
DATABASE_CHANNEL_ID = "radiotedu"
CHANNEL_DESCRIPTIONS = {
    "en": "Local AI radio running on your machine.",
    "fr": "Radio IA locale en français diffusée depuis votre ordinateur.",
}


SCHEMA_MIGRATIONS_SCHEMA = """
create table if not exists schema_migrations (
    version integer primary key,
    name text not null unique,
    checksum text not null,
    applied_at text not null
)
"""


class MigrationError(RuntimeError):
    """Raised when a database cannot be safely migrated."""


@dataclass(frozen=True)
class TableContract:
    name: str
    columns: tuple[tuple[str, str, int, str | None, int], ...]
    unique_indexes: frozenset[tuple[str, ...]]
    foreign_keys: frozenset[tuple[str, str, str, str, str, str]]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    required_columns: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    schema_contract: tuple[TableContract, ...] = ()
    migration_step: Callable[[sqlite3.Connection], None] | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    operation_name: str = ""
    checksum: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise MigrationError("migration version must be a positive integer")
        if not self.name or not self.name.strip():
            raise MigrationError("migration name must not be empty")
        if not self.sql.strip():
            raise MigrationError(f"migration {self.version} has no SQL")
        if self.migration_step is not None and not self.operation_name.strip():
            raise MigrationError(f"migration {self.version} operation requires a stable name")
        checksum_source = self.sql
        if self.operation_name:
            checksum_source = f"{checksum_source}\n-- operation: {self.operation_name}\n"
        expected_checksum = hashlib.sha256(checksum_source.encode("utf-8")).hexdigest()
        if self.checksum and self.checksum != expected_checksum:
            raise MigrationError(f"migration {self.version} checksum does not match its SQL")
        object.__setattr__(self, "checksum", expected_checksum)


PROGRAMS = [
    {
        "id": "morning_signal",
        "name": "TEDU Dawn",
        "description": "A warm pop-first morning broadcast with concise day-openers and occasional curated jazz and classical selections.",
        "vibe": "bright, warm, energetic pop",
        "host_name": "Ece",
        "host_gender": "female",
        "voice": "tr_female_warm",
        "personality": "warm, optimistic, concise, gently energetic",
        "start_time": "06:00",
        "end_time": "09:59",
        "days_of_week": "mon,tue,wed,thu,fri",
        "cover_path": "/static/generated/covers/morning_signal.png",
    },
    {
        "id": "campus_frequencies",
        "name": "Campus Flow",
        "description": "Pop, R&B, dance, indie, and soft rock for study, work, and movement across campus.",
        "vibe": "focused, bright, steady pop flow",
        "host_name": "Mert",
        "host_gender": "male",
        "voice": "tr_male_clear",
        "personality": "calm, precise, curious, lightly academic",
        "start_time": "10:00",
        "end_time": "17:59",
        "days_of_week": "mon,tue,wed,thu,fri",
        "cover_path": "/static/generated/covers/campus_frequencies.png",
    },
    {
        "id": "night_lab",
        "name": "Jazz Lab",
        "description": "A pop-led evening with curated jazz and classical features, deeper context, and smart transitions.",
        "vibe": "cool, energetic, late-evening pop and jazz",
        "host_name": "Selin",
        "host_gender": "female",
        "voice": "tr_female_cool",
        "personality": "cool, informed, playful, music-first",
        "start_time": "18:00",
        "end_time": "23:59",
        "days_of_week": "mon,tue,wed,thu,fri,sat,sun",
        "cover_path": "/static/generated/covers/night_lab.png",
    },
    {
        "id": "weekend_transmission",
        "name": "Weekend Signal",
        "description": "Relaxed pop-first weekend radio with R&B, dance, indie, soft rock, and curated discoveries.",
        "vibe": "relaxed, sunny, eclectic pop",
        "host_name": "Deniz",
        "host_gender": "male",
        "voice": "tr_male_late",
        "personality": "easygoing, warm, conversational, weekend-minded",
        "start_time": "08:00",
        "end_time": "17:59",
        "days_of_week": "sat,sun",
        "cover_path": "/static/generated/covers/weekend_transmission.png",
    },
    {
        "id": "overnight_signal",
        "name": "Night Signal",
        "description": "Warm overnight pop and soft rock with sparse companionship and occasional curated classical calm.",
        "vibe": "warm, calm, focused night pop",
        "host_name": "Ece",
        "host_gender": "female",
        "voice": "tr_female_warm",
        "personality": "warm, concise, reassuring, music-first",
        "start_time": "00:00",
        "end_time": "05:59",
        "days_of_week": "mon,tue,wed,thu,fri",
        "cover_path": "/static/generated/covers/overnight_signal.png",
    },
    {
        "id": "weekend_overnight",
        "name": "Weekend Night Signal",
        "description": "Late-night weekend pop, R&B, indie, and soft rock with relaxed real-radio liners.",
        "vibe": "calm, warm, relaxed weekend pop",
        "host_name": "Deniz",
        "host_gender": "male",
        "voice": "tr_male_late",
        "personality": "easygoing, warm, conversational, music-first",
        "start_time": "00:00",
        "end_time": "07:59",
        "days_of_week": "sat,sun",
        "cover_path": "/static/generated/covers/weekend_overnight.png",
    },
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _casefolded_resolved_path(path: Path) -> Path:
    return Path(os.path.normcase(str(path.expanduser().resolve())))


def _validated_station_database_file(context: StationContext) -> Path:
    expected_database = context.settings.path(context.profile.runtime.database)
    expected_data_root = context.settings.path(context.profile.runtime.data_root)
    actual_database = context.settings.database_file.expanduser().resolve()
    expected_normalized = _casefolded_resolved_path(expected_database)
    data_root_normalized = _casefolded_resolved_path(expected_data_root)
    actual_normalized = _casefolded_resolved_path(actual_database)
    if data_root_normalized not in expected_normalized.parents:
        raise ValueError("station profile database path containment escape")
    if actual_normalized != expected_normalized:
        raise ValueError("station context database path mismatch")
    return actual_database


@contextmanager
def connect(runtime: Runtime):
    context = coerce_station_context(runtime)
    database = _validated_station_database_file(context) if isinstance(runtime, StationContext) else runtime.database_path
    conn = sqlite3.connect(database)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        yield conn
    finally:
        conn.close()


def _ordered_migrations(migrations: Sequence[Migration]) -> tuple[Migration, ...]:
    versions: set[int] = set()
    names: set[str] = set()
    for migration in migrations:
        if migration.version in versions:
            raise MigrationError(f"duplicate migration version {migration.version}")
        if migration.name in names:
            raise MigrationError(f"duplicate migration name {migration.name}")
        versions.add(migration.version)
        names.add(migration.name)
    return tuple(sorted(migrations, key=lambda migration: migration.version))


def _validate_migration_ledger(conn: sqlite3.Connection) -> None:
    columns = {
        row[1]: row
        for row in conn.execute("pragma table_info(schema_migrations)").fetchall()
    }
    expected_types = {
        "version": "integer",
        "name": "text",
        "checksum": "text",
        "applied_at": "text",
    }
    if set(columns) != set(expected_types):
        raise MigrationError("incompatible schema_migrations table")
    for name, expected_type in expected_types.items():
        column = columns.get(name)
        if column is None or str(column[2]).casefold() != expected_type:
            raise MigrationError("incompatible schema_migrations table")
    if columns["version"][5] != 1:
        raise MigrationError("incompatible schema_migrations table")
    if any(not columns[name][3] for name in ("name", "checksum", "applied_at")):
        raise MigrationError("incompatible schema_migrations table")

    unique_name_index = False
    for index in conn.execute("pragma index_list(schema_migrations)").fetchall():
        if not index[2]:
            continue
        index_name = str(index[1]).replace('"', '""')
        indexed_columns = [
            row[2]
            for row in conn.execute(f'pragma index_info("{index_name}")').fetchall()
        ]
        if indexed_columns == ["name"]:
            unique_name_index = True
            break
    if not unique_name_index:
        raise MigrationError("incompatible schema_migrations table")


def _ensure_migration_ledger(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA_MIGRATIONS_SCHEMA)
    _validate_migration_ledger(conn)


def _sql_statements(sql: str, version: int) -> Iterable[str]:
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            completed = statement.strip()
            if completed:
                yield completed
            statement = ""
    if statement.strip():
        raise MigrationError(f"migration {version} has incomplete SQL")


def _validate_required_columns(conn: sqlite3.Connection, migration: Migration) -> None:
    for table, expected_columns in migration.required_columns:
        quoted_table = table.replace('"', '""')
        actual_columns = {
            row[1]: str(row[2]).casefold()
            for row in conn.execute(f'pragma table_info("{quoted_table}")').fetchall()
        }
        for column, expected_type in expected_columns:
            if actual_columns.get(column) != expected_type:
                raise MigrationError(
                    f"incompatible schema for migration {migration.version}: {table}.{column}"
                )


def _table_contract(conn: sqlite3.Connection, table: str) -> TableContract:
    quoted_table = table.replace('"', '""')
    columns = tuple(
        (row[1], str(row[2]).casefold(), int(row[3]), row[4], int(row[5]))
        for row in conn.execute(f'pragma table_info("{quoted_table}")').fetchall()
    )
    unique_indexes: set[tuple[str, ...]] = set()
    for index in conn.execute(f'pragma index_list("{quoted_table}")').fetchall():
        if not index[2]:
            continue
        index_name = str(index[1]).replace('"', '""')
        unique_indexes.add(
            tuple(row[2] for row in conn.execute(f'pragma index_info("{index_name}")').fetchall())
        )
    foreign_keys = frozenset(
        (row[2], row[3], row[4], row[5], row[6], row[7])
        for row in conn.execute(f'pragma foreign_key_list("{quoted_table}")').fetchall()
    )
    return TableContract(table, columns, frozenset(unique_indexes), foreign_keys)


def _validate_schema_contract(conn: sqlite3.Connection, migration: Migration) -> None:
    for expected in migration.schema_contract:
        actual = _table_contract(conn, expected.name)
        actual_columns = {column[0]: column[1:] for column in actual.columns}
        for column in expected.columns:
            if actual_columns.get(column[0]) != column[1:]:
                raise MigrationError(
                    f"incompatible schema for migration {migration.version}: {expected.name}.{column[0]}"
                )
        if not expected.unique_indexes <= actual.unique_indexes:
            raise MigrationError(f"incompatible schema for migration {migration.version}: {expected.name}")
        if not expected.foreign_keys <= actual.foreign_keys:
            raise MigrationError(f"incompatible schema for migration {migration.version}: {expected.name}")


def _validate_migration_schema(conn: sqlite3.Connection, migration: Migration) -> None:
    _validate_required_columns(conn, migration)
    _validate_schema_contract(conn, migration)


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("rollback")


def apply_migrations(
    conn: sqlite3.Connection, migrations: Sequence[Migration] | None = None
) -> None:
    """Apply numbered migrations exactly once, recording checksums atomically."""

    if conn.in_transaction:
        raise MigrationError("migration runner requires an idle connection")
    ordered = _ordered_migrations(DEFAULT_MIGRATIONS if migrations is None else migrations)
    _ensure_migration_ledger(conn)
    known_versions = {migration.version for migration in ordered}

    for migration in ordered:
        try:
            conn.execute("begin immediate")
            applied = {
                row[0]: (row[1], row[2])
                for row in conn.execute("select version, name, checksum from schema_migrations")
            }
            unknown_versions = sorted(set(applied) - known_versions)
            if unknown_versions:
                raise MigrationError(f"database contains unknown migration version {unknown_versions[0]}")
            checkpoint = applied.get(migration.version)
            if checkpoint is not None:
                name, checksum = checkpoint
                if name != migration.name:
                    raise MigrationError(f"migration name drift for version {migration.version}")
                if checksum != migration.checksum:
                    raise MigrationError(f"migration checksum drift for version {migration.version}")
                _validate_migration_schema(conn, migration)
                conn.execute("commit")
                continue
            for statement in _sql_statements(migration.sql, migration.version):
                conn.execute(statement)
            if migration.migration_step is not None:
                migration.migration_step(conn)
            _validate_migration_schema(conn, migration)
            conn.execute(
                "insert into schema_migrations(version, name, checksum, applied_at) values (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, now_iso()),
            )
            conn.execute("commit")
        except MigrationError:
            _rollback(conn)
            raise
        except Exception as error:
            _rollback(conn)
            raise MigrationError(f"migration {migration.version} failed: {error}") from error


def init_db(runtime: Runtime) -> None:
    context = coerce_station_context(runtime)
    if isinstance(runtime, StationContext):
        ensure_station_runtime_dirs(context)
    else:
        ensure_runtime_dirs(context.settings)
    with connect(context if isinstance(runtime, StationContext) else runtime) as conn:
        apply_migrations(conn)
        conn.execute("drop table if exists donations")
        seed_channel(conn, context)
        seed_programs(conn, context)
        conn.commit()


def seed_channel(conn: sqlite3.Connection, context: StationContext) -> None:
    timestamp = now_iso()
    conn.execute(
        """
        insert into channels (id, name, description, host_model, status, cover_path, created_at, updated_at)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(id) do update set
            name=excluded.name,
            description=excluded.description,
            host_model=excluded.host_model,
            cover_path=excluded.cover_path,
            updated_at=excluded.updated_at
        """,
        (
            DATABASE_CHANNEL_ID,
            context.profile.display_name,
            CHANNEL_DESCRIPTIONS[context.profile.language],
            context.settings.ollama_model,
            "idle",
            "/static/generated/covers/radiotedu_station.png",
            timestamp,
            timestamp,
        ),
    )


def seed_programs(conn: sqlite3.Connection, context: StationContext) -> None:
    timestamp = now_iso()
    for program in PROGRAMS:
        conn.execute(
            """
            insert into programs (
                id, channel_id, name, description, vibe, start_time, end_time,
                days_of_week, cover_path, host_name, host_gender, voice, personality, active, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            on conflict(id) do update set
                name=excluded.name,
                description=excluded.description,
                vibe=excluded.vibe,
                start_time=excluded.start_time,
                end_time=excluded.end_time,
                days_of_week=excluded.days_of_week,
                cover_path=excluded.cover_path,
                host_name=excluded.host_name,
                host_gender=excluded.host_gender,
                voice=excluded.voice,
                personality=excluded.personality,
                active=1,
                updated_at=excluded.updated_at
            """,
            (
                program["id"],
                DATABASE_CHANNEL_ID,
                program["name"],
                program["description"],
                program["vibe"],
                program["start_time"],
                program["end_time"],
                program["days_of_week"],
                program["cover_path"],
                program["host_name"],
                program["host_gender"],
                program["voice"],
                program["personality"],
                timestamp,
                timestamp,
            ),
        )


def migrate_program_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("pragma table_info(programs)").fetchall()}
    additions = {
        "host_name": "text",
        "host_gender": "text",
        "voice": "text",
        "personality": "text",
    }
    for name, column_type in additions.items():
        if name not in existing:
            conn.execute(f"alter table programs add column {name} {column_type}")


def migrate_track_cue_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("pragma table_info(tracks)").fetchall()}
    additions = {
        "cue_source": "text",
        "intro_end_seconds": "real",
        "intro_confidence": "real",
        "immediate_loud_vocal": "integer not null default 0",
    }
    for name, column_type in additions.items():
        if name not in existing:
            conn.execute(f"alter table tracks add column {name} {column_type}")


def log_event(conn: sqlite3.Connection, level: str, message: str, metadata: dict | None = None) -> None:
    conn.execute(
        "insert into agent_logs (level, message, metadata_json, created_at) values (?, ?, ?, ?)",
        (level, message, json.dumps(metadata or {}, ensure_ascii=True), now_iso()),
    )


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


SCHEMA = """
create table if not exists channels (
    id text primary key,
    name text not null,
    description text not null,
    host_model text not null,
    status text not null,
    cover_path text,
    created_at text not null,
    updated_at text not null
);

create table if not exists programs (
    id text primary key,
    channel_id text not null references channels(id),
    name text not null,
    description text not null,
    vibe text,
    start_time text not null,
    end_time text not null,
    days_of_week text not null,
    cover_path text,
    host_name text,
    host_gender text,
    voice text,
    personality text,
    active integer not null default 1,
    created_at text not null,
    updated_at text not null
);

create table if not exists tracks (
    id integer primary key autoincrement,
    title text not null,
    artist text not null,
    album text,
    genre text,
    mood text,
    bpm integer,
    duration_seconds real,
    file_path text not null unique,
    cover_path text,
    last_played_at text,
    play_count integer not null default 0,
    created_at text not null,
    updated_at text not null
);

create table if not exists play_history (
    id integer primary key autoincrement,
    track_id integer not null references tracks(id),
    program_id text references programs(id),
    played_at text not null,
    duration_seconds real,
    source text not null
);

create table if not exists generated_clips (
    id integer primary key autoincrement,
    clip_type text not null,
    text text not null,
    file_path text not null,
    voice text,
    program_id text references programs(id),
    created_at text not null
);

create table if not exists station_metrics (
    id integer primary key autoincrement,
    channel_id text not null references channels(id),
    key text not null,
    value text not null,
    updated_at text not null,
    unique(channel_id, key)
);

create table if not exists listener_events (
    id integer primary key autoincrement,
    channel_id text not null references channels(id),
    event_type text not null,
    created_at text not null,
    metadata_json text not null default '{}'
);

create table if not exists agent_logs (
    id integer primary key autoincrement,
    level text not null,
    message text not null,
    metadata_json text not null default '{}',
    created_at text not null
);

create table if not exists autonomy_memory (
    id integer primary key autoincrement,
    kind text not null,
    content text not null,
    source text not null,
    weight real not null default 1.0,
    created_at text not null
);

create table if not exists schedule_revisions (
    id integer primary key autoincrement,
    program_id text not null references programs(id),
    old_start_time text not null,
    old_end_time text not null,
    old_days_of_week text not null,
    new_start_time text not null,
    new_end_time text not null,
    new_days_of_week text not null,
    reason text not null,
    created_at text not null
);

create table if not exists outbound_drafts (
    id integer primary key autoincrement,
    draft_type text not null,
    content text not null,
    status text not null default 'draft',
    created_at text not null
);

create table if not exists incidents (
    id integer primary key autoincrement,
    component text not null,
    severity text not null,
    status text not null default 'open',
    summary text not null,
    details_json text not null default '{}',
    created_at text not null,
    updated_at text not null,
    resolved_at text
);

create table if not exists autonomous_tasks (
    id integer primary key autoincrement,
    task_type text not null,
    component text not null,
    title text not null,
    status text not null default 'queued',
    priority integer not null default 50,
    attempts integer not null default 0,
    details_json text not null default '{}',
    created_at text not null,
    updated_at text not null,
    completed_at text
);

create table if not exists announcement_queue (
    id integer primary key autoincrement,
    text text not null,
    file_path text not null,
    status text not null,
    program_id text references programs(id),
    source text not null,
    created_at text not null,
    used_at text,
    metadata_json text not null default '{}'
);

create table if not exists public_snapshots (
    id integer primary key autoincrement,
    payload_json text not null,
    received_at text not null
);

create table if not exists public_listener_sessions (
    session_id text primary key,
    started_at text not null,
    last_seen_at text not null,
    ended_at text,
    user_agent text
);

create index if not exists idx_tracks_file_path on tracks(file_path);
create index if not exists idx_tracks_last_played on tracks(last_played_at);
create index if not exists idx_play_history_played_at on play_history(played_at);
create index if not exists idx_agent_logs_created_at on agent_logs(created_at);
create index if not exists idx_autonomy_memory_created_at on autonomy_memory(created_at);
create index if not exists idx_announcement_queue_status on announcement_queue(status, created_at);
create index if not exists idx_incidents_status on incidents(status, severity, created_at);
create index if not exists idx_autonomous_tasks_status on autonomous_tasks(status, priority, created_at);
create index if not exists idx_public_snapshots_received_at on public_snapshots(received_at);
create index if not exists idx_public_listener_sessions_seen on public_listener_sessions(last_seen_at, ended_at);
"""


def _schema_column_requirements(
    schema: str,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    requirements: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for statement in _sql_statements(schema, 1):
        lowered = statement.casefold()
        if not lowered.startswith("create table if not exists "):
            continue
        prefix, body = statement.split("(", 1)
        table = prefix.split()[-1].strip('"`[]')
        columns: list[tuple[str, str]] = []
        for definition in body.rsplit(")", 1)[0].splitlines():
            normalized = definition.strip().rstrip(",")
            if normalized.casefold().startswith(
                ("check", "constraint", "foreign", "primary", "unique")
            ):
                continue
            parts = normalized.split()
            if len(parts) < 2:
                continue
            columns.append((parts[0].strip('"`[]'), parts[1].casefold()))
        requirements.append((table, tuple(columns)))
    return tuple(requirements)


def _schema_contract(schema: str) -> tuple[TableContract, ...]:
    with sqlite3.connect(":memory:") as conn:
        for statement in _sql_statements(schema, 1):
            conn.execute(statement)
        tables = [
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
            ).fetchall()
        ]
        return tuple(_table_contract(conn, table) for table in tables)


ANNOUNCEMENT_JOB_SCHEMA = """
create table if not exists announcement_jobs (
    job_id text primary key,
    station_id text not null,
    planner_key text not null,
    language text not null,
    kind text not null,
    planned_airtime text not null,
    deadline text not null,
    freshness_class text not null,
    priority integer not null,
    state text not null,
    text_state text not null,
    audio_state text not null,
    attempts integer not null default 0,
    text_hash text,
    audio_path text,
    audio_checksum text,
    lease_expires_at text,
    failure_reason text,
    created_at text not null,
    updated_at text not null,
    unique(station_id, planner_key)
);

create table if not exists announcement_job_events (
    event_id text primary key,
    job_id text not null references announcement_jobs(job_id) on delete cascade,
    from_state text not null,
    to_state text not null,
    actor text not null,
    reason text,
    metadata_json text not null default '{}',
    occurred_at text not null
);

create index if not exists announcement_jobs_station_state_deadline_idx
    on announcement_jobs(station_id, state, deadline);
create index if not exists announcement_jobs_station_airtime_idx
    on announcement_jobs(station_id, planned_airtime);
create index if not exists announcement_jobs_state_lease_expiry_idx
    on announcement_jobs(state, lease_expires_at);
create index if not exists announcement_jobs_station_kind_expiry_idx
    on announcement_jobs(station_id, kind, deadline);
create index if not exists announcement_job_events_job_time_idx
    on announcement_job_events(job_id, occurred_at);
"""


AIRCHECK_REPORT_SCHEMA = """
create table if not exists aircheck_reports (
    report_id integer primary key,
    station_id text not null,
    window_start text not null,
    window_end text not null,
    file_relative_path text not null,
    file_checksum text not null,
    codec text not null,
    bitrate_kbps integer not null,
    channels integer not null,
    loudness_lufs real not null,
    true_peak_dbtp real not null,
    silence_seconds real not null,
    clipping_count integer not null,
    transition_count integer not null,
    result text not null,
    analyzer_version text not null,
    created_at text not null,
    unique(station_id, window_start)
);

create index if not exists idx_aircheck_reports_station_window
    on aircheck_reports(station_id, window_start);
"""


PROGRAM_CLOCK_SCHEMA = """
create table if not exists station_clocks (
    clock_id text primary key,
    station_id text not null,
    name text not null,
    daypart text not null,
    timezone_name text not null,
    version integer not null,
    effective_from text not null,
    effective_until text,
    active integer not null check(active in (0, 1)),
    checksum text not null,
    unique(station_id, daypart, version)
);

create table if not exists clock_positions (
    position_id text primary key,
    clock_id text not null references station_clocks(clock_id) on delete restrict,
    ordinal integer not null,
    offset_seconds integer not null,
    item_kind text not null,
    category text not null,
    boundary_kind text not null,
    maximum_lateness_seconds integer not null,
    rules_json text not null,
    unique(clock_id, ordinal)
);

create index if not exists idx_station_clocks_active_lookup
    on station_clocks(station_id, daypart, active, effective_from, effective_until, version);
create index if not exists idx_clock_positions_clock_ordinal
    on clock_positions(clock_id, ordinal);
"""


PUBLIC_PLATFORM_SCHEMA = """
create table if not exists public_station_snapshots (
    station_id text primary key,
    sequence integer not null,
    generated_at text not null,
    expires_at text,
    received_at text not null,
    correlation_id text not null,
    payload_json text not null
);

create table if not exists public_play_events (
    station_id text not null,
    event_id text not null,
    occurred_at text not null,
    classification text not null,
    duration_ms integer not null,
    payload_json text not null,
    correlation_id text not null,
    received_at text not null,
    primary key(station_id, event_id)
);

create table if not exists public_cover_assets (
    station_id text not null,
    cover_id text not null,
    content_type text not null,
    body blob not null,
    correlation_id text not null,
    updated_at text not null,
    primary key(station_id, cover_id)
);

create table if not exists public_agent_nonces (
    agent_id text not null,
    nonce text not null,
    seen_at text not null,
    primary key(agent_id, nonce)
);

create table if not exists public_idempotency_records (
    agent_id text not null,
    idempotency_key text not null,
    method text not null,
    path text not null,
    body_hash text not null,
    status_code integer not null,
    response_json text not null,
    created_at text not null,
    primary key(agent_id, idempotency_key)
);

create table if not exists public_station_sessions (
    station_id text not null,
    session_id text not null,
    started_at text not null,
    last_seen_at text not null,
    ended_at text,
    primary key(station_id, session_id)
);

create index if not exists idx_public_play_events_airtime
    on public_play_events(station_id, occurred_at, classification);
create index if not exists idx_public_agent_nonces_seen
    on public_agent_nonces(seen_at);
create index if not exists idx_public_idempotency_created
    on public_idempotency_records(created_at);
create index if not exists idx_public_station_sessions_seen
    on public_station_sessions(station_id, last_seen_at, ended_at);
"""


STATION_PUBLIC_EVENT_SCHEMA = """
create table if not exists station_public_events (
    id integer primary key autoincrement,
    event_type text not null,
    occurred_at text not null,
    classification text not null,
    duration_seconds real not null,
    program_id text references programs(id),
    title text,
    metadata_json text not null default '{}'
);

create index if not exists idx_station_public_events_delivery
    on station_public_events(id, event_type);
"""


EDITORIAL_FACT_CARD_SCHEMA = """
create table if not exists editorial_fact_cards (
    id integer primary key autoincrement,
    track_id integer not null references tracks(id) on delete cascade,
    language text not null check(language in ('en', 'fr')),
    fact text not null check(length(trim(fact)) between 1 and 1000),
    fact_hash text not null check(length(fact_hash) = 64),
    url text not null check(url like 'http://%' or url like 'https://%'),
    source text not null check(length(trim(source)) between 1 and 80),
    retrieved_at text not null,
    match_evidence text not null,
    created_at text not null,
    unique(track_id, language, url, fact_hash)
);

create index if not exists idx_editorial_fact_cards_track_language
    on editorial_fact_cards(track_id, language, retrieved_at desc);
"""


RUNDOWN_SCHEMA = """
create table if not exists rundown_items (
    id integer primary key autoincrement,
    station_id text not null check(station_id in ('radiotedu-en', 'radiotedu-fr')),
    planned_start text not null,
    planned_end text not null,
    actual_start text,
    actual_end text,
    measured_duration_seconds real not null check(measured_duration_seconds > 0),
    actual_duration_seconds real,
    item_type text not null check(item_type in ('music_track', 'speech', 'imaging', 'talkover_composite', 'fallback')),
    track_id integer references tracks(id),
    program_id text references programs(id),
    state text not null check(state in ('planned', 'researching', 'rendering', 'ready', 'queued', 'playing', 'completed', 'failed', 'skipped', 'stale')),
    source_path text not null,
    rendered_path text,
    editorial_kind text,
    fact_card_id integer references editorial_fact_cards(id),
    template_id text,
    transition_id integer,
    attempts integer not null default 0 check(attempts >= 0),
    error_code text,
    queued_at text,
    metadata_json text not null default '{}',
    created_at text not null,
    updated_at text not null,
    check(planned_end > planned_start)
);

create table if not exists rundown_transitions (
    id integer primary key autoincrement,
    from_item_id integer references rundown_items(id),
    to_item_id integer not null references rundown_items(id),
    transition_kind text not null,
    cue_source text,
    cue_confidence real,
    speech_start_seconds real,
    speech_end_seconds real,
    duck_db real,
    reason text not null,
    metadata_json text not null default '{}',
    created_at text not null
);

create table if not exists liner_template_usage (
    id integer primary key autoincrement,
    station_id text not null check(station_id in ('radiotedu-en', 'radiotedu-fr')),
    template_id text not null,
    language text not null check(language in ('en', 'fr')),
    track_id integer references tracks(id),
    program_id text references programs(id),
    rundown_item_id integer references rundown_items(id),
    used_at text not null,
    unique(station_id, template_id, rundown_item_id)
);

create unique index if not exists uq_rundown_active_track_start
    on rundown_items(track_id, planned_start)
    where track_id is not null and state in ('planned', 'researching', 'rendering', 'ready', 'queued', 'playing');
create index if not exists idx_rundown_state_start on rundown_items(state, planned_start);
create index if not exists idx_rundown_station_start on rundown_items(station_id, planned_start);
create index if not exists idx_rundown_transition_to on rundown_transitions(to_item_id);
create index if not exists idx_liner_template_usage_recent on liner_template_usage(station_id, used_at desc);
"""


DEFAULT_MIGRATIONS = (
    Migration(
        1,
        "initial_station_schema",
        SCHEMA,
        required_columns=_schema_column_requirements(SCHEMA),
        schema_contract=_schema_contract(SCHEMA),
        migration_step=migrate_program_columns,
        operation_name="migrate_program_columns_v1",
    ),
    Migration(
        2,
        "create_announcement_jobs",
        ANNOUNCEMENT_JOB_SCHEMA,
        required_columns=_schema_column_requirements(ANNOUNCEMENT_JOB_SCHEMA),
        schema_contract=_schema_contract(ANNOUNCEMENT_JOB_SCHEMA),
    ),
    Migration(
        3,
        "create_aircheck_reports",
        AIRCHECK_REPORT_SCHEMA,
        required_columns=_schema_column_requirements(AIRCHECK_REPORT_SCHEMA),
        schema_contract=_schema_contract(AIRCHECK_REPORT_SCHEMA),
    ),
    Migration(
        4,
        "create_station_program_clocks",
        PROGRAM_CLOCK_SCHEMA,
        required_columns=_schema_column_requirements(PROGRAM_CLOCK_SCHEMA),
        schema_contract=_schema_contract(PROGRAM_CLOCK_SCHEMA),
    ),
    Migration(
        5,
        "create_public_platform_storage",
        PUBLIC_PLATFORM_SCHEMA,
        required_columns=_schema_column_requirements(PUBLIC_PLATFORM_SCHEMA),
        schema_contract=_schema_contract(PUBLIC_PLATFORM_SCHEMA),
    ),
    Migration(
        6,
        "create_station_public_event_journal",
        STATION_PUBLIC_EVENT_SCHEMA,
        required_columns=_schema_column_requirements(STATION_PUBLIC_EVENT_SCHEMA),
        schema_contract=_schema_contract(STATION_PUBLIC_EVENT_SCHEMA),
    ),
    Migration(
        7,
        "create_editorial_fact_cards",
        EDITORIAL_FACT_CARD_SCHEMA,
        required_columns=_schema_column_requirements(EDITORIAL_FACT_CARD_SCHEMA),
        schema_contract=_schema_contract(EDITORIAL_FACT_CARD_SCHEMA),
    ),
    Migration(
        8,
        "create_durable_rundowns",
        RUNDOWN_SCHEMA,
        required_columns=_schema_column_requirements(RUNDOWN_SCHEMA),
        schema_contract=_schema_contract(RUNDOWN_SCHEMA),
    ),
    Migration(
        9,
        "add_track_cue_metadata",
        "select 1;",
        required_columns=(
            (
                "tracks",
                (
                    ("cue_source", "text"),
                    ("intro_end_seconds", "real"),
                    ("intro_confidence", "real"),
                    ("immediate_loud_vocal", "integer"),
                ),
            ),
        ),
        migration_step=migrate_track_cue_columns,
        operation_name="migrate_track_cue_columns_v1",
    ),
)
