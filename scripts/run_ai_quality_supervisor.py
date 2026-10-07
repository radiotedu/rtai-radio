from __future__ import annotations

import argparse
from collections import deque
from contextlib import nullcontext
import hashlib
import hmac
import http.client
import json
import math
import os
import queue
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Callable
from zoneinfo import ZoneInfo

from mutagen import File as MutagenFile
from metadata_cleanup import clean_track_metadata
from selection_outbox_store import SelectionOutboxStore
from audio_prefetch import PcmPrefetch
from pcm_edges import iter_trimmed_pcm
from audio_thread_priority import set_audio_thread_priority
from pcm_replay_buffer import PcmReplayBuffer
from playout_clock import AirSegment, Mp3FrameClock, PlayoutClock

from dynamic_host_queue import (
    DynamicHostQueue,
    normalize_station_name,
    normalize_tts_text,
)
from transition_liner_library import TransitionLinerLibrary, canonical_genre

from run_ai_stream_supervisor import (
    IcecastSource,
    OutputPacer,
    StationConfig,
    SupervisorConfig,
    _drain_stderr,
    autonomous_rotation,
    autonomous_selection_status,
    assert_autonomous_song_ai_ready,
    broadcast_metadata_is_safe,
    deterministic_rotation,
    discover_audio,
    load_config,
    LIVE_LAYA_QUEUE_DEPTH,
    LIVE_LAYA_STARTUP_TRACKS,
    SONG_SELECTION_LOCK,
    mark_autonomous_program_restored,
    mark_prebaked_transition_program,
    resolve_source_password,
    start_station_health_servers,
    write_status_atomic,
    write_playlist,
)


RECONNECT_DELAYS_SECONDS = (1, 2, 4, 8, 15, 30)
ORIGIN_PROBE_INITIAL_DELAY_SECONDS = 15
ORIGIN_PROBE_MAX_DELAY_SECONDS = 120
# A single Icecast listener-probe timeout must not turn a healthy, audio-sending
# station into a false outage.  Keep the last successful probe during this
# short grace window while continuing to probe and preserving the error detail.
ORIGIN_PROBE_GRACE_SECONDS = 60
LIVE_CATALOG_REFRESH_SECONDS = 120
PCM_CHUNK_BYTES = 3_840  # 20 ms, signed 16-bit, stereo, 48 kHz.
PRE_HOST_SILENCE_MS = 0
POST_HOST_SILENCE_MS = 0
BETWEEN_SONGS_SILENCE_MS = 0
def _silence_bytes(duration_ms: int) -> bytes:
    if duration_ms <= 0:
        return b""
    samples = int(48000 * duration_ms / 1000)
    return b"\x00" * (samples * 4)
PRE_HOST_SILENCE = _silence_bytes(PRE_HOST_SILENCE_MS)
POST_HOST_SILENCE = _silence_bytes(POST_HOST_SILENCE_MS)
BETWEEN_SONGS_SILENCE = _silence_bytes(BETWEEN_SONGS_SILENCE_MS)
PUBLIC_PROTOCOL = "radiotedu-platform/v1"
MAX_PENDING_PUBLIC_PLAYS = 2_000
PUBLIC_PLAY_FLUSH_BATCH = 8
MAX_PENDING_PUBLIC_SPOKEN_SEGMENTS = 2_000
PUBLIC_SPOKEN_FLUSH_BATCH = 8
MAX_PENDING_PUBLIC_SELECTION_DECISIONS = 500
PUBLIC_SELECTION_FLUSH_BATCH = 8
PUBLIC_SELECTION_CONTRACT_PROBE_SECONDS = 60
MAX_PUBLIC_SELECTION_EVENT_BYTES = 2 * 1024 * 1024
MAX_PUBLIC_SELECTION_STATUS_BYTES = 48 * 1024 * 1024
PUBLIC_SOUND_TAGS = frozenset({"warm", "bright", "calm", "focused", "energetic"})
GENRE_SOUND_TAGS = {
    "blues": ("warm",),
    "classical": ("calm",),
    "electronic": ("energetic",),
    "folk": ("warm",),
    "hip hop": ("energetic",),
    "jazz": ("focused",),
    "lo fi": ("calm",),
    "pop": ("bright",),
    "rock": ("energetic",),
}
GENERIC_METADATA_FOLDERS = frozenset({
    "ai", "rights-cleared", "radiotedu-en", "radiotedu-fr", "voting",
    "blues", "classical", "electronic", "folk", "hip-hop", "jazz",
    "other", "pop", "rock", "unknown", "unspecified",
})
EDITORIAL_TIMEZONE = "Europe/Istanbul"


