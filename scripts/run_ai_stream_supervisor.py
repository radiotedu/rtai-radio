from __future__ import annotations

import argparse
import base64
import calendar
import hashlib
import http.server
import json
import os
import queue
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from mutagen import File as MutagenFile

try:
    from .metadata_cleanup import clean_track_metadata
except ImportError:
    from metadata_cleanup import clean_track_metadata

try:
    from .laya_song_selector import (
        LAYA_MODEL_ID,
        LAYA_MODEL_REVISION,
        LAYA_LIBRARY_SHORTLIST_SIZE,
        SELECTION_POLICY_SHA256,
        choice_key as laya_choice_key,
        choose_song as laya_choose_song,
        load_agent as load_laya_agent,
    )
except ImportError:
    from laya_song_selector import (
        LAYA_MODEL_ID,
        LAYA_MODEL_REVISION,
        LAYA_LIBRARY_SHORTLIST_SIZE,
        SELECTION_POLICY_SHA256,
        choice_key as laya_choice_key,
        choose_song as laya_choose_song,
        load_agent as load_laya_agent,
    )


SUPPORTED_AUDIO_EXTENSIONS = frozenset(
    {".aac", ".ape", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm"}
)
RECONNECT_DELAYS_SECONDS = (1, 2, 4, 8, 15, 30)
AUTONOMOUS_PROGRAM_TRACKS = 13
LIVE_LAYA_QUEUE_DEPTH = 4
# First item plus four future choices: startup playout does not consume the
# desired future horizon while the model is still loading.
LIVE_LAYA_STARTUP_TRACKS = LIVE_LAYA_QUEUE_DEPTH + 1
SONG_SELECTION_LOCK = threading.RLock()
_RECENT_AUTONOMOUS_TRACKS: dict[str, list[Path]] = {}
_AUTONOMOUS_SELECTION_STATUS: dict[str, dict[str, object]] = {}


@dataclass(frozen=True)
class StationConfig:
    station_id: str
    mount: str
    name: str
    music_roots: tuple[Path, ...]
    preferred_extensions: tuple[str, ...]
    health_port: int
    dynamic_host_enabled: bool = False
    host_lead_songs_min: int = 3
    host_lead_songs_max: int = 5
    host_ollama_url: str = "http://127.0.0.1:11434"
    host_model: str = "qwen3:0.6b"
    selection_model_id: str = LAYA_MODEL_ID
    selection_model_revision: str = LAYA_MODEL_REVISION
    selection_cache_dir: Path = Path(r"C:\ProgramData\RadioTEDU\cache\laya")
    host_qwen_url: str = "http://127.0.0.1:8090"
    host_voice_design: str = ""
    host_night_voice_design: str = ""
    playout_seed: int = 20260813
    public_stream_url: str = ""
    public_mount: str = ""
    host_queue_target: int = 10
    host_queue_start_min: int = 0
    host_rolling_prepare: int = 3
    max_track_seconds: int | None = None
    require_musicbrainz_metadata: bool = False
    prebaked_transition_host: bool = False
    transition_liner_root: Path | None = None
    transition_liner_genres: tuple[str, ...] = ()
    transition_liner_min_variants: int = 5
    transition_liner_timezone: str = "Europe/Istanbul"


@dataclass(frozen=True)
class SupervisorConfig:
    icecast_host: str
    icecast_port: int
    icecast_user: str
    credential_store: Path
    credential_reference: str
    credential_env_file: Path | None
    onair_source_root: Path
    ffmpeg: Path
    state_file: Path
    bitrate_kbps: int
    stations: tuple[StationConfig, ...]
    public_api_base_url: str = "https://radiotedu.com"
    public_agent_id: str = "school-radio-pc"
    public_sync_interval_seconds: int = 10
    playout_mode: str = "autonomous-local-ai"


class IcecastSourceError(RuntimeError):
    pass


class IcecastSource:
    """Authenticated Icecast HTTP source without command-line secrets.

    The request mirrors FFmpeg's Icecast PUT handshake, including
    ``Expect: 100-continue``. The MP3 body remains raw and indefinite, without
    chunk markers, while the source password stays out of process arguments.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        mount: str,
        user: str,
        password: str,
        name: str,
        content_type: str = "audio/mpeg",
        public: bool = False,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._socket = socket.create_connection((host, port), timeout=timeout_seconds)
        self._socket.settimeout(timeout_seconds)
        authorization = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        request = (
            f"PUT {mount} HTTP/1.1\r\n"
            "User-Agent: RadioTEDU-AI-Source/1.0\r\n"
            "Accept: */*\r\n"
            "Expect: 100-continue\r\n"
            "Connection: close\r\n"
            f"Host: {host}:{port}\r\n"
            f"Content-Type: {content_type}\r\n"
            "Icy-MetaData: 1\r\n"
            f"Ice-Name: {name}\r\n"
            "Ice-Description: RadioTEDU AI radio\r\n"
            "Ice-Genre: AI Radio\r\n"
            f"Ice-Public: {1 if public else 0}\r\n"
            f"Authorization: Basic {authorization}\r\n\r\n"
        ).encode("utf-8")
        self._socket.sendall(request)
        response = self._read_response_headers()
        status_line = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        if not any(code in f" {status_line} " for code in (" 100 ", " 200 ")):
            self.close(abort=True)
            raise IcecastSourceError(f"Icecast rejected source: {status_line[:120]}")

    def _read_response_headers(self) -> bytes:
        response = bytearray()
        while b"\r\n\r\n" not in response and len(response) < 16_384:
            chunk = self._socket.recv(2048)
            if not chunk:
                break
            response.extend(chunk)
        if b"\r\n\r\n" not in response:
            raise IcecastSourceError("Icecast did not return complete response headers")
        return bytes(response)

    def send(self, audio: bytes) -> None:
        if not audio:
            return
        self._socket.sendall(audio)

    def close(self, *, abort: bool = False) -> None:
        sock = getattr(self, "_socket", None)
        if sock is None:
            return
        self._socket = None
        del abort
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def __enter__(self) -> "IcecastSource":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class OutputPacer:
    """Send CBR audio steadily even when FFmpeg or a pipe produces bursts."""

    def __init__(self, bitrate_kbps: int, *, packet_bytes: int = 1_024) -> None:
        self._bytes_per_second = max(1.0, float(bitrate_kbps) * 1_000.0 / 8.0)
        self._packet_bytes = max(256, int(packet_bytes))
        self._next_send_at = time.monotonic()

    def send(
        self,
        source: IcecastSource,
        audio: bytes,
        stop: threading.Event,
    ) -> None:
        for offset in range(0, len(audio), self._packet_bytes):
            if stop.is_set():
                return
            now = time.monotonic()
            # Do not repay a producer or network stall as a listener-hostile burst.
            if self._next_send_at < now - 0.25:
                self._next_send_at = now
            delay = self._next_send_at - now
            if delay > 0 and stop.wait(delay):
                return
            packet = audio[offset : offset + self._packet_bytes]
            source.send(packet)
            self._next_send_at += len(packet) / self._bytes_per_second


def _absolute_path(value: object, field: str) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    return path.resolve()


def _optional_absolute_path(value: object, field: str) -> Path | None:
    if not str(value or "").strip():
        return None
    return _absolute_path(value, field)


def load_config(path: Path) -> SupervisorConfig:
    # Windows PowerShell 5.1 writes UTF-8 files with a BOM. Service-owned
    # ProgramData config must remain readable after an idempotent reinstall.
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != 1:
        raise ValueError("unsupported AI stream supervisor config")
    playout_mode = str(payload.get("playout_mode") or "autonomous-local-ai")
    allowed_playout_modes = {"autonomous-local-ai", "prebaked-transition-host"}
    if playout_mode not in allowed_playout_modes:
        raise ValueError(
            "AI stream playout_mode must be autonomous-local-ai or "
            "prebaked-transition-host"
        )
    raw_stations = payload.get("stations")
    if not isinstance(raw_stations, list) or not raw_stations:
        raise ValueError("at least one AI station is required")
    stations: list[StationConfig] = []
    mounts: set[str] = set()
    station_ids: set[str] = set()
    for raw in raw_stations:
        if not isinstance(raw, dict):
            raise ValueError("station config must be an object")
        station_id = str(raw.get("station_id") or "").strip()
        mount = "/" + str(raw.get("mount") or "").strip().lstrip("/")
        name = str(raw.get("name") or station_id).strip()
        roots = tuple(
            _absolute_path(item, f"{station_id}.music_roots")
            for item in (raw.get("music_roots") or [])
        )
        preferred_extensions = tuple(
            "." + str(item).strip().lower().lstrip(".")
            for item in (raw.get("preferred_extensions") or [])
            if str(item).strip()
        )
        if any(item not in SUPPORTED_AUDIO_EXTENSIONS for item in preferred_extensions):
            raise ValueError(f"unsupported preferred extension for {station_id}")
        if station_id not in {"radiotedu-en", "radiotedu-fr"}:
            raise ValueError(f"unsupported AI station: {station_id}")
        if not re.fullmatch(r"/[A-Za-z0-9._-]{1,80}", mount):
            raise ValueError(f"unsupported public AI mount: {mount}")
        if station_id in station_ids or mount in mounts or not roots:
            raise ValueError(f"duplicate or incomplete AI station config: {station_id}")
        expected_health_port = {
            "radiotedu-en": 8765,
            "radiotedu-fr": 8766,
        }[station_id]
        health_port = int(raw.get("health_port") or expected_health_port)
        if health_port != expected_health_port:
            raise ValueError(f"unsupported health port for {station_id}: {health_port}")
        dynamic_host_enabled = bool(raw.get("dynamic_host_enabled", False))
        prebaked_transition_host = playout_mode == "prebaked-transition-host"
        if prebaked_transition_host and dynamic_host_enabled:
            raise ValueError(
                f"pre-baked and dynamic host modes cannot both be enabled for {station_id}"
            )
        lead_min = int(raw.get("host_lead_songs_min") or 3)
        lead_max = int(raw.get("host_lead_songs_max") or 5)
        every_song = lead_min == 1 and lead_max == 1
        spaced_announcements = 3 <= lead_min <= lead_max <= 5
        if not (every_song or spaced_announcements):
            raise ValueError(f"invalid dynamic host lead for {station_id}")
        host_ollama_url = str(raw.get("host_ollama_url") or "http://127.0.0.1:11434").strip()
        if not host_ollama_url.startswith("http://127.0.0.1:"):
            raise ValueError(f"host_ollama_url must be loopback HTTP for {station_id}")
        host_model = str(raw.get("host_model") or "qwen3:0.6b").strip()
        if dynamic_host_enabled and host_model != "qwen3:0.6b":
            raise ValueError(
                f"dynamic host text model must be qwen3:0.6b for {station_id}"
            )
        selection_model_id = str(
            raw.get("selection_model_id") or LAYA_MODEL_ID
        ).strip()
        selection_model_revision = str(
            raw.get("selection_model_revision") or LAYA_MODEL_REVISION
        ).strip()
        if (
            selection_model_id != LAYA_MODEL_ID
            or selection_model_revision != LAYA_MODEL_REVISION
        ):
            raise ValueError(f"unsupported or unpinned Laya model for {station_id}")
        selection_cache_dir = _absolute_path(
            raw.get("selection_cache_dir")
            or r"C:\ProgramData\RadioTEDU\cache\laya",
            f"{station_id}.selection_cache_dir",
        )
        host_qwen_url = str(
            raw.get("host_qwen_url") or "http://127.0.0.1:8090"
        ).strip().rstrip("/")
        if host_qwen_url != "http://127.0.0.1:8090":
            raise ValueError(f"host_qwen_url must be the local Qwen service for {station_id}")
        host_voice_design = str(raw.get("host_voice_design") or "").strip()
        host_night_voice_design = str(
            raw.get("host_night_voice_design") or host_voice_design
        ).strip()
        host_queue_target = int(raw.get("host_queue_target") or 10)
        if host_queue_target < 1 or host_queue_target > 20:
            raise ValueError(f"invalid dynamic host queue target for {station_id}")
        raw_start_min = raw.get("host_queue_start_min")
        # These values control announcement cadence. Queue lookahead is separate
        # and configured by host_rolling_prepare (3Ã¢â‚¬â€œ5 song-bound clips).
        required_start_min = 0
        host_queue_start_min = int(
            required_start_min if raw_start_min is None else raw_start_min
        )
        if (
            host_queue_start_min < required_start_min
            or host_queue_start_min > host_queue_target
        ):
            raise ValueError(f"invalid dynamic host startup buffer for {station_id}")
        host_rolling_prepare = int(raw.get("host_rolling_prepare") or 3)
        minimum_rolling_prepare = 3 if dynamic_host_enabled else 1
        maximum_rolling_prepare = (
            min(5, host_queue_target) if dynamic_host_enabled else host_queue_target
        )
        if not minimum_rolling_prepare <= host_rolling_prepare <= maximum_rolling_prepare:
            raise ValueError(f"invalid dynamic host rolling preparation for {station_id}")
        require_musicbrainz_metadata = bool(
            raw.get("require_musicbrainz_metadata", False)
        )
        transition_liner_root = _optional_absolute_path(
            raw.get("transition_liner_root"),
            f"{station_id}.transition_liner_root",
        )
        transition_liner_genres = tuple(
            str(value).strip()
            for value in (raw.get("transition_liner_genres") or [])
            if str(value).strip()
        )
        transition_liner_min_variants = int(
            raw.get("transition_liner_min_variants") or 5
        )
        transition_liner_timezone = str(
            raw.get("transition_liner_timezone") or "Europe/Istanbul"
        ).strip()
        if prebaked_transition_host:
            if transition_liner_root is None:
                raise ValueError(
                    f"transition_liner_root is required for {station_id}"
                )
            if not transition_liner_genres:
                raise ValueError(
                    f"transition_liner_genres are required for {station_id}"
                )
            if not 1 <= transition_liner_min_variants <= 20:
                raise ValueError(
                    f"invalid transition liner variant count for {station_id}"
                )
        raw_max_track_seconds = raw.get("max_track_seconds")
        max_track_seconds = (
            int(raw_max_track_seconds)
            if raw_max_track_seconds is not None
            else None
        )
        if max_track_seconds is not None and not 30 <= max_track_seconds <= 3_600:
            raise ValueError(f"invalid maximum track duration for {station_id}")
        playout_seed = int(raw.get("playout_seed") or 20260813)
        if playout_seed < 0 or playout_seed > 2_147_483_647:
            raise ValueError(f"invalid playout_seed for {station_id}")
        if dynamic_host_enabled and (
            not host_voice_design
            or not host_night_voice_design
        ):
            raise ValueError(
                f"Qwen CustomVoice model path and voice IDs are required for {station_id}"
            )
        expected_public_mount = {
            "radiotedu-en": "/en",
            "radiotedu-fr": "/fr",
        }[station_id]
        public_mount = "/" + str(
            raw.get("public_mount") or expected_public_mount
        ).strip().lstrip("/")
        if public_mount != expected_public_mount:
            raise ValueError(
                f"unsupported website player mount for {station_id}: {public_mount}"
            )
        expected_stream_url = f"https://stream.radiotedu.com{expected_public_mount}"
        public_stream_url = str(
            raw.get("public_stream_url") or expected_stream_url
        ).strip()
        if public_stream_url != expected_stream_url:
            raise ValueError(
                f"unsupported public stream URL for {station_id}: {public_stream_url}"
            )
        station_ids.add(station_id)
        mounts.add(mount)
        stations.append(StationConfig(
            station_id=station_id,
            mount=mount,
            name=name,
            music_roots=roots,
            preferred_extensions=preferred_extensions,
            health_port=health_port,
            dynamic_host_enabled=dynamic_host_enabled,
            host_lead_songs_min=lead_min,
            host_lead_songs_max=lead_max,
            host_ollama_url=host_ollama_url,
            host_model=host_model,
            selection_model_id=selection_model_id,
            selection_model_revision=selection_model_revision,
            selection_cache_dir=selection_cache_dir,
            host_qwen_url=host_qwen_url,
            host_voice_design=host_voice_design,
            host_night_voice_design=host_night_voice_design,
            playout_seed=playout_seed,
            public_stream_url=public_stream_url,
            public_mount=public_mount,
            host_queue_target=host_queue_target,
            host_queue_start_min=host_queue_start_min,
            host_rolling_prepare=host_rolling_prepare,
            max_track_seconds=max_track_seconds,
            require_musicbrainz_metadata=require_musicbrainz_metadata,
            prebaked_transition_host=prebaked_transition_host,
            transition_liner_root=transition_liner_root,
            transition_liner_genres=transition_liner_genres,
            transition_liner_min_variants=transition_liner_min_variants,
            transition_liner_timezone=transition_liner_timezone,
        ))
    bitrate = int(payload.get("bitrate_kbps") or 192)
    if bitrate != 192:
        raise ValueError("public AI bitrate_kbps must be 192")
    port = int(payload.get("icecast_port") or 0)
    if port < 1 or port > 65535:
        raise ValueError("icecast_port is invalid")
    public_api_base_url = str(
        payload.get("public_api_base_url") or "https://radiotedu.com"
    ).strip().rstrip("/")
    if public_api_base_url != "https://radiotedu.com":
        raise ValueError("public_api_base_url must be https://radiotedu.com")
    public_agent_id = str(
        payload.get("public_agent_id") or "school-radio-pc"
    ).strip()
    if public_agent_id != "school-radio-pc":
        raise ValueError("public_agent_id must be school-radio-pc")
    public_sync_interval_seconds = int(
        payload.get("public_sync_interval_seconds") or 10
    )
    if public_sync_interval_seconds < 5 or public_sync_interval_seconds > 300:
        raise ValueError("public_sync_interval_seconds must be between 5 and 300")
    return SupervisorConfig(
        icecast_host=str(payload.get("icecast_host") or "").strip(),
        icecast_port=port,
        icecast_user=str(payload.get("icecast_user") or "source").strip(),
        credential_store=_absolute_path(payload.get("credential_store"), "credential_store"),
        credential_reference=str(payload.get("credential_reference") or "").strip(),
        credential_env_file=_optional_absolute_path(
            payload.get("credential_env_file"), "credential_env_file"
        ),
        onair_source_root=_absolute_path(payload.get("onair_source_root"), "onair_source_root"),
        ffmpeg=_absolute_path(payload.get("ffmpeg"), "ffmpeg"),
        state_file=_absolute_path(payload.get("state_file"), "state_file"),
        bitrate_kbps=bitrate,
        stations=tuple(stations),
        public_api_base_url=public_api_base_url,
        public_agent_id=public_agent_id,
        public_sync_interval_seconds=public_sync_interval_seconds,
        playout_mode=playout_mode,
    )


def resolve_source_password(config: SupervisorConfig) -> str:
    password = ""
    if config.onair_source_root.is_dir() and config.credential_store.is_file():
        sys.path.insert(0, str(config.onair_source_root))
        os.environ["CLEANROOM_CREDENTIAL_STORE_FILE"] = str(config.credential_store)
        os.environ["CLEANROOM_CREDENTIAL_DPAPI_SCOPE"] = "machine"
        try:
            from app.security.credential_vault import CredentialVault

            password = CredentialVault(config.credential_store).get_secret(
                config.credential_reference
            )
        except (ImportError, OSError, RuntimeError, ValueError):
            password = ""
        finally:
            try:
                sys.path.remove(str(config.onair_source_root))
            except ValueError:
                pass
    if not password and config.credential_env_file and config.credential_env_file.is_file():
        for raw_line in config.credential_env_file.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "RADIOTEDU_AI_ICECAST_SOURCE_PASSWORD":
                password = value.strip().strip('"').strip("'")
                break
    if not password:
        raise RuntimeError("protected Icecast source credential is unavailable")
    return password


_MUSICBRAINZ_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_UNSAFE_METADATA_TERMS = frozenset(
    {"anonymous", "anonymous420", "unknown", "unknown artist", "unspecified", "untitled"}
)


def _first_easy_tag(tags: object, *names: str) -> str:
    if tags is None or not hasattr(tags, "get"):
        return ""
    for name in names:
        values = tags.get(name, [])
        if values:
            return str(values[0]).strip()
    return ""


def broadcast_metadata_is_safe(title: str, artist: str) -> bool:
    """Reject metadata that a presenter must never read on air."""
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    artist = re.sub(r"\s+", " ", str(artist or "")).strip()
    if not title or not artist or len(title) > 160 or len(artist) > 120:
        return False
    lowered = {title.casefold(), artist.casefold()}
    if any(
        term == value
        for value in lowered
        for term in _UNSAFE_METADATA_TERMS
    ):
        return False
    if any("\uff00" <= character <= "\uffef" for character in title + artist):
        return False
    if any(ord(character) < 32 for character in title + artist):
        return False
    if not any(character.isalpha() for character in title) and not re.fullmatch(r"\d{3,}", title):
        return False
    if not any(character.isalpha() for character in artist):
        return False
    return True


def has_verified_musicbrainz_metadata(path: Path) -> bool:
    """Require title, artist and a Picard/MusicBrainz recording or track MBID."""
    try:
        media = MutagenFile(path, easy=True)
        tags = media.tags if media is not None else None
        title = _first_easy_tag(tags, "title")
        artist = _first_easy_tag(tags, "artist")
        musicbrainz_id = _first_easy_tag(
            tags, "musicbrainz_recordingid", "musicbrainz_trackid"
        )
    except (OSError, TypeError, ValueError, AttributeError):
        return False
    return bool(
        broadcast_metadata_is_safe(title, artist)
        and _MUSICBRAINZ_ID.fullmatch(musicbrainz_id)
    )


def discover_audio(station: StationConfig) -> list[Path]:
    files: list[Path] = []
    for root in station.music_roots:
        if not root.is_dir():
            continue
        files.extend(
            item.resolve()
            for item in root.rglob("*")
            if item.is_file() and item.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
        )
    unique = sorted(dict.fromkeys(files), key=lambda item: str(item).casefold())
    if station.require_musicbrainz_metadata:
        unique = [item for item in unique if has_verified_musicbrainz_metadata(item)]
    if station.max_track_seconds is not None:
        duration_limited: list[Path] = []
        for item in unique:
            try:
                media = MutagenFile(item)
                duration = float(media.info.length) if media is not None and media.info else 0.0
            except (OSError, TypeError, ValueError, AttributeError):
                duration = 0.0
            if 0 < duration <= station.max_track_seconds:
                duration_limited.append(item)
        unique = duration_limited
    if station.preferred_extensions:
        preferred = [
            item for item in unique if item.suffix.lower() in station.preferred_extensions
        ]
        if not preferred:
            raise RuntimeError(
                f"no preferred audio files for {station.station_id}: "
                + ", ".join(station.preferred_extensions)
            )
        return preferred
    return unique


def _ffconcat_escape(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def probabilistic_rotation(
    files: list[Path], rng: random.Random | random.SystemRandom | None = None
) -> list[Path]:
    """Build a fresh weighted rotation with simple artist-folder separation."""
    chooser = rng or random.SystemRandom()
    buckets: dict[str, list[Path]] = {}
    for item in files:
        buckets.setdefault(str(item.parent).casefold(), []).append(item)
    for bucket in buckets.values():
        chooser.shuffle(bucket)

    rotation: list[Path] = []
    previous_bucket: str | None = None
    while buckets:
        eligible = [key for key in buckets if key != previous_bucket] or list(buckets)
        total = sum(len(buckets[key]) for key in eligible)
        ticket = chooser.randrange(total)
        selected_key = eligible[-1]
        for key in eligible:
            ticket -= len(buckets[key])
            if ticket < 0:
                selected_key = key
                break
        rotation.append(buckets[selected_key].pop())
        previous_bucket = selected_key
        if not buckets[selected_key]:
            del buckets[selected_key]
    return rotation


def deterministic_rotation(files: list[Path], seed: int, station_id: str = "") -> list[Path]:
    """Return the same separated rotation for the same catalog and seed."""
    catalog_signature = "\n".join(str(item).casefold() for item in sorted(files, key=lambda p: str(p).casefold()))
    digest = hashlib.sha256(
        f"{seed}|{station_id}|{catalog_signature}".encode("utf-8")
    ).digest()
    chooser = random.Random(int.from_bytes(digest[:8], "big"))
    ordered = sorted(files, key=lambda item: str(item).casefold())
    return probabilistic_rotation(ordered, chooser)


def _basic_track_metadata(path: Path) -> tuple[str, str]:
    """Return local, non-path metadata suitable for a bounded AI choice."""
    title = re.sub(r"^[0-9a-fA-F]{16,64}-", "", path.stem)
    title = re.sub(r"[._]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -_") or "Untitled"
    artist = path.parent.name.strip() or "Unknown artist"
    try:
        media = MutagenFile(path, easy=True)
        tags = media.tags if media is not None else None
        if tags is not None:
            raw_title = tags.get("title", [])
            raw_artist = tags.get("artist", [])
            if raw_title:
                title = str(raw_title[0]).strip() or title
            if raw_artist:
                artist = str(raw_artist[0]).strip() or artist
    except Exception:
        # Metadata parsing is advisory for candidate display. The selector is
        # still confined to the already-discovered Path objects.
        pass
    title, artist = clean_track_metadata(title, artist)
    return title[:200], artist[:160] if artist else "Unknown artist"


def assert_autonomous_song_ai_ready(station: StationConfig) -> None:
    try:
        load_laya_agent(
            station.selection_model_id,
            station.selection_model_revision,
            station.selection_cache_dir,
        )
    except Exception as exc:
        raise RuntimeError(f"local Laya song decision model is unavailable: {exc}") from exc


def _choose_autonomous_candidate(
    station: StationConfig,
    candidates: list[dict[str, object]],
    recent: list[dict[str, str]],
    program_context: dict[str, object],
) -> dict[str, object]:
    language = "French" if station.station_id.endswith("-fr") else "English"
    return laya_choose_song(
        station_id=station.station_id,
        station_name=station.name,
        language="fr" if language == "French" else "en",
        candidates=candidates,
        recent_tracks=recent,
        program_context=program_context,
        cache_root=station.selection_cache_dir,
        model_id=station.selection_model_id,
        revision=station.selection_model_revision,
    )


def _autonomous_track_id(path: Path) -> str:
    """Stable public identifier; the local path never leaves this process."""
    digest = hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()
    return "track-" + digest[:32]


def _selection_decision_event(
    station: StationConfig,
    candidates: list[dict[str, object]],
    recent: list[dict[str, str]],
    program_context: dict[str, object],
    decision: dict[str, object],
    program_selection: list[dict[str, object]],
    prefilter_rules: dict[str, object],
    *,
    live_next_track: bool = False,
) -> dict[str, object]:
    choice_id = int(decision["choice_id"])
    model_probabilities = decision.get("probabilities")
    if not isinstance(model_probabilities, dict):
        raise RuntimeError("Laya evidence omitted its actual choice probabilities")
    by_choice_id = {int(item["id"]): item for item in candidates}
    selected = by_choice_id.get(choice_id)
    if selected is None:
        raise RuntimeError("Laya event choice is outside the candidate set")
    finalists = {
        int(value) for value in decision.get("finalist_choice_ids", [])
    }
    if not finalists:
        raise RuntimeError("Laya evidence omitted its full-catalog shortlist")
    finalist_candidates = {
        int(item["id"]): item
        for item in candidates
        if int(item["id"]) in finalists
    }
    if set(finalist_candidates) != finalists or set(model_probabilities) != {
        laya_choice_key(choice_id) for choice_id in finalists
    }:
        raise RuntimeError("Laya probability keys do not match its finalist candidates")
    candidate_probabilities = [
        {
            "choice_id": choice_id,
            "track_id": str(finalist_candidates[choice_id]["track_id"]),
            "probability": float(model_probabilities[laya_choice_key(choice_id)]),
        }
        for choice_id in decision.get("finalist_choice_ids", [])
    ]
    pool_evidence = decision.get("candidate_pool")
    if not isinstance(pool_evidence, list) or len(pool_evidence) != len(candidates):
        raise RuntimeError("Laya evidence omitted candidates from its full catalog scan")
    if {
        int(item["choice_id"]) for item in pool_evidence if isinstance(item, dict)
    } != {int(item["id"]) for item in candidates}:
        raise RuntimeError("Laya full catalog evidence does not match its supplied pool")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    program_id = str(program_context.get("id") or "ai_live_rotation")[:128]
    program_name = str(program_context.get("name") or station.name)[:160]
    return {
        "protocol": "radiotedu-platform/v1",
        "schema_version": 1,
        "event_id": f"selection-{station.station_id}-{os.urandom(16).hex()}",
        "station_id": station.station_id,
        "event_type": "song.selection.completed",
        "occurred_at": now,
        "language": "fr" if station.station_id.endswith("-fr") else "en",
        "program_id": program_id,
        "program_name": program_name,
        "selection_mode": (
            "laya_library_shortlist_single_next_track"
            if live_next_track
            else "laya_library_shortlist_probability_ranked_batch"
        ),
        "selector_version": (
            "laya-live-next-track-v1"
            if live_next_track
            else "laya-full-catalog-shortlist-v1"
        ),
        "selector_policy_version": (
            "laya-live-next-track-policy-v4"
            if live_next_track
            else "laya-music-full-catalog-policy-v3"
        ),
        "selector_policy_sha256": SELECTION_POLICY_SHA256,
        "model_provider": "laya",
        "model_version": f"{decision['model_id']}@{decision['model_revision']}",
        "model_package_version": "0.3.22",
        "decision_schema_version": (
            "radio-song-choice-library-shortlist-single-v1"
            if live_next_track
            else "radio-song-choice-library-shortlist-batch-v1"
        ),
        "prefilter_rules": dict(prefilter_rules),
        "live_selection_queue": (
            dict(program_context["live_selection_queue"])
            if live_next_track
            and isinstance(program_context.get("live_selection_queue"), dict)
            else None
        ),
        "recent_tracks": [dict(item) for item in recent[-6:]],
        "catalog_candidate_count": len(pool_evidence),
        "catalog_ordered_pool_sha256": str(
            decision["shortlist_evidence"]["ordered_pool_sha256"]
        ),
        "candidate_tracks": [dict(item) for item in pool_evidence],
        "shortlist_evidence": dict(decision["shortlist_evidence"]),
        "finalist_choice_ids": [
            int(value) for value in decision.get("finalist_choice_ids", [])
        ],
        "model_input": decision["model_input"],
        "model_output_raw_json": decision["model_output_raw_json"],
        "model_output": {
            "choice_id": choice_id,
            "choice_key": str(decision["choice_key"]),
            "candidate_probabilities": candidate_probabilities,
            "confidence": decision.get("confidence"),
            "answer_confidence": decision.get("answer_confidence"),
            "action": decision.get("action"),
            "usage": decision.get("usage"),
            "routing": decision.get("routing"),
        },
        "program_selection": [dict(item) for item in program_selection],
        "selected_track_id": str(selected["track_id"]),
        "validation": {
            "choice_was_candidate": True,
            "probability_map_complete": True,
            "program_choices_were_candidates": all(
                int(item["choice_id"]) in by_choice_id
                and str(item["track_id"])
                == str(by_choice_id[int(item["choice_id"])]["track_id"])
                for item in program_selection
            ),
            "program_choices_unique": len(
                {int(item["choice_id"]) for item in program_selection}
            )
            == len(program_selection),
            "catalog_pool_complete": len(pool_evidence) == len(candidates),
            "shortlist_choices_were_catalog_candidates": finalists.issubset(
                {int(item["id"]) for item in candidates}
            ),
            "fallback": False,
        },
    }


def autonomous_rotation(
    files: list[Path],
    station: StationConfig,
    *,
    metadata_resolver: Callable[[Path], tuple[str, str | None]] | None = None,
    genre_resolver: Callable[[Path], str | None] | None = None,
    selection_context: dict[str, object] | None = None,
    decision_sink: Callable[[dict[str, object]], object] | None = None,
    max_tracks: int | None = None,
    one_choice_per_track: bool = False,
    choice_id_resolver: Callable[[Path], int] | None = None,
    recent_track_paths: list[Path] | None = None,
    recent_track_records: list[dict[str, str]] | None = None,
    update_recent_history: bool = True,
) -> list[Path]:
    """Build a bounded program using strict local-AI choices and no fallback."""
    if not files:
        raise RuntimeError(f"no playable files for {station.station_id}")
    raw_resolver = metadata_resolver or _basic_track_metadata
    metadata_cache: dict[Path, tuple[str, str | None]] = {}

    def resolver(path: Path) -> tuple[str, str | None]:
        resolved_path = path.resolve()
        if resolved_path not in metadata_cache:
            metadata_cache[resolved_path] = raw_resolver(resolved_path)
        return metadata_cache[resolved_path]

    with SONG_SELECTION_LOCK:
        assert_autonomous_song_ai_ready(station)
        remaining = sorted(
            set(path.resolve() for path in files),
            key=lambda path: _autonomous_track_id(path),
        )
        context = dict(selection_context or {})
        required_genres = {
            str(value).strip().casefold()
            for value in context.get("required_genres", [])
            if str(value).strip()
        }
        if required_genres and genre_resolver is not None and not one_choice_per_track:
            remaining = [
                path
                for path in remaining
                if (genre_resolver(path) or "").strip().casefold() in required_genres
            ]
            if not remaining:
                promised = ", ".join(sorted(required_genres))
                raise RuntimeError(
                    f"no broadcast-safe tracks match the required program genres: {promised}"
                )
        program: list[Path] = []
        recent_paths = list(
            recent_track_paths
            if recent_track_paths is not None
            else _RECENT_AUTONOMOUS_TRACKS.get(station.station_id, [])
        )[-6:]
        recent = [dict(record) for record in recent_track_records[-6:]] if recent_track_records is not None else []
        if recent_track_records is None:
            for path in recent_paths:
                title, artist = resolver(path)
                recent.append(
                    {
                        "track_id": _autonomous_track_id(path),
                        "title": title,
                        "artist": artist or "Unknown artist",
                    }
                )
        requested_tracks = (
            AUTONOMOUS_PROGRAM_TRACKS
            if max_tracks is None
            else max(1, int(max_tracks))
        )
        target_length = min(requested_tracks, len(remaining))
        last_decision: dict[str, object] = {}
        last_event_id = ""
        trace_persistence_error = ""
        while remaining and len(program) < target_length:
            recent_artists = {
                item["artist"].casefold() for item in recent[-3:] if item.get("artist")
            }
            separated = [
                path
                for path in remaining
                if (resolver(path)[1] or "").casefold() not in recent_artists
            ]
            source_pool = separated if separated else remaining
            pool = sorted(source_pool, key=lambda path: _autonomous_track_id(path))
            decision_context = context
            if one_choice_per_track and not isinstance(
                context.get("live_selection_queue"), dict
            ):
                decision_context = dict(context)
                decision_context["live_selection_queue"] = {
                    "mode": "laya-live-next-track",
                    "current_track_id": None,
                    "queued_track_ids": [
                        _autonomous_track_id(path) for path in program
                    ],
                    "queued_track_count": len(program),
                    "queue_target_depth": LIVE_LAYA_QUEUE_DEPTH,
                    "candidate_pool_excludes_queued_tracks": True,
                    "selection_timing": "startup_buffer_prefill",
                }
            prefilter_rules = {
                "required_genres": sorted(required_genres),
                "required_genre_filter_applied": bool(
                    required_genres and genre_resolver is not None and not one_choice_per_track
                ),
                "genre_policy": "advisory" if one_choice_per_track else "required",
                "recent_artist_window_positions": 3,
                "recent_artist_exclusion_applied": bool(separated),
                "recent_artist_exclusions": sorted(recent_artists),
                "recent_artist_exclusion_fell_back_to_all_remaining": not bool(separated),
                "candidate_order": "stable_opaque_track_id_ascending",
                "random_candidate_sampling": False,
            }
            live_queue_state = decision_context.get("live_selection_queue")
            if one_choice_per_track and isinstance(live_queue_state, dict):
                prefilter_rules.update(
                    {
                        "queued_track_exclusion_applied": True,
                        "queued_track_ids": list(
                            live_queue_state.get("queued_track_ids") or []
                        ),
                        "queued_track_count": int(
                            live_queue_state.get("queued_track_count") or 0
                        ),
                        "current_track_id": live_queue_state.get("current_track_id"),
                    }
                )
            candidate_records: list[dict[str, object]] = []
            by_id: dict[int, Path] = {}
            for pool_position, path in enumerate(pool, start=1):
                candidate_id = (
                    int(choice_id_resolver(path))
                    if choice_id_resolver is not None
                    else pool_position
                )
                if candidate_id < 1 or candidate_id in by_id:
                    raise RuntimeError("Laya candidate IDs must be unique positive integers")
                title, artist = resolver(path)
                genre = genre_resolver(path) if genre_resolver is not None else path.parent.name
                candidate_records.append(
                    {
                        "id": candidate_id,
                        "track_id": _autonomous_track_id(path),
                        "title": title,
                        "artist": artist or "Unknown artist",
                        "genre": genre or "Unspecified",
                    }
                )
                by_id[candidate_id] = path
            decision = _choose_autonomous_candidate(
                station, candidate_records, recent, decision_context
            )
            choice = int(decision["choice_id"])
            if choice not in by_id:
                raise RuntimeError(
                    "Laya selected an option outside the supplied candidate set"
                )
            probabilities = decision.get("probabilities")
            if not isinstance(probabilities, dict):
                raise RuntimeError("Laya evidence omitted its actual choice probabilities")
            finalist_ids = [int(value) for value in decision.get("finalist_choice_ids", [])]
            finalist_records = [
                item for item in candidate_records if int(item["id"]) in set(finalist_ids)
            ]
            finalist_keys = {laya_choice_key(int(item["id"])) for item in finalist_records}
            if not finalist_records or set(probabilities) != finalist_keys:
                raise RuntimeError("Laya probabilities do not match its recorded finalist set")
            probability_order = sorted(
                finalist_records,
                key=lambda item: (
                    -float(probabilities[laya_choice_key(int(item["id"]))]),
                    int(item["id"]),
                ),
            )
            ordered_records = [
                next(item for item in candidate_records if int(item["id"]) == choice)
            ] + [item for item in probability_order if int(item["id"]) != choice]
            unselected = {int(item["id"]): item for item in ordered_records}
            batch_recent = list(recent)
            batch_selection: list[dict[str, object]] = []
            batch_paths: list[Path] = []
            batch_limit = min(
                1 if one_choice_per_track else target_length - len(program),
                target_length - len(program),
                len(finalist_records),
            )
            while unselected and len(batch_selection) < batch_limit:
                recent_artists = {
                    item["artist"].casefold()
                    for item in batch_recent[-3:]
                    if item.get("artist")
                }
                separated_records = [
                    item
                    for item in ordered_records
                    if int(item["id"]) in unselected
                    and str(item["artist"]).casefold() not in recent_artists
                ]
                eligible_records = separated_records or [
                    item for item in ordered_records if int(item["id"]) in unselected
                ]
                if not batch_selection:
                    selected_record = next(
                        item
                        for item in eligible_records
                        if int(item["id"]) == choice
                    )
                    basis = "typed_model_choice"
                else:
                    selected_record = eligible_records[0]
                    basis = "highest_remaining_model_probability"
                selected_id = int(selected_record["id"])
                selected_path = by_id[selected_id]
                selected_title, selected_artist = resolver(selected_path)
                selected_artist = selected_artist or "Unknown artist"
                batch_selection.append(
                    {
                        "position": (
                            1
                            if one_choice_per_track
                            else len(program) + len(batch_selection) + 1
                        ),
                        "choice_id": selected_id,
                        "track_id": str(selected_record["track_id"]),
                        "selection_basis": basis,
                        "probability": float(
                            probabilities[laya_choice_key(selected_id)]
                        ),
                    }
                )
                batch_paths.append(selected_path)
                batch_recent.append(
                    {
                        "track_id": str(selected_record["track_id"]),
                        "title": selected_title,
                        "artist": selected_artist,
                    }
                )
                del unselected[selected_id]
            last_decision = decision
            if decision_sink is not None:
                try:
                    event = _selection_decision_event(
                        station,
                        candidate_records,
                        recent,
                        decision_context,
                        decision,
                        batch_selection,
                        prefilter_rules,
                        live_next_track=one_choice_per_track,
                    )
                    accepted = decision_sink(event)
                    last_event_id = str(event["event_id"])
                    if accepted is False:
                        trace_persistence_error = "selection event was not durably queued"
                except Exception as exc:
                    trace_persistence_error = (
                        f"selection event could not be durably queued: {type(exc).__name__}"
                    )[:160]
            if one_choice_per_track and decision_sink is not None and trace_persistence_error:
                raise RuntimeError(trace_persistence_error)
            if not batch_paths:
                raise RuntimeError("Laya probability ranking produced an empty program batch")
            for selected in batch_paths:
                program.append(selected)
                remaining.remove(selected)
                title, artist = resolver(selected)
                recent.append(
                    {
                        "track_id": _autonomous_track_id(selected),
                        "title": title,
                        "artist": artist or "Unknown artist",
                    }
                )
        if not program:
            raise RuntimeError("autonomous song AI produced an empty program")
        history = (recent_paths + program)[-6:]
        if update_recent_history:
            _RECENT_AUTONOMOUS_TRACKS[station.station_id] = history
        _AUTONOMOUS_SELECTION_STATUS[station.station_id] = {
            "mode": (
                "autonomous-local-laya-live-next-track"
                if one_choice_per_track
                else "autonomous-local-laya-full-catalog-shortlist-batch"
            ),
            "model": station.selection_model_id,
            "model_revision": station.selection_model_revision,
            "program_items": len(program),
            "laya_decision_calls": len(program) if one_choice_per_track else 1,
            "catalog_candidates_considered": len(
                last_decision.get("candidate_pool", [])
                if isinstance(last_decision.get("candidate_pool"), list)
                else []
            ),
            "typed_choice_finalists": len(
                last_decision.get("finalist_choice_ids", [])
                if isinstance(last_decision.get("finalist_choice_ids"), list)
                else []
            ),
            "last_choice_confidence": last_decision.get("answer_confidence"),
            "last_event_id": last_event_id,
            "decision_trace_persistence_error": trace_persistence_error,
            "selected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "fallback": False,
        }
        return program


def autonomous_selection_status(station_id: str) -> dict[str, object]:
    return dict(_AUTONOMOUS_SELECTION_STATUS.get(station_id, {}))


def set_autonomous_recent_history(
    station_id: str,
    tracks: list[Path],
) -> None:
    """Replace the per-station selection context after an editorial queue reset."""
    with SONG_SELECTION_LOCK:
        _RECENT_AUTONOMOUS_TRACKS[station_id] = list(tracks[-6:])


def mark_autonomous_program_restored(
    station: StationConfig, *, program_items: int
) -> None:
    _AUTONOMOUS_SELECTION_STATUS[station.station_id] = {
        "mode": "autonomous-local-laya-probability-ranked-batch",
        "model": station.selection_model_id,
        "model_revision": station.selection_model_revision,
        "program_items": int(program_items),
        "decision_trace_available_for_restore": False,
        "selected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "restored": True,
        "fallback": False,
    }


def mark_prebaked_transition_program(
    station: StationConfig,
    *,
    program_items: int,
    restored: bool = False,
) -> None:
    """Report the offline, genre-aware selector without probing Ollama."""
    _AUTONOMOUS_SELECTION_STATUS[station.station_id] = {
        "mode": "prebaked-transition-host",
        "model": None,
        "program_items": int(program_items),
        "last_reason": (
            "restored prior validated genre-aware program"
            if restored
            else "deterministic local rotation for the pre-baked host library"
        ),
        "selected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "restored": bool(restored),
        "fallback": False,
        "ollama_required": False,
    }


def build_program(station: StationConfig) -> list[Path]:
    music = discover_audio(station)
    return autonomous_rotation(music, station)


def read_cached_playlist(station: StationConfig, state_root: Path) -> tuple[Path, int] | None:
    # FFmpeg keeps its ffconcat input open on Windows. Refreshed playlists are
    # immutable content-addressed files, so a catalog scan never attempts to
    # replace the file currently owned by the live decoder.
    candidates = sorted(
        state_root.glob(f"{station.station_id}-*.ffconcat"),
        key=_safe_mtime,
        reverse=True,
    )
    candidates.append(state_root / f"{station.station_id}.ffconcat")
    for playlist in candidates:
        try:
            lines = playlist.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        if not lines or lines[0] != "ffconcat version 1.0":
            continue
        track_count = sum(1 for line in lines[1:] if line.startswith("file '"))
        if track_count >= 1:
            return playlist, track_count
    return None


def write_playlist(
    station: StationConfig,
    state_root: Path,
    program: list[Path] | None = None,
) -> tuple[Path, int]:
    files = list(program) if program is not None else build_program(station)
    state_root.mkdir(parents=True, exist_ok=True)
    content = "ffconcat version 1.0\n" + "".join(
        f"file '{_ffconcat_escape(item)}'\n" for item in files
    )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    playlist = state_root / f"{station.station_id}-{digest}.ffconcat"
    if playlist.is_file():
        return playlist, len(files)
    temporary = state_root / f".{playlist.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, playlist)
    # Bound storage while tolerating a still-open older file on Windows.
    versions = sorted(
        state_root.glob(f"{station.station_id}-*.ffconcat"),
        key=_safe_mtime,
        reverse=True,
    )
    for stale in versions[5:]:
        try:
            stale.unlink()
        except OSError:
            pass
    for stale_temp in state_root.glob(f"{station.station_id}.ffconcat.tmp"):
        try:
            stale_temp.unlink()
        except OSError:
            pass
    return playlist, len(files)


def _is_streaming_status(station: dict[str, object]) -> bool:
    def fresh(value: object) -> bool:
        try:
            updated = calendar.timegm(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ"))
        except (TypeError, ValueError, OverflowError):
            return False
        return -5 <= time.time() - updated <= 30

    if station.get("state") == "streaming" and fresh(station.get("updated_at")):
        return True
    outputs = station.get("outputs")
    return isinstance(outputs, dict) and any(
        isinstance(branch, dict)
        and branch.get("state") == "streaming"
        and fresh(branch.get("audio_sent_at", branch.get("updated_at")))
        and branch.get("origin_listener_ready") is not False
        for branch in outputs.values()
    )


def start_station_health_servers(
    config: SupervisorConfig,
    stop: threading.Event,
    status_provider: Callable[[str], dict[str, object]],
) -> list[http.server.ThreadingHTTPServer]:
    servers: list[http.server.ThreadingHTTPServer] = []
    for station in config.stations:
        def handler_factory(active_station: StationConfig):
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802 - stdlib contract
                    if self.path.rstrip("/") != "/health":
                        self.send_error(404)
                        return
                    station_status = status_provider(active_station.station_id)
                    streaming = _is_streaming_status(station_status)
                    body = json.dumps(
                        {
                            "status": "ready" if streaming else "degraded",
                            "service": "radiotedu-ai-stream",
                            "station_id": active_station.station_id,
                            "language": active_station.station_id.rsplit("-", 1)[-1],
                            "mount": active_station.mount,
                            "streaming": streaming,
                            "state": station_status,
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                    self.send_response(200 if streaming else 503)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, _format: str, *_args: object) -> None:
                    return

            return Handler

        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", station.health_port), handler_factory(station)
        )
        threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.5},
            name=f"ai-health-{station.station_id}",
            daemon=True,
        ).start()
        servers.append(server)

    def shutdown_when_stopped() -> None:
        stop.wait()
        for server in servers:
            server.shutdown()
            server.server_close()

    threading.Thread(
        target=shutdown_when_stopped,
        name="ai-health-shutdown",
        daemon=True,
    ).start()
    return servers


def write_status_atomic(target: Path, payload: str, attempts: int = 8) -> bool:
    """Atomically update status without putting audio in the I/O failure domain."""
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(attempts):
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{threading.get_ident()}."
            f"{time.monotonic_ns()}.tmp"
        )
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, target)
            return True
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            transient = (
                isinstance(exc, PermissionError)
                or getattr(exc, "winerror", None) in {5, 32}
                or getattr(exc, "errno", None) in {13, 16, 32}
            )
            if not transient:
                raise
            if attempt + 1 < attempts:
                time.sleep(min(0.01 * (2**attempt), 0.25))
    return False


def build_ffmpeg_command(
    config: SupervisorConfig, station: StationConfig, playlist: Path
) -> list[str]:
    del station
    return [
        str(config.ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-re",
        "-stream_loop",
        "-1",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(playlist),
        "-vn",
        "-map_metadata",
        "-1",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-af",
        "dynaudnorm=f=500:g=15,alimiter=limit=0.891251",
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{config.bitrate_kbps}k",
        "-write_xing",
        "0",
        "-f",
        "mp3",
        "pipe:1",
    ]


def _drain_stderr(stream: BinaryIO, station_id: str, stop: threading.Event) -> None:
    while not stop.is_set():
        line = stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").strip()
        if text:
            print(f"[{station_id}] ffmpeg: {text[:400]}", flush=True)


def _read_audio(
    stream: BinaryIO,
    output: "queue.Queue[bytes | None]",
    stop: threading.Event,
) -> None:
    # BufferedReader.read(size) waits for the whole requested size. At 128 kbps,
    # the former 16 KiB read turned a continuous source into roughly one-second
    # bursts. read1() returns currently available pipe data and keeps Icecast
    # fed at small, listener-friendly intervals.
    read_available = getattr(stream, "read1", stream.read)
    while not stop.is_set():
        chunk = read_available(4_096)
        if not chunk:
            try:
                output.put(None, timeout=1)
            except queue.Full:
                pass
            return
        while not stop.is_set():
            try:
                output.put(chunk, timeout=1)
                break
            except queue.Full:
                continue


class Supervisor:
    def __init__(self, config: SupervisorConfig, password: str) -> None:
        self.config = config
        self.password = password
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._status: dict[str, object] = {
            "version": 1,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "content_policy": "operator-approved royalty-free roots only",
            "metadata_suppressed": True,
            "credential_storage": "DPAPI reference",
            "stations": {},
        }

    def _write_status(self) -> None:
        with self._lock:
            payload = json.dumps(self._status, indent=2, sort_keys=True) + "\n"
            target = self.config.state_file
            write_status_atomic(target, payload)

    def station_status(self, station_id: str) -> dict[str, object]:
        with self._lock:
            stations = self._status.get("stations")
            if not isinstance(stations, dict):
                return {}
            return json.loads(json.dumps(stations.get(station_id) or {}))

    def _station_status(self, station: StationConfig, **changes: object) -> None:
        with self._lock:
            stations = self._status["stations"]
            assert isinstance(stations, dict)
            current = dict(stations.get(station.station_id) or {})
            current.update(
                {
                    "mount": station.mount,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    **changes,
                }
            )
            stations[station.station_id] = current
        self._write_status()

    def _refresh_playlist(self, station: StationConfig, state_root: Path) -> None:
        try:
            playlist, track_count = write_playlist(station, state_root)
        except Exception as exc:
            self._station_status(
                station,
                playlist_refresh_error=str(exc)[:240],
            )
            return
        self._station_status(
            station,
            track_count=track_count,
            playlist_refreshed_at=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            playlist_refresh_error="",
            playlist_generation=playlist.name,
            selection_mode="deterministic_no_replacement_with_folder_separation",
        )

    def _run_station(self, station: StationConfig) -> None:
        failures = 0
        state_root = self.config.state_file.parent / "playlists"
        playlist_refresh_started = False
        while not self.stop.is_set():
            process: subprocess.Popen[bytes] | None = None
            source: IcecastSource | None = None
            playlist: Path | None = None
            used_cached_playlist = False
            encoder_ready = False
            stderr_stop = threading.Event()
            audio_stop = threading.Event()
            try:
                cached_playlist = read_cached_playlist(station, state_root)
                if cached_playlist is None:
                    playlist, track_count = write_playlist(station, state_root)
                else:
                    playlist, track_count = cached_playlist
                    used_cached_playlist = True
                command = build_ffmpeg_command(self.config, station, playlist)
                process = subprocess.Popen(
                    command,
                    cwd=str(playlist.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                assert process.stdout is not None and process.stderr is not None
                threading.Thread(
                    target=_drain_stderr,
                    args=(process.stderr, station.station_id, stderr_stop),
                    daemon=True,
                ).start()
                encoded_audio: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
                threading.Thread(
                    target=_read_audio,
                    args=(process.stdout, encoded_audio, audio_stop),
                    daemon=True,
                ).start()
                try:
                    first_audio = encoded_audio.get(timeout=20)
                except queue.Empty as exc:
                    raise RuntimeError("encoder produced no audio within 20 seconds") from exc
                if not first_audio:
                    raise RuntimeError(f"encoder exited with code {process.poll()}")
                encoder_ready = True
                source = IcecastSource(
                    host=self.config.icecast_host,
                    port=self.config.icecast_port,
                    mount=station.mount,
                    user=self.config.icecast_user,
                    password=self.password,
                    name=station.name,
                )
                pacer = OutputPacer(self.config.bitrate_kbps)
                pacer.send(source, first_audio, self.stop)
                self._station_status(
                    station,
                    state="streaming",
                    process_id=process.pid,
                    track_count=track_count,
                    reconnect_count=failures,
                    last_error="",
                )
                if not playlist_refresh_started:
                    playlist_refresh_started = True
                    threading.Thread(
                        target=self._refresh_playlist,
                        args=(station, state_root),
                        name=f"ai-playlist-refresh-{station.station_id}",
                        daemon=True,
                    ).start()
                connected_at = time.monotonic()
                while not self.stop.is_set():
                    try:
                        chunk = encoded_audio.get(timeout=15)
                    except queue.Empty as exc:
                        raise RuntimeError("encoder output stalled for 15 seconds") from exc
                    if not chunk:
                        raise RuntimeError(
                            f"encoder exited with code {process.poll()}"
                        )
                    pacer.send(source, chunk, self.stop)
                if time.monotonic() - connected_at >= 300:
                    failures = 0
            except Exception as exc:
                failures += 1
                if used_cached_playlist and not encoder_ready and playlist is not None:
                    try:
                        playlist.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._station_status(
                    station,
                    state="reconnecting",
                    process_id=None,
                    reconnect_count=failures,
                    last_error=str(exc)[:240],
                )
            finally:
                stderr_stop.set()
                audio_stop.set()
                if source is not None:
                    source.close(abort=self.stop.is_set())
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
            if not self.stop.is_set():
                self.stop.wait(RECONNECT_DELAYS_SECONDS[min(failures - 1, len(RECONNECT_DELAYS_SECONDS) - 1)])
        self._station_status(station, state="stopped", process_id=None)

    def run(self) -> int:
        for station in self.config.stations:
            self._station_status(
                station,
                state="starting",
                process_id=None,
                selection_mode="deterministic_no_replacement_with_folder_separation",
            )
        start_station_health_servers(self.config, self.stop, self.station_status)
        threads = [
            threading.Thread(
                target=self._run_station,
                args=(station,),
                name=f"ai-stream-{station.station_id}",
                daemon=True,
            )
            for station in self.config.stations
        ]
        for thread in threads:
            thread.start()
        try:
            while any(thread.is_alive() for thread in threads):
                self.stop.wait(2)
                if self.stop.is_set():
                    break
        finally:
            self.stop.set()
            for thread in threads:
                thread.join(timeout=10)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RadioTEDU EN/FR secure Icecast source supervisor")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    config = load_config(args.config.expanduser().resolve())
    if not config.ffmpeg.is_file():
        raise RuntimeError(f"FFmpeg not found: {config.ffmpeg}")
    password = resolve_source_password(config)
    supervisor = Supervisor(config, password)

    def stop_handler(_signum: int, _frame: object) -> None:
        supervisor.stop.set()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
