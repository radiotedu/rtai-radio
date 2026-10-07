"""Retain consumed PCM until its encoded frames reach the source socket."""
from collections import deque
import threading


class PcmReplayBuffer:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.chunks: deque[tuple[int, bytes]] = deque()
        self.consumed_bytes = 0
        self.confirmed_bytes = 0
        self.generation = 0

    def append(self, pcm: bytes) -> None:
        if len(pcm) % 4:
            raise ValueError("stereo s16le PCM must contain complete samples")
        with self.lock:
            self.chunks.append((self.consumed_bytes, pcm))
            self.consumed_bytes += len(pcm)

    def begin_cycle(self) -> tuple[int, int, deque[bytes]]:
        with self.lock:
            self.generation += 1
            return self.generation, self.confirmed_bytes, deque(data for _, data in self.chunks)

    def confirm(self, generation: int, pcm_bytes: int) -> int | None:
        with self.lock:
            if generation != self.generation:
                return None
            confirmed = min(self.consumed_bytes, max(self.confirmed_bytes, pcm_bytes))
            confirmed -= confirmed % 4
            self.confirmed_bytes = confirmed
            while self.chunks:
                start, pcm = self.chunks[0]
                end = start + len(pcm)
                if end <= confirmed:
                    self.chunks.popleft()
                elif start < confirmed:
                    self.chunks[0] = (confirmed, pcm[confirmed - start:])
                    break
                else:
                    break
            return confirmed

    def snapshot(self) -> dict[str, int | float]:
        with self.lock:
            return {
                "generation": self.generation,
                "consumed_pcm_seconds": self.consumed_bytes / 192000,
                "confirmed_pcm_seconds": self.confirmed_bytes / 192000,
                "retained_pcm_seconds": (self.consumed_bytes - self.confirmed_bytes) / 192000,
                "retained_chunks": len(self.chunks),
            }
