"""Station-scoped Qwen recovery policy.

This module intentionally decides only whether speech is safe to re-enable.
It never selects, synthesizes, or substitutes a speech provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuntimeMode(str, Enum):
    STARTING = "starting"
    LIVE = "live"
    MUSIC_ONLY = "music_only"
    RECOVERING = "recovering"
    STOPPED = "stopped"


@dataclass(frozen=True)
class SupervisorStatus:
    station_id: str
    mode: RuntimeMode
    qwen_healthy: bool
    speech_ready_minutes: float
    normal_ready_minutes: float
    consecutive_qwen_probes: int
    stable_recovery_samples: int


class StationSupervisor:
    """Keep music running while requiring real Qwen recovery evidence."""

    def __init__(
        self,
        *,
        station_id: str,
        music_only_ready_minutes: float = 10.0,
        speech_recovery_minutes: float = 60.0,
        required_qwen_probes: int = 3,
        required_stable_samples: int = 5,
    ) -> None:
        if not station_id:
            raise ValueError("supervisor requires a station id")
        self.station_id = station_id
        self.music_only_ready_minutes = music_only_ready_minutes
        self.speech_recovery_minutes = speech_recovery_minutes
        self.required_qwen_probes = required_qwen_probes
        self.required_stable_samples = required_stable_samples
        self._mode = RuntimeMode.STARTING
        self._consecutive_qwen_probes = 0
        self._stable_recovery_samples = 0

    def evaluate(
        self,
        *,
        qwen_healthy: bool,
        speech_ready_minutes: float,
        normal_ready_minutes: float,
    ) -> SupervisorStatus:
        """Return the mode permitted by a fresh Qwen probe and queue coverage."""
        if speech_ready_minutes < 0 or normal_ready_minutes < 0:
            raise ValueError("ready minutes cannot be negative")

        if not qwen_healthy or normal_ready_minutes < self.music_only_ready_minutes:
            self._mode = RuntimeMode.MUSIC_ONLY
            self._consecutive_qwen_probes = 0
            self._stable_recovery_samples = 0
            return self._status(qwen_healthy, speech_ready_minutes, normal_ready_minutes)

        if self._mode is RuntimeMode.STARTING:
            self._mode = (
                RuntimeMode.LIVE
                if speech_ready_minutes >= self.speech_recovery_minutes
                else RuntimeMode.RECOVERING
            )
            return self._status(qwen_healthy, speech_ready_minutes, normal_ready_minutes)

        if self._mode is RuntimeMode.MUSIC_ONLY:
            self._consecutive_qwen_probes += 1
            if self._consecutive_qwen_probes >= self.required_qwen_probes:
                self._mode = RuntimeMode.RECOVERING
                self._stable_recovery_samples = 0
            return self._status(qwen_healthy, speech_ready_minutes, normal_ready_minutes)

        if self._mode is RuntimeMode.RECOVERING:
            if speech_ready_minutes < self.speech_recovery_minutes:
                self._stable_recovery_samples = 0
            else:
                self._stable_recovery_samples += 1
                if self._stable_recovery_samples >= self.required_stable_samples:
                    self._mode = RuntimeMode.LIVE
            return self._status(qwen_healthy, speech_ready_minutes, normal_ready_minutes)

        return self._status(qwen_healthy, speech_ready_minutes, normal_ready_minutes)

    def stop(self) -> SupervisorStatus:
        self._mode = RuntimeMode.STOPPED
        return self._status(False, 0.0, 0.0)

    def _status(
        self,
        qwen_healthy: bool,
        speech_ready_minutes: float,
        normal_ready_minutes: float,
    ) -> SupervisorStatus:
        return SupervisorStatus(
            station_id=self.station_id,
            mode=self._mode,
            qwen_healthy=qwen_healthy,
            speech_ready_minutes=speech_ready_minutes,
            normal_ready_minutes=normal_ready_minutes,
            consecutive_qwen_probes=self._consecutive_qwen_probes,
            stable_recovery_samples=self._stable_recovery_samples,
        )
