"""Run one temporary RadioTEDU language feed with real local media and Qwen IDs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.track_announcements import TrackAnnouncementAssetLibrary


RUNTIME_ROOT = ROOT / "data" / "runtime" / "temporary-dual-station"
QWEN_ROOT = ROOT / "data" / "runtime" / "qwen-commissioning"
TRACK_ANNOUNCEMENT_ROOT = ROOT / "data" / "runtime" / "qwen-track-announcements"
DEFAULT_ENV = Path(
    r"C:\Users\tedu\Desktop\voting\rtjukebox\tools\local-voting-agent\.env"
)
DEFAULT_FFMPEG = Path(
    r"C:\Users\tedu\AppData\Local\RadioTEDU Broadcast Wall\tools\bin\ffmpeg.exe"
)
DEFAULT_FFPROBE = DEFAULT_FFMPEG.with_name("ffprobe.exe")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
REQUIRED_ANNOUNCEMENT_BUFFER = 5
TARGET_ANNOUNCEMENT_BUFFER = 8
MIN_TRACK_SPECIFIC_ROTATION_POOL = 8
FALLBACK_TALKOVER_PROBABILITY = 1.0
MAX_TRACKS_WITHOUT_TALKOVER = 0
FALLBACK_TALKOVER_POLICY = {
    "radio_id": {"weight": 0.55, "cooldown_tracks": 1},
    "continuity": {"weight": 0.45, "cooldown_tracks": 1},
}


STATIONS = {
    "radiotedu-en": {
        "language": "en",
        "label": "RadioTEDU English",
        "mount": "/ai",
        "stream_url": "https://stream.radiotedu.com/ai",
        "qwen": (
            QWEN_ROOT / "en-radio-id.wav",
            QWEN_ROOT / "en-continuity-v2.wav",
        ),
        "programs": {
            "morning": "TEDU Dawn",
            "day": "Campus Flow",
            "evening": "Jazz Lab",
            "night": "Night Signal",
            "weekend_day": "Weekend Signal",
            "weekend_night": "Weekend Night Signal",
        },
        "seed": 7301,
    },
    "radiotedu-fr": {
        "language": "fr",
        "label": "RadioTEDU Français",
        "mount": "/event",
        "stream_url": "https://stream.radiotedu.com/event",
        "qwen": (
            QWEN_ROOT / "fr-radio-id.wav",
            QWEN_ROOT / "fr-continuity-v2.wav",
        ),
        "programs": {
            "morning": "Aube TEDU",
            "day": "Flux Campus",
            "evening": "Laboratoire Jazz",
            "night": "Signal de nuit",
            "weekend_day": "Signal du week-end",
            "weekend_night": "Nuit du week-end",
        },
        "seed": 7302,
    },
}


@dataclass(frozen=True)
class Item:
    path: Path
    kind: str
    title: str
    artist: str
    duration_seconds: float
    source: str
    voice_path: Path | None = None
    voice_duration_seconds: float = 0.0
    track_id: int | None = None
    voice_text: str | None = None
    voice_fact_source_url: str | None = None


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    return values


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_duration(path: Path, ffprobe: Path) -> float:
    result = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
        timeout=30,
    )
    try:
        return max(0.0, float(result.stdout.strip()))
    except ValueError:
        return 0.0


def load_tracks(station_id: str) -> list[Item]:
    database = ROOT / "data" / "stations" / station_id / "radio.db"
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        select id, title, artist, duration_seconds, file_path
        from tracks
        where duration_seconds > 30
        order by id
        """
    ).fetchall()
    connection.close()
    items = []
    for row in rows:
        path = Path(row["file_path"])
        if path.is_file():
            items.append(
                Item(
                    path=path,
                    kind="music",
                    title=str(row["title"] or path.stem),
                    artist=str(row["artist"] or "RadioTEDU"),
                    duration_seconds=float(row["duration_seconds"]),
                    source="local_music_library",
                    track_id=int(row["id"]),
                )
            )
    return items


