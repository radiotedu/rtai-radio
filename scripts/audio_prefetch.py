"""Decode the actual rolling program ahead to bounded disk-backed PCM."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import BinaryIO, Callable, Iterator
from pcm_edges import POLICY as PCM_EDGE_POLICY, trim_pcm_file


class PcmPrefetch:
    def __init__(self, *, root: Path, command: Callable[[Path], list[str]],
                 stop: threading.Event, creationflags: int = 0, max_items: int = 8) -> None:
        self.root = root
        self.command = command
        self.stop = stop
        self.creationflags = creationflags
        self.max_items = max_items
        self.closed = threading.Event()
        self.condition = threading.Condition()
        self.wanted: dict[str, tuple[Path, list[str]]] = {}
        self.ready: dict[str, Path] = {}
        self.leases: dict[str, int] = {}
        self.retry_at: dict[str, float] = {}
        self.active_key = ""
        self.last_error = ""
        self.hits = 0
        self.misses = 0
        self.edge_trim_count = 0
        self.edge_seconds_removed = 0.0
        self.reused_cache_count = 0
        self.thread = threading.Thread(target=self._run, name=f"pcm-prefetch-{root.name}", daemon=True)

    def _identity(self, item: Path) -> tuple[str, Path, list[str]]:
        item = item.resolve()
        stat = item.stat()
        command = self.command(item)
        material = [str(item), stat.st_size, stat.st_mtime_ns, command]
        key = hashlib.sha256(json.dumps(material, ensure_ascii=False).encode("utf-8")).hexdigest()
        return key, item, command

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # Completed output is atomic and keyed by source size/mtime and the
        # full decoder command. Preserve it for the resumed genuine horizon.
        for pattern in ("*.part", "*.stderr"):
            for stale in self.root.glob(pattern):
                stale.unlink(missing_ok=True)
        self.thread.start()

    def set_horizon(self, items: list[Path]) -> None:
        wanted = {}
        for item in items[:self.max_items]:
            try:
                key, source, command = self._identity(item)
            except OSError:
                continue
            wanted[key] = (source, command)
        with self.condition:
            self.wanted = wanted
            self._evict_locked()
            self.condition.notify_all()

    def _evict_locked(self) -> None:
        for key in list(self.ready):
            if key not in self.wanted and not self.leases.get(key):
                try:
                    self.ready[key].unlink(missing_ok=True)
                except OSError:
                    continue
                del self.ready[key]
        for cached in self.root.glob("*.s16le"):
            key = cached.stem
            if key not in self.wanted and key != self.active_key and not self.leases.get(key):
                try:
                    cached.unlink(missing_ok=True)
                except OSError:
                    pass
        self.retry_at = {key: value for key, value in self.retry_at.items() if key in self.wanted}

    @contextmanager
    def open_ready(self, item: Path) -> Iterator[BinaryIO | None]:
        stream = None
        key = ""
        try:
            key, _, _ = self._identity(item)
            with self.condition:
                path = self.ready.get(key)
                if path is not None:
                    stream = path.open("rb")
                    self.leases[key] = self.leases.get(key, 0) + 1
                    self.hits += 1
                else:
                    self.misses += 1
        except OSError:
            with self.condition:
                self.misses += 1
        try:
            yield stream
        finally:
            if stream is not None:
                stream.close()
                with self.condition:
                    self.leases[key] -= 1
                    if not self.leases[key]:
                        del self.leases[key]
                    self._evict_locked()

    def snapshot(self) -> dict[str, object]:
        with self.condition:
            return {
                "mode": "disk-backed-pcm-prefetch", "target_count": len(self.wanted),
                "ready_count": sum(key in self.ready for key in self.wanted),
                "decoding": bool(self.active_key), "cache_hits": self.hits,
                "boundary_cache_misses": self.misses, "last_error": self.last_error,
                "edge_trim_policy": PCM_EDGE_POLICY,
                "edge_trim_count": self.edge_trim_count,
                "edge_seconds_removed": self.edge_seconds_removed,
                "reused_cache_count": self.reused_cache_count,
            }

    def wait_ready(self, item: Path, timeout: float) -> bool:
        key, _, _ = self._identity(item)
        deadline = time.monotonic() + timeout
        with self.condition:
            while key not in self.ready and not self.stop.is_set() and not self.closed.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(min(remaining, 0.5))
            return key in self.ready

    def _run(self) -> None:
        while not self.stop.is_set() and not self.closed.is_set():
            with self.condition:
                now = time.monotonic()
                jobs = [(key, job) for key, job in self.wanted.items()
                        if key not in self.ready and self.retry_at.get(key, 0) <= now]
                if not jobs:
                    self.condition.wait(0.5)
                    continue
                key, (_, command) = jobs[0]
                self.active_key = key
            process = None
            temporary = self.root / f"{key}.part"
            errors = self.root / f"{key}.stderr"
            target = self.root / f"{key}.s16le"
            try:
                if target.is_file() and target.stat().st_size > 0 and target.stat().st_size % 4 == 0:
                    # Reapply the current edge policy before publishing an old
                    # complete file. This is idempotent for already-trimmed PCM.
                    edge_result = trim_pcm_file(target)
                    with self.condition:
                        if key in self.wanted:
                            self.ready[key] = target
                            self.reused_cache_count += 1
                            if edge_result["trimmed"]:
                                self.edge_trim_count += 1
                                self.edge_seconds_removed += float(edge_result["leading_seconds_removed"]) + float(edge_result["trailing_seconds_removed"])
                            self.last_error = ""
                        self.condition.notify_all()
                    continue
                with temporary.open("wb") as output, errors.open("wb") as stderr:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                               stdout=output, stderr=stderr,
                                               creationflags=self.creationflags)
                    deadline = time.monotonic() + 120
                    while process.poll() is None:
                        if self.stop.is_set() or self.closed.is_set():
                            raise RuntimeError("PCM preparation stopped")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("PCM preparation exceeded 120 seconds")
                        try:
                            process.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            pass
                if process.returncode != 0:
                    detail = errors.read_text(encoding="utf-8", errors="replace")[-600:]
                    raise RuntimeError(f"PCM decoder exited {process.returncode}: {detail}")
                size = temporary.stat().st_size
                if size == 0 or size % 4:
                    raise RuntimeError("PCM decoder produced empty or unaligned stereo audio")
                edge_result = trim_pcm_file(temporary)
                with self.condition:
                    if key in self.wanted:
                        temporary.replace(target)
                        self.ready[key] = target
                        if edge_result["trimmed"]:
                            self.edge_trim_count += 1
                            self.edge_seconds_removed += float(edge_result["leading_seconds_removed"]) + float(edge_result["trailing_seconds_removed"])
                        self.last_error = ""
                    self.condition.notify_all()
            except Exception as exc:
                with self.condition:
                    self.last_error = f"{type(exc).__name__}: {exc}"[:240]
                    self.retry_at[key] = time.monotonic() + 15
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                temporary.unlink(missing_ok=True)
                errors.unlink(missing_ok=True)
                with self.condition:
                    self.active_key = ""
                    self._evict_locked()
                    self.condition.notify_all()

    def close(self) -> None:
        self.closed.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread.is_alive():
            self.thread.join(timeout=8)
        with self.condition:
            self.wanted.clear()
            self._evict_locked()
