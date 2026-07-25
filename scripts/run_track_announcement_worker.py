"""Render queued track intros locally without touching playout or public APIs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_ROOT = ROOT / "data" / "runtime" / "qwen-track-announcements"
STATION_RUNTIME = ROOT / "data" / "runtime" / "temporary-dual-station"
WORKER_STATUS = MANIFEST_ROOT / "worker-status.json"
FAILURE_STATE = MANIFEST_ROOT / "worker-failures.json"
MODEL = Path(
    r"C:\Program Files\RadioTEDU Broadcast Wall\_internal\models"
    r"\qwen3-tts-0.6b-customvoice"
)
STATIONS = ("radiotedu-en", "radiotedu-fr")
LANGUAGES = {
    "radiotedu-en": "English",
    "radiotedu-fr": "French",
}
INSTRUCTIONS = {
    "radiotedu-en": (
        "Warm, clear, conversational adult radio host. Natural English delivery, "
        "calm and precise, never promotional. Say Radio TED U distinctly and "
        "preserve artist and song names exactly."
    ),
    "radiotedu-fr": (
        "Voix adulte chaleureuse, claire et conversationnelle. Présentation "
        "radiophonique naturelle en français, posée et précise, sans emphase "
        "publicitaire. Prononcer Radio TED U distinctement et respecter "
        "exactement les noms propres."
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(event: str, **fields: object) -> None:
    print(
        json.dumps({"at": utc_now(), "event": event, **fields}, ensure_ascii=False),
        flush=True,
    )


def set_background_priority() -> None:
    if os.name != "nt":
        return
    import psutil

    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def identity(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def live_priority(station_id: str) -> dict[tuple[str, str], int]:
    status = read_json(STATION_RUNTIME / station_id / "status.json")
    ordered = [status.get("now_playing") or {}, *list(status.get("next") or [])]
    return {
        (identity(item.get("title")), identity(item.get("artist"))): index
        for index, item in enumerate(ordered)
        if identity(item.get("title"))
    }


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


def write_worker_status(
    state: str,
    *,
    rendered: int,
    current_station: str | None = None,
    current_cache_key: str | None = None,
    failures: int = 0,
) -> None:
    atomic_json(
        WORKER_STATUS,
        {
            "state": state,
            "updated_at": utc_now(),
            "pid": os.getpid(),
            "priority": "below_normal",
            "cpu_threads": 2,
            "rendered_this_run": rendered,
            "current_station": current_station,
            "current_cache_key": current_cache_key,
            "deferred_failures": failures,
        },
    )


def station_audio_is_fresh() -> bool:
    now = datetime.now(timezone.utc)
    for station_id in STATIONS:
        status = read_json(STATION_RUNTIME / station_id / "status.json")
        value = status.get("last_audio_write_at")
        if status.get("state") != "live" or not isinstance(value, str):
            return False
        try:
            heartbeat = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if (now - heartbeat).total_seconds() > 8:
            return False
    return True


def acquire_single_instance_lock():
    MANIFEST_ROOT.mkdir(parents=True, exist_ok=True)
    handle = (MANIFEST_ROOT / "worker.lock").open("a+b")
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            handle.close()
            raise SystemExit("another track-announcement worker is already running") from error
    return handle


def next_queued_entry(
    station_cursor: int,
    deferred_keys: set[str] | None = None,
) -> tuple[int, str, dict] | None:
    deferred_keys = deferred_keys or set()
    for offset in range(len(STATIONS)):
        index = (station_cursor + offset) % len(STATIONS)
        station_id = STATIONS[index]
        manifest = read_json(MANIFEST_ROOT / f"{station_id}.json")
        if (
            manifest.get("schema_version") != 1
            or manifest.get("station_id") != station_id
            or manifest.get("live_web_requests") is not False
        ):
            continue
        candidates: list[tuple[int, int, dict]] = []
        priority = live_priority(station_id)
        for manifest_index, entry in enumerate(manifest.get("entries", [])):
            if entry.get("state") != "queued":
                continue
            if str(entry.get("cache_key") or "") in deferred_keys:
                continue
            relative = Path(str(entry.get("asset_path") or ""))
            target = (MANIFEST_ROOT / relative).resolve()
            try:
                target.relative_to((MANIFEST_ROOT / station_id).resolve())
            except ValueError:
                continue
            if target.name != f"{entry.get('cache_key')}.wav":
                continue
            rank = priority.get(
                (identity(entry.get("title")), identity(entry.get("artist"))),
                100_000,
            )
            candidates.append((rank, manifest_index, entry))
        if candidates:
            _, _, entry = min(candidates, key=lambda item: (item[0], item[1]))
            return (index, station_id, entry)
    return None


def active_failures() -> tuple[dict[str, dict], set[str]]:
    payload = read_json(FAILURE_STATE)
    failures = payload.get("failures") if isinstance(payload, dict) else {}
    if not isinstance(failures, dict):
        failures = {}
    now = datetime.now(timezone.utc)
    deferred: set[str] = set()
    for key, value in failures.items():
        try:
            retry_at = datetime.fromisoformat(
                str(value.get("retry_at") or "").replace("Z", "+00:00")
            )
        except (AttributeError, ValueError):
            continue
        if retry_at > now:
            deferred.add(str(key))
    return failures, deferred


def record_failure(
    failures: dict[str, dict],
    *,
    station_id: str,
    cache_key: str,
    error_category: str,
) -> None:
    previous = failures.get(cache_key) if isinstance(failures.get(cache_key), dict) else {}
    attempts = int(previous.get("attempts") or 0) + 1
    delay_minutes = min(360, 15 * (2 ** min(attempts - 1, 5)))
    failures[cache_key] = {
        "station_id": station_id,
        "attempts": attempts,
        "error_category": error_category,
        "failed_at": utc_now(),
        "retry_at": (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).isoformat(),
    }
    atomic_json(
        FAILURE_STATE,
        {
            "schema_version": 1,
            "updated_at": utc_now(),
            "failures": failures,
        },
    )


def clear_failure(failures: dict[str, dict], cache_key: str) -> None:
    if failures.pop(cache_key, None) is None:
        return
    atomic_json(
        FAILURE_STATE,
        {
            "schema_version": 1,
            "updated_at": utc_now(),
            "failures": failures,
        },
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def mark_ready(station_id: str, cache_key: str, target: Path) -> None:
    manifest_path = MANIFEST_ROOT / f"{station_id}.json"
    payload = read_json(manifest_path)
    matched = False
    for entry in payload.get("entries", []):
        if entry.get("cache_key") == cache_key:
            entry["state"] = "ready"
            entry["audio_sha256"] = sha256_file(target)
            matched = True
            break
    if not matched:
        raise RuntimeError("rendered announcement disappeared from its manifest")
    payload["generated_at"] = utc_now()
    temporary = manifest_path.with_suffix(".json.partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for attempt in range(20):
        try:
            os.replace(temporary, manifest_path)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def validate_wav(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise RuntimeError("Qwen output is not mono PCM16")
        duration = audio.getnframes() / float(audio.getframerate())
    if not 0.25 <= duration <= 30:
        raise RuntimeError("Qwen output duration is outside the playout contract")
    return duration


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--startup-delay", type=float, default=120)
    parser.add_argument("--between-items", type=float, default=60)
    parser.add_argument("--empty-poll-seconds", type=float, default=300)
    parser.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="Zero means continue until the durable queue is empty.",
    )
    args = parser.parse_args()

    lock = acquire_single_instance_lock()
    set_background_priority()
    write_worker_status("starting", rendered=0)
    if args.startup_delay > 0:
        time.sleep(args.startup_delay)
    while not station_audio_is_fresh():
        emit("waiting_for_fresh_audio")
        write_worker_status("waiting_for_fresh_audio", rendered=0)
        time.sleep(30)

    import soundfile
    import torch
    from qwen_tts import Qwen3TTSModel

    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    emit("model_loading", cpu_threads=2, priority="below_normal")
    write_worker_status("model_loading", rendered=0)
    model = Qwen3TTSModel.from_pretrained(
        str(MODEL.resolve(strict=True)),
        device_map="cpu",
        dtype=torch.float32,
    )
    emit("model_ready")
    write_worker_status("ready", rendered=0)

    rendered = 0
    station_cursor = 0
    while args.max_items <= 0 or rendered < args.max_items:
        failures, deferred_keys = active_failures()
        work = next_queued_entry(station_cursor, deferred_keys)
        if work is None:
            emit(
                "queue_wait",
                rendered=rendered,
                deferred_failures=len(deferred_keys),
            )
            write_worker_status(
                "queue_wait",
                rendered=rendered,
                failures=len(deferred_keys),
            )
            if args.max_items > 0:
                break
            time.sleep(max(30, args.empty_poll_seconds))
            continue
        index, station_id, entry = work
        station_cursor = (index + 1) % len(STATIONS)
        while not station_audio_is_fresh():
            emit("paused_for_audio_health", station_id=station_id)
            write_worker_status(
                "paused_for_audio_health",
                rendered=rendered,
                current_station=station_id,
                current_cache_key=str(entry["cache_key"]),
                failures=len(deferred_keys),
            )
            time.sleep(30)

        target = (MANIFEST_ROOT / str(entry["asset_path"])).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".partial.wav")
        emit(
            "render_started",
            station_id=station_id,
            track_id=int(entry["track_id"]),
            cache_key=str(entry["cache_key"]),
        )
        write_worker_status(
            "rendering",
            rendered=rendered,
            current_station=station_id,
            current_cache_key=str(entry["cache_key"]),
            failures=len(deferred_keys),
        )
        try:
            waveforms, sample_rate = model.generate_custom_voice(
                text=str(entry["text"]),
                speaker="Ryan",
                language=LANGUAGES[station_id],
                instruct=INSTRUCTIONS[station_id],
            )
            soundfile.write(
                temporary,
                waveforms[0],
                int(sample_rate),
                subtype="PCM_16",
            )
            duration = validate_wav(temporary)
            os.replace(temporary, target)
            mark_ready(station_id, str(entry["cache_key"]), target)
            clear_failure(failures, str(entry["cache_key"]))
            rendered += 1
            emit(
                "render_complete",
                station_id=station_id,
                track_id=int(entry["track_id"]),
                duration_seconds=round(duration, 2),
                rendered=rendered,
            )
            write_worker_status(
                "ready",
                rendered=rendered,
                failures=len(active_failures()[1]),
            )
        except Exception as error:
            temporary.unlink(missing_ok=True)
            error_category = type(error).__name__
            record_failure(
                failures,
                station_id=station_id,
                cache_key=str(entry["cache_key"]),
                error_category=error_category,
            )
            emit(
                "render_deferred",
                station_id=station_id,
                track_id=int(entry["track_id"]),
                error_category=error_category,
            )
            write_worker_status(
                "ready",
                rendered=rendered,
                failures=len(active_failures()[1]),
            )
        if args.between_items > 0:
            time.sleep(args.between_items)

    del lock
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
