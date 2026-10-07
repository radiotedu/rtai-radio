"""Trim only long quiet file edges; retain every interior PCM sample."""
from __future__ import annotations

from array import array
import os
from pathlib import Path
import sys
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Iterator

PCM_RATE = 48000
FRAME_BYTES = 4  # stereo signed 16-bit little endian
THRESHOLD = int(32767 * 10 ** (-55 / 20))
MIN_EDGE_BYTES = int(0.5 * PCM_RATE) * FRAME_BYTES
PAD_BYTES = int(0.05 * PCM_RATE) * FRAME_BYTES
POLICY = "pcm-edges-minus55db-half-second-keep50ms-v1"


def _samples(data: bytes) -> array:
    samples = array("h", data)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _audible(data: bytes) -> bool:
    samples = _samples(data)
    return bool(samples) and (max(samples) > THRESHOLD or min(samples) < -THRESHOLD)


def pcm_bounds(stream: BinaryIO) -> tuple[int, int]:
    """Find audible edges in a seekable file, retaining short edges and fades."""
    stream.seek(0, 2)
    size = stream.tell()
    if size % FRAME_BYTES:
        raise ValueError("unaligned stereo PCM")
    first = None
    for offset in range(0, size, 65536):
        stream.seek(offset)
        samples = _samples(stream.read(min(65536, size - offset)))
        if samples and (max(samples) > THRESHOLD or min(samples) < -THRESHOLD):
            index = next(i for i, sample in enumerate(samples) if abs(sample) > THRESHOLD)
            first = offset + (index // 2) * FRAME_BYTES
            break
    if first is None:
        return 0, size  # Never erase a wholly quiet recording as an empty item.
    end = size
    while end:
        offset = max(0, end - 65536)
        stream.seek(offset)
        samples = _samples(stream.read(end - offset))
        if samples and (max(samples) > THRESHOLD or min(samples) < -THRESHOLD):
            index = next(i for i in range(len(samples) - 1, -1, -1) if abs(samples[i]) > THRESHOLD)
            last = offset + (index // 2 + 1) * FRAME_BYTES
            break
        end = offset
    start = max(0, first - PAD_BYTES) if first >= MIN_EDGE_BYTES else 0
    finish = min(size, last + PAD_BYTES) if size - last >= MIN_EDGE_BYTES else size
    return start, finish


def trim_pcm_file(path: Path) -> dict[str, object]:
    """Atomically replace generated PCM only; an open Windows lease blocks it."""
    stamp = path.stat()
    temporary = path.with_name(path.name + f".edge-{os.getpid()}.part")
    try:
        with path.open("rb") as source:
            start, finish = pcm_bounds(source)
            if start == 0 and finish == stamp.st_size:
                return {"trimmed": False, "policy": POLICY}
            source.seek(start)
            remaining = finish - start
            with temporary.open("wb") as output:
                while remaining:
                    data = source.read(min(1048576, remaining))
                    if not data:
                        raise OSError("PCM file changed during edge preparation")
                    output.write(data)
                    remaining -= len(data)
        current = path.stat()
        if current.st_size != stamp.st_size or current.st_mtime_ns != stamp.st_mtime_ns:
            raise OSError("PCM file changed before edge replacement")
        temporary.replace(path)
        return {"trimmed": True, "policy": POLICY,
                "leading_seconds_removed": start / (PCM_RATE * FRAME_BYTES),
                "trailing_seconds_removed": (stamp.st_size - finish) / (PCM_RATE * FRAME_BYTES),
                "original_pcm_bytes": stamp.st_size, "played_pcm_bytes": finish - start}
    finally:
        temporary.unlink(missing_ok=True)


def iter_trimmed_pcm(stream: BinaryIO, chunk_bytes: int) -> Iterator[bytes]:
    """Pipe fallback: hold quiet runs until known to be an edge or interior."""
    leading = True
    carry = b""
    with SpooledTemporaryFile(max_size=2 * 1024 * 1024) as quiet:
        quiet_size = 0
        while True:
            data = stream.read(chunk_bytes)
            if not data:
                break
            data = carry + data
            aligned = len(data) // FRAME_BYTES * FRAME_BYTES
            data, carry = data[:aligned], data[aligned:]
            if not data:
                continue
            if not _audible(data):
                quiet.write(data)
                quiet_size += len(data)
                continue
            quiet.seek(max(0, quiet_size - PAD_BYTES) if leading and quiet_size >= MIN_EDGE_BYTES else 0)
            while buffered := quiet.read(chunk_bytes):
                yield buffered
            quiet.seek(0)
            quiet.truncate()
            quiet_size = 0
            leading = False
            yield data
        if carry:
            raise ValueError("decoder ended with unaligned stereo PCM")
        quiet.seek(0)
        remaining = quiet_size if leading or quiet_size < MIN_EDGE_BYTES else PAD_BYTES
        while remaining:
            buffered = quiet.read(min(chunk_bytes, remaining))
            if not buffered:
                break
            remaining -= len(buffered)
            yield buffered
