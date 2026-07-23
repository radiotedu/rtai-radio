"""Continuously publish read-only AI Music Director shadow recommendations."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.shadow_music_director import (
    DirectorTrack,
    RecentPlay,
    ShadowMusicDirector,
    atomic_json,
    parse_timestamp,
    status_payload,
)


STATIONS = ("radiotedu-en", "radiotedu-fr")
LIVE_ROOT = ROOT / "data" / "runtime" / "temporary-dual-station"
SHADOW_ROOT = ROOT / "data" / "runtime" / "shadow-music-director"


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_tracks(station_id: str) -> list[DirectorTrack]:
    database = ROOT / "data" / "stations" / station_id / "radio.db"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        select
            t.id, t.title, t.artist, t.genre, t.mood, t.bpm,
            t.duration_seconds, t.file_path, m.match_status
        from tracks t
        left join track_musicbrainz_metadata m on m.track_id = t.id
        where t.duration_seconds > 30
        order by t.id
        """
    ).fetchall()
    connection.close()
    return [
        DirectorTrack(
            track_id=int(row["id"]),
            title=str(row["title"] or ""),
            artist=str(row["artist"] or "Unknown Artist"),
            genre=str(row["genre"]) if row["genre"] else None,
            mood=str(row["mood"]) if row["mood"] else None,
            bpm=int(row["bpm"]) if row["bpm"] else None,
            duration_seconds=float(row["duration_seconds"] or 0),
            file_path=str(row["file_path"]),
            metadata_status=(
                str(row["match_status"]) if row["match_status"] else None
            ),
        )
        for row in rows
    ]


def load_recent_plays(station_id: str, limit: int = 500) -> list[RecentPlay]:
    history_path = LIVE_ROOT / station_id / "history.jsonl"
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    plays: list[RecentPlay] = []
    for line in lines:
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        played_at = parse_timestamp(raw.get("ended_at"))
        if not played_at or raw.get("completed") is not True:
            continue
        plays.append(
            RecentPlay(
                title=str(raw.get("title") or ""),
                artist=str(raw.get("artist") or ""),
                played_at=played_at,
            )
        )
    return plays


def build_once(station_id: str, tracks: list[DirectorTrack] | None = None) -> dict:
    live = read_json(LIVE_ROOT / station_id / "status.json")
    now_playing = live.get("now_playing") or {}
    program = str(live.get("program") or "Campus Flow")
    recent = load_recent_plays(station_id)
    loaded_tracks = tracks if tracks is not None else load_tracks(station_id)
    now = datetime.now(timezone.utc)
    recommendations = ShadowMusicDirector(station_id).recommend(
        loaded_tracks,
        recent,
        program=program,
        now=now,
        current_title=str(now_playing.get("title") or ""),
        current_artist=str(now_playing.get("artist") or ""),
        count=6,
    )
    return status_payload(
        station_id,
        program=program,
        generated_at=now,
        recommendations=recommendations,
        actual_next=list(live.get("next") or [])[:6],
        recent_play_count=len(recent),
        track_count=len(loaded_tracks),
        current={
            "title": str(now_playing.get("title") or ""),
            "artist": str(now_playing.get("artist") or ""),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args()
    if args.interval < 5:
        raise SystemExit("--interval must be at least 5 seconds")

    tracks_by_station = {station: load_tracks(station) for station in STATIONS}
    SHADOW_ROOT.mkdir(parents=True, exist_ok=True)
    (SHADOW_ROOT / "director.pid").write_text(str(__import__("os").getpid()), encoding="ascii")
    try:
        while True:
            for station_id in STATIONS:
                target = SHADOW_ROOT / station_id / "status.json"
                try:
                    payload = build_once(station_id, tracks_by_station[station_id])
                except Exception as exc:
                    payload = {
                        "schema_version": 1,
                        "station_id": station_id,
                        "state": "error",
                        "read_only": True,
                        "controls_live_playout": False,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                atomic_json(target, payload)
            if args.once:
                return 0
            time.sleep(args.interval)
    finally:
        (SHADOW_ROOT / "director.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
