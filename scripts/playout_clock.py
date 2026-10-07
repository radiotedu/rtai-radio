"""Track PCM item boundaries using MP3 frames actually sent to Icecast."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import queue
import threading
import time
from typing import Callable


class Mp3FrameClock:
    """Incremental MPEG Layer III frame reader, including split ID3 headers."""
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.samples = 0
        self.frames = 0
        self.sample_rate: int | None = None
        self.skip = 0

    def feed(self, audio: bytes) -> int:
        self.buffer.extend(audio)
        while True:
            if self.skip:
                count = min(self.skip, len(self.buffer))
                del self.buffer[:count]
                self.skip -= count
                if self.skip:
                    break
            if len(self.buffer) < 4:
                break
            if self.buffer[:3] == b"ID3":
                if len(self.buffer) < 10:
                    break
                size = 0
                for byte in self.buffer[6:10]:
                    size = (size << 7) | (byte & 0x7f)
                self.skip = 10 + size + (10 if self.buffer[5] & 0x10 else 0)
                continue
            header = int.from_bytes(self.buffer[:4], "big")
            version = (header >> 19) & 3
            layer = (header >> 17) & 3
            rate_index = (header >> 10) & 3
            bitrate_index = (header >> 12) & 15
            if (header >> 21 != 0x7ff or version == 1 or layer != 1
                    or rate_index == 3 or bitrate_index in (0, 15)):
                del self.buffer[0]
                continue
            rates = (44100, 48000, 32000)
            rate = rates[rate_index] // (1 if version == 3 else 2 if version == 2 else 4)
            bitrates = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
                        if version == 3 else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))
            size = (144 if version == 3 else 72) * bitrates[bitrate_index] * 1000 // rate
            size += (header >> 9) & 1
            if len(self.buffer) < size:
                break
            del self.buffer[:size]
            self.sample_rate = rate
            self.samples += 1152 if version == 3 else 576
            self.frames += 1
        return self.samples


@dataclass
class AirSegment:
    start_byte: int
    metadata: dict[str, object]
    end_byte: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    callbacks: list[Callable[["AirSegment"], None]] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return int(max(0, (self.end_byte or self.start_byte) - self.start_byte) / 192)


class PlayoutClock:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.offered_bytes = 0
        self.sent_pcm_bytes = 0
        self.segments: deque[AirSegment] = deque()
        self.current: dict[str, object] | None = None
        self.revision = 0
        self.last_publication_error = ""
        self.retry_publication_at = 0.0
        self.actions: queue.SimpleQueue[tuple[Callable[[AirSegment], None], AirSegment]] = queue.SimpleQueue()

    def begin(self, metadata: dict[str, object]) -> AirSegment:
        with self.lock:
            segment = AirSegment(self.offered_bytes, dict(metadata))
            self.segments.append(segment)
            return segment

    def offered(self, size: int) -> None:
        with self.lock:
            self.offered_bytes += size

    def finish(self, segment: AirSegment) -> None:
        with self.lock:
            segment.end_byte = self.offered_bytes

    def on_complete(self, segment: AirSegment, callback: Callable[[AirSegment], None]) -> None:
        with self.lock:
            if segment.completed_at is not None:
                self.actions.put((callback, segment))
            else:
                segment.callbacks.append(callback)

    def advance(self, sent_pcm_bytes: int, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        with self.lock:
            self.sent_pcm_bytes = max(self.sent_pcm_bytes, sent_pcm_bytes)
            while self.segments:
                segment = self.segments[0]
                if self.sent_pcm_bytes <= segment.start_byte:
                    break
                if segment.started_at is None:
                    segment.started_at = now
                    self.current = {**segment.metadata, "started_at": now.isoformat()}
                    self.revision += 1
                if segment.end_byte is None or self.sent_pcm_bytes < segment.end_byte:
                    break
                segment.completed_at = now
                self.segments.popleft()
                for callback in segment.callbacks:
                    self.actions.put((callback, segment))
                segment.callbacks.clear()

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "basis": "sent-mp3-frame-samples", "revision": self.revision,
                "now_playing": dict(self.current) if self.current is not None else None,
                "offered_pcm_seconds": self.offered_bytes / 192000,
                "sent_pcm_seconds": self.sent_pcm_bytes / 192000,
                "queued_pcm_seconds": max(0, self.offered_bytes - self.sent_pcm_bytes) / 192000,
                "publication_error": self.last_publication_error,
            }

    def dispatch(self) -> None:
        if time.monotonic() < self.retry_publication_at:
            return
        while True:
            try:
                callback, segment = self.actions.get_nowait()
            except queue.Empty:
                return
            try:
                callback(segment)
                self.last_publication_error = ""
            except Exception as exc:
                # Retry publication outside the sender without restarting audio.
                self.actions.put((callback, segment))
                self.last_publication_error = f"{type(exc).__name__}: {exc}"[:240]
                self.retry_publication_at = time.monotonic() + 5
                return
