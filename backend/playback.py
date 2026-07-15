from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .liquidsoap import append_liquidsoap_item, render_liquidsoap_config


@dataclass
class QueueItem:
    item_type: str
    title: str
    file_path: str
    duration_seconds: float | None = None
    artist: str | None = None
    track_id: int | None = None


class PlaybackController:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.queue: list[QueueItem] = []
        self.now_playing: QueueItem | None = None
        self.now_started_at: datetime | None = None
        self.running = False
        self.backend = self._backend()

    def _backend(self) -> str:
        requested = self.settings.playback_backend.lower()
        if requested == "mpv" and shutil.which(self.settings.mpv_path):
            return "mpv"
        if requested == "ffplay" and shutil.which(self.settings.ffplay_path):
            return "ffplay"
        if requested == "auto":
            if shutil.which(self.settings.mpv_path):
                return "mpv"
            if shutil.which(self.settings.ffplay_path):
                return "ffplay"
        if requested == "liquidsoap":
            render_liquidsoap_config(self.settings)
            return "liquidsoap"
        return "simulate"

    def add(self, item: QueueItem) -> None:
        self.queue.append(item)

    def queue_talkover(
        self,
        speech: QueueItem,
        track: QueueItem,
        *,
        output_path: str | Path,
        decision,
        renderer,
    ) -> bool:
        """Queue one composite, or preserve speech-then-track order on any render error."""

        try:
            rendered = renderer.render(
                speech.file_path,
                track.file_path,
                output_path,
                decision,
            )
        except Exception:
            self.add(speech)
            self.add(track)
            return False
        self.add(
            QueueItem(
                item_type="talkover_composite",
                title=track.title,
                file_path=str(rendered),
                duration_seconds=track.duration_seconds,
                artist=track.artist,
                track_id=track.track_id,
            )
        )
        return True

    def skip(self) -> None:
        self.now_playing = None
        self.now_started_at = None

    def play_next(self) -> QueueItem | None:
        if not self.queue:
            self.now_playing = None
            self.now_started_at = None
            return None
        item = self.queue.pop(0)
        self.now_playing = item
        self.now_started_at = datetime.now(timezone.utc)
        self.running = True
        if self.backend == "simulate":
            time.sleep(min(float(item.duration_seconds or 1), 1.0))
        elif self.backend == "mpv":
            subprocess.Popen([self.settings.mpv_path, "--really-quiet", item.file_path]).wait()
        elif self.backend == "ffplay":
            subprocess.Popen([self.settings.ffplay_path, "-nodisp", "-autoexit", "-loglevel", "quiet", item.file_path]).wait()
        elif self.backend == "liquidsoap":
            append_liquidsoap_item(self.settings, item.file_path)
        return item

    def state(self) -> dict:
        if self.now_playing:
            current = self.now_playing
            return {
                "type": current.item_type,
                "title": current.title,
                "artist": current.artist,
                "file_path": current.file_path,
                "started_at": self.now_started_at.isoformat() if self.now_started_at else None,
            }
        return {
            "type": "idle",
            "title": "Idle — waiting for music library.",
            "artist": None,
            "started_at": None,
        }

    def health(self) -> str:
        return self.backend

    def watchdog_status(self, grace_seconds: float = 30.0) -> dict:
        if not self.now_playing or not self.now_started_at:
            return {
                "stuck_playback": 0,
                "elapsed_seconds": None,
                "threshold_seconds": None,
                "title": None,
            }
        elapsed = max(0.0, (datetime.now(timezone.utc) - self.now_started_at).total_seconds())
        duration = float(self.now_playing.duration_seconds or 0)
        threshold = max(0.0, duration + max(0.0, float(grace_seconds)))
        return {
            "stuck_playback": int(elapsed >= threshold),
            "elapsed_seconds": round(elapsed, 3),
            "threshold_seconds": round(threshold, 3),
            "title": self.now_playing.title,
        }
