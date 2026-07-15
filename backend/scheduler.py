from __future__ import annotations

from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from .config import Settings
from .database import DATABASE_CHANNEL_ID, PROGRAMS, connect, rows_to_dicts
from .programming.clocks import ProgramClock
from .programming.logs import (
    ImagingLogCandidate,
    MusicLogCandidate,
    SevenDayLogGenerator,
    SevenDayStationLog,
)
from .stations.context import StationContext, coerce_station_context


DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _programs_from_db(runtime: Settings | StationContext) -> list[dict]:
    context = coerce_station_context(runtime)
    with connect(context) as conn:
        rows = conn.execute(
            """
            select id, name, description, vibe, start_time, end_time, days_of_week, cover_path, active
            from programs
            where channel_id=? and active=1
            order by case id
                when 'morning_signal' then 0
                when 'campus_frequencies' then 1
                when 'night_lab' then 2
                when 'weekend_transmission' then 3
                when 'overnight_signal' then 4
                when 'weekend_overnight' then 5
                else 99
            end, start_time
            """,
            (DATABASE_CHANNEL_ID,),
        ).fetchall()
    return rows_to_dicts(rows)


def _matches(program: dict, day: str, minute: int) -> bool:
    if day not in str(program["days_of_week"]).split(","):
        return False
    start = _minutes(program["start_time"])
    end = _minutes(program["end_time"])
    if start <= end:
        return start <= minute <= end
    return minute >= start or minute <= end


def current_program(runtime: Settings | StationContext | None = None, now: datetime | None = None) -> dict:
    context = coerce_station_context(runtime) if runtime is not None else None
    now = now or datetime.now(ZoneInfo(context.profile.timezone) if context else None)
    day = DAY_KEYS[now.weekday()]
    minute = now.hour * 60 + now.minute
    programs = _programs_from_db(context) if context else [dict(program) for program in PROGRAMS]
    for program in programs:
        if _matches(program, day, minute):
            return dict(program)
    return dict(programs[0] if programs else PROGRAMS[0])


def next_programs(runtime: Settings | StationContext | None = None, limit: int = 4) -> list[dict]:
    context = coerce_station_context(runtime) if runtime is not None else None
    programs = _programs_from_db(context) if context else [dict(program) for program in PROGRAMS]
    return [dict(program) for program in programs[:limit]]


def generate_station_qualification_log(
    station_id: str,
    clock: ProgramClock,
    first_boundary_at: datetime,
    music_candidates: Iterable[MusicLogCandidate],
    imaging_candidates: Iterable[ImagingLogCandidate],
    *,
    generator: SevenDayLogGenerator | None = None,
) -> SevenDayStationLog:
    """Generate an isolated, deterministic seven-day scheduling fixture.

    This is a qualification helper only: it neither starts playout nor claims
    to observe live broadcasting.  The supplied inventories remain entirely
    station-local and are checked by the generator before a log is returned.
    """

    return (generator or SevenDayLogGenerator()).generate(
        station_id,
        clock,
        first_boundary_at,
        music_candidates,
        imaging_candidates,
    )
