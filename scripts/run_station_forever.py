import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


STATION_PORTS = {"radiotedu-en": 8765, "radiotedu-fr": 8766}
RESTART_DELAYS_SECONDS = (2, 4, 8, 16, 30)
MAX_STARTS_PER_WINDOW = 5
RESTART_WINDOW_SECONDS = 10 * 60
RESTART_RESET_SECONDS = 30 * 60


@dataclass(frozen=True)
class ProcessSpec:
    """The complete, station-scoped contract for one backend process."""

    name: str
    command: list[str]
    cwd: Path
    environment: dict[str, str]
    port: int
    stdout_path: Path
    stderr_path: Path

    @property
    def args(self) -> list[str]:
        """Compatibility alias for callers of the original runner."""
        return self.command


@dataclass
class ProcessState:
    process: subprocess.Popen[bytes] | None = None
    started_at: float = 0.0
    restart_times: deque[float] = field(default_factory=deque)
    next_start_at: float = 0.0
    suppressed: bool = False


def station_environment(station_id: str, port: int, root: Path) -> dict[str, str]:
    """Return only the selected station's launch inputs; never copy secrets."""
    if station_id not in STATION_PORTS:
        raise ValueError(f"unsupported station id: {station_id}")
    return {
        "STATION_ID": station_id,
        "RADIOTEDU_STATION_ID": station_id,
        "API_HOST": "127.0.0.1",
        "API_PORT": str(port),
        "STATION_PROFILES_DIR": str(root / "config" / "stations"),
        "PYTHONPATH": str(root),
    }