def current_editorial_program(
    station_id: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return the exact public schedule block for Ankara local time."""
    zone = ZoneInfo(EDITORIAL_TIMEZONE)
    local = now.astimezone(zone) if now is not None else datetime.now(zone)
    is_english = station_id.endswith("-en")
    weekend = local.weekday() >= 5

    if weekend and local.hour < 8:
        key = "weekend_night_signal"
    elif weekend and local.hour < 18:
        key = "weekend_signal"
    elif not weekend and local.hour < 6:
        key = "night_signal"
    elif not weekend and local.hour < 10:
        key = "tedu_dawn"
    elif not weekend and local.hour < 18:
        key = "campus_flow"
    else:
        key = "jazz_lab"

    programs = {
        "night_signal": {
            "en": ("Night Signal", "Late-night RadioTEDU flow", "00:00", "05:59"),
            "fr": ("Signal de nuit", "Flux nocturne de RadioTEDU", "00:00", "05:59"),
        },
        "tedu_dawn": {
            "en": ("TEDU Dawn", "Warm morning campus flow", "06:00", "09:59"),
            "fr": ("Aube TEDU", "Flux matinal chaleureux du campus", "06:00", "09:59"),
        },
        "campus_flow": {
            "en": ("Campus Flow", "Focused daytime campus flow", "10:00", "17:59"),
            "fr": ("Flux du campus", "Flux de journÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©e concentrÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â© du campus", "10:00", "17:59"),
        },
        "weekend_night_signal": {
            "en": ("Weekend Night Signal", "Late-night weekend flow", "00:00", "07:59"),
            "fr": (
                "Signal de nuit du week-end",
                "Flux nocturne du week-end",
                "00:00",
                "07:59",
            ),
        },
        "weekend_signal": {
            "en": ("Weekend Signal", "Relaxed weekend flow", "08:00", "17:59"),
            "fr": ("Signal du week-end", "Flux dÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©tendu du week-end", "08:00", "17:59"),
        },
        "jazz_lab": {
            "en": ("Jazz Lab", "Jazz-only evening laboratory", "18:00", "23:59"),
            "fr": (
                "Laboratoire jazz",
                "Laboratoire du soir exclusivement jazz",
                "18:00",
                "23:59",
            ),
        },
    }
    language = "en" if is_english else "fr"
    name, vibe, starts_at, ends_at = programs[key][language]
    required_genres = ("Jazz",) if key == "jazz_lab" else ()
    return {
        "id": key,
        "name": name,
        "vibe": vibe,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "timezone": EDITORIAL_TIMEZONE,
        "required_genres": required_genres,
        "sound_tags": ["calm"] if required_genres else ["focused"],
    }


def public_editorial_program(block: dict[str, object]) -> dict[str, object]:
    """Strip internal enforcement fields from the public program payload."""
    return {
        "id": str(block["id"]).replace("_", "-"),
        "name": block["name"],
        "vibe": block["vibe"],
        "sound_tags": list(block.get("sound_tags") or []),
    }


def next_editorial_program(
    station_id: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return the next published schedule block after the current one."""
    zone = ZoneInfo(EDITORIAL_TIMEZONE)
    local = now.astimezone(zone) if now is not None else datetime.now(zone)
    current = current_editorial_program(station_id, local)
    end_hour, end_minute = (int(value) for value in str(current["ends_at"]).split(":"))
    next_start = local.replace(
        hour=end_hour,
        minute=end_minute,
        second=0,
        microsecond=0,
    ) + timedelta(minutes=1)
    return public_editorial_program(current_editorial_program(station_id, next_start))


def _origin_probe_effective_ready(
    probe_succeeded: bool,
    last_ready_at: float | None,
    now_monotonic: float,
) -> bool:
    """Return listener readiness with a bounded timeout grace period."""
    if probe_succeeded:
        return True
    return (
        last_ready_at is not None
        and now_monotonic - last_ready_at <= ORIGIN_PROBE_GRACE_SECONDS
    )


def fallback_track_metadata(path: Path) -> tuple[str, str | None]:
    """Derive readable metadata from RadioTEDU's hash-prefixed library paths."""
    stem = re.sub(r"^[0-9a-fA-F]{16,64}-", "", path.stem).strip()
    parts = [part.strip() for part in stem.split(" - ")]
    if len(parts) >= 3 and parts[1].isdigit():
        title = " - ".join(parts[2:])
        artist = parts[0]
    elif len(parts) >= 2:
        artist = parts[0]
        title = " - ".join(parts[1:])
    else:
        title = re.sub(r"(?<=[a-zÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â -ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¿])(?=[A-Z])", " ", stem)
        title = re.sub(r"[._]+", " ", title)
        artist = ""
    title = re.sub(r"\s+", " ", title).strip(" -_")
    if not artist:
        for parent in path.parents:
            candidate = re.sub(r"\s+", " ", parent.name).strip()
            if candidate and candidate.casefold() not in GENERIC_METADATA_FOLDERS:
                artist = candidate
                break
    if artist:
        title = re.sub(
            rf"\s+(?:by|par)\s+{re.escape(artist)}[.!]?\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip()
    title, artist = clean_track_metadata(title, artist)
    return title[:200] or "Untitled", artist[:160] if artist else None


def track_metadata(path: Path) -> tuple[str, str | None]:
    """Return broadcast-safe title and artist without rewriting the media file."""
    try:
        media = MutagenFile(path, easy=True)
        tags = media.tags if media is not None else None
        title_values = []
        artist_values = []
        if tags is not None:
            title_values = tags.get("title", [])
            artist_values = tags.get("artist", [])
            # WAV/AIFF ID3 tags are exposed by Mutagen as TIT2/TPE1 even in
            # easy mode. Read those frames before falling back to the path.
            if not title_values and tags.get("TIT2") is not None:
                title_values = tags.get("TIT2")
            if not artist_values and tags.get("TPE1") is not None:
                artist_values = tags.get("TPE1")
        title = str(title_values[0]).strip() if title_values else ""
        artist = str(artist_values[0]).strip() if artist_values else ""
        presenter_fixes = {
            ("classic and perfect", "bryce vine"): ("Classic and Perfect", "Bryce Vine"),
            ("fuego", "dj snake, sean paul"): ("Fuego", "DJ Snake, Sean Paul"),
            ("instagram", "dimitri vegas"): ("Instagram", "Dimitri Vegas"),
        }
        title, artist = presenter_fixes.get(
            (artist.casefold(), title.casefold()),
            (title, artist),
        )
        title, artist = clean_track_metadata(title, artist)
        if title and artist and broadcast_metadata_is_safe(title, artist):
            if artist:
                title = re.sub(
                    rf"\s+(?:by|par)\s+{re.escape(artist)}[.!]?\s*$",
                    "",
                    title,
                    flags=re.IGNORECASE,
                ).strip()
            if broadcast_metadata_is_safe(title, artist):
                return title[:200], artist[:160]
    except (OSError, TypeError, ValueError):
        pass
    title, artist = fallback_track_metadata(path)
    if artist and broadcast_metadata_is_safe(title, artist):
        return title, artist
    raise RuntimeError(f"unsafe broadcast metadata: {path.name}")


def build_safe_catalog(station: StationConfig) -> tuple[list[Path], int]:
    """Return every discovered track with safe, speakable broadcast metadata."""
    candidates = discover_audio(station)
    safe: list[Path] = []
    skipped = 0
    for item in candidates:
        try:
            track_metadata(item)
        except RuntimeError:
            skipped += 1
            continue
        safe.append(item)
    if len(safe) < 2:
        raise RuntimeError(
            f"fewer than two broadcast-safe tracks for {station.station_id} "
            f"({len(safe)} usable, {skipped} skipped)"
        )
    return safe, skipped


def build_safe_program(
    station: StationConfig,
    program_cache_path: Path | None = None,
    *,
    selection_context: dict[str, object] | None = None,
    decision_sink: Callable[[dict[str, object]], object] | None = None,
    max_tracks: int | None = None,
    one_choice_per_track: bool = False,
) -> tuple[list[Path], int]:
    """Build a locally selected program from only safe-to-air catalog tracks."""
    safe, skipped = build_safe_catalog(station)
    signature_payload = []
    for path in safe:
        try:
            stat = path.stat()
            signature_payload.append(
                f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
            )
        except OSError:
            signature_payload.append(str(path.resolve()))
    catalog_signature = hashlib.sha256(
        "\n".join(sorted(signature_payload, key=str.casefold)).encode("utf-8")
    ).hexdigest()
    by_path = {str(path.resolve()).casefold(): path.resolve() for path in safe}
    selection_mode = (
        "prebaked-transition-host"
        if station.prebaked_transition_host
        else "autonomous-local-laya-full-catalog-shortlist-batch"
    )
    selection_context_sha256 = hashlib.sha256(
        json.dumps(
            selection_context or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    if program_cache_path is not None and program_cache_path.is_file():
        try:
            cached = json.loads(program_cache_path.read_text(encoding="utf-8"))
            cached_paths = [str(value).casefold() for value in cached.get("tracks") or []]
            if (
                cached.get("version") == 2
                and cached.get("station_id") == station.station_id
                and cached.get("selection_mode") == selection_mode
                and cached.get("selection_context_sha256")
                == selection_context_sha256
                and (
                    station.prebaked_transition_host
                    or (
                        cached.get("selection_model_id") == station.selection_model_id
                        and cached.get("selection_model_revision")
                        == station.selection_model_revision
                    )
                )
                and cached.get("catalog_signature") == catalog_signature
                and len(cached_paths) >= 2
                and len(cached_paths) == len(set(cached_paths))
                and all(value in by_path for value in cached_paths)
            ):
                restored = [by_path[value] for value in cached_paths]
                if station.prebaked_transition_host:
                    mark_prebaked_transition_program(
                        station,
                        program_items=len(restored),
                        restored=True,
                    )
                else:
                    assert_autonomous_song_ai_ready(station)
                    mark_autonomous_program_restored(
                        station, program_items=len(restored)
                    )
                return restored, skipped
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    if station.prebaked_transition_host:
        # The already-classified local catalog and complete transition library
        # make live text inference unnecessary. Keep the whole safe catalog in
        # a repeatable, artist-separated order so every boundary is known.
        program = deterministic_rotation(
            safe,
            station.playout_seed,
            station.station_id,
        )
        mark_prebaked_transition_program(
            station,
            program_items=len(program),
        )
    else:
        program = autonomous_rotation(
            safe,
            station,
            metadata_resolver=track_metadata,
            genre_resolver=lambda path: track_genre(station, path),
            selection_context=selection_context,
            decision_sink=decision_sink,
            max_tracks=max_tracks,
            one_choice_per_track=one_choice_per_track,
        )
    if program_cache_path is not None:
        write_status_atomic(
            program_cache_path,
            json.dumps(
                {
                    "version": 2,
                    "station_id": station.station_id,
                    "selection_mode": selection_mode,
                    "selection_context_sha256": selection_context_sha256,
                    "model": (
                        None
                        if station.prebaked_transition_host
                        else station.selection_model_id
                    ),
                    "selection_model_id": (
                        None
                        if station.prebaked_transition_host
                        else station.selection_model_id
                    ),
                    "selection_model_revision": (
                        None
                        if station.prebaked_transition_host
                        else station.selection_model_revision
                    ),
                    "catalog_signature": catalog_signature,
                    "tracks": [str(path.resolve()) for path in program],
                    "selected_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return program, skipped


def public_track_id(path: Path) -> str:
    """Return a stable opaque id without exposing the local media path."""
    digest = hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()
    return "track-" + digest[:32]


def track_genre(station: StationConfig, path: Path) -> str | None:
    """Read the operator-classified folder first, then embedded metadata."""
    resolved = path.resolve()
    for root in station.music_roots:
        try:
            relative = resolved.relative_to(root.resolve())
        except ValueError:
            continue
        root_genre = canonical_genre(root.name)
        if root_genre:
            return root_genre
        if relative.parts:
            relative_genre = canonical_genre(relative.parts[0])
            if relative_genre:
                return relative_genre
    try:
        media = MutagenFile(path, easy=True)
        tags = media.tags if media is not None else None
        values = tags.get("genre", []) if tags is not None else []
        if values:
            value = canonical_genre(str(values[0]))
            if value:
                return value
    except (OSError, TypeError, ValueError):
        pass
    return None


def program_for_editorial_block(
    program: list[Path],
    station: StationConfig,
    block: dict[str, object],
) -> list[Path]:
    """Apply a genre promise strictly; the caller repeats this list when exhausted."""
    required = {
        canonical_genre(str(value))
        for value in block.get("required_genres", ())
        if canonical_genre(str(value))
    }
    if not required:
        return list(program)
    selected = [path for path in program if track_genre(station, path) in required]
    if not selected:
        # A valid AI-selected program can omit a genre needed by the current
        # scheduled block. Build a deterministic rotation from the installed,
        # metadata-safe tracks in that genre instead of taking both streams
        # offline when the block starts.
        eligible: list[Path] = []
        for path in discover_audio(station):
            if track_genre(station, path) not in required:
                continue
            try:
                track_metadata(path)
            except RuntimeError:
                continue
            eligible.append(path)
        if eligible:
            return deterministic_rotation(
                eligible,
                station.playout_seed,
                station.station_id,
            )
        promised = ", ".join(sorted(required))
        raise RuntimeError(
            f"program {block.get('name')} requires {promised}; "
            "no eligible media files are installed"
        )
    return selected


def queue_preview(
    program: list[Path],
    start_index: int,
    station: StationConfig,
    liners: TransitionLinerLibrary | None,
    *,
    limit: int = 8,
) -> list[dict[str, object]]:
    """Expose the exact upcoming music/host boundaries to durable status."""
    if not program:
        return []
    preview: list[dict[str, object]] = []
    for offset in range(min(max(1, limit), len(program))):
        index = (start_index + offset) % len(program)
        following_index = (index + 1) % len(program)
        path = program[index]
        title, artist = track_metadata(path)
        genre = track_genre(station, path)
        following_genre = track_genre(station, program[following_index])
        entry: dict[str, object] = {
            "position": offset,
            "title": title,
            "artist": artist,
            "genre": genre,
        }
        if liners is not None:
            entry["host_after"] = liners.describe_transition(
                genre,
                following_genre,
            )
        preview.append(entry)
    return preview


@dataclass(frozen=True)
class OutputSpec:
    output_id: str
    mount: str
    name: str
    codec_profile: str
    bitrate_kbps: int
    content_type: str
    public: bool


def _absolute_optional_path(value: object) -> Path | None:
    token = str(value or "").strip()
    if not token:
        return None
    path = Path(token).expanduser()
    if not path.is_absolute():
        raise ValueError("quality_outputs_path must be absolute")
    return path.resolve()


def quality_outputs_path(config_path: Path) -> Path | None:
    payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    return _absolute_optional_path(payload.get("quality_outputs_path"))


def _quality_specs(path: Path | None, station: StationConfig) -> tuple[OutputSpec, ...]:
    legacy = OutputSpec(
        "legacy",
        station.mount,
        station.name,
        "mp3",
        0,
        "audio/mpeg",
        False,
    )
    if path is None or not path.is_file():
        return (legacy,)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError("unsupported quality output bridge schema")
    channel_id = station.mount.lstrip("/")
    channels = payload.get("channels") or []
    channel = next(
        (
            item
            for item in channels
            if isinstance(item, dict) and item.get("channel_id") == channel_id
        ),
        None,
    )
    if channel is None:
        return (legacy,)
    expected_mounts = {
        suffix: f"{station.mount}-{suffix}"
        for suffix in ("low", "normal", "high", "flac")
    }
    specs = [legacy]
    seen: set[str] = set()
    for raw in channel.get("outputs") or []:
        if not isinstance(raw, dict) or not bool(raw.get("enabled")):
            continue
        suffix = str(raw.get("quality") or "").strip().lower()
        mount = "/" + str(raw.get("mount") or "").strip().lstrip("/")
        profile = str(raw.get("stream_codec_profile") or "").strip().lower()
        bitrate = int(raw.get("stream_bitrate_kbps") or 0)
        if suffix not in expected_mounts or mount != expected_mounts[suffix]:
            raise ValueError(f"non-canonical AI quality mount: {mount}")
        if suffix == "flac":
            if profile != "ogg_flac_lossless" or bitrate != 0:
                raise ValueError(f"invalid FLAC profile for {mount}")
            content_type = "application/ogg"
        else:
            expected_bitrate = {"low": 96, "normal": 128, "high": 320}[suffix]
            if profile != f"aac_lc_{expected_bitrate}" or bitrate != expected_bitrate:
                raise ValueError(f"invalid AAC profile for {mount}")
            content_type = "audio/aac"
        if mount in seen:
            raise ValueError(f"duplicate AI quality mount: {mount}")
        seen.add(mount)
        specs.append(
            OutputSpec(
                suffix,
                mount,
                f"{station.name} - {suffix}",
                profile,
                bitrate,
                content_type,
                bool(raw.get("icecast_public", True)),
            )
        )
    return tuple(specs)


def build_decoder_command(config: SupervisorConfig, playlist: Path) -> list[str]:
    return [
        str(config.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-re", "-stream_loop", "-1", "-f", "concat", "-safe", "0", "-i",
        str(playlist), "-vn", "-map_metadata", "-1", "-ac", "2", "-ar", "48000",
        "-af", "dynaudnorm=f=500:g=15,alimiter=limit=0.891251",
        "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
    ]


def build_track_decoder_command(config: SupervisorConfig, track: Path) -> list[str]:
    """Decode one heterogeneous source at a time into the shared PCM timeline."""
    return [
        str(config.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-i", str(track), "-vn", "-map_metadata", "-1", "-ac", "2",
        "-ar", "48000", "-af", "dynaudnorm=f=500:g=15,alimiter=limit=0.891251",
        "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
    ]
def _ffmpeg_creationflags() -> int:
    base = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    prio = getattr(subprocess, "ABOVE_NORMAL_PRIORITY_CLASS", 0)
    return base | prio


def build_encoder_command(
    config: SupervisorConfig, spec: OutputSpec
) -> list[str]:
    command = [
        str(config.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-f", "s16le", "-ac", "2", "-ar", "48000", "-i", "pipe:0",
        "-vn", "-map_metadata", "-1",
    ]
    if spec.output_id == "legacy":
        command += [
            "-c:a", "libmp3lame", "-b:a", f"{config.bitrate_kbps}k",
            "-write_xing", "0", "-f", "mp3", "pipe:1",
        ]
    elif spec.output_id == "flac":
        command += [
            "-c:a", "flac", "-compression_level", "8", "-f", "ogg", "pipe:1",
        ]
    else:
        command += [
            "-c:a", "aac", "-profile:a", "aac_low", "-b:a",
            f"{spec.bitrate_kbps}k", "-f", "adts", "pipe:1",
        ]
    return command


class DurableStatus:
    def __init__(self, target: Path) -> None:
        self.target = target
        self.lock = threading.Lock()
        self.payload: dict[str, object] = {
            "version": 2,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "single_program_timeline": True,
            "metadata_suppressed": True,
            "credential_storage": "machine DPAPI reference",
            "stations": {},
        }

    def update(self, station_id: str, branch_id: str, **changes: object) -> None:
        with self.lock:
            stations = self.payload["stations"]
            assert isinstance(stations, dict)
            station = dict(stations.get(station_id) or {})
            branches = dict(station.get("outputs") or {})
            branch = dict(branches.get(branch_id) or {})
            branch.update(changes)
            branch["updated_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            branches[branch_id] = branch
            station["outputs"] = branches
            station["updated_at"] = branch["updated_at"]
            stations[station_id] = station
            encoded = json.dumps(self.payload, indent=2, sort_keys=True) + "\n"
            write_status_atomic(self.target, encoded)

    def station(self, station_id: str) -> dict[str, object]:
        with self.lock:
            stations = self.payload.get("stations")
            if not isinstance(stations, dict):
                return {}
            return json.loads(json.dumps(stations.get(station_id) or {}))


def _public_api_secrets(config: SupervisorConfig) -> dict[str, str]:
    values: dict[str, str] = {}
    source = config.credential_env_file
    if source and source.is_file():
        for raw_line in source.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return {
        "radiotedu-en": values.get("RADIOTEDU_EN_SNAPSHOT_SECRET", ""),
        "radiotedu-fr": values.get("RADIOTEDU_FR_SNAPSHOT_SECRET", ""),
    }


class PublicApiSync:
    """Outbound-only signed status sync for the public listener website."""

    def __init__(
        self,
        config: SupervisorConfig,
        status: DurableStatus,
        stop: threading.Event,
    ) -> None:
        self.config = config
        self.status = status
        self.stop = stop
        self.wakeup = threading.Event()
        self.selection_wakeup = threading.Event()
        self.secrets = _public_api_secrets(config)
        self.thread: threading.Thread | None = None
        self.selection_thread: threading.Thread | None = None
        self.handshake_until: dict[str, float] = {}
        self.sequence_file = config.state_file.parent / "public-sync-sequence.json"
        self.play_outbox_file = config.state_file.parent / "public-sync-play-outbox.json"
        self.spoken_outbox_file = config.state_file.parent / "public-sync-spoken-outbox.json"
        self.selection_outbox_file = (
            config.state_file.parent / "public-sync-selection-decision-outbox.json"
        )
        self.play_lock = threading.Lock()
        self.play_outbox: dict[str, list[dict[str, object]]] = {
            station.station_id: [] for station in config.stations
        }
        self.play_errors: dict[str, str] = {}
        self._load_play_outbox()
        self.spoken_lock = threading.Lock()
        self.spoken_outbox: dict[str, list[dict[str, object]]] = {
            station.station_id: [] for station in config.stations
        }
        self.spoken_errors: dict[str, str] = {}
        self._load_spoken_outbox()
        self.selection_lock = threading.Lock()
        self.selection_outbox: dict[str, list[dict[str, object]]] = {
            station.station_id: [] for station in config.stations
        }
        self.selection_errors: dict[str, str] = {}
        self.selection_delivery: dict[str, dict[str, str]] = {}
        self.selection_contract_supported: dict[str, bool] = {}
        self.selection_contract_probe_at: dict[str, float] = {}
        self.selection_store = SelectionOutboxStore(
            config.state_file.parent / "public-sync-selection-decisions.sqlite3"
        )
        self._load_selection_outbox()
        self.sequence_lock = threading.Lock()
        try:
            stored = json.loads(self.sequence_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            stored = {}
        floor = int(time.time() * 1_000)
        self.sequences = {
            station.station_id: max(floor, int(stored.get(station.station_id) or 0))
            for station in config.stations
        }

    def _load_play_outbox(self) -> None:
        try:
            payload = json.loads(self.play_outbox_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        for station in self.config.stations:
            raw_events = payload.get(station.station_id)
            if not isinstance(raw_events, list):
                continue
            retained: list[dict[str, object]] = []
            for raw_event in raw_events[-MAX_PENDING_PUBLIC_PLAYS:]:
                if not isinstance(raw_event, dict):
                    continue
                event_id = str(raw_event.get("event_id") or "").strip()
                occurred_at = str(raw_event.get("occurred_at") or "").strip()
                if not event_id or not occurred_at:
                    continue
                try:
                    event_time = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if event_time.tzinfo is None or event_time < cutoff:
                    continue
                retained.append(dict(raw_event))
            self.play_outbox[station.station_id] = retained

    def _persist_play_outbox_locked(self) -> None:
        write_status_atomic(
            self.play_outbox_file,
            json.dumps(self.play_outbox, ensure_ascii=False, sort_keys=True) + "\n",
        )

    def _load_spoken_outbox(self) -> None:
        try:
            payload = json.loads(self.spoken_outbox_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for station in self.config.stations:
            raw_events = payload.get(station.station_id)
            if not isinstance(raw_events, list):
                continue
            retained: list[dict[str, object]] = []
            for raw_event in raw_events[-MAX_PENDING_PUBLIC_SPOKEN_SEGMENTS:]:
                if not isinstance(raw_event, dict):
                    continue
                event_id = str(raw_event.get("event_id") or "").strip()
                occurred_at = str(raw_event.get("occurred_at") or "").strip()
                transcript = str(raw_event.get("transcript") or "").strip()
                if not event_id or not occurred_at or not transcript:
                    continue
                try:
                    event_time = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if event_time.tzinfo is None or event_time < cutoff:
                    continue
                retained.append(dict(raw_event))
            self.spoken_outbox[station.station_id] = retained

    def _persist_spoken_outbox_locked(self) -> None:
        write_status_atomic(
            self.spoken_outbox_file,
            json.dumps(self.spoken_outbox, ensure_ascii=False, sort_keys=True) + "\n",
        )

    def _load_selection_outbox(self) -> None:
        station_ids = tuple(station.station_id for station in self.config.stations)
        self.selection_store.migrate_legacy(self.selection_outbox_file, station_ids)
        # Keep only small references in memory. Never encode the backlog while
        # the same interpreter is delivering real-time audio.
        self.selection_outbox = self.selection_store.references(station_ids)

    def pending_selection_events(self, station_id: str, limit: int = 20) -> list[dict[str, object]]:
        return self.selection_store.latest_pending(station_id, limit)

    def pending_play_count(self, station_id: str) -> int:
        with self.play_lock:
            return len(self.play_outbox.get(station_id, []))

    def pending_spoken_segment_count(self, station_id: str) -> int:
        with self.spoken_lock:
            return len(self.spoken_outbox.get(station_id, []))

    def pending_selection_decision_count(self, station_id: str) -> int:
        with self.selection_lock:
            return len(self.selection_outbox.get(station_id, []))

    def record_selection_decision(self, event: dict[str, object]) -> bool:
        """Durably queue the real Laya response and its local ranking."""
        station_id = str(event.get("station_id") or "")
        if station_id not in self.selection_outbox:
            raise ValueError("selection event has an unknown station")
        decision_schema = str(event.get("decision_schema_version") or "")
        selection_mode = str(event.get("selection_mode") or "")
        if decision_schema == "radio-song-choice-v1":
            if selection_mode != "typed_decision_model":
                raise ValueError("legacy selection event is not a typed decision")
        elif decision_schema == "radio-song-choice-batch-v1":
            if selection_mode != "laya_probability_ranked_batch":
                raise ValueError("batch selection event has an invalid selection mode")
            selection = event.get("program_selection")
            output = event.get("model_output")
            if not isinstance(selection, list) or not selection or not isinstance(output, dict):
                raise ValueError("batch selection event has no local program selection")
            if int(selection[0].get("choice_id", -1)) != int(output.get("choice_id", -2)):
                raise ValueError("batch program does not begin with Laya's typed choice")
            if selection[0].get("selection_basis") != "typed_model_choice":
                raise ValueError("batch program mislabels Laya's typed choice")
        elif decision_schema in {
            "radio-song-choice-library-shortlist-batch-v1",
            "radio-song-choice-library-shortlist-single-v1",
        }:
            expected_mode = (
                "laya_library_shortlist_single_next_track"
                if decision_schema == "radio-song-choice-library-shortlist-single-v1"
                else "laya_library_shortlist_probability_ranked_batch"
            )
            if selection_mode != expected_mode:
                raise ValueError("full-catalog shortlist event has an invalid selection mode")
            selection = event.get("program_selection")
            output = event.get("model_output")
            shortlist = event.get("shortlist_evidence")
            if (
                not isinstance(selection, list)
                or not selection
                or not isinstance(output, dict)
                or not isinstance(shortlist, dict)
            ):
                raise ValueError("full-catalog event omitted local decision evidence")
            if (
                int(selection[0].get("choice_id", -1))
                != int(output.get("choice_id", -2))
                or selection[0].get("selection_basis") != "typed_model_choice"
                or (
                    decision_schema == "radio-song-choice-library-shortlist-single-v1"
                    and len(selection) != 1
                )
            ):
                raise ValueError("full-catalog event does not match Laya's typed choice")
        else:
            raise ValueError("selection event has an unsupported decision schema")
        candidates = event.get("candidate_tracks")
        selected_track_id = str(event.get("selected_track_id") or "")
        if (
            not isinstance(candidates, list)
            or not selected_track_id
            or selected_track_id
            not in {
                str(item.get("track_id") or "")
                for item in candidates
                if isinstance(item, dict)
            }
        ):
            raise ValueError("selection event does not validate against its candidates")
        if decision_schema in {
            "radio-song-choice-batch-v1",
            "radio-song-choice-library-shortlist-batch-v1",
            "radio-song-choice-library-shortlist-single-v1",
        }:
            candidates_by_choice = {
                int(item.get("choice_id", -1)): item
                for item in candidates
                if isinstance(item, dict)
            }
            if len(candidates_by_choice) != len(candidates):
                raise ValueError("selection event contains duplicate candidate choice ids")
            selection = event["program_selection"]
            output = event["model_output"]
            candidate_probabilities = {
                int(item.get("choice_id", -1)): item.get("probability")
                for item in output.get("candidate_probabilities", [])
                if isinstance(item, dict)
            }
            if decision_schema in {
                "radio-song-choice-library-shortlist-batch-v1",
                "radio-song-choice-library-shortlist-single-v1",
            }:
                finalist_ids = event.get("finalist_choice_ids")
                shortlist = event["shortlist_evidence"]
                shortlist_ids = shortlist.get("choice_ids_by_similarity_rank")
                option_texts = shortlist.get("candidate_option_texts")
                if (
                    not isinstance(finalist_ids, list)
                    or not isinstance(shortlist_ids, list)
                    or not isinstance(option_texts, list)
                    or len(finalist_ids) != len(set(finalist_ids))
                    or finalist_ids != shortlist_ids
                    or len(finalist_ids) != min(64, len(candidates))
                    or event.get("catalog_candidate_count") != len(candidates)
                    or shortlist.get("candidate_count") != len(candidates)
                    or shortlist.get("shortlist_size") != len(finalist_ids)
                    or shortlist.get("ordered_pool_sha256")
                    != event.get("catalog_ordered_pool_sha256")
                    or shortlist.get("score_type")
                    != "cosine_similarity_not_probability"
                ):
                    raise ValueError("full-catalog shortlist evidence is incomplete")
                pool_for_hash = []
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        raise ValueError("full-catalog candidate must be an object")
                    pool_for_hash.append(
                        {
                            "choice_id": int(candidate.get("choice_id", -1)),
                            "track_id": str(candidate.get("track_id") or ""),
                            "title": str(candidate.get("title") or ""),
                            "artist": str(candidate.get("artist") or ""),
                            "genre": str(candidate.get("genre") or ""),
                        }
                    )
                expected_pool_hash = hashlib.sha256(
                    json.dumps(
                        pool_for_hash,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
                if expected_pool_hash != event.get("catalog_ordered_pool_sha256"):
                    raise ValueError("full-catalog ordered pool hash does not match its candidates")
                if shortlist.get("method") == "laya.shortlist.predict_shortlist_cosine_similarity":
                    if len(option_texts) != len(candidates) or not isinstance(
                        shortlist.get("query_text"), str
                    ):
                        raise ValueError("Laya encoder input text was not captured in full")
                    for candidate, option in zip(candidates, option_texts):
                        score = candidate.get("shortlist_cosine_similarity")
                        if (
                            not isinstance(option, dict)
                            or int(candidate.get("choice_id", -1))
                            != int(option.get("choice_id", -2))
                            or not isinstance(option.get("text"), str)
                            or isinstance(score, bool)
                            or not isinstance(score, (int, float))
                            or not math.isfinite(float(score))
                            or not -1.0 <= float(score) <= 1.0
                        ):
                            raise ValueError("full-catalog encoder input or score is malformed")
                    expected_ranked_ids = [
                        int(candidate.get("choice_id", -1))
                        for candidate in sorted(
                            candidates,
                            key=lambda item: (
                                -float(item["shortlist_cosine_similarity"]),
                                int(item["choice_id"]),
                            ),
                        )[: len(shortlist_ids)]
                    ]
                    if [int(value) for value in shortlist_ids] != expected_ranked_ids:
                        raise ValueError("shortlist order does not match the recorded cosine scores")
                    rank_by_choice = {
                        choice_id: rank
                        for rank, choice_id in enumerate(expected_ranked_ids, start=1)
                    }
                    if any(
                        candidate.get("shortlist_rank")
                        != rank_by_choice.get(int(candidate.get("choice_id", -1)))
                        for candidate in candidates
                    ):
                        raise ValueError("candidate finalist ranks do not match the shortlist")
                elif shortlist.get("method") == "laya_typed_choice_over_entire_pool":
                    if (
                        option_texts
                        or set(finalist_ids) != set(candidates_by_choice)
                        or any(item.get("shortlist_cosine_similarity") is not None for item in candidates)
                    ):
                        raise ValueError("full-pool direct choice evidence is malformed")
                else:
                    raise ValueError("selection event uses an unsupported shortlist method")
                if set(candidate_probabilities) != set(finalist_ids):
                    raise ValueError("Laya probability map does not match the shortlisted finalists")
                model_input = event.get("model_input")
                state = model_input.get("state") if isinstance(model_input, dict) else None
                state_pool = state.get("catalog_pool") if isinstance(state, dict) else None
                if (
                    not isinstance(state_pool, dict)
                    or state_pool.get("candidate_count") != len(candidates)
                    or state_pool.get("ordered_pool_sha256") != expected_pool_hash
                ):
                    raise ValueError("typed model state does not identify the recorded full pool")
                questions = model_input.get("questions") if isinstance(model_input, dict) else None
                song_choice = questions.get("song_choice") if isinstance(questions, dict) else None
                criteria = song_choice.get("criteria") if isinstance(song_choice, dict) else None
                expected_keys = {f"candidate_{int(value):04d}" for value in finalist_ids}
                ordered_keys = [f"candidate_{int(value):04d}" for value in finalist_ids]
                if (
                    not isinstance(criteria, dict)
                    or set(criteria) != expected_keys
                    or list(criteria) != ordered_keys
                ):
                    raise ValueError("typed model input does not match its recorded shortlist")
            seen_choices: set[int] = set()
            for item in selection:
                if not isinstance(item, dict):
                    raise ValueError("batch program selection entry must be an object")
                choice_id = int(item.get("choice_id", -1))
                candidate = candidates_by_choice.get(choice_id)
                if (
                    candidate is None
                    or str(item.get("track_id") or "")
                    != str(candidate.get("track_id") or "")
                    or choice_id in seen_choices
                    or item.get("probability") != candidate_probabilities.get(choice_id)
                ):
                    raise ValueError("batch program selection does not match model evidence")
                seen_choices.add(choice_id)
        encoded = json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > MAX_PUBLIC_SELECTION_EVENT_BYTES:
            raise ValueError("selection event exceeds the public API body limit")
        with self.selection_lock:
            pending = self.selection_outbox[station_id]
            try:
                inserted = self.selection_store.enqueue(event)
            except Exception as exc:
                self.selection_errors[station_id] = f"local selection outbox write failed: {exc}"[:240]
                return False
            if inserted:
                pending.append({key: str(event[key]) for key in ("station_id", "event_id", "occurred_at")})
        # Local persistence is not a delivery acknowledgement. Keep the last
        # API failure visible until a request succeeds or the outbox is empty.
        self.selection_wakeup.set()
        return True

    def record_play(
        self,
        *,
        station_id: str,
        classification: str,
        duration_ms: int,
        occurred_at: str,
        track_id: str | None,
        title: str | None,
        artist: str | None,
        genre: str | None,
        program_id: str = "ai_live_rotation",
        program_name: str,
        sound_tags: list[str] | None = None,
    ) -> None:
        """Queue one canonical platform play event durably before airing continues."""
        if station_id not in self.play_outbox:
            return
        event: dict[str, object] = {
            "protocol": PUBLIC_PROTOCOL,
            "schema_version": 1,
            "event_id": f"play-{station_id}-{uuid.uuid4().hex}",
            "station_id": station_id,
            "event_type": "play.completed",
            "occurred_at": occurred_at,
            "classification": classification,
            "duration_ms": max(0, min(86_400_000, int(duration_ms))),
            "track_id": track_id[:128] if track_id else None,
            "track_title": title[:200] if title else None,
            "artist": artist[:160] if artist else None,
            "program_id": program_id[:128] if program_id else None,
            "program_name": program_name[:160] if program_name else None,
            "cover_id": None,
            "sound_tags": [
                tag
                for value in sound_tags or []
                if (tag := str(value).strip().casefold()) in PUBLIC_SOUND_TAGS
            ][:5],
        }
        # The deployed platform accepts genre as an additive event field and
        # uses it to build the 14-day genre airtime ranking.  Keep null values
        # out of the payload so older platform revisions remain compatible.
        if genre:
            event["genre"] = genre[:64]
        with self.play_lock:
            pending = self.play_outbox[station_id]
            pending.append(event)
            del pending[:-MAX_PENDING_PUBLIC_PLAYS]
            self._persist_play_outbox_locked()

    def record_spoken_segment(
        self,
        *,
        station_id: str,
        transcript: str,
        occurred_at: str,
        completed_at: str,
        duration_ms: int,
        language: str,
        segment_type: str,
        well_wish: bool,
        program_id: str | None,
        program_name: str | None,
        preceding_track: dict[str, str | None] | None = None,
        following_track: dict[str, str | None] | None = None,
        text_model: str | None = None,
        tts_engine: str | None = None,
        tts_model: str | None = None,
        voice_design: str | None = None,
        source: str | None = None,
    ) -> None:
        """Persist the exact final on-air words for later signed API delivery."""
        if station_id not in self.spoken_outbox:
            return
        clean_transcript = re.sub(r"\s+", " ", str(transcript or "")).strip()
        if not clean_transcript:
            return
        event: dict[str, object] = {
            "protocol": PUBLIC_PROTOCOL,
            "schema_version": 1,
            "event_id": f"speech-{station_id}-{uuid.uuid4().hex}",
            "station_id": station_id,
            "event_type": "speech.completed",
            "occurred_at": occurred_at,
            "completed_at": completed_at,
            "duration_ms": max(0, min(86_400_000, int(duration_ms))),
            "language": language[:2].casefold(),
            "segment_type": segment_type,
            "well_wish": bool(well_wish),
            "transcript": clean_transcript[:2_000],
            "program_id": program_id[:128] if program_id else None,
            "program_name": program_name[:160] if program_name else None,
            "preceding_track": preceding_track,
            "following_track": following_track,
            "text_model": text_model[:100] if text_model else None,
            "tts_engine": tts_engine[:80] if tts_engine else None,
            "tts_model": tts_model[:100] if tts_model else None,
            "voice_design": voice_design[:120] if voice_design else None,
            "source": source[:80] if source else None,
        }
        with self.spoken_lock:
            try:
                pending = self.spoken_outbox[station_id]
                pending.append(event)
                del pending[:-MAX_PENDING_PUBLIC_SPOKEN_SEGMENTS]
                self._persist_spoken_outbox_locked()
            except Exception as exc:
                self.spoken_errors[station_id] = f"local outbox write failed: {exc}"[:240]

    def _flush_plays(self, station: StationConfig) -> None:
        station_id = station.station_id
        if not self.secrets.get(station_id):
            return
        for _ in range(PUBLIC_PLAY_FLUSH_BATCH):
            with self.play_lock:
                pending = self.play_outbox.get(station_id, [])
                event = dict(pending[0]) if pending else None
            if event is None:
                self.play_errors.pop(station_id, None)
                return
            try:
                self._post(
                    station_id,
                    f"/v1/radio/stations/{station_id}/plays",
                    event,
                )
            except Exception as exc:
                self.play_errors[station_id] = str(exc)[:240]
                return
            with self.play_lock:
                pending = self.play_outbox.get(station_id, [])
                if pending and pending[0].get("event_id") == event.get("event_id"):
                    pending.pop(0)
                    self._persist_play_outbox_locked()
        self.play_errors.pop(station_id, None)

    def _flush_spoken_segments(self, station: StationConfig) -> None:
        station_id = station.station_id
        if not self.secrets.get(station_id):
            return
        for _ in range(PUBLIC_SPOKEN_FLUSH_BATCH):
            with self.spoken_lock:
                pending = self.spoken_outbox.get(station_id, [])
                event = dict(pending[0]) if pending else None
            if event is None:
                self.spoken_errors.pop(station_id, None)
                return
            try:
                self._post(
                    station_id,
                    f"/v1/radio/stations/{station_id}/spoken-segments",
                    event,
                )
            except Exception as exc:
                self.spoken_errors[station_id] = str(exc)[:240]
                return
            with self.spoken_lock:
                pending = self.spoken_outbox.get(station_id, [])
                if pending and pending[0].get("event_id") == event.get("event_id"):
                    delivered = pending.pop(0)
                    try:
                        self._persist_spoken_outbox_locked()
                    except Exception as exc:
                        pending.insert(0, delivered)
                        self.spoken_errors[station_id] = f"local outbox update failed: {exc}"[:240]
                        return
        self.spoken_errors.pop(station_id, None)

    def _selection_contract_is_live(self, station_id: str) -> bool:
        now = time.monotonic()
        last_probe = self.selection_contract_probe_at.get(station_id, 0.0)
        if now - last_probe < PUBLIC_SELECTION_CONTRACT_PROBE_SECONDS:
            return self.selection_contract_supported.get(station_id, False)
        path = f"/v1/radio/stations/{station_id}/status"
        request = urllib.request.Request(
            self.config.public_api_base_url + path,
            headers={"Accept": "application/json"},
            method="GET",
        )
        self.selection_contract_probe_at[station_id] = now
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                if not 200 <= int(response.status) < 300:
                    raise OSError(f"public status returned HTTP {response.status}")
                payload = json.loads(
                    response.read(MAX_PUBLIC_SELECTION_STATUS_BYTES).decode("utf-8")
                )
            metrics = payload.get("metrics") if isinstance(payload, dict) else None
            supported = (
                isinstance(metrics, dict)
                and "recent_song_selection_decisions" in metrics
            )
            self.selection_contract_supported[station_id] = supported
            if not supported:
                self.selection_errors[station_id] = (
                    "selection API contract is not deployed; evidence remains queued locally"
                )
            return supported
        except Exception as exc:
            self.selection_contract_supported[station_id] = False
            self.selection_errors[station_id] = (
                f"could not verify the selection API contract: {type(exc).__name__}"
            )[:200]
            return False

    @staticmethod
    def _selection_event_for_api(event: dict[str, object]) -> dict[str, object]:
        """Normalize queued choice IDs while preserving the recorded Laya trace."""
        payload = dict(event)
        raw_candidates = payload.get("candidate_tracks")
        if not isinstance(raw_candidates, list):
            return payload
        candidates: list[dict[str, object]] = []
        for raw_candidate in raw_candidates:
            if not isinstance(raw_candidate, dict):
                continue
            candidate = dict(raw_candidate)
            if "choice_id" not in candidate and "id" in candidate:
                candidate["choice_id"] = candidate.pop("id")
            candidates.append(candidate)
        payload["candidate_tracks"] = candidates
        return payload

    def _flush_selection_decisions(self, station: StationConfig) -> None:
        station_id = station.station_id
        if not self.secrets.get(station_id):
            return
        if self.pending_selection_decision_count(station_id) == 0:
            self.selection_errors.pop(station_id, None)
            return
        if not self._selection_contract_is_live(station_id):
            return
        with self.selection_lock:
            pending = self.selection_outbox.get(station_id, [])
            # Deliver the newest genuine decision before draining history.
            # An unsupported legacy schema must not hold the live panel back.
            # Every older event remains durable and is still retried.
            newest = dict(pending[-1]) if pending else None
            batch = [newest] if newest is not None else []
            batch.extend(dict(item) for item in pending[:PUBLIC_SELECTION_FLUSH_BATCH - 1]
                         if newest is None or item['event_id'] != newest['event_id'])
        delivery_error = ''
        for index, reference in enumerate(batch):
            event = reference
            event_id = str(event.get("event_id") or "")
            try:
                event = self.selection_store.event(station_id, event_id)
                self._post(
                    station_id,
                    f"/v1/radio/stations/{station_id}/selection-decisions",
                    self._selection_event_for_api(event),
                    idempotency_key=f"{station_id}:{event_id}",
                )
            except Exception as exc:
                delivery_error = str(exc)[:240]
                self.selection_errors[station_id] = delivery_error
                # Try the oldest schema as well when the newest schema is
                # rejected. Other failures wait for the normal retry cycle.
                if index == 0 and 'returned HTTP 422:' in str(exc):
                    continue
                return
            with self.selection_lock:
                pending = self.selection_outbox.get(station_id, [])
                delivered_reference = next(
                    (item for item in pending if item.get('event_id') == event_id), None
                )
                if delivered_reference is not None:
                    try:
                        self.selection_store.acknowledge(station_id, event_id)
                    except Exception:
                        self.selection_errors[station_id] = (
                            "local selection outbox update failed after API acknowledgement"
                        )
                        return
                    pending.remove(delivered_reference)
                    self.selection_delivery[station_id] = {
                        'event_id': event_id,
                        'occurred_at': str(event.get('occurred_at') or ''),
                        'accepted_at': datetime.now(timezone.utc).isoformat(),
                    }
        if not delivery_error:
            self.selection_errors.pop(station_id, None)

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run,
            name="radiotedu-public-api-sync",
            daemon=True,
        )
        self.thread.start()
        self.selection_thread = threading.Thread(
            target=self._run_selections,
            name="radiotedu-public-selection-delivery",
            daemon=True,
        )
        self.selection_thread.start()

    def join(self) -> None:
        if self.thread is not None:
            self.thread.join(timeout=10)
        if self.selection_thread is not None:
            self.selection_thread.join(timeout=10)

    def _next_sequence(self, station_id: str) -> int:
        with self.sequence_lock:
            value = self.sequences[station_id] + 1
            self.sequences[station_id] = value
            write_status_atomic(
                self.sequence_file,
                json.dumps(self.sequences, sort_keys=True) + "\n",
            )
            return value

    @staticmethod
    def _headers(
        *,
        secret: str,
        method: str,
        path: str,
        station_id: str,
        agent_id: str,
        body: bytes,
        nonce: str,
        idempotency_key: str,
        correlation_id: str,
    ) -> dict[str, str]:
        timestamp = str(int(time.time()))
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = "\n".join(
            (
                method.upper(),
                path,
                agent_id,
                station_id,
                timestamp,
                nonce,
                idempotency_key,
                correlation_id,
                body_hash,
            )
        ).encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-RadioTEDU-Agent-ID": agent_id,
            "X-RadioTEDU-Timestamp": timestamp,
            "X-RadioTEDU-Nonce": nonce,
            "X-RadioTEDU-Signature": f"sha256={signature}",
            "Idempotency-Key": idempotency_key,
            "X-Correlation-ID": correlation_id,
        }

    def _post(
        self,
        station_id: str,
        path: str,
        payload: dict[str, object],
        *,
        nonce: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if (
            path.endswith("/selection-decisions")
            and len(body) > MAX_PUBLIC_SELECTION_EVENT_BYTES
        ):
            raise ValueError("selection event exceeds the public API body limit")
        request_nonce = nonce or uuid.uuid4().hex
        correlation_id = str(uuid.uuid4())
        headers = self._headers(
            secret=self.secrets[station_id],
            method="POST",
            path=path,
            station_id=station_id,
            agent_id=self.config.public_agent_id,
            body=body,
            nonce=request_nonce,
            idempotency_key=(
                idempotency_key or f"{station_id}:{uuid.uuid4().hex}"
            ),
            correlation_id=correlation_id,
        )
        request = urllib.request.Request(
            self.config.public_api_base_url + path,
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                if not 200 <= int(response.status) < 300:
                    raise OSError(f"public API returned HTTP {response.status}")
                decoded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(800).decode("utf-8", errors="replace").strip()
            except OSError:
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise OSError(f"public API returned HTTP {exc.code}{suffix}") from exc
        if not isinstance(decoded, dict):
            raise OSError("public API returned an invalid response")
        return decoded

    def _handshake(self, station_id: str) -> None:
        client_nonce = uuid.uuid4().hex
        path = f"/v1/radio/stations/{station_id}/handshake"
        response = self._post(
            station_id,
            path,
            {
                "protocol": PUBLIC_PROTOCOL,
                "schema_version": 1,
                "station_id": station_id,
                "agent_id": self.config.public_agent_id,
                "client_nonce": client_nonce,
            },
            nonce=client_nonce,
        )
        correlation_id = str(response.get("correlation_id") or "")
        fields = (
            PUBLIC_PROTOCOL,
            "handshake-response",
            station_id,
            self.config.public_agent_id,
            client_nonce,
            str(response.get("server_nonce") or ""),
            str(response.get("server_timestamp") or ""),
            correlation_id,
        )
        expected = "sha256=" + hmac.new(
            self.secrets[station_id].encode("utf-8"),
            "\n".join(fields).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        supplied = str(response.get("server_signature") or "")
        if (
            response.get("authenticated") is not True
            or response.get("station_id") != station_id
            or response.get("agent_id") != self.config.public_agent_id
            or response.get("client_nonce") != client_nonce
            or not hmac.compare_digest(expected, supplied)
        ):
            raise OSError("public API mutual handshake verification failed")
        self.handshake_until[station_id] = time.monotonic() + min(
            50, max(5, int(response.get("expires_in_seconds") or 60) - 5)
        )

    def _snapshot(
        self,
        station: StationConfig,
        now: datetime | None = None,
    ) -> dict[str, object]:
        station_status = self.status.station(station.station_id)
        outputs = station_status.get("outputs")
        branches = outputs if isinstance(outputs, dict) else {}
        legacy = branches.get("legacy")
        local = legacy if isinstance(legacy, dict) else {}
        source_state = str(local.get("state") or "starting")
        origin_ready = local.get("origin_listener_ready") is True
        audio_active = source_state == "streaming" and bool(local.get("audio_sent_at"))
        if audio_active and origin_ready:
            stream_state = "live"
            operational_state = "live"
        elif audio_active:
            stream_state = "degraded"
            operational_state = "degraded"
        elif source_state in {"starting", "reconnecting"}:
            stream_state = "offline"
            operational_state = "starting"
        else:
            stream_state = "offline"
            operational_state = "offline"
        now_playing = local.get("now_playing")
        public_now = now_playing if isinstance(now_playing, dict) else None
        kind = str((public_now or {}).get("kind") or "unknown")
        if kind not in {"music", "talking", "imaging", "unknown"}:
            kind = "unknown"
        if public_now is not None:
            raw_tags = public_now.get("sound_tags")
            genre = str(public_now.get("genre") or "").strip()[:80]
            sound_tags = list(dict.fromkeys(
                str(value).strip().casefold()
                for value in raw_tags
                if str(value).strip().casefold() in PUBLIC_SOUND_TAGS
            )) if isinstance(raw_tags, list) else []
            if genre:
                genre_key = " ".join(re.findall(r"[a-z0-9]+", genre.casefold()))
                sound_tags.extend(
                    tag
                    for tag in GENRE_SOUND_TAGS.get(genre_key, ())
                    if tag not in sound_tags
                )
            public_now = {
                "kind": kind,
                "track_id": str(public_now.get("track_id") or "")[:100] or None,
                "title": str(public_now.get("title") or "")[:200] or None,
                "artist": str(public_now.get("artist") or "")[:160] or None,
                "cover_id": None,
                "mood": str(public_now.get("mood") or "")[:80] or None,
                "sound_tags": sound_tags,
                "started_at": public_now.get("started_at"),
            }
        generated = now or datetime.now(timezone.utc)
        status_program = local.get("current_program")
        current_program = (
            dict(status_program)
            if isinstance(status_program, dict)
            else public_editorial_program(
                current_editorial_program(station.station_id, generated)
            )
        )
        editorial_sound_tags = [
            str(value).strip().casefold()
            for value in current_program.get("sound_tags", [])
            if str(value).strip().casefold() in PUBLIC_SOUND_TAGS
        ]
        if not editorial_sound_tags:
            editorial_sound_tags = list(
                current_editorial_program(station.station_id, generated).get("sound_tags") or []
            )
        return {
            "protocol": PUBLIC_PROTOCOL,
            "schema_version": 2,
            "station": {
                "id": station.station_id,
                "language": "en" if station.station_id.endswith("-en") else "fr",
                "display_name": station.name,
            },
            "sequence": self._next_sequence(station.station_id),
            "generated_at": generated.isoformat(),
            "expires_at": (generated + timedelta(seconds=30)).isoformat(),
            "operational_state": operational_state,
            "speech_state": {
                "active": kind == "talking",
                "kind": kind if kind in {"music", "talking"} else "unknown",
            },
            "now_playing": public_now,
            "current_program": current_program,
            "next_program": next_editorial_program(station.station_id, generated),
            "stream": {
                "url": station.public_stream_url,
                "mount": station.public_mount,
                "status": stream_state,
                "codec": "MP3",
                "bitrate_kbps": 192,
                "public": True,
            },
            "editorial": {"sound_tags": editorial_sound_tags[:5]},
        }

    def _send_station(self, station: StationConfig) -> None:
        station_id = station.station_id
        endpoint = f"{self.config.public_api_base_url}/v1/radio/stations/{station_id}"
        if not self.secrets.get(station_id):
            self.status.update(
                station_id,
                "public_api",
                state="awaiting_secret_provisioning",
                configured=False,
                endpoint=endpoint,
                stream_url=station.public_stream_url,
                last_error="station HMAC secret is not configured",
                pending_spoken_segments=self.pending_spoken_segment_count(station_id),
                spoken_sync_error=self.spoken_errors.get(station_id, ""),
                pending_selection_decisions=self.pending_selection_decision_count(station_id),
                selection_sync_error=self.selection_errors.get(station_id, ""),
                selection_last_acknowledgement=self.selection_delivery.get(station_id),
            )
            return
        try:
            if time.monotonic() >= self.handshake_until.get(station_id, 0):
                self._handshake(station_id)
            path = f"/v1/radio/stations/{station_id}/snapshot"
            response = self._post(station_id, path, self._snapshot(station))
            self.status.update(
                station_id,
                "public_api",
                state="connected",
                configured=True,
                mutually_authenticated=True,
                endpoint=endpoint,
                stream_url=station.public_stream_url,
                last_success_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                last_error="",
                accepted_sequence=response.get("sequence"),
                pending_play_events=self.pending_play_count(station_id),
                play_sync_error=self.play_errors.get(station_id, ""),
                pending_spoken_segments=self.pending_spoken_segment_count(station_id),
                spoken_sync_error=self.spoken_errors.get(station_id, ""),
                pending_selection_decisions=self.pending_selection_decision_count(station_id),
                selection_sync_error=self.selection_errors.get(station_id, ""),
                selection_last_acknowledgement=self.selection_delivery.get(station_id),
            )
        except Exception as exc:
            self.handshake_until.pop(station_id, None)
            self.status.update(
                station_id,
                "public_api",
                state="retrying",
                configured=True,
                mutually_authenticated=False,
                endpoint=endpoint,
                stream_url=station.public_stream_url,
                last_error=str(exc)[:240],
                pending_spoken_segments=self.pending_spoken_segment_count(station_id),
                spoken_sync_error=self.spoken_errors.get(station_id, ""),
                pending_selection_decisions=self.pending_selection_decision_count(station_id),
                selection_sync_error=self.selection_errors.get(station_id, ""),
                selection_last_acknowledgement=self.selection_delivery.get(station_id),
            )

    def wake(self) -> None:
        self.wakeup.set()

    def record_play_on_air(self, clock: PlayoutClock, segment: AirSegment, **payload: object) -> None:
        def publish(aired: AirSegment) -> None:
            data = dict(payload)
            data.update(occurred_at=aired.started_at.isoformat(), duration_ms=aired.duration_ms)
            self.record_play(**data)
            self.wake()
        clock.on_complete(segment, publish)

    def record_spoken_on_air(self, clock: PlayoutClock, segment: AirSegment, **payload: object) -> None:
        def publish(aired: AirSegment) -> None:
            data = dict(payload)
            data.update(occurred_at=aired.started_at.isoformat(),
                        completed_at=aired.completed_at.isoformat(), duration_ms=aired.duration_ms)
            self.record_spoken_segment(**data)
            self.wake()
        clock.on_complete(segment, publish)

    def _run(self) -> None:
        while not self.stop.is_set():
            # Preserve source changes arriving during network work.
            self.wakeup.clear()
            for station in self.config.stations:
                if self.stop.is_set():
                    break
                self._send_station(station)
            for station in self.config.stations:
                if self.stop.is_set():
                    break
                self._flush_plays(station)
                self._flush_spoken_segments(station)
            self.wakeup.wait(self.config.public_sync_interval_seconds)

    def _run_selections(self) -> None:
        # Full-pool evidence retries must not delay current-track snapshots.
        while not self.stop.is_set():
            self.selection_wakeup.clear()
            for station in self.config.stations:
                if self.stop.is_set():
                    break
                self._flush_selection_decisions(station)
            self.selection_wakeup.wait(self.config.public_sync_interval_seconds)


def restore_laya_startup_tracks(
    catalog: list[Path], station_id: str, program_id: str,
    events: list[dict[str, object]], limit: int = LIVE_LAYA_STARTUP_TRACKS,
    allow_prior_program: bool = False,
) -> list[Path]:
    """Reuse only actual persisted single-choice decisions; never manufacture a choice."""
    catalog_by_id = {public_track_id(path): path.resolve() for path in catalog}
    restored: list[Path] = []
    for event in reversed(events):
        if (
            event.get("station_id") != station_id
            or (event.get("program_id") != program_id and not allow_prior_program)
            or event.get("decision_schema_version") != "radio-song-choice-library-shortlist-single-v1"
            or event.get("selection_mode") != "laya_library_shortlist_single_next_track"
            or not event.get("event_id")
            or not event.get("model_output_raw_json")
        ):
            continue
        selections = event.get("program_selection")
        if not isinstance(selections, list) or len(selections) != 1:
            continue
        selected = selections[0]
        if not isinstance(selected, dict) or selected.get("selection_basis") != "typed_model_choice":
            continue
        track_id = str(event.get("selected_track_id") or "")
        if selected.get("track_id") != track_id:
            continue
        path = catalog_by_id.get(track_id)
        if path is not None and path not in restored:
            restored.append(path)
            if len(restored) >= limit:
                break
    return list(reversed(restored))


def load_live_queue_checkpoint(path: Path, station_id: str) -> dict[str, object]:
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(checkpoint, dict) or checkpoint.get("station_id") != station_id or checkpoint.get("version") != 1:
        return {}
    return checkpoint


def restore_live_queue_tracks(
    catalog: list[Path], checkpoint: dict[str, object],
    events: list[dict[str, object]], station_id: str,
) -> list[Path]:
    """Resume unconsumed choices, validated against their original Laya evidence."""
    by_id = {public_track_id(path): path.resolve() for path in catalog}
    proven = {
        str(event.get("selected_track_id"))
        for event in events
        if event.get("station_id") == station_id
        and event.get("decision_schema_version") == "radio-song-choice-library-shortlist-single-v1"
        and event.get("selection_mode") == "laya_library_shortlist_single_next_track"
        and event.get("model_output_raw_json")
        and isinstance(event.get("program_selection"), list)
        and len(event["program_selection"]) == 1
        and event["program_selection"][0].get("selection_basis") == "typed_model_choice"
        and event["program_selection"][0].get("track_id") == event.get("selected_track_id")
    }
    queued = checkpoint.get("ready_track_ids")
    if not isinstance(queued, list):
        return []
    restored: list[Path] = []
    for track_id in queued:
        path = by_id.get(str(track_id))
        if path is None or str(track_id) not in proven or path in restored:
            # A partial reconstruction would change the persisted order.
            return []
        restored.append(path)
    return restored[:LIVE_LAYA_STARTUP_TRACKS]


class LiveLayaSongQueue:
    """Keep independently chosen Laya tracks ahead of the continuous playout."""

    def __init__(
        self,
        *,
        station: StationConfig,
        catalog: list[Path],
        initial_tracks: list[Path],
        selection_context: dict[str, object],
        context_provider: Callable[[], dict[str, object]],
        decision_sink: Callable[[dict[str, object]], object],
        stop: threading.Event,
        target_depth: int = LIVE_LAYA_QUEUE_DEPTH,
        on_air_context_getter: Callable[[], dict[str, object]] | None = None,
        model_warmup: Callable[[], None] | None = None,
        checkpoint_file: Path | None = None,
        restored_checkpoint: dict[str, object] | None = None,
    ) -> None:
        if not catalog or not initial_tracks:
            raise ValueError("live Laya queue requires a safe catalog and initial decisions")
        if target_depth < 3:
            raise ValueError("live Laya queue must keep at least three tracks ahead")
        self.station = station
        self.checkpoint_file = checkpoint_file
        self.checkpoint_error = ""
        normalized_catalog = dict.fromkeys(path.resolve() for path in catalog)
        self.track_ids = {path: public_track_id(path) for path in normalized_catalog}
        self.paths_by_track_id = {track_id: path for path, track_id in self.track_ids.items()}
        self.catalog = tuple(sorted(normalized_catalog, key=self.track_ids.__getitem__))
        self.choice_ids = {
            path: position
            for position, path in enumerate(self.catalog, start=1)
        }
        self.stop = stop
        self.target_depth = int(target_depth)
        self.context_provider = context_provider
        self.on_air_context_getter = on_air_context_getter
        self.model_warmup = model_warmup
        self.model_warmup_state = "waiting_for_playout" if model_warmup is not None else "not_requested"
        self.decision_sink = decision_sink
        self.condition = threading.Condition()
        self.startup_track: Path | None = initial_tracks[0].resolve()
        self.ready: deque[Path] = deque(path.resolve() for path in initial_tracks[1:self.target_depth + 1])
        self.reserved_track_ids = {self.track_ids[path] for path in [self.startup_track, *self.ready]}
        checkpoint = restored_checkpoint or {}
        self.reserved_track_ids.update(
            str(track_id) for track_id in checkpoint.get("reserved_track_ids", [])
            if str(track_id) in self.paths_by_track_id
        )
        self.started = False
        self.played_recent: deque[Path] = deque(maxlen=6)
        self.played_recent_records: deque[dict[str, str]] = deque(maxlen=6)
        for record in checkpoint.get("played_recent_records", []):
            if isinstance(record, dict) and str(record.get("track_id")) in self.paths_by_track_id:
                self.played_recent.append(self.paths_by_track_id[str(record["track_id"])])
                self.played_recent_records.append(dict(record))
        self.current_track_id = ""
        self.current_path: Path | None = None
        self.context = dict(selection_context)
        self.last_error = ""
        self.selection_count = len(self.ready) + 1
        self.last_selected_track_id = self.track_ids[self.ready[-1] if self.ready else self.startup_track]
        self.last_event_id = str(autonomous_selection_status(station.station_id).get("last_event_id") or "")
        self.closed = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"laya-live-song-queue-{station.station_id}",
            daemon=True,
        )

    @staticmethod
    def _context_id(context: dict[str, object]) -> str:
        return hashlib.sha256(
            json.dumps(
                context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    def start(self) -> None:
        with self.condition:
            self._persist_checkpoint()
        self.thread.start()

    def _persist_checkpoint(self) -> None:
        """Called under the queue lock; never from the audio sender thread."""
        if self.checkpoint_file is None:
            return
        pending = ([self.startup_track] if self.startup_track is not None else []) + list(self.ready)
        checkpoint = {
            "version": 1, "station_id": self.station.station_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "ready_track_ids": [self.track_ids[path] for path in pending],
            "pipeline_current_track_id": self.current_track_id or None,
            "reserved_track_ids": sorted(self.reserved_track_ids),
            "played_recent_records": list(self.played_recent_records),
        }
        try:
            saved = write_status_atomic(self.checkpoint_file, json.dumps(checkpoint, ensure_ascii=False) + "\n")
            self.checkpoint_error = "" if saved else "could not persist live Laya queue checkpoint"
        except OSError as exc:
            self.checkpoint_error = f"queue checkpoint: {exc}"[:240]

    def update_context(self, context: dict[str, object]) -> None:
        normalized = dict(context)
        with self.condition:
            if self._context_id(normalized) == self._context_id(self.context):
                return
            self.context = normalized
            # Editorial genres are preferences in live mode. Keep already
            # chosen songs and the actual current track across schedule changes;
            # apply the new context to the next fresh decision without a gap.
            self.last_error = ""
            self.condition.notify_all()

    def update_catalog(self, catalog: list[Path]) -> bool:
        """Install a validated catalog without doing filesystem work on playout."""
        normalized = dict.fromkeys(path.resolve() for path in catalog)
        if len(normalized) < 2:
            raise ValueError("live catalog must retain at least two safe tracks")
        incoming_ids = {path: public_track_id(path) for path in normalized}
        ordered = tuple(sorted(normalized, key=incoming_ids.__getitem__))
        with self.condition:
            if ordered == self.catalog:
                return False
            # Keep old IDs available while a genuine model call is in flight.
            # New imports receive new IDs; existing option IDs never shift.
            self.track_ids.update(incoming_ids)
            self.paths_by_track_id.update({track_id: path for path, track_id in incoming_ids.items()})
            next_choice_id = max(self.choice_ids.values(), default=0) + 1
            for path in ordered:
                if path not in self.choice_ids:
                    self.choice_ids[path] = next_choice_id
                    next_choice_id += 1
            self.catalog = ordered
            available = set(ordered)
            self.ready = deque(path for path in self.ready if path in available)
            self._persist_checkpoint()
            self.condition.notify_all()
            return True

    def next_track(self, timeout: float = 2.0) -> Path | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self.condition:
            if self.startup_track is not None:
                path = self.startup_track
                self.startup_track = None
                self.started = True
                self.current_track_id = self.track_ids[path]
                self.current_path = path
                self._persist_checkpoint()
                self.condition.notify_all()
                return path
            while not self.ready and not self.stop.is_set() and not self.closed.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(min(remaining, 1.0))
            if not self.ready:
                return None
            path = self.ready.popleft()
            self.current_track_id = self.track_ids[path]
            self.current_path = path
            self._persist_checkpoint()
            self.condition.notify_all()
            return path

    def peek(self, limit: int = 32) -> list[Path]:
        with self.condition:
            items = ([self.startup_track] if self.startup_track is not None else []) + list(self.ready)
            return items[: max(0, int(limit))]

    def window(self, current: Path, limit: int = 32) -> list[Path]:
        return [current.resolve(), *self.peek(limit)]

    def current_window(self) -> list[Path]:
        with self.condition:
            return [self.current_path, *self.ready] if self.current_path is not None else []

    def finish_production(self, path: Path) -> None:
        """Advance the producer horizon; aired history still waits for the source clock."""
        with self.condition:
            if self.current_track_id == self.track_ids[path.resolve()]:
                self.current_track_id = ""
                self.current_path = None
                self._persist_checkpoint()

    def mark_played(self, path: Path, metadata: dict[str, object] | None = None) -> None:
        path = path.resolve()
        with self.condition:
            self.played_recent.append(path)
            if metadata is not None:
                self.played_recent_records.append({
                    "track_id": self.track_ids[path],
                    "title": str(metadata.get("title") or ""),
                    "artist": str(metadata.get("artist") or "Unknown artist"),
                })
            if self.current_track_id == self.track_ids[path]:
                self.current_track_id = ""
                self.current_path = None
            self._persist_checkpoint()
            self.condition.notify_all()

    def discard_current(self, path: Path) -> None:
        """Release a failed decode without recording it as an aired track."""
        path = path.resolve()
        with self.condition:
            if self.current_track_id == self.track_ids[path]:
                self.current_track_id = ""
                self.current_path = None
            self._persist_checkpoint()
            self.condition.notify_all()

    def iter_tracks(self):
        while not self.stop.is_set() and not self.closed.is_set():
            try:
                self.update_context(self.context_provider())
            except Exception as exc:
                with self.condition:
                    self.last_error = f"editorial context: {type(exc).__name__}: {exc}"[:240]
            path = self.next_track(timeout=2.0)
            if path is not None:
                yield path

    def snapshot(self) -> dict[str, object]:
        with self.condition:
            return {
                "mode": "laya-live-next-track",
                "queue_depth": len(self.ready),
                "queue_target": self.target_depth,
                "catalog_track_count": len(self.catalog),
                "reserved_track_count": len(self.reserved_track_ids),
                "selection_count": self.selection_count,
                "last_selected_track_id": self.last_selected_track_id,
                "last_event_id": self.last_event_id,
                "last_error": self.last_error,
                "current_track_id": self.current_track_id or None,
                "model_warmup_state": self.model_warmup_state,
                "checkpoint_error": self.checkpoint_error,
            }

    def close(self) -> None:
        self.closed.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread.is_alive():
            self.thread.join(timeout=15)

    def _run(self) -> None:
        if self.model_warmup is not None:
            with self.condition:
                while not self.started and not self.stop.is_set() and not self.closed.is_set():
                    self.condition.wait(1)
            while not self.stop.is_set() and not self.closed.is_set():
                try:
                    with self.condition:
                        self.model_warmup_state = "warming"
                    with SONG_SELECTION_LOCK:
                        self.model_warmup()
                    with self.condition:
                        self.model_warmup_state = "ready"
                        self.last_error = ""
                    break
                except Exception as exc:
                    with self.condition:
                        self.model_warmup_state = "failed"
                        self.last_error = f"model warmup: {type(exc).__name__}: {exc}"[:240]
                    self.closed.wait(5)
        while not self.stop.is_set() and not self.closed.is_set():
            with self.condition:
                while (
                    (not self.started or len(self.ready) >= self.target_depth)
                    and not self.stop.is_set()
                    and not self.closed.is_set()
                ):
                    self.condition.wait(1.0)
                if self.stop.is_set() or self.closed.is_set():
                    return
            with SONG_SELECTION_LOCK:
                self._choose_next()
            # Yield after one decision so the other station can acquire the
            # shared model before this producer attempts another queue slot.
            self.closed.wait(0.05)

    def _choose_next(self) -> None:
        on_air = self.on_air_context_getter() if self.on_air_context_getter is not None else None
        with self.condition:
            if self.stop.is_set() or self.closed.is_set() or not self.started or len(self.ready) >= self.target_depth:
                return
            context = dict(self.context)
            context_id = self._context_id(context)
            used = set(self.reserved_track_ids)
            available = [
                path for path in self.catalog
                if self.track_ids[path] not in used
            ]
            if not available:
                # Begin a new catalog cycle only after every track has
                # already been selected; never duplicate a queued track.
                queued_ids = {self.track_ids[path] for path in self.ready}
                if self.current_track_id:
                    queued_ids.add(self.current_track_id)
                self.reserved_track_ids = set(queued_ids)
                available = [
                    path for path in self.catalog
                    if self.track_ids[path] not in queued_ids
                ]
            queued_paths = list(self.ready)
            recent_paths = list(self.played_recent)
            recent_records = list(self.played_recent_records)
            actual_current_id = self.current_track_id
            if on_air is not None:
                actual_current_id = str(on_air.get("track_id") or "") if on_air.get("kind") == "music" else ""
                actual_path = self.paths_by_track_id.get(actual_current_id)
                if actual_path is not None:
                    recent_paths.append(actual_path)
                    if not recent_records or recent_records[-1]["track_id"] != actual_current_id:
                        recent_records.append({
                            "track_id": actual_current_id,
                            "title": str(on_air.get("title") or ""),
                            "artist": str(on_air.get("artist") or "Unknown artist"),
                        })
                if self.current_path is not None and self.current_track_id != actual_current_id:
                    queued_paths.insert(0, self.current_path)
            elif self.current_path is not None:
                recent_paths.append(self.current_path)
            recent = recent_paths[-6:]
            decision_context = dict(context)
            decision_context["live_selection_queue"] = {
                "mode": "laya-live-next-track",
                "catalog_track_count": len(self.catalog),
                "rotation_cycle_reserved_track_count": len(self.reserved_track_ids),
                "rotation_cycle_policy": "exclude_tracks_selected_in_current_catalog_cycle",
                "selection_started_at": datetime.now(timezone.utc).isoformat(),
                "current_track_id": actual_current_id or None,
                "queued_track_ids": [
                    self.track_ids[path] for path in queued_paths
                ],
                "queued_track_count": len(queued_paths),
                "queue_target_depth": self.target_depth,
                "candidate_pool_excludes_queued_tracks": True,
                "selection_timing": (
                    "rolling_while_current_track_plays"
                    if actual_current_id else (
                        "rolling_while_announcement_plays" if on_air and on_air.get("kind") == "talking"
                        else "startup_buffer_prefill"
                    )
                ),
            }
            if on_air is not None:
                decision_context["live_selection_queue"]["pipeline_current_track_id"] = self.current_track_id or None
                decision_context["live_selection_queue"]["current_track_clock"] = "sent-mp3-frame-samples"
        if not available:
            with self.condition:
                self.last_error = "no unreserved safe tracks remain for Laya"
                self.condition.wait(5.0)
            return
        try:
            upcoming_tracks = []
            for queued_path in queued_paths:
                if not queued_path.is_file():
                    continue
                try:
                    queued_title, queued_artist = track_metadata(queued_path)
                    upcoming_tracks.append({
                        "title": queued_title, "artist": queued_artist,
                        "genre": track_genre(self.station, queued_path),
                    })
                except Exception:
                    # A removed/unreadable queued file cannot provide musical
                    # facts. Its original ID still remains in queue evidence.
                    continue
            decision_context["upcoming_tracks"] = upcoming_tracks
            selected = autonomous_rotation(
                available,
                self.station,
                metadata_resolver=track_metadata,
                genre_resolver=lambda path: track_genre(self.station, path),
                selection_context=decision_context,
                decision_sink=self.decision_sink,
                max_tracks=1,
                one_choice_per_track=True,
                choice_id_resolver=lambda candidate: self.choice_ids[
                    candidate.resolve()
                ],
                recent_track_paths=recent,
                recent_track_records=(recent_records[-6:] if on_air is not None else None),
                update_recent_history=False,
            )
            if len(selected) != 1:
                raise RuntimeError("one Laya call must select exactly one next song")
            path = selected[0].resolve()
            track_id = self.track_ids[path]
            with self.condition:
                if self.closed.is_set() or self.stop.is_set():
                    return
                if self._context_id(self.context) != context_id:
                    return
                if path not in self.catalog:
                    # A removed file's real decision remains evidence, but
                    # cannot enter the audio queue after catalog refresh.
                    return
                if any(self.track_ids[item] == track_id for item in self.ready):
                    raise RuntimeError("Laya selected a track already reserved in the live queue")
                self.ready.append(path)
                self.reserved_track_ids.add(track_id)
                self.selection_count += 1
                status = autonomous_selection_status(self.station.station_id)
                self.last_event_id = str(status.get("last_event_id") or "")
                self.last_selected_track_id = track_id
                self.last_error = ""
                self._persist_checkpoint()
                self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                self.last_error = f"{type(exc).__name__}: {exc}"[:240]
                self.condition.wait(5.0)


class OutputWorker:
    def __init__(
        self,
        config: SupervisorConfig,
        station: StationConfig,
        spec: OutputSpec,
        password: str,
        status: DurableStatus,
        stop: threading.Event,
        on_playout_change: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.station = station
        self.spec = spec
        self.reconnect_total = 0
        self.pcm_replay = PcmReplayBuffer()
        self.password = password
        self.status = status
        self.stop = stop
        # Ten seconds of PCM absorbs short scheduler stalls without allowing an
        # unbounded backlog on this low-RAM broadcast PC.
        self.pcm: queue.Queue[bytes] = queue.Queue(maxsize=500)
        self.dropped_chunks = 0
        self.last_audio_sent_at = 0.0
        self.playout_clock = PlayoutClock()
        self.encoder_written_bytes = 0
        self.encoder_cycle_start_bytes = 0
        self.on_playout_change = on_playout_change

    def offer(self, chunk: bytes) -> None:
        if not chunk:
            return
        # Never discard program audio. Backpressure propagates through FFmpeg's
        # pipe while the network pacer keeps Icecast transmission real-time.
        while not self.stop.is_set():
            try:
                self.pcm.put(chunk, timeout=1)
                self.playout_clock.offered(len(chunk))
                return
            except queue.Full:
                continue

    def _send_encoded(
        self,
        stream: BinaryIO,
        cycle_stop: threading.Event,
        errors: queue.Queue[Exception],
        first_audio: threading.Event,
        replay_generation: int,
        cycle_start_bytes: int,
    ) -> None:
        read_available = getattr(stream, "read1", stream.read)
        target_bitrate = (
            self.config.bitrate_kbps
            if self.spec.output_id == "legacy"
            else self.spec.bitrate_kbps
        )
        set_audio_thread_priority()
        pacer = OutputPacer(target_bitrate) if target_bitrate > 0 else None
        source: IcecastSource | None = None
        frame_clock = Mp3FrameClock() if self.spec.output_id == "legacy" else None
        try:
            while not self.stop.is_set() and not cycle_stop.is_set():
                chunk = read_available(4_096)
                if not chunk:
                    raise RuntimeError("encoder output closed")
                if source is None:
                    # Connect only once encoded audio is ready. Opening the source
                    # earlier can trigger Icecast's source timeout while FFmpeg is
                    # still buffering its first frames.
                    source = IcecastSource(
                        host=self.config.icecast_host,
                        port=self.config.icecast_port,
                        mount=self.spec.mount,
                        user=self.config.icecast_user,
                        password=self.password,
                        name=self.spec.name,
                        content_type=self.spec.content_type,
                        public=self.spec.public,
                    )
                if pacer is None:
                    source.send(chunk)
                else:
                    pacer.send(source, chunk, cycle_stop)
                if cycle_stop.is_set() or self.stop.is_set():
                    break
                if frame_clock is not None:
                    samples = frame_clock.feed(chunk)
                    if frame_clock.sample_rate is not None and frame_clock.sample_rate != 48000:
                        raise RuntimeError("legacy playout clock requires 48 kHz MP3 frames")
                    confirmed = self.pcm_replay.confirm(
                        replay_generation, cycle_start_bytes + samples * 4
                    )
                    if confirmed is None:
                        break
                    self.playout_clock.advance(confirmed)
                self.last_audio_sent_at = time.monotonic()
                first_audio.set()
        except Exception as exc:
            try:
                errors.put_nowait(exc)
            except queue.Full:
                pass
            cycle_stop.set()
        finally:
            if source is not None:
                source.close(abort=True)

    def run(self) -> None:
        failures = 0
        while not self.stop.is_set():
            # Do not discard the first program chunks when the worker starts.
            replay_generation, cycle_start_bytes, replay_pending = self.pcm_replay.begin_cycle()
            self.encoder_cycle_start_bytes = cycle_start_bytes
            process: subprocess.Popen[bytes] | None = None
            cycle_stop = threading.Event()
            stderr_stop = threading.Event()
            errors: queue.Queue[Exception] = queue.Queue(maxsize=1)
            first_audio = threading.Event()
            # Keep encoder stdin writes off the timeline thread.  A pipe can
            # block indefinitely when an encoder or Icecast sender wedges;
            # bounding this hand-off lets the worker reconnect instead of
            # freezing the whole station on one blocked write.
            # Match the timeline's ten-second PCM budget.  FFmpeg can need a
            # few seconds to initialize its encoder and Icecast handshake;
            # keeping this bounded but larger than cold-start latency avoids a
            # false reconnect while still surfacing a real blocked pipe.
            encoder_input: queue.Queue[bytes | None] = queue.Queue(maxsize=500)
            writer: threading.Thread | None = None

            def write_encoder_input(
                cycle_process: subprocess.Popen[bytes],
                cycle_input: queue.Queue[bytes | None],
                writer_stop: threading.Event,
                writer_errors: queue.Queue[Exception],
            ) -> None:
                set_audio_thread_priority()
                try:
                    assert cycle_process.stdin is not None
                    while not self.stop.is_set() and not writer_stop.is_set():
                        try:
                            pcm = cycle_input.get(timeout=1)
                        except queue.Empty:
                            continue
                        if pcm is None:
                            return
                        offset = 0
                        while offset < len(pcm):
                            written = cycle_process.stdin.write(pcm[offset:])
                            if not written:
                                raise RuntimeError("encoder PCM pipe stopped accepting audio")
                            self.encoder_written_bytes += written
                            offset += written
                except Exception as exc:
                    try:
                        writer_errors.put_nowait(exc)
                    except queue.Full:
                        pass
                    writer_stop.set()
            try:
                process = subprocess.Popen(
                    build_encoder_command(self.config, self.spec),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    creationflags=_ffmpeg_creationflags(),
                )
                assert process.stdin is not None and process.stdout is not None
                assert process.stderr is not None
                threading.Thread(
                    target=_drain_stderr,
                    args=(process.stderr, f"{self.station.station_id}:{self.spec.output_id}", stderr_stop),
                    daemon=True,
                ).start()
                sender = threading.Thread(
                    target=self._send_encoded,
                    args=(process.stdout, cycle_stop, errors, first_audio, replay_generation, cycle_start_bytes),
                    daemon=True,
                )
                sender.start()
                writer = threading.Thread(
                    target=write_encoder_input,
                    args=(process, encoder_input, cycle_stop, errors),
                    name=f"{self.station.station_id}-encoder-writer-{self.spec.output_id}",
                    daemon=True,
                )
                writer.start()
                connected_at = time.monotonic()
                last_status_update = 0.0
                last_playout_revision = -1
                while not self.stop.is_set() and not cycle_stop.is_set():
                    chunk: bytes | None = None
                    try:
                        if replay_pending:
                            chunk = replay_pending.popleft()
                        else:
                            chunk = self.pcm.get(timeout=1)
                            if self.spec.output_id == "legacy":
                                self.pcm_replay.append(chunk)
                    except queue.Empty:
                        # Encoded audio may still be draining a bounded buffer.
                        # Input activity alone does not determine stream health.
                        if not first_audio.is_set() and time.monotonic() - connected_at > 60:
                            raise RuntimeError("shared program timeline did not start")
                    if chunk is not None:
                        delivered = False
                        while not self.stop.is_set() and not cycle_stop.is_set():
                            try:
                                encoder_input.put(chunk, timeout=1)
                                delivered = True
                                break
                            except queue.Full:
                                if not errors.empty():
                                    raise errors.get_nowait()
                                if (
                                    first_audio.is_set()
                                    and time.monotonic() - self.last_audio_sent_at > 10
                                ) or (
                                    not first_audio.is_set()
                                    and time.monotonic() - connected_at > 60
                                ):
                                    raise RuntimeError("encoder input stalled without audio progress")
                                # Preserve bounded backpressure while the sender
                                # is actually delivering audio; never disconnect
                                # a healthy mount solely because its input is full.
                        if not delivered:
                            break
                    if not errors.empty():
                        raise errors.get_nowait()
                    # A live encoder process can keep accepting PCM even when
                    # its Icecast sender thread is wedged.  Treat that as a
                    # worker failure so the outer loop closes the encoder and
                    # reconnects instead of leaving a silent mount online.
                    if (
                        first_audio.is_set()
                        and time.monotonic() - self.last_audio_sent_at > 10
                    ):
                        raise RuntimeError("encoded audio sender stalled")
                    if (
                        first_audio.is_set()
                        and time.monotonic() - self.last_audio_sent_at <= 10
                        and time.monotonic() - last_status_update >= 1.0
                    ):
                        # Durable JSON is not an audio-chunk log. Writing it
                        # for every PCM chunk stalled all outputs on Windows;
                        # one heartbeat per second preserves live observability.
                        reported_bitrate = (
                            self.config.bitrate_kbps
                            if self.spec.output_id == "legacy"
                            else self.spec.bitrate_kbps
                        )
                        playout = self.playout_clock.snapshot()
                        self.status.update(
                            self.station.station_id,
                            self.spec.output_id,
                            state="streaming",
                            mount=self.spec.mount,
                            codec_profile=self.spec.codec_profile,
                            bitrate_kbps=reported_bitrate,
                            process_id=process.pid,
                            reconnect_count=failures,
                            reconnect_total=self.reconnect_total,
                            dropped_pcm_chunks=self.dropped_chunks,
                            last_error="",
                            audio_sent_at=time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                            ),
                            **(
                                {
                                    "stream_startup_started_at": None,
                                    "stream_startup_stage": None,
                                }
                                if self.spec.output_id == "legacy"
                                else {}
                            ),
                            **(
                                {"now_playing": playout["now_playing"], "playout_clock": playout,
                                 "pcm_replay": self.pcm_replay.snapshot()}
                                if self.spec.output_id == "legacy" and playout["now_playing"] is not None
                                else {}
                            ),
                        )
                        if self.spec.output_id == "legacy":
                            self.playout_clock.dispatch()
                            if playout["revision"] != last_playout_revision:
                                last_playout_revision = int(playout["revision"])
                                if self.on_playout_change is not None:
                                    self.on_playout_change()
                        last_status_update = time.monotonic()
                    if process.poll() is not None:
                        raise RuntimeError(f"encoder exited with code {process.returncode}")
                if not errors.empty():
                    raise errors.get_nowait()
                if time.monotonic() - connected_at >= 300:
                    failures = 0
            except Exception as exc:
                failures += 1
                self.reconnect_total += 1
                self.status.update(
                    self.station.station_id,
                    self.spec.output_id,
                    state="reconnecting",
                    mount=self.spec.mount,
                    process_id=None,
                    reconnect_count=failures,
                    dropped_pcm_chunks=self.dropped_chunks,
                    last_error=str(exc)[:240],
                    last_reconnect_error=f"{type(exc).__name__}: {exc}"[:240],
                    last_reconnect_at=datetime.now(timezone.utc).isoformat(),
                    reconnect_total=self.reconnect_total,
                )
            finally:
                cycle_stop.set()
                stderr_stop.set()
                try:
                    encoder_input.put_nowait(None)
                except queue.Full:
                    pass
                if process is not None and process.poll() is None:
                    # Terminating the child also releases a writer blocked in
                    # the OS pipe.  The short join makes the next reconnect
                    # independent from that stale writer thread.
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                if writer is not None:
                    writer.join(timeout=2)
            if not self.stop.is_set():
                self.stop.wait(
                    RECONNECT_DELAYS_SECONDS[
                        min(failures - 1, len(RECONNECT_DELAYS_SECONDS) - 1)
                    ]
                )


class QualitySupervisor:
    def __init__(
        self,
        config: SupervisorConfig,
        password: str,
        bridge_path: Path | None,
    ) -> None:
        self.config = config
        self.password = password
        self.bridge_path = bridge_path
        self.stop = threading.Event()
        self.status = DurableStatus(config.state_file)
        self.public_sync: PublicApiSync | None = None
        self._warmup_condition = threading.Condition()
        self._warm_stations: set[str] = set()
        self._expected_warm_stations = {
            station.station_id for station in config.stations
        }

    def _wait_for_all_station_warmups(
        self,
        station: StationConfig,
        workers: list[OutputWorker],
        host_queue: DynamicHostQueue | None,
    ) -> bool:
        """Keep every encoder offline until every configured station is warm."""
        with self._warmup_condition:
            self._warm_stations.add(station.station_id)
            self._warmup_condition.notify_all()
            while (
                not self.stop.is_set()
                and not self._expected_warm_stations.issubset(self._warm_stations)
            ):
                warmup = {
                    "state": "waiting-for-all-stations",
                    "ready_stations": sorted(self._warm_stations),
                    "required_stations": sorted(self._expected_warm_stations),
                }
                for worker in workers:
                    self.status.update(
                        station.station_id,
                        worker.spec.output_id,
                        state="warming-announcements",
                        all_station_warmup=warmup,
                        host_queue=(
                            host_queue.snapshot()
                            if host_queue is not None
                            else {"mode": "disabled"}
                        ),
                    )
                self._warmup_condition.wait(timeout=1)
            ready = not self.stop.is_set()
            warmup = {
                "state": "ready" if ready else "stopped",
                "ready_stations": sorted(self._warm_stations),
                "required_stations": sorted(self._expected_warm_stations),
            }
            for worker in workers:
                self.status.update(
                    station.station_id,
                    worker.spec.output_id,
                    state="starting" if ready else "stopped",
                    all_station_warmup=warmup,
                    host_queue=(
                        host_queue.snapshot()
                        if host_queue is not None
                        else {"mode": "disabled"}
                    ),
                )
            return ready

    def _run_station_guarded(
        self,
        station: StationConfig,
        public_sync: PublicApiSync,
    ) -> None:
        try:
            self._run_station(station, public_sync)
        except Exception as exc:
            self.status.update(
                station.station_id,
                "legacy",
                state="blocked",
                process_id=None,
                last_error=f"{type(exc).__name__}: {exc}"[:240],
            )
            self.stop.set()
            with self._warmup_condition:
                self._warmup_condition.notify_all()

    def _monitor_origin_listener(self, station: StationConfig) -> None:
        retry_delay = ORIGIN_PROBE_INITIAL_DELAY_SECONDS
        last_ready_at: float | None = None
        last_success_at = ""
        consecutive_failures = 0
        while not self.stop.is_set():
            connection: http.client.HTTPConnection | None = None
            ready = False
            error = ""
            try:
                connection = http.client.HTTPConnection(
                    self.config.icecast_host,
                    self.config.icecast_port,
                    timeout=8,
                )
                connection.request(
                    "GET",
                    station.mount,
                    headers={
                        "User-Agent": "RadioTEDU-origin-health/1.0",
                        "Connection": "close",
                    },
                )
                response = connection.getresponse()
                content_type = (response.getheader("Content-Type") or "").lower()
                audio = response.read(1024)
                ready = (
                    response.status == 200
                    and content_type.startswith("audio/")
                    and len(audio) >= 256
                )
                if not ready:
                    error = f"listener probe returned HTTP {response.status} {content_type}".strip()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:240]
            finally:
                if connection is not None:
                    connection.close()

            now_monotonic = time.monotonic()
            if ready:
                last_ready_at = now_monotonic
                last_success_at = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                )
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            effective_ready = _origin_probe_effective_ready(
                ready, last_ready_at, now_monotonic
            )
            self.status.update(
                station.station_id,
                "legacy",
                origin_listener_ready=effective_ready,
                origin_probe_error=error,
                origin_probe_consecutive_failures=consecutive_failures,
                origin_probe_last_success_at=last_success_at,
                origin_probe_grace_seconds=ORIGIN_PROBE_GRACE_SECONDS,
                origin_probe_retry_seconds=(
                    ORIGIN_PROBE_INITIAL_DELAY_SECONDS if effective_ready else retry_delay
                ),
                origin_probed_at=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
            )
            wait_seconds = (
                ORIGIN_PROBE_INITIAL_DELAY_SECONDS if effective_ready else retry_delay
            )
            if effective_ready:
                retry_delay = ORIGIN_PROBE_INITIAL_DELAY_SECONDS
            else:
                retry_delay = min(
                    retry_delay * 2, ORIGIN_PROBE_MAX_DELAY_SECONDS
                )
            self.stop.wait(wait_seconds)

    def _run_station(
        self,
        station: StationConfig,
        public_sync: PublicApiSync,
    ) -> None:
        state_root = self.config.state_file.parent / "playlists"
        self.status.update(
            station.station_id,
            "legacy",
            state="selecting-songs",
            mount=station.mount,
            stream_startup_started_at=datetime.now(timezone.utc).isoformat(),
            stream_startup_stage="building_local_program",
            selection_model_id=station.selection_model_id,
            selection_model_revision=station.selection_model_revision,
        )
        editorial_block = current_editorial_program(station.station_id)
        live_song_queue: LiveLayaSongQueue | None = None
        safe_catalog: list[Path] | None = None
        if station.prebaked_transition_host:
            program, skipped_metadata = build_safe_program(
                station,
                state_root / f"{station.station_id}-autonomous-program.json",
                selection_context=editorial_block,
                decision_sink=public_sync.record_selection_decision,
            )
        else:
            safe_catalog, skipped_metadata = build_safe_catalog(station)
            stable_choice_ids = {
                path.resolve(): position
                for position, path in enumerate(
                    sorted(
                        (item.resolve() for item in safe_catalog),
                        key=public_track_id,
                    ),
                    start=1,
                )
            }
            archive_file = state_root / f"{station.station_id}-laya-decisions.json"
            try:
                archived = json.loads(archive_file.read_text(encoding="utf-8-sig"))
                if not isinstance(archived, list):
                    archived = []
            except (OSError, ValueError, TypeError):
                archived = []
            # The API outbox also contains real decisions made before this
            # durable restart archive was introduced. Keep original event IDs.
            pending = public_sync.pending_selection_events(station.station_id, limit=20)
            by_event = {
                str(event["event_id"]): event
                for event in [*archived, *pending]
                if isinstance(event, dict) and event.get("event_id")
            }
            decision_archive = sorted(
                by_event.values(), key=lambda event: str(event.get("occurred_at") or "")
            )[-20:]
            archive_lock = threading.Lock()

            def record_live_decision(event: dict[str, object]) -> bool:
                if public_sync.record_selection_decision(event) is False:
                    return False
                with archive_lock:
                    decision_archive.append(dict(event))
                    del decision_archive[:-20]
                    return write_status_atomic(
                        archive_file, json.dumps(decision_archive, ensure_ascii=False) + "\n"
                    )

            checkpoint_file = state_root / f"{station.station_id}-live-song-queue.json"
            checkpoint = load_live_queue_checkpoint(checkpoint_file, station.station_id)
            startup_choices = restore_live_queue_tracks(
                safe_catalog, checkpoint, decision_archive, station.station_id,
            )
            startup_source = "resumed_unconsumed_laya_queue"
            if not startup_choices:
                checkpoint = {}
                startup_choices = restore_laya_startup_tracks(
                    safe_catalog, station.station_id, str(editorial_block["id"]), decision_archive
                )
                startup_source = "restored_real_laya_decisions"
            if startup_source != "resumed_unconsumed_laya_queue" and len(startup_choices) < LIVE_LAYA_STARTUP_TRACKS:
                recovered = restore_laya_startup_tracks(
                    safe_catalog, station.station_id, str(editorial_block["id"]),
                    decision_archive, allow_prior_program=True,
                )
                if len(recovered) >= LIVE_LAYA_STARTUP_TRACKS:
                    startup_choices = recovered
                    startup_source = "restored_real_laya_decisions_prior_program"
            if not startup_choices or (len(startup_choices) < 3 and startup_source != "resumed_unconsumed_laya_queue"):
                startup_source = "fresh_laya_decisions"
                startup_choices = autonomous_rotation(
                    safe_catalog, station,
                    metadata_resolver=track_metadata,
                    genre_resolver=lambda path: track_genre(station, path),
                    selection_context=editorial_block,
                    decision_sink=record_live_decision,
                    max_tracks=min(LIVE_LAYA_STARTUP_TRACKS, len(safe_catalog)),
                    one_choice_per_track=True,
                    choice_id_resolver=lambda path: stable_choice_ids[path.resolve()],
                )
            elif not write_status_atomic(
                archive_file, json.dumps(decision_archive, ensure_ascii=False) + "\n"
            ):
                raise RuntimeError("could not persist real Laya startup decision archive")
            self.status.update(
                station.station_id, "legacy", laya_startup_source=startup_source,
                restored_decisions_are_new_inference=False,
            )
            program = startup_choices
            live_song_queue = LiveLayaSongQueue(
                station=station,
                catalog=safe_catalog,
                initial_tracks=startup_choices,
                selection_context=editorial_block,
                context_provider=lambda: current_editorial_program(station.station_id),
                decision_sink=record_live_decision,
                stop=self.stop,
                target_depth=LIVE_LAYA_QUEUE_DEPTH,
                # Workers are constructed before the queue starts playout.
                # Read the sender's current frame state directly, rather than
                # the one-second durable status heartbeat.
                on_air_context_getter=lambda: dict(workers[0].playout_clock.snapshot()["now_playing"] or {}),
                model_warmup=lambda: assert_autonomous_song_ai_ready(station),
                checkpoint_file=checkpoint_file,
                restored_checkpoint=checkpoint,
            )
            live_song_queue.start()

            def refresh_live_catalog() -> None:
                while not self.stop.wait(LIVE_CATALOG_REFRESH_SECONDS):
                    if live_song_queue.closed.is_set():
                        return
                    try:
                        refreshed, skipped = build_safe_catalog(station)
                        changed = live_song_queue.update_catalog(refreshed)
                        updates: dict[str, object] = {
                            "catalog_track_count": len(refreshed),
                            "track_count": len(refreshed),
                            "metadata_skipped_count": skipped,
                            "live_catalog_scan_error": "",
                            "live_catalog_scanned_at": datetime.now(timezone.utc).isoformat(),
                        }
                        if changed:
                            updates["live_catalog_changed_at"] = datetime.now(timezone.utc).isoformat()
                        self.status.update(station.station_id, "legacy", **updates)
                    except Exception as exc:
                        # Retain the last validated pool and prepared music.
                        self.status.update(
                            station.station_id, "legacy",
                            live_catalog_scan_error=f"{type(exc).__name__}: {exc}"[:240],
                        )

            threading.Thread(
                target=refresh_live_catalog,
                name=f"ai-catalog-refresh-{station.station_id}",
                daemon=True,
            ).start()
        air_program = (
            list(program) if live_song_queue is not None
            else program_for_editorial_block(program, station, editorial_block)
        )
        self.status.update(
            station.station_id,
            "legacy",
            state="starting",
            stream_startup_stage="connecting_stream_outputs",
            prepared_program_items=len(air_program),
        )
        playlist, track_count = write_playlist(station, state_root, air_program)
        self.status.update(
            station.station_id,
            "legacy",
            origin_listener_ready=False,
            origin_probe_error="listener probe pending",
            metadata_skipped_count=skipped_metadata,
        )
        threading.Thread(
            target=self._monitor_origin_listener,
            args=(station,),
            name=f"ai-origin-probe-{station.station_id}",
            daemon=True,
        ).start()
        specs = _quality_specs(self.bridge_path, station)
        workers = [
            OutputWorker(
                self.config, station, spec, self.password, self.status, self.stop,
                on_playout_change=public_sync.wake,
            )
            for spec in specs
        ]
        threads = [
            threading.Thread(
                target=worker.run,
                name=f"ai-output-{station.station_id}-{worker.spec.output_id}",
                daemon=True,
            )
            for worker in workers
        ]
        for worker in workers:
            self.status.update(
                station.station_id,
                worker.spec.output_id,
                state="starting",
                mount=worker.spec.mount,
                track_count=track_count,
            )
        language = "en" if station.station_id == "radiotedu-en" else "fr"
        host_queue = (
            DynamicHostQueue(
                language=language,
                voice_design=station.host_voice_design,
                night_voice_design=station.host_night_voice_design,
                qwen_url=station.host_qwen_url,
                model=station.host_model,
                ollama_url=station.host_ollama_url,
                root=self.config.state_file.parent / "host-queue" / station.station_id,
                stop=self.stop,
                lead_min=min(station.host_lead_songs_min, LIVE_LAYA_QUEUE_DEPTH) if live_song_queue else station.host_lead_songs_min,
                lead_max=min(station.host_lead_songs_max, LIVE_LAYA_QUEUE_DEPTH) if live_song_queue else station.host_lead_songs_max,
                queue_target=station.host_queue_target,
                startup_min=station.host_queue_start_min,
                rolling_prepare=station.host_rolling_prepare,
                seed=station.playout_seed,
            )
            if station.dynamic_host_enabled
            else None
        )
        transition_library = (
            TransitionLinerLibrary(
                root=station.transition_liner_root,
                language=language,
                required_genres=station.transition_liner_genres,
                min_variants=station.transition_liner_min_variants,
                timezone_name=station.transition_liner_timezone,
                seed=station.playout_seed,
            )
            if station.prebaked_transition_host
            and station.transition_liner_root is not None
            else None
        )
        if transition_library is not None:
            library_status = transition_library.validate()
            for worker in workers:
                self.status.update(
                    station.station_id,
                    worker.spec.output_id,
                    state="warming-announcements",
                    host_library=library_status,
                    queue_preview=queue_preview(
                        air_program,
                        0,
                        station,
                        transition_library,
                    ),
        )
        pcm_prefetch = PcmPrefetch(
            root=self.config.state_file.parent / "pcm-prefetch" / station.station_id,
            command=lambda item: build_track_decoder_command(self.config, item),
            stop=self.stop,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ) if live_song_queue is not None else None
        if pcm_prefetch is not None:
            pcm_prefetch.start()
            pcm_prefetch.set_horizon(air_program)

        if host_queue is not None or live_song_queue is not None:
            if host_queue is not None:
                host_queue.start(invalidate_restored_contexts=live_song_queue is not None)
            def report_host_queue() -> None:
                while not self.stop.wait(5):
                    queue_status: dict[str, object] = {}
                    if live_song_queue is not None:
                        horizon = live_song_queue.current_window() or live_song_queue.peek()
                        # These counts describe the live producer horizon, not
                        # the startup playlist written before streaming began.
                        queue_status = {
                            "program_items": len(horizon),
                            "prepared_program_items": len(horizon),
                            "prepared_buffer_track_count": len(horizon),
                            "queue_preview": queue_preview(
                                horizon, 0, station, transition_library,
                                limit=max(1, len(horizon)),
                            ),
                        }
                    if live_song_queue is not None and host_queue is not None:
                        # Pair the music horizon with the host countdown under
                        # the same guard used when advancing a song boundary.
                        with live_song_queue.condition:
                            horizon = live_song_queue.current_window() or live_song_queue.peek()
                            if horizon:
                                prepare_host_horizon(horizon, limit=host_queue.rolling_prepare, wrap=False)
                    if live_song_queue is not None and pcm_prefetch is not None:
                        music = live_song_queue.current_window() or live_song_queue.peek()
                        speech = host_queue.prepared_clips() if host_queue is not None else []
                        pcm_prefetch.set_horizon([*speech, *music])
                    self.status.update(
                        station.station_id,
                        "legacy",
                        host_queue=(
                            host_queue.snapshot()
                            if host_queue is not None
                            else {"mode": "disabled"}
                        ),
                        laya_song_queue=(
                            live_song_queue.snapshot()
                            if live_song_queue is not None
                            else {"mode": "disabled"}
                        ),
                        pcm_prefetch=(pcm_prefetch.snapshot() if pcm_prefetch is not None else {"mode": "disabled"}),
                        **queue_status,
                    )

        def prepare_host_horizon(
            items: list[Path],
            start_index: int = 0,
            limit: int | None = None,
            offset_start: int = 0,
            wrap: bool = True,
        ) -> None:
            if host_queue is None or not items:
                return
            host_snapshot = host_queue.snapshot()
            queue_depth = int(host_snapshot.get("queue_depth") or 0)
            scheduled_offsets = host_snapshot["scheduled_song_offsets"]
            scheduled_sequences = host_snapshot["scheduled_sequences"]
            first_offset = max(0, min(int(offset_start), max(0, queue_depth - 1)))
            last_offset = queue_depth
            if limit is not None:
                last_offset = min(queue_depth, first_offset + max(1, int(limit)))
            for offset in range(first_offset, last_offset):
                if offset >= len(scheduled_offsets):
                    break
                context_index = start_index + scheduled_offsets[offset]
                following_index = context_index + 1
                if wrap:
                    context_index %= len(items)
                    following_index %= len(items)
                elif following_index >= len(items):
                    # Live Laya contexts are only prepared when both songs
                    # are already in the real rolling queue.
                    break
                context_title, context_artist = track_metadata(items[context_index])
                following_title, following_artist = track_metadata(items[following_index])
                host_queue.prepare_for_song(
                    title=context_title,
                    artist=context_artist or "",
                    next_title=following_title,
                    next_artist=following_artist or "",
                    offset=offset,
                    expected_sequence=scheduled_sequences[offset],
                )

        if host_queue is not None or live_song_queue is not None:
            threading.Thread(
                target=report_host_queue,
                name=f"ai-host-status-{station.station_id}",
                daemon=True,
            ).start()

        if host_queue is not None:
            # A persisted audio slot may describe the interrupted program.
            # Recheck the live program's first rolling window before trusting
            # any restored announcement as ready to air.
            prepare_host_horizon(
                live_song_queue.peek() if live_song_queue else air_program,
                limit=host_queue.rolling_prepare,
                offset_start=0,
                wrap=live_song_queue is None,
            )

        if host_queue is not None and station.host_queue_start_min > 0:
            for worker in workers:
                self.status.update(
                    station.station_id,
                    worker.spec.output_id,
                    state="warming-announcements",
                    host_queue=host_queue.snapshot(),
                )
            while not self.stop.is_set():
                snapshot = host_queue.snapshot()
                prepared_ahead = int(snapshot.get("prepared_ahead_count") or 0)
                # The Qwen model is intentionally single-flight on this CPU.
                # Prepare exactly the next contiguous context at startup so
                # ten ready links form a real no-gap horizon; launching all
                # ten requests at once lets a later French/English offset
                # finish first and leaves the head of the queue unready.
                if prepared_ahead < station.host_queue_start_min:
                    prepare_host_horizon(
                        live_song_queue.peek() if live_song_queue else air_program,
                        limit=1,
                        offset_start=prepared_ahead,
                        wrap=live_song_queue is None,
                    )
                self.status.update(
                    station.station_id,
                    "legacy",
                    state="warming-announcements",
                    host_queue=snapshot,
                )
                if int(snapshot.get("prepared_ahead_count") or 0) >= station.host_queue_start_min:
                    break
                self.stop.wait(5)
        if pcm_prefetch is not None and air_program:
            # Prepare initial audio before connecting the encoder mounts.
            pcm_prefetch.wait_ready(air_program[0], timeout=30)
        if not self._wait_for_all_station_warmups(station, workers, host_queue):
            if host_queue is not None:
                host_queue.close()
            if live_song_queue is not None:
                live_song_queue.close()
            if pcm_prefetch is not None:
                pcm_prefetch.close()
            return
        for thread in threads:
            thread.start()
        failures = 0
        try:
            while not self.stop.is_set():
                try:
                    catalog_signature = (
                        () if live_song_queue is not None else tuple(
                            str(item).casefold() for item in discover_audio(station)
                        )
                    )
                    editorial_block = current_editorial_program(station.station_id)
                    if live_song_queue is not None:
                        live_song_queue.update_context(editorial_block)
                        air_program = live_song_queue.peek()
                    else:
                        air_program = program_for_editorial_block(
                            program,
                            station,
                            editorial_block,
                        )
                    public_program = public_editorial_program(editorial_block)
                    playlist, track_count = write_playlist(
                        station,
                        state_root,
                        air_program,
                    )
                    self.status.update(
                        station.station_id,
                        "legacy",
                        playlist_generation=playlist.name,
                        track_count=(len(live_song_queue.catalog) if live_song_queue is not None else track_count),
                        program_items=track_count,
                        prepared_buffer_track_count=track_count,
                        catalog_track_count=(
                            len(live_song_queue.catalog) if live_song_queue is not None else len(program)
                        ),
                        current_program=public_program,
                        enforced_genres=list(
                            editorial_block.get("required_genres") or []
                        ),
                        selection_mode=(
                            "prebaked-transition-host"
                            if transition_library is not None
                            else (
                                "laya-live-next-track"
                                if live_song_queue is not None
                                else "autonomous-local-ai-music-kokoro-host"
                            )
                        ),
                        autonomous_selection=autonomous_selection_status(
                            station.station_id
                        ),
                        live_catalog=True,
                        metadata_skipped_count=skipped_metadata,
                        host_queue=(
                            host_queue.snapshot()
                            if host_queue
                            else (
                                transition_library.snapshot()
                                if transition_library is not None
                                else {"mode": "disabled"}
                            )
                        ),
                        laya_song_queue=(
                            live_song_queue.snapshot()
                            if live_song_queue is not None
                            else {"mode": "disabled"}
                        ),
                        queue_preview=queue_preview(
                            air_program,
                            0,
                            station,
                            transition_library,
                            limit=(
                                max(1, len(air_program))
                                if live_song_queue is not None and air_program
                                else 8
                            ),
                        ),
                    )
                    connected_at = time.monotonic()
                    track_source = (
                        live_song_queue.iter_tracks()
                        if live_song_queue is not None
                        else iter(air_program)
                    )
                    for track_index, track in enumerate(track_source):
                        if self.stop.is_set():
                            break
                        latest_block = current_editorial_program(station.station_id)
                        if latest_block["id"] != editorial_block["id"]:
                            if live_song_queue is None:
                                break
                            live_song_queue.update_context(latest_block)
                            editorial_block = latest_block
                            public_program = public_editorial_program(editorial_block)
                        if live_song_queue is not None:
                            air_program = live_song_queue.window(track)
                            track_index = 0
                        title, artist = track_metadata(track)
                        next_track = (
                            air_program[track_index + 1]
                            if track_index + 1 < len(air_program)
                            else (air_program[0] if live_song_queue is None else None)
                        )
                        if host_queue is not None:
                            queue_snapshot = host_queue.snapshot()
                            every_song = host_queue.lead_min == host_queue.lead_max == 1
                            prepared_ahead = int(
                                queue_snapshot.get("prepared_ahead_count") or 0
                            )
                            prepare_host_horizon(
                                air_program,
                                track_index,
                                limit=host_queue.rolling_prepare,
                                offset_start=(0 if live_song_queue is not None else prepared_ahead),
                                wrap=live_song_queue is None,
                            )
                        started_at = datetime.now(timezone.utc)
                        started_monotonic = time.monotonic()
                        self.status.update(
                            station.station_id,
                            "legacy",
                            preparing_playout={
                                "kind": "music",
                                "title": title,
                                "artist": artist,
                                "genre": track_genre(station, track),
                                "track_id": public_track_id(track),
                                "sound_tags": [track_genre(station, track)]
                                if track_genre(station, track)
                                else [],
                                "started_at": started_at.isoformat(),
                            },
                            current_program=public_program,
                            queue_preview=queue_preview(
                                air_program,
                                track_index,
                                station,
                                transition_library,
                                limit=(
                                    max(1, len(air_program))
                                    if live_song_queue is not None
                                    else 8
                                ),
                            ),
                            laya_song_queue=(
                                live_song_queue.snapshot()
                                if live_song_queue is not None
                                else {"mode": "disabled"}
                            ),
                        )
                        try:
                            music_span = self._decode_item(station, track, workers, pcm_prefetch)
                        except Exception:
                            if live_song_queue is not None:
                                live_song_queue.discard_current(track)
                            raise
                        if live_song_queue is not None:
                            workers[0].playout_clock.on_complete(
                                music_span,
                                lambda span, aired_track=track: live_song_queue.mark_played(aired_track, span.metadata),
                            )
                        if not self.stop.is_set():
                            public_sync.record_play_on_air(
                                workers[0].playout_clock, music_span,
                                station_id=station.station_id,
                                classification="music",
                                duration_ms=int(
                                    max(0.0, time.monotonic() - started_monotonic) * 1_000
                                ),
                                occurred_at=started_at.isoformat(),
                                track_id=public_track_id(track),
                                title=title,
                                artist=artist,
                                genre=track_genre(station, track),
                                program_name=str(editorial_block["name"]),
                            )
                        with (live_song_queue.condition if live_song_queue is not None else nullcontext()):
                            liner_selection = None
                            if transition_library is not None:
                                liner_selection = transition_library.choose(
                                    track_genre(station, track),
                                    track_genre(station, next_track),
                                )
                                host_clip = liner_selection.path
                            else:
                                following_title, following_artist = track_metadata(next_track) if next_track is not None else ("", "")
                                host_clip = (
                                    host_queue.after_song(
                                        skip_unprepared=every_song,
                                        expected_song_context={
                                            "previous_title": title, "previous_artist": artist,
                                            "next_title": following_title, "next_artist": following_artist,
                                        },
                                    )
                                    if host_queue
                                    else None
                                )
                            if live_song_queue is not None and not self.stop.is_set():
                                live_song_queue.finish_production(track)
                        if host_clip is not None and next_track is None:
                            # No announcement may claim a next title until
                            # Laya has placed that song in the live queue.
                            if host_queue is not None:
                                host_queue.played(host_clip)
                            host_clip = None
                        self.status.update(
                            station.station_id,
                            "legacy",
                            host_queue=(
                                host_queue.snapshot()
                                if host_queue
                                else (
                                    transition_library.snapshot()
                                    if transition_library is not None
                                    else {"mode": "disabled"}
                                )
                            ),
                        )
                        if host_clip is not None:
                            announcement_text = (
                                liner_selection.text
                                if liner_selection is not None
                                else (
                                    host_queue.announcement_line(host_clip)
                                    if host_queue is not None
                                    else None
                                )
                            )
                            if transition_library is None:
                                for worker in workers:
                                    worker.offer(PRE_HOST_SILENCE)
                            host_completed = False
                            try:
                                host_started_at = datetime.now(timezone.utc)
                                host_started_monotonic = time.monotonic()
                                self.status.update(
                                    station.station_id,
                                    "legacy",
                                    preparing_playout={
                                        "kind": "talking",
                                        "title": announcement_text or "RadioTEDU announcer",
                                        "artist": "RadioTEDU AI announcer",
                                        "track_id": public_track_id(host_clip),
                                "sound_tags": ["warm"],
                                        "started_at": host_started_at.isoformat(),
                                    },
                                )
                                host_span = self._decode_item(station, host_clip, workers, pcm_prefetch)
                                host_completed = True
                                if transition_library is None:
                                    for worker in workers:
                                        worker.offer(POST_HOST_SILENCE)
                                if announcement_text:
                                    if liner_selection is not None:
                                        segment_metadata = {
                                            "voice_design": (
                                                f"{liner_selection.language}-"
                                                f"{'night' if 'night' in liner_selection.tone else 'day'}"
                                            )
                                        }
                                    else:
                                        segment_metadata = (
                                            host_queue.announcement_metadata(host_clip)
                                            if host_queue is not None
                                            else {}
                                        )
                                    following_title, following_artist = track_metadata(next_track)
                                    spoken_transcript = normalize_tts_text(
                                        normalize_station_name(announcement_text)
                                    )
                                    public_sync.record_spoken_on_air(
                                        workers[0].playout_clock, host_span,
                                        station_id=station.station_id,
                                        transcript=spoken_transcript,
                                        occurred_at=host_started_at.isoformat(),
                                        completed_at=datetime.now(timezone.utc).isoformat(),
                                        duration_ms=int(
                                            max(
                                                0.0,
                                                time.monotonic() - host_started_monotonic,
                                            )
                                            * 1_000
                                        ),
                                        language="en" if station.station_id.endswith("-en") else "fr",
                                        segment_type=(
                                            "transition_liner"
                                            if liner_selection is not None
                                            else "track_link"
                                        ),
                                        well_wish=bool(segment_metadata.get("well_wish")),
                                        program_id=str(editorial_block["id"]),
                                        program_name=str(editorial_block["name"]),
                                        preceding_track={
                                            "track_id": public_track_id(track),
                                            "title": title[:200],
                                            "artist": artist[:160] if artist else None,
                                        },
                                        following_track={
                                            "track_id": public_track_id(next_track),
                                            "title": following_title[:200],
                                            "artist": following_artist[:160]
                                            if following_artist
                                            else None,
                                        },
                                        text_model=(
                                            str(segment_metadata["text_model"])
                                            if segment_metadata.get("text_model")
                                            else None
                                        ),
                                        tts_engine=(
                                            str(segment_metadata["tts_engine"])
                                            if segment_metadata.get("tts_engine")
                                            else None
                                        ),
                                        tts_model=(
                                            str(segment_metadata["tts_model"])
                                            if segment_metadata.get("tts_model")
                                            else None
                                        ),
                                        voice_design=(
                                            str(segment_metadata["voice_design"])
                                            if segment_metadata.get("voice_design")
                                            else None
                                        ),
                                        source=(
                                            "pre_rendered_transition"
                                            if liner_selection is not None
                                            else "ai_song_context"
                                        ),
                                    )
                                if not self.stop.is_set():
                                    public_sync.record_play_on_air(
                                        workers[0].playout_clock, host_span,
                                        station_id=station.station_id,
                                        classification="talking",
                                        duration_ms=int(
                                            max(
                                                0.0,
                                                time.monotonic() - host_started_monotonic,
                                            )
                                            * 1_000
                                        ),
                                        occurred_at=host_started_at.isoformat(),
                                        track_id=public_track_id(host_clip),
                                        title=announcement_text or "RadioTEDU announcer",
                                        artist="RadioTEDU AI announcer",
                                        genre=None,
                                        program_name=str(editorial_block["name"]),
                                        sound_tags=["warm"],
                                    )
                            except Exception as exc:
                                if transition_library is None:
                                    raise
                                # A damaged optional liner must never interrupt
                                # the PCM timeline. Continue directly into music.
                                self.status.update(
                                    station.station_id,
                                    "legacy",
                                    transition_liner_error=str(exc)[:240],
                                    transition_liner_failed=host_clip.name,
                                )
                            finally:
                                if host_queue is not None:
                                    host_queue.played(host_clip)
                            if not host_completed:
                                for worker in workers:
                                    worker.offer(BETWEEN_SONGS_SILENCE)
                        else:
                            for worker in workers:
                                worker.offer(BETWEEN_SONGS_SILENCE)
                        if live_song_queue is not None:
                            # Background refresh owns all catalog I/O in live
                            # mode; a song boundary only advances ready audio.
                            continue
                        current_catalog = discover_audio(station)
                        current_signature = tuple(
                            str(item).casefold() for item in current_catalog
                        )
                        if current_signature != catalog_signature:
                            self.status.update(
                                station.station_id,
                                "legacy",
                                live_catalog_changed_at=time.strftime(
                                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                ),
                                track_count=len(current_catalog),
                            )
                            # Re-enter only through a clean service start, which
                            # rebuilds the AI program and its entire Qwen horizon
                            # before any encoder can reconnect.
                            self.stop.set()
                            break
                    if time.monotonic() - connected_at >= 300:
                        failures = 0
                except Exception as exc:
                    failures += 1
                    for worker in workers:
                        self.status.update(
                            station.station_id,
                            worker.spec.output_id,
                            timeline_state="restarting",
                            timeline_error=str(exc)[:240],
                        )
                if not self.stop.is_set() and failures > 0:
                    self.stop.wait(
                        RECONNECT_DELAYS_SECONDS[
                            min(failures - 1, len(RECONNECT_DELAYS_SECONDS) - 1)
                        ]
                    )
        finally:
            if host_queue is not None:
                host_queue.close()
            if live_song_queue is not None:
                live_song_queue.close()
            if pcm_prefetch is not None:
                pcm_prefetch.close()
            for thread in threads:
                thread.join(timeout=10)

    def _decode_item(
        self,
        station: StationConfig,
        item: Path,
        workers: list[OutputWorker],
        pcm_prefetch: PcmPrefetch | None = None,
    ) -> AirSegment:
        local = self.status.station(station.station_id)["outputs"]["legacy"]
        metadata = dict(local["preparing_playout"])
        segment = workers[0].playout_clock.begin(metadata)
        try:
            self._feed_item(station, item, workers, pcm_prefetch)
        finally:
            workers[0].playout_clock.finish(segment)
        return segment

    def _feed_item(
        self,
        station: StationConfig,
        item: Path,
        workers: list[OutputWorker],
        pcm_prefetch: PcmPrefetch | None = None,
    ) -> None:
        if pcm_prefetch is not None:
            with pcm_prefetch.open_ready(item) as prepared:
                if prepared is not None:
                    while not self.stop.is_set():
                        chunk = prepared.read(PCM_CHUNK_BYTES)
                        if not chunk:
                            break
                        for worker in workers:
                            worker.offer(chunk)
                    return
        process: subprocess.Popen[bytes] | None = None
        stderr_stop = threading.Event()
        try:
            process = subprocess.Popen(
                build_track_decoder_command(self.config, item),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=_ffmpeg_creationflags(),
            )
            assert process.stdout is not None and process.stderr is not None
            threading.Thread(
                target=_drain_stderr,
                args=(process.stderr, f"{station.station_id}:timeline", stderr_stop),
                daemon=True,
            ).start()
            for chunk in iter_trimmed_pcm(process.stdout, PCM_CHUNK_BYTES):
                if self.stop.is_set():
                    break
                for worker in workers:
                    worker.offer(chunk)
            return_code = process.wait(timeout=5)
            if return_code != 0:
                raise RuntimeError(f"track decoder exited with code {return_code}: {item.name}")
        finally:
            stderr_stop.set()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

    def run(self, blocked_reason: str | None = None) -> int:
        if os.name == "nt":
            # Heavy Laya inference shares this process with control threads.
            # The FFmpeg audio children run above normal; keep inference at
            # normal priority so it cannot starve realtime audio encoders.
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x20):
                raise RuntimeError("could not apply normal AI process priority")
        start_station_health_servers(self.config, self.stop, self.status.station)
        public_sync = PublicApiSync(self.config, self.status, self.stop)
        self.public_sync = public_sync
        if blocked_reason:
            for station in self.config.stations:
                self.status.update(
                    station.station_id,
                    "legacy",
                    state="blocked",
                    mount=station.mount,
                    process_id=None,
                    last_error=blocked_reason,
                )
            public_sync.start()
            while not self.stop.wait(2):
                pass
            public_sync.join()
            return 0
        threads = [
            threading.Thread(
                target=self._run_station_guarded,
                args=(station, public_sync),
                name=f"ai-timeline-{station.station_id}",
                daemon=True,
            )
            for station in self.config.stations
        ]
        for thread in threads:
            thread.start()
        public_sync.start()
        try:
            while any(thread.is_alive() for thread in threads):
                self.stop.wait(2)
                if self.stop.is_set():
                    break
        finally:
            self.stop.set()
            public_sync.join()
            for thread in threads:
                thread.join(timeout=15)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RadioTEDU synchronized EN/FR quality stream supervisor"
    )
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_config(config_path)
    blocked_reasons: list[str] = []
    try:
        password = resolve_source_password(config)
    except RuntimeError:
        password = ""
        blocked_reasons.append("protected Icecast source credential is unavailable")
    for station in config.stations:
        try:
            if not discover_audio(station):
                blocked_reasons.append(f"no rights-cleared audio is available for {station.station_id}")
        except (OSError, RuntimeError, ValueError):
            blocked_reasons.append(f"rights-cleared audio preflight failed for {station.station_id}")
    supervisor = QualitySupervisor(
        config, password, quality_outputs_path(config_path)
    )

    def stop_handler(_signum: int, _frame: object) -> None:
        supervisor.stop.set()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    return supervisor.run("; ".join(blocked_reasons) if blocked_reasons else None)


if __name__ == "__main__":
    raise SystemExit(main())
