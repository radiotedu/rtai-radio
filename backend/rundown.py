from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .database import connect, init_db, now_iso
from .scheduler import current_program
from .stations.context import StationContext, coerce_station_context


ACTIVE_STATES = ("planned", "researching", "rendering", "ready", "queued", "playing")
RENDERED_STATES = ("ready", "queued", "playing")


@dataclass(frozen=True, slots=True)
class CoveragePolicy:
    planned_seconds: int = 14_400
    rendered_seconds: int = 3_600
    refill_seconds: int = 7_200
    fallback_seconds: int = 21_600

    def __post_init__(self) -> None:
        values = (
            self.planned_seconds,
            self.rendered_seconds,
            self.refill_seconds,
            self.fallback_seconds,
        )
        if any(value <= 0 for value in values):
            raise ValueError("coverage policy seconds must be positive")
        if self.rendered_seconds > self.planned_seconds:
            raise ValueError("rendered coverage cannot exceed planned coverage")
        if self.refill_seconds > self.planned_seconds:
            raise ValueError("refill threshold cannot exceed planned coverage")

    @classmethod
    def from_settings(cls, settings: Settings) -> "CoveragePolicy":
        return cls(
            planned_seconds=settings.rundown_planned_seconds,
            rendered_seconds=settings.rundown_rendered_seconds,
            refill_seconds=settings.rundown_refill_seconds,
            fallback_seconds=settings.fallback_coverage_seconds,
        )


@dataclass(frozen=True, slots=True)
class CoverageStatus:
    planned_seconds: int
    rendered_seconds: int
    fallback_seconds: int
    needs_refill: bool
    air_ready: bool


@dataclass(frozen=True, slots=True)
class RundownItem:
    id: int
    station_id: str
    planned_start: str
    planned_end: str
    measured_duration_seconds: float
    item_type: str
    track_id: int | None
    program_id: str | None
    state: str
    source_path: str
    rendered_path: str | None
    editorial_kind: str | None
    fact_card_id: int | None
    template_id: str | None
    transition_id: int | None
    attempts: int
    error_code: str | None