def build_process_specs(root: Path, start_frontend: bool = False) -> list[ProcessSpec]:
    """Build independent EN and FR broadcast processes.

    ``start_frontend`` is retained for local development compatibility only;
    production services never request it and therefore never make playout
    depend on a frontend process.
    """
    root = root.resolve()
    specs: list[ProcessSpec] = []
    for station_id, port in STATION_PORTS.items():
        logs = root / "data" / "stations" / station_id / "logs"
        specs.append(
            ProcessSpec(
                name=f"backend-{station_id}",
                command=[
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "backend.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=root,
                environment=station_environment(station_id, port, root),
                port=port,
                stdout_path=logs / "station.out.log",
                stderr_path=logs / "station.err.log",
            )
        )
    if start_frontend:
        npm = "npm.cmd" if os.name == "nt" else "npm"
        logs = root / "logs"
        specs.append(
            ProcessSpec(
                name="frontend-development-only",
                command=[npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173"],
                cwd=root,
                environment={"PYTHONPATH": str(root)},
                port=5173,
                stdout_path=logs / "frontend-forever.out.log",
                stderr_path=logs / "frontend-forever.err.log",
            )
        )
    return specs


def backend_is_healthy(url: str, timeout_seconds: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            return 200 <= response.status < 500
    except (OSError, urllib.error.URLError):
        return False


def backend_health_due(started_at: float, now: float, grace_seconds: int) -> bool:
    return now - started_at >= grace_seconds


def station_snapshot_provider(station_id: str, port: int) -> dict:
    from backend.public_sync import snapshot_state_from_operator_status

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/status",
        headers={"User-Agent": "RadioTEDU-PublicSync/1.0"},
    )
    with urllib.request.urlopen(request, timeout=1) as response:
        status = json.loads(response.read().decode("utf-8"))
    return snapshot_state_from_operator_status(station_id, status)


def build_public_sync_service(root: Path, *, settings=None, transport=None):
    """Create the one outbound sync owner shared by both station children."""

    from backend.config import Settings
    from backend.public_sync import PublicSyncService

    root = root.resolve()
    sync_settings = settings or Settings.from_env(root / ".env")
    providers = {
        station_id: (lambda station_id=station_id, port=port: station_snapshot_provider(station_id, port))
        for station_id, port in STATION_PORTS.items()
    }
    options = {
        "snapshot_providers": providers,
        "station_databases": {
            station_id: root / "data" / "stations" / station_id / "radio.db"
            for station_id in STATION_PORTS
        },
    }
    if transport is not None:
        options["transport"] = transport
    return PublicSyncService(
        sync_settings,
        root / "data" / "public-sync" / "public-sync.db",
        **options,
    )


def _record_restart(state: ProcessState, now: float) -> None:
    while state.restart_times and now - state.restart_times[0] > RESTART_WINDOW_SECONDS:
        state.restart_times.popleft()
    state.restart_times.append(now)
    state.suppressed = len(state.restart_times) >= MAX_STARTS_PER_WINDOW
    if state.suppressed:
        return
    delay_index = min(len(state.restart_times) - 1, len(RESTART_DELAYS_SECONDS) - 1)
    state.next_start_at = now + RESTART_DELAYS_SECONDS[delay_index]


def process_environment(spec: ProcessSpec, inherited: dict[str, str]) -> dict[str, str]:
    """Give each child only its own Icecast source credential and no HMAC secret."""
    environment = {**inherited, **spec.environment}
    source_names = {
        "radiotedu-en": "RADIOTEDU_EN_SOURCE_CREDENTIALS",
        "radiotedu-fr": "RADIOTEDU_FR_SOURCE_CREDENTIALS",
    }
    selected_source = next(
        (name for station_id, name in source_names.items() if spec.name == f"backend-{station_id}"),
        None,
    )
    selected_value = inherited.get(selected_source, "") if selected_source else ""
    for secret_name in (
        *source_names.values(),
        "RADIOTEDU_EN_SNAPSHOT_SECRET",
        "RADIOTEDU_FR_SNAPSHOT_SECRET",
    ):
        environment.pop(secret_name, None)
    if selected_source and selected_value:
        environment[selected_source] = selected_value
    return environment


def _start(spec: ProcessSpec, state: ProcessState, now: float) -> None:
    spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout = spec.stdout_path.open("ab")
    stderr = spec.stderr_path.open("ab")
    environment = process_environment(spec, dict(os.environ))
    state.process = subprocess.Popen(
        spec.command,
        cwd=spec.cwd,
        env=environment,
        stdout=stdout,
        stderr=stderr,
    )
    state.started_at = now


def _stop_process(state: ProcessState) -> None:
    if state.process is None or state.process.poll() is not None:
        return
    state.process.terminate()
    try:
        state.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        state.process.kill()


def supervise(root: Path, start_frontend: bool, health_url: str, interval_seconds: int, restart_delay_seconds: int) -> None:
    """Supervise each station independently with bounded restart attempts."""
    del health_url, restart_delay_seconds  # Each station owns its health endpoint and policy.
    specs = build_process_specs(root, start_frontend=start_frontend)
    states = {spec.name: ProcessState() for spec in specs}
    public_sync = build_public_sync_service(root)
    public_sync.start_background()
    try:
        while True:
            now = time.time()
            for spec in specs:
                state = states[spec.name]
                process = state.process
                if process is None:
                    if not state.suppressed and now >= state.next_start_at:
                        _start(spec, state, now)
                    continue
                if process.poll() is not None:
                    if now - state.started_at >= RESTART_RESET_SECONDS:
                        state.restart_times.clear()
                    _record_restart(state, now)
                    state.process = None
                    continue
                if spec.name.startswith("backend-") and backend_health_due(
                    state.started_at, now, max(interval_seconds, 15)
                ) and not backend_is_healthy(f"http://127.0.0.1:{spec.port}/api/status"):
                    _stop_process(state)
                    _record_restart(state, now)
                    state.process = None
            time.sleep(interval_seconds)
    finally:
        public_sync.stop_background()
        for state in states.values():
            _stop_process(state)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Keep isolated RadioTEDU station processes running locally.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--frontend", action="store_true", help="Development only; never use for broadcast services.")
    parser.add_argument("--health-url", default="", help="Deprecated; health is station-local.")
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--restart-delay-seconds", type=int, default=2, help="Deprecated; bounded delays are fixed.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    supervise(
        root=Path(args.root).resolve(),
        start_frontend=bool(args.frontend),
        health_url=args.health_url,
        interval_seconds=args.interval_seconds,
        restart_delay_seconds=args.restart_delay_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
