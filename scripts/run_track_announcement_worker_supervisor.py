"""Keep the local Qwen track-announcement worker alive across recoverable failures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts" / "run_track_announcement_worker.py"
RUNTIME = ROOT / "data" / "runtime" / "qwen-track-announcements"
STATUS = RUNTIME / "supervisor-status.json"
PID_PATH = RUNTIME / "supervisor.pid"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def main() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="ascii")
    restart_count = 0
    worker: subprocess.Popen | None = None
    worker_started_at: str | None = None
    stdout = (RUNTIME / "worker.out.log").open("a", encoding="utf-8")
    stderr = (RUNTIME / "worker.err.log").open("a", encoding="utf-8")
    try:
        while True:
            if worker is None or worker.poll() is not None:
                previous_exit = worker.returncode if worker is not None else None
                if worker is not None:
                    restart_count += 1
                    time.sleep(min(300, 10 * restart_count))
                worker = subprocess.Popen(
                    [
                        sys.executable,
                        str(WORKER),
                        "--startup-delay",
                        "30" if restart_count else "0",
                        "--between-items",
                        "60",
                        "--empty-poll-seconds",
                        "300",
                    ],
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    creationflags=CREATE_NO_WINDOW,
                )
                worker_started_at = utc_now()
                atomic_json(
                    STATUS,
                    {
                        "state": "starting",
                        "updated_at": utc_now(),
                        "supervisor_pid": os.getpid(),
                        "worker_pid": worker.pid,
                        "worker_started_at": worker_started_at,
                        "restart_count": restart_count,
                        "previous_exit_code": previous_exit,
                    },
                )
            atomic_json(
                STATUS,
                {
                    "state": "live",
                    "updated_at": utc_now(),
                    "supervisor_pid": os.getpid(),
                    "worker_pid": worker.pid,
                    "worker_started_at": worker_started_at,
                    "restart_count": restart_count,
                    "previous_exit_code": None,
                },
            )
            time.sleep(5)
    finally:
        if worker is not None and worker.poll() is None:
            kill_tree(worker.pid)
        atomic_json(
            STATUS,
            {
                "state": "stopped",
                "updated_at": utc_now(),
                "supervisor_pid": os.getpid(),
                "worker_pid": None,
                "worker_started_at": worker_started_at,
                "restart_count": restart_count,
            },
        )
        PID_PATH.unlink(missing_ok=True)
        stdout.close()
        stderr.close()


if __name__ == "__main__":
    raise SystemExit(main())