def load_imaging(station_id: str, ffprobe: Path) -> list[Item]:
    release_root = ROOT / "media" / "imaging" / station_id
    manifest = json.loads((release_root / "manifest.json").read_text(encoding="utf-8"))
    items = []
    for raw in manifest.get("assets", []):
        relative = raw.get("path") or raw.get("file") or raw.get("relative_path")
        if not relative:
            continue
        path = release_root / relative
        if path.is_file():
            items.append(
                Item(
                    path=path,
                    kind="imaging",
                    title=str(raw.get("title") or raw.get("asset_id") or "RadioTEDU ID"),
                    artist="RadioTEDU",
                    duration_seconds=probe_duration(path, ffprobe),
                    source="validated_station_jingle",
                )
            )
    return items


def load_qwen(station_id: str, ffprobe: Path) -> list[Item]:
    items = []
    for index, path in enumerate(STATIONS[station_id]["qwen"], start=1):
        if not path.is_file():
            continue
        header = path.read_bytes()[:12]
        if not (header.startswith(b"RIFF") and header[8:12] == b"WAVE"):
            continue
        items.append(
            Item(
                path=path,
                kind="talking",
                title=f"AI host continuity {index}",
                artist="RadioTEDU AI · Qwen3-TTS",
                duration_seconds=probe_duration(path, ffprobe),
                source="qwen3-tts-0.6b-customvoice",
            )
        )
    return items


def current_program(station_id: str) -> str:
    now = datetime.now(ZoneInfo("Europe/Istanbul"))
    programs = STATIONS[station_id]["programs"]
    weekend = now.weekday() >= 5
    if weekend:
        return programs["weekend_day"] if 8 <= now.hour < 18 else programs["weekend_night"]
    if 6 <= now.hour < 10:
        return programs["morning"]
    if 10 <= now.hour < 18:
        return programs["day"]
    if 18 <= now.hour:
        return programs["evening"]
    return programs["night"]


