"""Bridge older live code until its next restart, using generated PCM only."""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import time
from datetime import datetime, timezone
import psutil
from pcm_edges import POLICY, trim_pcm_file


def unleased_on_windows(path: Path) -> bool:
    """Avoid copying files the broadcaster is already reading."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        return False
    close(handle)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--broadcast-pid", type=int, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    parent = psutil.Process(args.broadcast_pid)
    birth = parent.create_time()
    if "run_ai_quality_supervisor.py" not in " ".join(parent.cmdline()):
        raise ValueError("target PID is not the live AI broadcast supervisor")
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except (psutil.AccessDenied, AttributeError):
        pass
    root = args.root.resolve(strict=True)
    report = {"policy": POLICY, "broadcast_pid": args.broadcast_pid,
              "broadcast_created_at": birth, "helper_pid": psutil.Process().pid,
              "state": "running", "trim_count": 0, "seconds_removed": 0.0,
              "recent_changes": [], "last_error": ""}
    seen = {}

    def save() -> None:
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = args.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(args.state)

    save()
    try:
        while parent.is_running() and parent.create_time() == birth:
            for path in root.glob("radiotedu-*/*.s16le"):
                try:
                    stamp = path.stat()
                    signature = (stamp.st_size, stamp.st_mtime_ns)
                    if seen.get(path) == signature:
                        continue
                    if not unleased_on_windows(path):
                        continue
                    result = trim_pcm_file(path)
                    stamp = path.stat()
                    seen[path] = (stamp.st_size, stamp.st_mtime_ns)
                    if result["trimmed"]:
                        report["trim_count"] += 1
                        report["seconds_removed"] += result["leading_seconds_removed"] + result["trailing_seconds_removed"]
                        report["recent_changes"].append({"station": path.parent.name, "cache_key": path.stem, **result})
                        del report["recent_changes"][:-100]
                        report["last_error"] = ""
                        save()
                except (PermissionError, FileNotFoundError):
                    # Open playout leases and concurrent cache eviction are expected.
                    continue
                except OSError as exc:
                    report["last_error"] = f"{type(exc).__name__}: {exc}"[:240]
                    save()
            seen = {path: stamp for path, stamp in seen.items() if path.exists()}
            time.sleep(0.5)
    except psutil.NoSuchProcess:
        pass
    finally:
        report["state"] = "broadcast_process_ended"
        save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
