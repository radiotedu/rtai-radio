"""Migrate an older supervisor's genuine queue evidence before its next start."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from run_ai_stream_supervisor import write_status_atomic


def read_state(path: Path) -> dict:
    for attempt in range(20):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.1)
    raise RuntimeError("state unavailable")


def migrate(root: Path, *, write: bool) -> dict:
    state = read_state(root / "ffmpeg-supervisor.json")
    database = sqlite3.connect((root / "public-sync-selection-decisions.sqlite3").as_uri() + "?mode=ro", uri=True)
    report = {"observed_at": datetime.now(timezone.utc).isoformat(), "written": write, "stations": {}}
    try:
        for station_id, station in state["stations"].items():
            local = station["outputs"]["legacy"]
            queue = local["laya_song_queue"]
            row = database.execute(
                "SELECT payload FROM decisions WHERE station_id=? AND event_id=?",
                (station_id, queue["last_event_id"]),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"{station_id}: current decision evidence missing")
            event = json.loads(row[0])
            if event["decision_schema_version"] != "radio-song-choice-library-shortlist-single-v1":
                raise RuntimeError(f"{station_id}: not a genuine single-choice event")
            evidence = event["live_selection_queue"]
            pending = list(evidence["queued_track_ids"])
            pending.append(event["selected_track_id"])
            pipeline_id = queue["current_track_id"]
            if pipeline_id in pending:
                pending = pending[pending.index(pipeline_id) + 1:]
            elif pipeline_id != evidence.get("pipeline_current_track_id"):
                raise RuntimeError(f"{station_id}: producer position cannot be reconstructed")
            if len(pending) != queue["queue_depth"] or len(set(pending)) != len(pending):
                raise RuntimeError(f"{station_id}: evidence and actual queue depth disagree")
            if not pending:
                raise RuntimeError(f"{station_id}: no unconsumed choices available")
            on_air_id = (local.get("now_playing") or {}).get("track_id")
            heard = [dict(record) for record in event.get("recent_tracks", [])
                     if record.get("track_id") not in {pipeline_id, on_air_id}]
            reserved = set(pending)
            reserved.update(record["track_id"] for record in event.get("recent_tracks", []))
            if pipeline_id:
                reserved.add(pipeline_id)
            for payload, in database.execute(
                "SELECT payload FROM decisions WHERE station_id=? AND occurred_at>=?",
                (station_id, state["started_at"]),
            ):
                chosen = json.loads(payload)
                reserved.add(chosen["selected_track_id"])
                reserved.update(record["track_id"] for record in chosen.get("recent_tracks", []))
                reserved.update((chosen.get("live_selection_queue") or {}).get("queued_track_ids", []))
            checkpoint = {
                "version": 1, "station_id": station_id,
                "updated_at": report["observed_at"],
                "ready_track_ids": pending,
                "pipeline_current_track_id": pipeline_id,
                "reserved_track_ids": sorted(reserved),
                "played_recent_records": heard[-6:],
                "migration_source_event_id": event["event_id"],
                "migration_source_state_started_at": state["started_at"],
            }
            path = root / "playlists" / f"{station_id}-live-song-queue.json"
            report["stations"][station_id] = checkpoint
            if write and not write_status_atomic(path, json.dumps(checkpoint, ensure_ascii=False) + "\n"):
                raise RuntimeError(f"{station_id}: checkpoint write failed")
    finally:
        database.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    report = migrate(args.state_root.resolve(), write=args.write)
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
