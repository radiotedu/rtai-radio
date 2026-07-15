from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .platform_api import (
    LANGUAGES,
    PROTOCOL,
    STATIONS,
    PlayEventEnvelope,
    SnapshotV2,
    sign_platform_headers,
)


SnapshotProvider = Callable[[], dict]
Transport = Callable[..., int]


def editorial_sound_tags(*values: object) -> list[str]:
    text = " ".join(str(value or "").casefold() for value in values)
    return [tag for tag in ("warm", "bright", "calm", "focused", "energetic") if tag in text]


def _public_program(value: object) -> dict | None:
    if not isinstance(value, dict) or not value.get("id") or not value.get("name"):
        return None
    vibe = str(value.get("vibe") or "")[:240] or None
    return {
        "id": str(value["id"])[:128],
        "name": str(value["name"])[:160],
        "vibe": vibe,
        "sound_tags": editorial_sound_tags(vibe),
    }


def snapshot_state_from_operator_status(station_id: str, status: dict) -> dict:
    """Reduce a station-local status response to the public snapshot allowlist."""

    if station_id not in STATIONS:
        raise ValueError("unsupported station")
    mount = "/en" if station_id.endswith("-en") else "/fr"
    now = status.get("now_playing") if isinstance(status.get("now_playing"), dict) else {}
    source_kind = str(now.get("type") or "unknown").casefold()
    if source_kind in {"track", "music"}:
        public_kind = "music"
        speech_kind = "music"
    elif source_kind in {"tts", "speech", "announcement", "live"}:
        public_kind = "talking"
        speech_kind = "talking"
    elif source_kind == "idle":
        public_kind = "unknown"
        speech_kind = "idle"
    else:
        public_kind = "unknown"
        speech_kind = "unknown"
    current = _public_program(status.get("current_program"))
    upcoming = status.get("next_programs")
    next_program = _public_program(upcoming[0]) if isinstance(upcoming, list) and upcoming else None
    liquidsoap = status.get("liquidsoap") if isinstance(status.get("liquidsoap"), dict) else {}
    if liquidsoap.get("running") and liquidsoap.get("mount_active"):
        stream_status = "live"
    elif liquidsoap.get("running"):
        stream_status = "degraded"
    else:
        stream_status = "offline"
    channel = status.get("channel") if isinstance(status.get("channel"), dict) else {}
    channel_status = str(channel.get("status") or "unknown").casefold()
    if channel_status == "live" and stream_status == "live":
        operational = "live"
    elif channel_status in {"stopped", "offline"} and stream_status == "offline":
        operational = "offline"
    elif channel_status in {"starting", "idle"}:
        operational = "starting"
    else:
        operational = "degraded"
    program_tags = current["sound_tags"] if current else []
    return {
        "operational_state": operational,
        "speech_state": {"active": speech_kind == "talking", "kind": speech_kind},
        "now_playing": {
            "kind": public_kind,
            "track_id": str(now.get("track_id"))[:128] if now.get("track_id") is not None else None,
            "title": str(now.get("title"))[:200] if now.get("title") is not None else None,
            "artist": str(now.get("artist"))[:160] if now.get("artist") is not None else None,
            "cover_id": None,
            "mood": None,
            "sound_tags": list(program_tags),
            "started_at": now.get("started_at"),
        },
        "current_program": current,
        "next_program": next_program,
        "stream": {
            "url": f"https://stream.radiotedu.com{mount}",
            "mount": mount,
            "status": stream_status,
            "codec": "AAC-LC",
            "bitrate_kbps": 192,
            "public": True,
        },
        "editorial": {"sound_tags": list(program_tags)},
    }