class Rotation:
    def __init__(
        self,
        tracks: list[Item],
        imaging: list[Item],
        qwen: list[Item],
        announcements: TrackAnnouncementAssetLibrary,
        seed: int,
    ) -> None:
        self.random = random.Random(seed)
        self.tracks = list(tracks)
        self.imaging = list(imaging)
        self.qwen = list(qwen)
        self.announcements = announcements
        self.track_index = 0
        self.imaging_index = 0
        self.qwen_index = 0
        self.planned_track_number = 0
        self.tracks_without_voice = 0
        self.last_fallback_use: dict[str, int] = {}
        self.last_track_id: int | None = None
        self.specific_pool_ids: frozenset[int] = frozenset()
        self.specific_cycle: list[Item] = []
        self.specific_index = 0
        self.pending: deque[Item] = deque()
        self.random.shuffle(self.tracks)
        self._top_up()

    def _next_general_track(self) -> Item:
        if self.track_index >= len(self.tracks):
            self.track_index = 0
            self.random.shuffle(self.tracks)
        item = self.tracks[self.track_index]
        self.track_index += 1
        if (
            len(self.tracks) > 1
            and item.track_id == self.last_track_id
            and self.track_index < len(self.tracks)
        ):
            replacement = self.tracks[self.track_index]
            self.tracks[self.track_index - 1], self.tracks[self.track_index] = (
                replacement,
                item,
            )
            item = replacement
            self.track_index += 1
        self.last_track_id = item.track_id
        return item

    def _sync_specific_cycle(self) -> None:
        ready_ids = self.announcements.ready_track_ids()
        previous_pool_ids = self.specific_pool_ids
        eligible = [
            track
            for track in self.tracks
            if track.track_id is not None and track.track_id in ready_ids
        ]
        eligible_ids = frozenset(
            int(track.track_id) for track in eligible if track.track_id is not None
        )
        if len(eligible_ids) < MIN_TRACK_SPECIFIC_ROTATION_POOL:
            self.specific_pool_ids = eligible_ids
            self.specific_cycle = []
            self.specific_index = 0
            return
        if eligible_ids == self.specific_pool_ids and self.specific_index < len(
            self.specific_cycle
        ):
            return

        remaining = [
            track
            for track in self.specific_cycle[self.specific_index :]
            if track.track_id in eligible_ids
        ]
        new_ids = eligible_ids - previous_pool_ids
        additions = [track for track in eligible if track.track_id in new_ids]
        if not remaining and not additions:
            additions = eligible
        self.random.shuffle(additions)
        self.specific_pool_ids = eligible_ids
        self.specific_cycle = remaining + additions
        self.specific_index = 0

    def _next_track(self) -> Item:
        self._sync_specific_cycle()
        if self.specific_cycle:
            if self.specific_index >= len(self.specific_cycle):
                self.specific_cycle = [
                    track
                    for track in self.tracks
                    if track.track_id in self.specific_pool_ids
                ]
                self.random.shuffle(self.specific_cycle)
                self.specific_index = 0
            if (
                len(self.specific_cycle) > 1
                and self.specific_cycle[self.specific_index].track_id
                == self.last_track_id
            ):
                swap_index = (self.specific_index + 1) % len(self.specific_cycle)
                self.specific_cycle[self.specific_index], self.specific_cycle[swap_index] = (
                    self.specific_cycle[swap_index],
                    self.specific_cycle[self.specific_index],
                )
            item = self.specific_cycle[self.specific_index]
            self.specific_index += 1
            self.last_track_id = item.track_id
            return item
        return self._next_general_track()

    @staticmethod
    def _fallback_role(item: Item) -> str:
        stem = item.path.stem.casefold()
        return "continuity" if "continuity" in stem else "radio_id"

    def _choose_fallback_qwen(self) -> Item | None:
        force_voice = self.tracks_without_voice >= MAX_TRACKS_WITHOUT_TALKOVER
        if not force_voice and self.random.random() >= FALLBACK_TALKOVER_PROBABILITY:
            self.tracks_without_voice += 1
            return None

        eligible: list[Item] = []
        weights: list[float] = []
        for item in self.qwen:
            role = self._fallback_role(item)
            policy = FALLBACK_TALKOVER_POLICY[role]
            last_used = self.last_fallback_use.get(role, -100_000)
            if self.planned_track_number - last_used < int(policy["cooldown_tracks"]):
                continue
            eligible.append(item)
            weights.append(float(policy["weight"]))

        if not eligible:
            self.tracks_without_voice += 1
            return None

        selected = self.random.choices(eligible, weights=weights, k=1)[0]
        role = self._fallback_role(selected)
        self.last_fallback_use[role] = self.planned_track_number
        self.tracks_without_voice = 0
        return selected

    def _fill(self, count: int = 2) -> None:
        for _ in range(count):
            track = self._next_track()
            self.planned_track_number += 1
            prepared = self.announcements.resolve(track.track_id)
            qwen = None if prepared else self._choose_fallback_qwen()
            if prepared:
                self.tracks_without_voice = 0
            voice = prepared or qwen
            self.pending.append(
                Item(
                    path=track.path,
                    kind=track.kind,
                    title=track.title,
                    artist=track.artist,
                    duration_seconds=track.duration_seconds,
                    source=(
                        f"{track.source}+qwen_verified_track_intro"
                        if prepared
                        else (
                            f"{track.source}+qwen_{self._fallback_role(qwen)}"
                            if qwen
                            else f"{track.source}+music_only"
                        )
                    ),
                    voice_path=voice.path if voice else None,
                    voice_duration_seconds=(
                        voice.duration_seconds if voice else 0.0
                    ),
                    track_id=track.track_id,
                    voice_text=prepared.text if prepared else None,
                    voice_fact_source_url=(
                        prepared.fact_source_url if prepared else None
                    ),
                )
            )

    def _top_up(self) -> None:
        while len(self.pending) < TARGET_ANNOUNCEMENT_BUFFER:
            self._fill(1)

    def ready_items(self) -> int:
        return len(self.pending)

    def ready_talkovers(self) -> int:
        return sum(1 for item in self.pending if item.voice_path)

    def next(self) -> Item:
        self._top_up()
        item = self.pending.popleft()
        self._top_up()
        return item

    def preview(self, count: int = 6) -> list[dict]:
        while len(self.pending) < count:
            self._fill(1)
        return [
            {
                "kind": "talkover" if item.voice_path else item.kind,
                "title": item.title,
                "artist": item.artist,
            }
            for item in list(self.pending)[:count]
        ]


