from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..config import Settings, ensure_runtime_dirs
from ..database import init_db
from ..orchestrator import AutonomousOrchestrator
from ..radio_agent import RadioAgent
from ..stations.context import (
    StationContext,
    coerce_station_context,
    ensure_station_runtime_dirs,
)


class SnapshotPusher(Protocol):
    def start_background(self) -> dict:
        raise NotImplementedError

    def stop_background(self) -> dict:
        raise NotImplementedError


SnapshotPusherFactory = Callable[[StationContext, RadioAgent], SnapshotPusher | None]


@dataclass
class StationRuntime:
    """Own the lifecycle of one isolated broadcast station process."""

    context: StationContext
    agent: RadioAgent
    orchestrator: AutonomousOrchestrator
    snapshot_pusher: SnapshotPusher | None = None
    _started: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        components = (
            self.agent,
            self.orchestrator,
            getattr(self.agent, "rundown_planner", None),
            getattr(self.agent, "fallback_playlist", None),
        )
        for component in (item for item in components if item is not None):
            component_context = getattr(component, "context", self.context)
            if not _same_station_context(component_context, self.context):
                raise ValueError("station runtime components must share one station context")

    def start(self) -> dict:
        if self._started:
            return {"station_id": self.context.profile.station_id, "running": True}

        music = self.agent.start()
        public_state = self._start_public_state()
        autonomy = self._start_autonomy()
        self._started = True
        return {
            "station_id": self.context.profile.station_id,
            "music": music,
            "public_state": public_state,
            "autonomy": autonomy,
        }

    def stop(self) -> dict:
        if not self._started:
            return {"station_id": self.context.profile.station_id, "running": False}

        autonomy = self.orchestrator.stop_background()
        public_state = self._stop_public_state()
        music = self.agent.stop()
        self._started = False
        return {
            "station_id": self.context.profile.station_id,
            "music": music,
            "public_state": public_state,
            "autonomy": autonomy,
        }

    def _start_autonomy(self) -> dict:
        if not getattr(getattr(self.context, "settings", None), "autonomy_enabled", True):
            return {"running": False, "reason": "disabled"}
        try:
            return self.orchestrator.start_background()
        except Exception:
            return {"running": False, "reason": "unavailable"}

    def _start_public_state(self) -> dict | None:
        if self.snapshot_pusher is None:
            return None
        try:
            return self.snapshot_pusher.start_background()
        except Exception:
            return {"running": False, "reason": "unavailable"}

    def _stop_public_state(self) -> dict | None:
        if self.snapshot_pusher is None:
            return None
        try:
            return self.snapshot_pusher.stop_background()
        except Exception:
            return {"running": False, "reason": "unavailable"}


def create_station_runtime(
    context: Settings | StationContext,
    snapshot_pusher: SnapshotPusher | None = None,
    snapshot_pusher_factory: SnapshotPusherFactory | None = None,
) -> StationRuntime:
    """Build the complete lifecycle for one and only one station context."""

    if isinstance(context, Settings):
        ensure_runtime_dirs(context)
        init_db(context)
        agent = RadioAgent(context)
        station_context = agent.context
        orchestrator = AutonomousOrchestrator(context, agent)
    else:
        station_context = coerce_station_context(context)
        ensure_runtime_dirs(station_context.settings)
        ensure_station_runtime_dirs(station_context)
        init_db(station_context)
        agent = RadioAgent(station_context)
        orchestrator = AutonomousOrchestrator(station_context, agent)
    pusher = snapshot_pusher
    if pusher is None and snapshot_pusher_factory is not None:
        pusher = snapshot_pusher_factory(station_context, agent)
    return StationRuntime(station_context, agent, orchestrator, pusher)


def _same_station_context(left: object, right: StationContext) -> bool:
    if left is right:
        return True
    if not isinstance(left, StationContext):
        return False
    return (
        left.profile.station_id == right.profile.station_id
        and left.database_file == right.database_file
        and left.data_root == right.data_root
        and left.music_root == right.music_root
        and left.announcement_root == right.announcement_root
        and left.cache_root == right.cache_root
        and left.log_root == right.log_root
    )