OUTBOX_SCHEMA = """
create table if not exists sync_station_state (
    station_id text primary key,
    sequence integer not null default 0,
    fingerprint text,
    last_snapshot_at real
);

create table if not exists sync_source_cursors (
    station_id text not null,
    source text not null,
    last_id integer not null default 0,
    primary key(station_id, source)
);

create table if not exists sync_outbox (
    id integer primary key autoincrement,
    station_id text not null,
    kind text not null,
    event_key text not null,
    method text not null,
    path text not null,
    content_type text not null,
    body blob not null,
    idempotency_key text not null,
    status text not null,
    attempts integer not null default 0,
    next_attempt_at real not null default 0,
    created_at real not null,
    updated_at real not null,
    unique(station_id, kind, event_key)
);

create index if not exists idx_sync_outbox_delivery
    on sync_outbox(status, next_attempt_at, created_at);
"""


class PublicSyncService:
    """The single outbound owner for both station processes.

    It only accepts sanitized state/events, persists them, and sends them to the
    website API. It deliberately has no dependency on playout or orchestration.
    """

    station_ids = ("radiotedu-en", "radiotedu-fr")
    can_control_playout = False

    def __init__(
        self,
        settings: Settings,
        database_path: str | Path,
        *,
        snapshot_providers: dict[str, SnapshotProvider] | None = None,
        station_databases: dict[str, str | Path] | None = None,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.settings = settings
        self.database_path = Path(database_path)
        self.snapshot_providers = dict(snapshot_providers or {})
        self.station_databases = {
            station_id: Path(path) for station_id, path in (station_databases or {}).items()
        }
        if not set(self.snapshot_providers) <= STATIONS:
            raise ValueError("snapshot providers contain an unsupported station")
        if not set(self.station_databases) <= STATIONS:
            raise ValueError("station databases contain an unsupported station")
        self.transport = transport or _http_transport
        self.clock = clock
        self.random_value = random_value
        self.running = False
        self.last_result: dict | None = None
        self._stop = threading.Event()
        self._wake = threading.Condition()
        self._thread: threading.Thread | None = None
        self._init_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=1)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_database(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(OUTBOX_SCHEMA)
            for station_id in self.station_ids:
                conn.execute(
                    "insert into sync_station_state(station_id, sequence) values (?, 0) on conflict(station_id) do nothing",
                    (station_id,),
                )
                for source in ("play_history", "station_public_events"):
                    conn.execute(
                        "insert into sync_source_cursors(station_id, source, last_id) values (?, ?, 0) on conflict(station_id, source) do nothing",
                        (station_id, source),
                    )
            conn.commit()

    def configured(self) -> bool:
        return bool(
            self.settings.public_sync_url
            and self.settings.platform_agent_id == "school-radio-pc"
            and self.settings.platform_agent_scope == "agent:playout"
            and self.settings.platform_hmac_secret_en
            and self.settings.platform_hmac_secret_fr
        )

    def publish_snapshot(self, station_id: str, state: dict) -> dict:
        if station_id not in STATIONS:
            raise ValueError("unsupported station")
        now = self.clock()
        generated_at = datetime.fromtimestamp(now, tz=timezone.utc)
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select sequence from sync_station_state where station_id=?",
                (station_id,),
            ).fetchone()
            sequence = int(row["sequence"]) + 1
            payload = {
                **state,
                "protocol": PROTOCOL,
                "schema_version": 2,
                "station": {
                    "id": station_id,
                    "language": LANGUAGES[station_id],
                    "display_name": "RadioTEDU" if station_id.endswith("-en") else "RadioTEDU Français",
                },
                "sequence": sequence,
                "generated_at": generated_at.isoformat(),
                "expires_at": (
                    generated_at + timedelta(seconds=max(5, int(self.settings.snapshot_ttl_seconds)))
                ).isoformat(),
            }
            validated = SnapshotV2.model_validate(payload).model_dump(mode="json")
            body = json.dumps(validated, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            path = f"/v1/radio/stations/{station_id}/snapshot"
            conn.execute(
                "update sync_station_state set sequence=?, last_snapshot_at=? where station_id=?",
                (sequence, now, station_id),
            )
            conn.execute(
                """
                insert into sync_outbox(
                    station_id, kind, event_key, method, path, content_type, body,
                    idempotency_key, status, attempts, next_attempt_at, created_at, updated_at
                ) values (?, 'snapshot', 'latest', 'POST', ?, 'application/json', ?, ?, 'pending', 0, 0, ?, ?)
                on conflict(station_id, kind, event_key) do update set
                    method=excluded.method,
                    path=excluded.path,
                    content_type=excluded.content_type,
                    body=excluded.body,
                    idempotency_key=excluded.idempotency_key,
                    status='pending',
                    attempts=0,
                    next_attempt_at=0,
                    updated_at=excluded.updated_at
                """,
                (
                    station_id,
                    path,
                    body,
                    f"snapshot:{station_id}:{sequence}",
                    now,
                    now,
                ),
            )
            conn.commit()
        self._notify()
        return validated

    def enqueue_play_event(self, station_id: str, event: dict) -> dict:
        if station_id not in STATIONS:
            raise ValueError("unsupported station")
        validated = PlayEventEnvelope.model_validate(event)
        if validated.station_id != station_id:
            raise ValueError("play event station mismatch")
        payload = validated.model_dump(mode="json")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        now = self.clock()
        with self._connect() as conn:
            conn.execute(
                """
                insert into sync_outbox(
                    station_id, kind, event_key, method, path, content_type, body,
                    idempotency_key, status, attempts, next_attempt_at, created_at, updated_at
                ) values (?, 'play', ?, 'POST', ?, 'application/json', ?, ?, 'pending', 0, 0, ?, ?)
                on conflict(station_id, kind, event_key) do nothing
                """,
                (
                    station_id,
                    validated.event_id,
                    f"/v1/radio/stations/{station_id}/plays",
                    body,
                    f"play:{station_id}:{validated.event_id}",
                    now,
                    now,
                ),
            )
            conn.commit()
        self._notify()
        return payload

    def enqueue_cover(self, station_id: str, cover_id: str, body: bytes, content_type: str) -> None:
        if station_id not in STATIONS:
            raise ValueError("unsupported station")
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("unsupported cover content type")
        now = self.clock()
        with self._connect() as conn:
            conn.execute(
                """
                insert into sync_outbox(
                    station_id, kind, event_key, method, path, content_type, body,
                    idempotency_key, status, attempts, next_attempt_at, created_at, updated_at
                ) values (?, 'cover', ?, 'PUT', ?, ?, ?, ?, 'pending', 0, 0, ?, ?)
                on conflict(station_id, kind, event_key) do update set
                    content_type=excluded.content_type,
                    body=excluded.body,
                    idempotency_key=excluded.idempotency_key,
                    status='pending',
                    attempts=0,
                    next_attempt_at=0,
                    updated_at=excluded.updated_at
                """,
                (
                    station_id,
                    cover_id,
                    f"/v1/radio/stations/{station_id}/covers/{cover_id}",
                    content_type,
                    body,
                    f"cover:{station_id}:{cover_id}:{uuid.uuid4().hex}",
                    now,
                    now,
                ),
            )
            conn.commit()
        self._notify()

    def collect_snapshots_once(self) -> dict[str, str]:
        results: dict[str, str] = {}
        now = self.clock()
        for station_id, provider in self.snapshot_providers.items():
            try:
                state = provider()
                fingerprint = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            except Exception:
                results[station_id] = "unavailable"
                continue
            with self._connect() as conn:
                row = conn.execute(
                    "select fingerprint, last_snapshot_at from sync_station_state where station_id=?",
                    (station_id,),
                ).fetchone()
            changed = row["fingerprint"] != fingerprint
            elapsed = now - float(row["last_snapshot_at"] or 0)
            if not changed and elapsed < 10:
                results[station_id] = "waiting"
                continue
            self.publish_snapshot(station_id, state)
            with self._connect() as conn:
                conn.execute(
                    "update sync_station_state set fingerprint=? where station_id=?",
                    (fingerprint, station_id),
                )
                conn.commit()
            results[station_id] = "changed" if changed else "heartbeat"
        return results

    def _cursor(self, station_id: str, source: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "select last_id from sync_source_cursors where station_id=? and source=?",
                (station_id, source),
            ).fetchone()
        return int(row["last_id"] if row else 0)

    def _advance_cursor(self, station_id: str, source: str, last_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "update sync_source_cursors set last_id=? where station_id=? and source=?",
                (last_id, station_id, source),
            )
            conn.commit()

    def collect_play_events_once(self) -> dict[str, int]:
        results: dict[str, int] = {}
        for station_id, database_path in self.station_databases.items():
            stored = 0
            if not database_path.exists():
                results[station_id] = 0
                continue
            try:
                station = sqlite3.connect(
                    f"file:{database_path.resolve().as_posix()}?mode=ro",
                    uri=True,
                    timeout=0.25,
                )
                station.row_factory = sqlite3.Row
                music_cursor = self._cursor(station_id, "play_history")
                music_rows = station.execute(
                    """
                    select
                        h.id, h.played_at, h.duration_seconds,
                        t.id as track_id, t.title, t.artist, t.mood,
                        p.id as program_id, p.name as program_name, p.vibe
                    from play_history h
                    join tracks t on t.id=h.track_id
                    left join programs p on p.id=h.program_id
                    where h.id>?
                    order by h.id
                    limit 250
                    """,
                    (music_cursor,),
                ).fetchall()
                for row in music_rows:
                    duration_ms = max(0, int(float(row["duration_seconds"] or 0) * 1000))
                    event = {
                        "protocol": PROTOCOL,
                        "schema_version": 1,
                        "event_id": f"{station_id}:music:{row['id']}",
                        "station_id": station_id,
                        "event_type": "play.completed",
                        "occurred_at": row["played_at"],
                        "classification": "music" if duration_ms else "unknown",
                        "duration_ms": duration_ms,
                        "track_id": str(row["track_id"]),
                        "track_title": row["title"],
                        "artist": row["artist"],
                        "program_id": row["program_id"],
                        "program_name": row["program_name"],
                        "cover_id": None,
                        "sound_tags": editorial_sound_tags(row["mood"], row["vibe"]),
                    }
                    self.enqueue_play_event(station_id, event)
                    self._advance_cursor(station_id, "play_history", int(row["id"]))
                    stored += 1

                journal_cursor = self._cursor(station_id, "station_public_events")
                journal_rows = station.execute(
                    """
                    select
                        e.id, e.occurred_at, e.classification, e.duration_seconds,
                        e.program_id, e.title, e.metadata_json,
                        p.name as program_name, p.vibe
                    from station_public_events e
                    left join programs p on p.id=e.program_id
                    where e.id>? and e.event_type='play.completed'
                    order by e.id
                    limit 250
                    """,
                    (journal_cursor,),
                ).fetchall()
                for row in journal_rows:
                    duration_ms = max(0, int(float(row["duration_seconds"] or 0) * 1000))
                    metadata = json.loads(row["metadata_json"] or "{}")
                    event = {
                        "protocol": PROTOCOL,
                        "schema_version": 1,
                        "event_id": f"{station_id}:journal:{row['id']}",
                        "station_id": station_id,
                        "event_type": "play.completed",
                        "occurred_at": row["occurred_at"],
                        "classification": row["classification"] if row["classification"] in {"music", "talking", "silence", "unknown"} else "unknown",
                        "duration_ms": duration_ms,
                        "track_id": metadata.get("track_id"),
                        "track_title": row["title"],
                        "artist": metadata.get("artist"),
                        "program_id": row["program_id"],
                        "program_name": row["program_name"],
                        "cover_id": metadata.get("cover_id"),
                        "sound_tags": editorial_sound_tags(row["vibe"], *(metadata.get("sound_tags") or [])),
                    }
                    self.enqueue_play_event(station_id, event)
                    self._advance_cursor(station_id, "station_public_events", int(row["id"]))
                    stored += 1
                station.close()
            except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
                results[station_id] = stored
                continue
            results[station_id] = stored
        return results

    def flush_once(self) -> dict:
        if not self.configured():
            self.last_result = {"sent": False, "reason": "not_configured"}
            return self.last_result
        now = self.clock()
        with self._connect() as conn:
            row = conn.execute(
                """
                select * from sync_outbox
                where status='pending' and next_attempt_at<=?
                order by case kind when 'play' then 0 when 'cover' then 1 else 2 end, created_at, id
                limit 1
                """,
                (now,),
            ).fetchone()
        if row is None:
            self.last_result = {"sent": False, "reason": "empty"}
            return self.last_result
        body = bytes(row["body"])
        timestamp = str(int(now))
        correlation_id = str(uuid.uuid4())
        headers = sign_platform_headers(
            self.settings,
            method=row["method"],
            path=row["path"],
            station_id=row["station_id"],
            body=body,
            timestamp=timestamp,
            nonce=uuid.uuid4().hex,
            idempotency_key=row["idempotency_key"],
            correlation_id=correlation_id,
            agent_id=self.settings.platform_agent_id,
        )
        headers["Content-Type"] = row["content_type"]
        try:
            status = self.transport(
                method=row["method"],
                url=self.settings.public_sync_url.rstrip("/") + row["path"],
                path=row["path"],
                body=body,
                headers=headers,
            )
            if not 200 <= int(status) < 300:
                raise OSError("non-success website response")
        except Exception:
            attempts = int(row["attempts"]) + 1
            cap = min(60.0, float(2 ** max(0, attempts - 1)))
            retry = self.random_value() * cap
            with self._connect() as conn:
                conn.execute(
                    "update sync_outbox set attempts=?, next_attempt_at=?, updated_at=? where id=?",
                    (attempts, now + retry, now, row["id"]),
                )
                conn.commit()
            self.last_result = {
                "sent": False,
                "reason": "transport_error",
                "retry_in_seconds": retry,
            }
            return self.last_result
        with self._connect() as conn:
            conn.execute(
                "update sync_outbox set status='sent', updated_at=? where id=?",
                (now, row["id"]),
            )
            conn.commit()
        self.last_result = {
            "sent": True,
            "station_id": row["station_id"],
            "kind": row["kind"],
            "status": int(status),
        }
        return self.last_result

    def pending_records(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "select * from sync_outbox where status='pending' order by id"
            ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record["body"] = bytes(record["body"]).decode("utf-8", errors="replace")
            records.append(record)
        return records

    def start_background(self) -> dict:
        if not self.configured():
            return {"running": False, "reason": "not_configured"}
        if self.running:
            return {"running": True, "reason": "already_running"}
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="radiotedu-public-sync",
            daemon=True,
        )
        self._thread.start()
        return {"running": True, "reason": "started"}

    def stop_background(self) -> dict:
        self._stop.set()
        self._notify()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        self.running = False
        return {"running": False, "reason": "stopped"}

    def status(self) -> dict:
        with self._connect() as conn:
            pending = int(conn.execute("select count(*) from sync_outbox where status='pending'").fetchone()[0])
        return {
            "configured": self.configured(),
            "running": self.running,
            "outbound_only": True,
            "can_control_playout": False,
            "stations": list(self.station_ids),
            "pending": pending,
            "last_result": self.last_result,
            "heartbeat_seconds": 10,
        }

    def _notify(self) -> None:
        with self._wake:
            self._wake.notify_all()

    def _run_loop(self) -> None:
        try:
            while not self._stop.is_set():
                self.collect_play_events_once()
                self.collect_snapshots_once()
                while self.flush_once().get("sent"):
                    pass
                with self._wake:
                    self._wake.wait(timeout=0.5)
        finally:
            self.running = False


def _http_transport(*, method: str, url: str, path: str, body: bytes, headers: dict[str, str]) -> int:
    del path
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return int(response.status)