class TemporaryStation:
    def __init__(
        self,
        station_id: str,
        env_file: Path,
        ffmpeg: Path,
        ffprobe: Path,
    ) -> None:
        self.station_id = station_id
        self.definition = STATIONS[station_id]
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.runtime_dir = RUNTIME_ROOT / station_id
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.status_path = self.runtime_dir / "status.json"
        self.history_path = self.runtime_dir / "history.jsonl"
        self.pid_path = self.runtime_dir / "station.pid"
        self.pid_path.write_text(str(os.getpid()), encoding="ascii")
        self.started_at = datetime.now(timezone.utc)
        self.encoder: subprocess.Popen | None = None
        self.decoder: subprocess.Popen | None = None
        self.last_error: str | None = None
        self.last_audio_write_at: str | None = None
        self.counters = {
            "music_seconds": 0.0,
            "talking_seconds": 0.0,
            "imaging_seconds": 0.0,
            "items_completed": 0,
            "qwen_items_played": 0,
        }

        env = read_dotenv(env_file)
        source_user = env.get("ICECAST_SOURCE_USERNAME", "")
        source_password = env.get("ICECAST_SOURCE_PASSWORD", "")
        if not source_user or not source_password:
            raise RuntimeError("Icecast source credentials are missing")
        self.target = (
            f"icecast://{quote(source_user, safe='')}:{quote(source_password, safe='')}"
            f"@stream.radiotedu.com:11154{self.definition['mount']}"
        )

        self.tracks = load_tracks(station_id)
        self.imaging = load_imaging(station_id, ffprobe)
        self.qwen = load_qwen(station_id, ffprobe)
        self.announcements = TrackAnnouncementAssetLibrary(
            TRACK_ANNOUNCEMENT_ROOT,
            station_id,
        )
        if not self.tracks:
            raise RuntimeError("No playable music tracks were found")
        if len(self.imaging) != 6:
            raise RuntimeError(f"Expected six validated jingles, found {len(self.imaging)}")
        if len(self.qwen) != 2:
            raise RuntimeError(f"Expected two validated Qwen IDs, found {len(self.qwen)}")
        self.rotation = Rotation(
            self.tracks,
            self.imaging,
            self.qwen,
            self.announcements,
            int(self.definition["seed"]),
        )

    def start_encoder(self) -> None:
        if self.encoder and self.encoder.poll() is None:
            return
        self.encoder = subprocess.Popen(
            [
                str(self.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-i",
                "pipe:0",
                "-vn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-content_type",
                "audio/mpeg",
                "-f",
                "mp3",
                "-legacy_icecast",
                "1",
                "-user_agent",
                "RadioTEDU Temporary Dual Station",
                "-ice_name",
                str(self.definition["label"]),
                "-ice_description",
                "AI-led RadioTEDU temporary language service",
                "-ice_genre",
                "RadioTEDU",
                "-ice_public",
                "1",
                self.target,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )

    def status_payload(
        self,
        current: Item | None,
        started_at: datetime | None,
        elapsed: float,
    ) -> dict:
        music = float(self.counters["music_seconds"])
        talking = float(self.counters["talking_seconds"])
        if current and started_at:
            if current.kind == "music" and current.voice_path:
                current_talking = min(elapsed, current.voice_duration_seconds)
                talking += current_talking
                music += max(0.0, elapsed - current_talking)
            elif current.kind == "music":
                music += elapsed
            elif current.kind == "talking":
                talking += elapsed
        measured = music + talking
        music_percent = round((music / measured) * 100, 1) if measured else 0.0
        talking_percent = round(100.0 - music_percent, 1) if measured else 0.0
        return {
            "station_id": self.station_id,
            "language": self.definition["language"],
            "label": self.definition["label"],
            "mount": self.definition["mount"],
            "stream_url": self.definition["stream_url"],
            "program": current_program(self.station_id),
            "state": "live" if self.encoder and self.encoder.poll() is None else "recovering",
            "started_at": self.started_at.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "encoder_pid": self.encoder.pid if self.encoder and self.encoder.poll() is None else None,
            "decoder_pid": self.decoder.pid if self.decoder and self.decoder.poll() is None else None,
            "last_audio_write_at": self.last_audio_write_at,
            "last_error": self.last_error,
            "now_playing": (
                {
                    "kind": (
                        "talkover"
                        if current.voice_path
                        and elapsed <= current.voice_duration_seconds + 0.35
                        else current.kind
                    ),
                    "title": current.title,
                    "artist": current.artist,
                    "source": current.source,
                    "duration_seconds": current.duration_seconds,
                    "elapsed_seconds": round(elapsed, 1),
                    "remaining_seconds": round(max(0.0, current.duration_seconds - elapsed), 1),
                    "started_at": started_at.isoformat() if started_at else None,
                    "announcement_text": current.voice_text,
                    "fact_source_url": current.voice_fact_source_url,
                }
                if current
                else None
            ),
            "next": self.rotation.preview(),
            "announcement_buffer": {
                "state": (
                    "ready"
                    if self.rotation.ready_items() >= REQUIRED_ANNOUNCEMENT_BUFFER
                    else "warming"
                ),
                "ready": self.rotation.ready_items(),
                "talkovers_planned": self.rotation.ready_talkovers(),
                "required": REQUIRED_ANNOUNCEMENT_BUFFER,
                "target": TARGET_ANNOUNCEMENT_BUFFER,
                "fallback_talkover_probability": FALLBACK_TALKOVER_PROBABILITY,
                "maximum_tracks_without_voice": MAX_TRACKS_WITHOUT_TALKOVER,
                "fallback_cooldowns": {
                    role: int(policy["cooldown_tracks"])
                    for role, policy in FALLBACK_TALKOVER_POLICY.items()
                },
                "unique_qwen_assets": len(self.qwen) + self.announcements.ready_count,
                "runtime_generation_required": self.announcements.queued_count > 0,
                "verified_track_intros_ready": self.announcements.ready_count,
                "verified_track_intros_queued": self.announcements.queued_count,
                "track_specific_rotation_pool": len(self.rotation.specific_pool_ids),
                "track_specific_rotation_required": MIN_TRACK_SPECIFIC_ROTATION_POOL,
            },
            "session_stats": {
                **{key: round(float(value), 1) for key, value in self.counters.items()},
                "qwen_items_played": int(self.counters["qwen_items_played"])
                + (1 if current and current.voice_path and elapsed > 0 else 0),
                "music_percent": music_percent,
                "talking_percent": talking_percent,
            },
            "library": {
                "tracks": len(self.tracks),
                "music_hours": round(sum(item.duration_seconds for item in self.tracks) / 3600, 2),
                "validated_jingles": len(self.imaging),
                "validated_qwen_ids": len(self.qwen),
                "qwen_model": "Qwen3-TTS-12Hz-0.6B-CustomVoice",
                "qwen_assets": [
                    {
                        "name": item.path.name,
                        "sha256": sha256_file(item.path),
                        "duration_seconds": round(item.duration_seconds, 2),
                    }
                    for item in self.qwen
                ],
            },
        }

    def write_status(
        self,
        current: Item | None = None,
        started_at: datetime | None = None,
        elapsed: float = 0.0,
    ) -> None:
        atomic_json(self.status_path, self.status_payload(current, started_at, elapsed))

    def play_item(self, item: Item) -> bool:
        self.start_encoder()
        assert self.encoder and self.encoder.stdin
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        decoder_command = [
            str(self.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-re",
            "-i",
            str(item.path),
        ]
        if item.voice_path:
            voice_delay_seconds = 0.35
            music_release_seconds = item.voice_duration_seconds + voice_delay_seconds
            music_full_seconds = music_release_seconds + 2.5
            fade_out_seconds = min(2.5, max(0.5, item.duration_seconds / 8))
            fade_out_start = max(0.0, item.duration_seconds - fade_out_seconds)
            duck_gain = 0.2512
            filter_graph = (
                "[0:a]loudnorm=I=-16:LRA=11:TP=-1,"
                "aformat=sample_rates=48000:channel_layouts=stereo,"
                f"volume='if(lt(t,{music_release_seconds:.3f}),{duck_gain:.4f},"
                f"if(lt(t,{music_full_seconds:.3f}),"
                f"{duck_gain:.4f}+(1-{duck_gain:.4f})"
                f"*(t-{music_release_seconds:.3f})/2.5,1))':eval=frame,"
                f"afade=t=out:st={fade_out_start:.3f}:d={fade_out_seconds:.3f}[bed];"
                "[1:a]loudnorm=I=-16:LRA=7:TP=-1,"
                "aformat=sample_rates=48000:channel_layouts=stereo,"
                "adelay=350|350[voice];"
                "[bed][voice]amix=inputs=2:duration=first:dropout_transition=0:"
                "normalize=0,alimiter=limit=0.891[out]"
            )
            decoder_command.extend(
                [
                    "-i",
                    str(item.voice_path),
                    "-filter_complex",
                    filter_graph,
                    "-map",
                    "[out]",
                ]
            )
        else:
            decoder_command.extend(
                [
                    "-af",
                    "loudnorm=I=-16:LRA=11:TP=-1,"
                    f"afade=t=out:st={max(0.0, item.duration_seconds - 2.5):.3f}:d=2.5",
                ]
            )
        decoder_command.extend(
            [
                "-f",
                "s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                "pipe:1",
            ]
        )
        self.decoder = subprocess.Popen(
            decoder_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        assert self.decoder.stdout
        completed = False
        try:
            last_status = 0.0
            while True:
                block = self.decoder.stdout.read(65536)
                if not block:
                    completed = self.decoder.wait(timeout=10) == 0
                    break
                try:
                    self.encoder.stdin.write(block)
                    self.encoder.stdin.flush()
                except (BrokenPipeError, OSError):
                    self.last_error = "icecast_encoder_disconnected"
                    self.start_encoder()
                    return False
                self.last_audio_write_at = datetime.now(timezone.utc).isoformat()
                now = time.monotonic()
                if now - last_status >= 2:
                    self.write_status(item, started_at, now - started_monotonic)
                    last_status = now
        finally:
            elapsed = max(0.0, time.monotonic() - started_monotonic)
            if self.decoder and self.decoder.poll() is None:
                self.decoder.terminate()
            self.decoder = None

        result = {
            "station_id": self.station_id,
            "kind": item.kind,
            "title": item.title,
            "artist": item.artist,
            "source": item.source,
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "actual_duration_seconds": round(elapsed, 2),
            "talking_duration_seconds": round(
                min(elapsed, item.voice_duration_seconds)
                if item.kind == "music" and item.voice_path
                else elapsed
                if item.kind == "talking"
                else 0.0,
                2,
            ),
            "music_duration_seconds": round(
                max(0.0, elapsed - item.voice_duration_seconds)
                if item.kind == "music" and item.voice_path
                else elapsed
                if item.kind == "music"
                else 0.0,
                2,
            ),
            "completed": completed,
        }
        with self.history_path.open("a", encoding="utf-8") as history:
            history.write(json.dumps(result, ensure_ascii=False) + "\n")
        if completed:
            if item.kind == "music" and item.voice_path:
                voice_seconds = min(elapsed, item.voice_duration_seconds)
                self.counters["talking_seconds"] += voice_seconds
                self.counters["music_seconds"] += max(0.0, elapsed - voice_seconds)
            else:
                self.counters[f"{item.kind}_seconds"] += elapsed
            self.counters["items_completed"] += 1
            if item.kind == "talking" or item.voice_path:
                self.counters["qwen_items_played"] += 1
            self.last_error = None
        else:
            self.last_error = "media_decoder_failed"
        self.write_status()
        return completed

    def run(self) -> None:
        self.start_encoder()
        self.write_status()
        while True:
            item = self.rotation.next()
            try:
                self.play_item(item)
            except (BrokenPipeError, OSError, subprocess.SubprocessError) as exc:
                self.last_error = type(exc).__name__
                self.write_status()
                time.sleep(2)

    def stop(self) -> None:
        for process in (self.decoder, self.encoder):
            if process and process.poll() is None:
                process.terminate()
        self.pid_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--station", choices=tuple(STATIONS), required=True)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG)
    parser.add_argument("--ffprobe", type=Path, default=DEFAULT_FFPROBE)
    args = parser.parse_args()
    station = TemporaryStation(
        args.station,
        args.env_file.resolve(strict=True),
        args.ffmpeg.resolve(strict=True),
        args.ffprobe.resolve(strict=True),
    )
    try:
        station.run()
    except KeyboardInterrupt:
        return 0
    finally:
        station.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
