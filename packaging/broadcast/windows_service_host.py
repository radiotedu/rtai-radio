"""Native PyWin32 hosts for the two RadioTEDU Windows services.

The service process itself uses the machine Python 3.12 PyWin32 runtime.  Each
host launches the repository's PowerShell runner, which in turn uses the
isolated project virtual environment for application code.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time


USER_SITE_ROOTS = {
    Path.home() / "AppData" / "Roaming" / "Python" / "Python312" / "site-packages"
}
users_root = Path(os.environ.get("SystemDrive", "C:")) / "Users"
if users_root.is_dir():
    USER_SITE_ROOTS.update(
        users_root.glob("*/AppData/Roaming/Python/Python312/site-packages")
    )
for user_site in sorted(USER_SITE_ROOTS):
    for candidate in (
        user_site,
        user_site / "win32",
        user_site / "win32" / "lib",
        user_site / "pythonwin",
        user_site / "pywin32_system32",
    ):
        if candidate.exists():
            text = str(candidate)
            if text not in sys.path:
                sys.path.insert(0, text)
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(text)
                except OSError:
                    pass

import servicemanager
import win32event
import win32service
import win32serviceutil


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = Path(os.environ.get("RADIOTEDU_CONFIG_ROOT", r"C:\ProgramData\RadioTEDU\config"))
RUNNER = PROJECT_ROOT / "packaging" / "broadcast" / "run-service.ps1"
RUNTIME_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
LOG_ROOT = PROJECT_ROOT / "data" / "service-logs"
CHILD_RESTART_DELAYS_SECONDS = (5, 15, 30, 60)
CHILD_RESTART_RESET_SECONDS = 10 * 60


class _RadioTEDUService(win32serviceutil.ServiceFramework):
    service_name: str

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.child: subprocess.Popen[bytes] | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogInfoMsg(f"{self._svc_display_name_} starting")
        try:
            self._run_child()
        finally:
            self._stop_child()
            servicemanager.LogInfoMsg(f"{self._svc_display_name_} stopped")

    def _run_child(self) -> None:
        """Keep the long-running runner alive until Windows stops the service."""

        restart_index = 0
        while win32event.WaitForSingleObject(self.stop_event, 0) != win32event.WAIT_OBJECT_0:
            started_at = time.monotonic()
            return_code = self._run_child_once()
            if win32event.WaitForSingleObject(self.stop_event, 0) == win32event.WAIT_OBJECT_0:
                return
            if time.monotonic() - started_at >= CHILD_RESTART_RESET_SECONDS:
                restart_index = 0
            delay = CHILD_RESTART_DELAYS_SECONDS[
                min(restart_index, len(CHILD_RESTART_DELAYS_SECONDS) - 1)
            ]
            restart_index += 1
            servicemanager.LogErrorMsg(
                f"{self.service_name} runner exited with {return_code}; retrying in {delay}s"
            )
            if (
                win32event.WaitForSingleObject(self.stop_event, delay * 1000)
                == win32event.WAIT_OBJECT_0
            ):
                return

    def _run_child_once(self) -> int:
        if not RUNNER.is_file():
            raise FileNotFoundError(str(RUNNER))
        if not RUNTIME_PYTHON.is_file():
            raise FileNotFoundError(str(RUNTIME_PYTHON))
        LOG_ROOT.mkdir(parents=True, exist_ok=True)
        stdout_path = LOG_ROOT / f"{self.service_name}.out.log"
        stderr_path = LOG_ROOT / f"{self.service_name}.err.log"
        command = [
            os.environ.get("SystemRoot", r"C:\Windows")
            + r"\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUNNER),
            "-ServiceName",
            self.service_name,
            "-ProjectRoot",
            str(PROJECT_ROOT),
            "-ConfigRoot",
            str(CONFIG_ROOT),
            "-Python",
            str(RUNTIME_PYTHON),
        ]
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            self.child = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=stdout,
                stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            while True:
                if self.child.poll() is not None:
                    return_code = int(self.child.returncode or 0)
                    self.child = None
                    return return_code
                if win32event.WaitForSingleObject(self.stop_event, 1000) == win32event.WAIT_OBJECT_0:
                    return 0

    def _stop_child(self) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        subprocess.run(
            ["taskkill.exe", "/PID", str(child.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=20,
        )
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)


class RadioTEDUSharedAIService(_RadioTEDUService):
    _svc_name_ = "RadioTEDU.SharedAI"
    _svc_display_name_ = "RadioTEDU Shared AI"
    _svc_description_ = "Loopback-only Ollama and approved Qwen TTS runtime."
    service_name = _svc_name_


class RadioTEDUBroadcastSupervisorService(_RadioTEDUService):
    _svc_name_ = "RadioTEDU.BroadcastSupervisor"
    _svc_display_name_ = "RadioTEDU Broadcast Supervisor"
    _svc_description_ = "Supervises isolated English and French RadioTEDU station children."
    service_name = _svc_name_


SERVICE_CLASSES = {
    "shared-ai": RadioTEDUSharedAIService,
    "broadcast-supervisor": RadioTEDUBroadcastSupervisorService,
}


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in SERVICE_CLASSES:
        choices = "|".join(SERVICE_CLASSES)
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} <{choices}> install|update|remove|start|stop")
    selector = sys.argv.pop(1)
    win32serviceutil.HandleCommandLine(SERVICE_CLASSES[selector])


if __name__ == "__main__":
    main()
