"""SQLite persistence for station-scoped announcement jobs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterator, Mapping, Sequence
from uuid import uuid4

from .models import AnnouncementJob, AnnouncementJobEvent, AnnouncementState


class AnnouncementJobNotFound(LookupError):
    """Raised when a job is not visible within the requested station."""


class InvalidAnnouncementTransition(ValueError):
    """Raised when a state change violates the durable job state machine."""


_ACTIVE_STATES = frozenset(
    {
        AnnouncementState.PLANNED,
        AnnouncementState.TEXT_READY,
        AnnouncementState.SYNTHESIZING,
        AnnouncementState.AUDIO_READY,
    }
)

_ALLOWED_TRANSITIONS: dict[AnnouncementState, frozenset[AnnouncementState]] = {
    AnnouncementState.PLANNED: frozenset(
        {
            AnnouncementState.TEXT_READY,
            AnnouncementState.EXPIRED,
            AnnouncementState.SKIPPED,
            AnnouncementState.FAILED,
        }
    ),
    AnnouncementState.TEXT_READY: frozenset(
        {
            AnnouncementState.SYNTHESIZING,
            AnnouncementState.EXPIRED,
            AnnouncementState.SKIPPED,
            AnnouncementState.FAILED,
        }
    ),
    AnnouncementState.SYNTHESIZING: frozenset(
        {
            AnnouncementState.TEXT_READY,
            AnnouncementState.AUDIO_READY,
            AnnouncementState.EXPIRED,
            AnnouncementState.SKIPPED,
            AnnouncementState.FAILED,
        }
    ),
    AnnouncementState.AUDIO_READY: frozenset(
        {
            AnnouncementState.CONSUMED,
            AnnouncementState.EXPIRED,
            AnnouncementState.SKIPPED,
            AnnouncementState.FAILED,
        }
    ),
    AnnouncementState.CONSUMED: frozenset(),
    AnnouncementState.EXPIRED: frozenset(),
    AnnouncementState.SKIPPED: frozenset(),
    AnnouncementState.FAILED: frozenset(),
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _state_details(state: AnnouncementState) -> tuple[str, str]:
    if state is AnnouncementState.PLANNED:
        return "pending", "pending"
    if state is AnnouncementState.TEXT_READY:
        return "ready", "pending"
    if state is AnnouncementState.SYNTHESIZING:
        return "ready", "synthesizing"
    if state is AnnouncementState.AUDIO_READY:
        return "ready", "ready"
    if state is AnnouncementState.CONSUMED:
        return "ready", "consumed"
    if state is AnnouncementState.FAILED:
        return "failed", "failed"
    return "skipped", "skipped"


class AnnouncementJobStore:
    """Persist and transition jobs without ever crossing a station boundary."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row

    def create(self, job: AnnouncementJob) -> AnnouncementJob:
        """Insert a job once per station/planner key and return the durable job."""
        with self._transaction():
            existing = self._connection.execute(
                "select * from announcement_jobs where station_id = ? and planner_key = ?",
                (job.station_id, job.planner_key),
            ).fetchone()
            if existing is not None:
                return self._job_from_row(existing)
            self._connection.execute(
                """
                insert into announcement_jobs (
                    job_id, station_id, planner_key, language, kind, planned_airtime,
                    deadline, freshness_class, priority, state, text_state, audio_state,
                    attempts, text_hash, audio_path, audio_checksum, lease_expires_at,
                    failure_reason, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.station_id,
                    job.planner_key,
                    job.language,
                    job.kind,
                    _iso(job.planned_airtime),
                    _iso(job.deadline),
                    job.freshness_class,
                    job.priority,
                    job.state.value,
                    job.text_state,
                    job.audio_state,
                    job.attempts,
                    job.text_hash,
                    job.audio_path,
                    job.audio_checksum,
                    _iso(job.lease_expires_at),
                    job.failure_reason,
                    _iso(job.created_at),
                    _iso(job.updated_at),
                ),
            )
            return job

    def get(self, station_id: str, job_id: str) -> AnnouncementJob | None:
        row = self._connection.execute(
            "select * from announcement_jobs where station_id = ? and job_id = ?",
            (station_id, job_id),
        ).fetchone()
        return self._job_from_row(row) if row is not None else None

    def list_for_station(
        self,
        station_id: str,
        states: Sequence[AnnouncementState] | None = None,
    ) -> list[AnnouncementJob]:
        query = "select * from announcement_jobs where station_id = ?"
        parameters: list[object] = [station_id]
        if states:
            query += " and state in (" + ", ".join("?" for _ in states) + ")"
            parameters.extend(state.value for state in states)
        query += " order by planned_airtime, job_id"
        rows = self._connection.execute(query, parameters).fetchall()
        return [self._job_from_row(row) for row in rows]

    def transition(
        self,
        station_id: str,
        job_id: str,
        target: AnnouncementState,
        *,
        actor: str,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> AnnouncementJob:
        with self._transaction():
            return self._transition_locked(
                station_id,
                job_id,
                AnnouncementState(target),
                actor=actor,
                reason=reason,
                metadata=metadata,
            )

    def expire_due(
        self, station_id: str, at: datetime, *, actor: str = "freshness"
    ) -> list[AnnouncementJob]:
        with self._transaction():
            placeholders = ", ".join("?" for _ in _ACTIVE_STATES)
            rows = self._connection.execute(
                f"""
                select job_id from announcement_jobs
                where station_id = ? and deadline <= ? and state in ({placeholders})
                order by deadline, job_id
                """,
                (station_id, _iso(at), *(state.value for state in _ACTIVE_STATES)),
            ).fetchall()
            return [
                self._transition_locked(
                    station_id,
                    row["job_id"],
                    AnnouncementState.EXPIRED,
                    actor=actor,
                    reason="deadline elapsed",
                    metadata=None,
                )
                for row in rows
            ]

    def events(self, station_id: str, job_id: str) -> list[AnnouncementJobEvent]:
        rows = self._connection.execute(
            """
            select event_id, job_id, from_state, to_state, actor, reason,
                   metadata_json, occurred_at
            from announcement_job_events
            where job_id = ? and exists (
                select 1 from announcement_jobs
                where announcement_jobs.job_id = announcement_job_events.job_id
                  and announcement_jobs.station_id = ?
            )
            order by occurred_at, announcement_job_events.rowid
            """,
            (job_id, station_id),
        ).fetchall()
        return [
            AnnouncementJobEvent(
                event_id=row["event_id"],
                job_id=row["job_id"],
                from_state=AnnouncementState(row["from_state"]),
                to_state=AnnouncementState(row["to_state"]),
                actor=row["actor"],
                reason=row["reason"],
                metadata=json.loads(row["metadata_json"]),
                occurred_at=_datetime(row["occurred_at"]),
            )
            for row in rows
        ]

    def _transition_locked(
        self,
        station_id: str,
        job_id: str,
        target: AnnouncementState,
        *,
        actor: str,
        reason: str | None,
        metadata: Mapping[str, object] | None,
    ) -> AnnouncementJob:
        row = self._connection.execute(
            "select * from announcement_jobs where station_id = ? and job_id = ?",
            (station_id, job_id),
        ).fetchone()
        if row is None:
            raise AnnouncementJobNotFound(f"unknown announcement job for station {station_id}")
        job = self._job_from_row(row)
        if job.state is target:
            return job
        if target not in _ALLOWED_TRANSITIONS[job.state]:
            raise InvalidAnnouncementTransition(f"cannot transition {job.state.value} to {target.value}")

        text_state, audio_state = _state_details(target)
        now = datetime.now(timezone.utc)
        attempts = job.attempts + (1 if target is AnnouncementState.SYNTHESIZING else 0)
        failure_reason = reason if target is AnnouncementState.FAILED else job.failure_reason
        updated = replace(
            job,
            state=target,
            text_state=text_state,
            audio_state=audio_state,
            attempts=attempts,
            failure_reason=failure_reason,
            updated_at=now,
        )
        self._connection.execute(
            """
            update announcement_jobs
            set state = ?, text_state = ?, audio_state = ?, attempts = ?,
                failure_reason = ?, updated_at = ?
            where station_id = ? and job_id = ?
            """,
            (
                updated.state.value,
                updated.text_state,
                updated.audio_state,
                updated.attempts,
                updated.failure_reason,
                _iso(updated.updated_at),
                station_id,
                job_id,
            ),
        )
        self._connection.execute(
            """
            insert into announcement_job_events (
                event_id, job_id, from_state, to_state, actor, reason,
                metadata_json, occurred_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                job_id,
                job.state.value,
                target.value,
                actor,
                reason,
                json.dumps(dict(metadata or {}), sort_keys=True, separators=(",", ":")),
                _iso(now),
            ),
        )
        return updated

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._connection.in_transaction:
            raise RuntimeError("announcement store requires an idle SQLite connection")
        self._connection.execute("begin immediate")
        try:
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> AnnouncementJob:
        return AnnouncementJob(
            job_id=row["job_id"],
            station_id=row["station_id"],
            language=row["language"],
            kind=row["kind"],
            planner_key=row["planner_key"],
            planned_airtime=_datetime(row["planned_airtime"]),
            deadline=_datetime(row["deadline"]),
            freshness_class=row["freshness_class"],
            priority=row["priority"],
            state=AnnouncementState(row["state"]),
            text_state=row["text_state"],
            audio_state=row["audio_state"],
            attempts=row["attempts"],
            text_hash=row["text_hash"],
            audio_path=row["audio_path"],
            audio_checksum=row["audio_checksum"],
            lease_expires_at=_datetime(row["lease_expires_at"]),
            failure_reason=row["failure_reason"],
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
        )