class RundownPlanner:
    def __init__(
        self,
        runtime: Settings | StationContext,
        policy: CoveragePolicy | None = None,
        fallback_seconds_provider: Callable[[], int] | None = None,
    ) -> None:
        self.context = coerce_station_context(runtime)
        self.settings = self.context.settings
        self._database_runtime: Settings | StationContext = (
            self.context if isinstance(runtime, StationContext) else self.settings
        )
        init_db(self._database_runtime)
        self.policy = policy or CoveragePolicy.from_settings(self.settings)
        self._fallback_seconds_provider = fallback_seconds_provider or (lambda: 0)

    @property
    def database_runtime(self) -> Settings | StationContext:
        return self._database_runtime

    def coverage(self, now: datetime) -> CoverageStatus:
        now = _aware_utc(now)
        with connect(self._database_runtime) as conn:
            rows = conn.execute(
                f"""
                select planned_start, planned_end, state
                from rundown_items
                where state in ({','.join('?' for _ in ACTIVE_STATES)})
                """,
                ACTIVE_STATES,
            ).fetchall()
        planned = 0.0
        rendered = 0.0
        for row in rows:
            start = _parse_time(row["planned_start"])
            end = _parse_time(row["planned_end"])
            remaining = max(0.0, (end - max(now, start)).total_seconds())
            planned += remaining
            if row["state"] in RENDERED_STATES:
                rendered += remaining
        fallback = max(0, int(self._fallback_seconds_provider()))
        planned_seconds = int(round(planned))
        rendered_seconds = int(round(rendered))
        return CoverageStatus(
            planned_seconds=planned_seconds,
            rendered_seconds=rendered_seconds,
            fallback_seconds=fallback,
            needs_refill=planned_seconds < self.policy.refill_seconds,
            air_ready=(
                planned_seconds >= self.policy.planned_seconds
                and rendered_seconds >= self.policy.rendered_seconds
                and fallback >= self.policy.fallback_seconds
            ),
        )

    def maintain(self, now: datetime) -> CoverageStatus:
        now = _aware_utc(now)
        self._retire_expired(now)
        status = self.coverage(now)
        if status.planned_seconds < self.policy.planned_seconds:
            self._append_valid_tracks_until(now, self.policy.planned_seconds)
        return self.coverage(now)

    def claim_next_ready(self, now: datetime) -> RundownItem | None:
        now = _aware_utc(now)
        with connect(self._database_runtime) as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                """
                select * from rundown_items
                where state='ready' and planned_end > ?
                order by planned_start, id
                limit 1
                """,
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                conn.execute("commit")
                return None
            updated_at = now_iso()
            conn.execute(
                "update rundown_items set state='queued', queued_at=?, updated_at=? where id=? and state='ready'",
                (now.isoformat(), updated_at, row["id"]),
            )
            claimed = conn.execute(
                "select * from rundown_items where id=?",
                (row["id"],),
            ).fetchone()
            conn.execute("commit")
        return _item_from_row(claimed)

    def mark_completed(
        self,
        item_id: int,
        *,
        actual_start: datetime,
        actual_end: datetime,
    ) -> None:
        start = _aware_utc(actual_start)
        end = _aware_utc(actual_end)
        if end < start:
            raise ValueError("actual rundown end cannot precede its start")
        with connect(self._database_runtime) as conn:
            conn.execute(
                """
                update rundown_items
                set state='completed', actual_start=?, actual_end=?,
                    actual_duration_seconds=?, updated_at=?
                where id=? and state in ('queued', 'playing')
                """,
                (
                    start.isoformat(),
                    end.isoformat(),
                    (end - start).total_seconds(),
                    now_iso(),
                    item_id,
                ),
            )
            conn.commit()

    def mark_failed(self, item_id: int, error_code: str) -> None:
        safe_code = "_".join(str(error_code).casefold().split())[:80] or "unknown"
        with connect(self._database_runtime) as conn:
            conn.execute(
                """
                update rundown_items
                set state='failed', attempts=attempts+1, error_code=?, updated_at=?
                where id=? and state in ('planned', 'researching', 'rendering', 'ready', 'queued', 'playing')
                """,
                (safe_code, now_iso(), item_id),
            )
            conn.commit()

    def _retire_expired(self, now: datetime) -> None:
        with connect(self._database_runtime) as conn:
            conn.execute(
                f"""
                update rundown_items set state='stale', updated_at=?
                where state in ({','.join('?' for _ in ACTIVE_STATES)}) and planned_end <= ?
                """,
                (now_iso(), *ACTIVE_STATES, now.isoformat()),
            )
            conn.commit()

    def _append_valid_tracks_until(self, now: datetime, target_seconds: int) -> None:
        with connect(self._database_runtime) as conn:
            rows = conn.execute(
                """
                select id, title, artist, genre, mood, duration_seconds, file_path
                from tracks
                where duration_seconds is not null and duration_seconds > 0
                order by coalesce(last_played_at, ''), play_count, id
                """
            ).fetchall()
            tracks = [dict(row) for row in rows if Path(row["file_path"]).expanduser().is_file()]
            if not tracks:
                return
            latest = conn.execute(
                f"""
                select max(planned_end) from rundown_items
                where state in ({','.join('?' for _ in ACTIVE_STATES)})
                """,
                ACTIVE_STATES,
            ).fetchone()[0]
            active_count = conn.execute(
                f"select count(*) from rundown_items where state in ({','.join('?' for _ in ACTIVE_STATES)})",
                ACTIVE_STATES,
            ).fetchone()[0]
            cursor = max(now, _parse_time(latest) if latest else now)
            coverage = self.coverage(now).planned_seconds
            rendered = self.coverage(now).rendered_seconds
            index = int(active_count) % len(tracks)
            timestamp = now_iso()
            while coverage < target_seconds:
                track = tracks[index % len(tracks)]
                duration = float(track["duration_seconds"])
                planned_end = cursor + timedelta(seconds=duration)
                state = "ready" if rendered < self.policy.rendered_seconds else "planned"
                local_start = cursor.astimezone(ZoneInfo(self.context.profile.timezone))
                program = current_program(self._database_runtime, local_start)
                rendered_path = track["file_path"] if state == "ready" else None
                conn.execute(
                    """
                    insert into rundown_items (
                        station_id, planned_start, planned_end, measured_duration_seconds,
                        item_type, track_id, program_id, state, source_path, rendered_path,
                        attempts, metadata_json, created_at, updated_at
                    ) values (?, ?, ?, ?, 'music_track', ?, ?, ?, ?, ?, 0, '{}', ?, ?)
                    """,
                    (
                        self.context.profile.station_id,
                        cursor.isoformat(),
                        planned_end.isoformat(),
                        duration,
                        track["id"],
                        program["id"],
                        state,
                        track["file_path"],
                        rendered_path,
                        timestamp,
                        timestamp,
                    ),
                )
                coverage += int(round(duration))
                if state == "ready":
                    rendered += int(round(duration))
                cursor = planned_end
                index += 1
            conn.commit()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("rundown times must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _aware_utc(parsed)


def _item_from_row(row) -> RundownItem:
    return RundownItem(
        id=int(row["id"]),
        station_id=row["station_id"],
        planned_start=row["planned_start"],
        planned_end=row["planned_end"],
        measured_duration_seconds=float(row["measured_duration_seconds"]),
        item_type=row["item_type"],
        track_id=int(row["track_id"]) if row["track_id"] is not None else None,
        program_id=row["program_id"],
        state=row["state"],
        source_path=row["source_path"],
        rendered_path=row["rendered_path"],
        editorial_kind=row["editorial_kind"],
        fact_card_id=int(row["fact_card_id"]) if row["fact_card_id"] is not None else None,
        template_id=row["template_id"],
        transition_id=int(row["transition_id"]) if row["transition_id"] is not None else None,
        attempts=int(row["attempts"]),
        error_code=row["error_code"],
    )

