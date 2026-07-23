from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _database_target(settings: Settings) -> str:
    target = str(settings.database_path)
    if target != ":memory:":
        path = Path(target).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)
    return target


@contextmanager
def connect(settings: Settings):
    connection = sqlite3.connect(_database_target(settings))
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys = on")
        yield connection
    finally:
        connection.close()


def init_db(settings: Settings) -> None:
    with connect(settings) as connection:
        connection.executescript(PUBLIC_PLATFORM_SCHEMA)
        connection.commit()
