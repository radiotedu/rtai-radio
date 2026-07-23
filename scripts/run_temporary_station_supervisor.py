"""Restart one temporary RadioTEDU station when its audio heartbeat stalls."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_temporary_station.py"
RUNTIME_ROOT = ROOT / "data" / "runtime" / "temporary-dual-station"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
STARTUP_GRACE_SECONDS = 15.0
HEARTBEAT_TIMEOUT_SECONDS = 6.0
POLL_SECONDS = 1.0
CONSECUTIVE_FAILURES_REQUIRED = 3


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def process_alive(pid: object) -> bool:
    try:
        numeric_pid = int(pid)
        if numeric_pid <= 0:
            return False
        if os.name == "nt":
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information,
                False,
                numeric_pid,
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(numeric_pid, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


class StationSupervisor:
    def __init__(self, station_id: str) -> None:
        self.station_id = station_id
        self.runtime_dir = RUNTIME_ROOT / station_id
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.station_status_path = self.runtime_dir / "status.json"
        self.supervisor_status_path = self.runtime_dir / "supervisor-status.json"
        self.supervisor_pid_path = self.runtime_dir / "supervisor.pid"
        self.supervisor_pid_path.write_text(str(os.getpid()), encoding="ascii")
        self.worker: subprocess.Popen | None = None
        self.worker_started_at: datetime | None = None
        self.worker_started_monotonic = 0.0
        self.restart_count = 0
        self.suspect_reason: str | None = None
        self.consecutive_failures = 0
        self.last_restart_reason = "initial_start"
        self.last_restart_at: datetime | None = None

    def write_status(self, state: str) -> None:
        station_status = read_json(self.station_status_path)
        heartbeat = parse_time(station_status.get("last_audio_write_at"))
        heartbeat_age = (
            max(0.0, (utc_now() - heartbeat).total_seconds())
            if heartbeat
            else None
        )
        atomic_json(
            self.supervisor_status_path,
            {
                "station_id": self.station_id,
                "state": state,
                "updated_at": utc_now().isoformat(),
                "supervisor_pid": os.getpid(),
                "worker_pid": (
                    self.worker.pid
                    if self.worker and self.worker.poll() is None
                    else None
                ),
                "worker_started_at": (
                    self.worker_started_at.isoformat()
                    if self.worker_started_at
                    else None
                ),
                "restart_count": self.restart_count,
                "last_restart_reason": self.last_restart_reason,
                "last_restart_at": (
                    self.last_restart_at.isoformat()
                    if self.last_restart_at
                    else None
                ),
                "heartbeat_age_seconds": (
                    round(heartbeat_age, 2)
                    if heartbeat_age is not None
                    else None
                ),
                "heartbeat_timeout_seconds": HEARTBEAT_TIMEOUT_SECONDS,
                "suspect_reason": self.suspect_reason,
                "consecutive_failures": self.consecutive_failures,
                "consecutive_failures_required": CONSECUTIVE_FAILURES_REQUIRED,
            },
        )

    def start_worker(self) -> None:
        self.worker = subprocess.Popen(
            [
                sys.executable,
                str(RUNNER),
                "--station",
                self.station_id,
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        self.worker_started_at = utc_now()
        self.worker_started_monotonic = time.monotonic()
        self.suspect_reason = None
        self.consecutive_failures = 0
        self.write_status("starting")
        print(
            json.dumps(
                {
                    "event": "worker_started",
                    "station_id": self.station_id,
                    "worker_pid": self.worker.pid,
                    "restart_count": self.restart_count,
                }
            ),
            flush=True,
        )

    def restart_worker(self, reason: str) -> None:
        if self.worker and self.worker.poll() is None:
            kill_tree(self.worker.pid)
        self.restart_count += 1
        self.last_restart_reason = reason
        self.last_restart_at = utc_now()
        self.write_status("restarting")
        time.sleep(min(5.0, 0.5 + self.restart_count * 0.25))
        self.start_worker()

    def unhealthy_reason(self) -> str | None:
        if not self.worker:
            return "worker_missing"
        exit_code = self.worker.poll()
        if exit_code is not None:
            return f"worker_exited_{exit_code}"
        if time.monotonic() - self.worker_started_monotonic < STARTUP_GRACE_SECONDS:
            return None
        status = read_json(self.station_status_path)
        heartbeat = parse_time(status.get("last_audio_write_at"))
        if heartbeat is None:
            return "audio_heartbeat_missing"
        if self.worker_started_at and heartbeat < self.worker_started_at:
            return "audio_heartbeat_not_started"
        encoder_pid = status.get("encoder_pid")
        if encoder_pid and not process_alive(encoder_pid):
            return "encoder_exited"
        age = (utc_now() - heartbeat).total_seconds()
        if age > HEARTBEAT_TIMEOUT_SECONDS:
            return f"audio_heartbeat_stale_{round(age, 1)}s"
        return None

    def run(self) -> None:
        self.start_worker()
        try:
            while True:
                reason = self.unhealthy_reason()
                if reason:
                    if reason == self.suspect_reason:
                        self.consecutive_failures += 1
                    else:
                        self.suspect_reason = reason
                        self.consecutive_failures = 1
                    if self.consecutive_failures >= CONSECUTIVE_FAILURES_REQUIRED:
                        self.restart_worker(reason)
                    else:
                        self.write_status("suspect")
                else:
                    self.suspect_reason = None
                    self.consecutive_failures = 0
                    self.write_status("live")
                time.sleep(POLL_SECONDS)
        finally:
            if self.worker and self.worker.poll() is None:
                kill_tree(self.worker.pid)
            self.write_status("stopped")
            self.supervisor_pid_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--station",
        choices=("radiotedu-en", "radiotedu-fr"),
        required=True,
    )
    args = parser.parse_args()
    supervisor = StationSupervisor(args.station)
    try:
        supervisor.run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
