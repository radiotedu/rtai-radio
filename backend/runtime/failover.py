"""Station-local source failover with listener-safe switching hysteresis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable


class FailoverState(str, Enum):
    PRIMARY = "primary"
    DEGRADED_PRIMARY = "degraded_primary"
    FALLBACK = "fallback"
    RECOVERING = "recovering"
    PRIMARY_RESTORED = "primary_restored"


@dataclass(frozen=True)
class FailoverStatus:
    station_id: str
    state: FailoverState
    active_source: str
    last_transition_at: datetime | None


class FailoverController:
    """Switch one station between its primary and independent fallback source.

    Callback ordering deliberately disconnects the old source before enabling
    the replacement.  That avoids an audible overlap at the Icecast mount.
    """

    def __init__(
        self,
        *,
        station_id: str,
        disconnect_primary: Callable[[], None],
        enable_fallback: Callable[[], None],
        disconnect_fallback: Callable[[], None],
        enable_primary: Callable[[], None],
        degraded_primary_seconds: float = 1.0,
        fallback_seconds: float = 1.5,
        recovery_seconds: float = 60.0,
        restore_seconds: float = 30.0,
    ) -> None:
        if not station_id:
            raise ValueError("failover requires a station id")
        if not 0 < degraded_primary_seconds <= fallback_seconds:
            raise ValueError("degraded threshold must be positive and no greater than fallback threshold")
        self.station_id = station_id
        self.disconnect_primary = disconnect_primary
        self.enable_fallback = enable_fallback
        self.disconnect_fallback = disconnect_fallback
        self.enable_primary = enable_primary
        self.degraded_primary_seconds = degraded_primary_seconds
        self.fallback_seconds = fallback_seconds
        self.recovery_seconds = recovery_seconds
        self.restore_seconds = restore_seconds
        self._state = FailoverState.PRIMARY
        self._last_transition_at: datetime | None = None
        self._recovery_started_at: datetime | None = None
        self._restore_started_at: datetime | None = None

    def evaluate(
        self,
        now: datetime,
        *,
        primary_healthy: bool,
        silence_seconds: float,
        recovery_healthy: bool | None = None,
    ) -> FailoverStatus:
        """Advance a state only from current, station-local health evidence."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("failover evaluation time must be timezone-aware")
        if silence_seconds < 0:
            raise ValueError("silence seconds cannot be negative")

        recovery_healthy = primary_healthy if recovery_healthy is None else recovery_healthy
        unhealthy = not primary_healthy or silence_seconds >= self.degraded_primary_seconds
        must_fallback = not primary_healthy or silence_seconds >= self.fallback_seconds

        if self._state in {FailoverState.PRIMARY, FailoverState.PRIMARY_RESTORED}:
            if must_fallback:
                self._activate_fallback(now)
            elif unhealthy:
                self._transition(FailoverState.DEGRADED_PRIMARY, now)
            elif self._state is FailoverState.PRIMARY_RESTORED:
                self._transition(FailoverState.PRIMARY, now)
            return self.status()

        if self._state is FailoverState.DEGRADED_PRIMARY:
            if must_fallback:
                self._activate_fallback(now)
            elif not unhealthy:
                self._transition(FailoverState.PRIMARY, now)
            return self.status()

        if self._state is FailoverState.FALLBACK:
            if primary_healthy and recovery_healthy and silence_seconds < self.degraded_primary_seconds:
                if self._recovery_started_at is None:
                    self._recovery_started_at = now
                elif (now - self._recovery_started_at).total_seconds() >= self.recovery_seconds:
                    self._restore_started_at = now
                    self._transition(FailoverState.RECOVERING, now)
            else:
                self._recovery_started_at = None
            return self.status()

        # A failed recovery keeps the proven fallback source live immediately.
        if not primary_healthy or not recovery_healthy or silence_seconds >= self.degraded_primary_seconds:
            self._recovery_started_at = None
            self._restore_started_at = None
            self._transition(FailoverState.FALLBACK, now)
        elif self._restore_started_at is not None and (
            now - self._restore_started_at
        ).total_seconds() >= self.restore_seconds:
            self.disconnect_fallback()
            self.enable_primary()
            self._transition(FailoverState.PRIMARY_RESTORED, now)
        return self.status()

    def status(self) -> FailoverStatus:
        return FailoverStatus(
            station_id=self.station_id,
            state=self._state,
            active_source="fallback" if self._state in {FailoverState.FALLBACK, FailoverState.RECOVERING} else "primary",
            last_transition_at=self._last_transition_at,
        )

    def _activate_fallback(self, now: datetime) -> None:
        self.disconnect_primary()
        self.enable_fallback()
        self._recovery_started_at = None
        self._restore_started_at = None
        self._transition(FailoverState.FALLBACK, now)

    def _transition(self, state: FailoverState, now: datetime) -> None:
        self._state = state
        self._last_transition_at = now
