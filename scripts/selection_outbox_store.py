"""Incremental durable decision delivery; audio must not serialize the backlog."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class SelectionOutboxStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS decisions (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id TEXT NOT NULL, event_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL, payload TEXT NOT NULL,
                delivered_at TEXT, UNIQUE(station_id, event_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS pending_decisions ON decisions(station_id, delivered_at, ordinal)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def encode(event: dict[str, object]) -> str:
        return json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def enqueue(self, event: dict[str, object]) -> bool:
        payload = self.encode(event)
        with self.connect() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO decisions(station_id,event_id,occurred_at,payload) VALUES(?,?,?,?)",
                (event["station_id"], event["event_id"], event["occurred_at"], payload),
            ).rowcount == 1
            if not inserted:
                existing = db.execute("SELECT payload FROM decisions WHERE station_id=? AND event_id=?",
                                      (event["station_id"], event["event_id"])).fetchone()
                if existing is None or json.loads(existing[0]) != event:
                    raise ValueError("decision event ID conflicts with its stored evidence")
        return inserted

    def references(self, station_ids: Iterable[str]) -> dict[str, list[dict[str, str]]]:
        result = {station_id: [] for station_id in station_ids}
        with self.connect() as db:
            for station_id, event_id, occurred_at in db.execute(
                "SELECT station_id,event_id,occurred_at FROM decisions WHERE delivered_at IS NULL ORDER BY ordinal"
            ):
                if station_id in result:
                    result[station_id].append(dict(station_id=station_id, event_id=event_id, occurred_at=occurred_at))
        return result

    def event(self, station_id: str, event_id: str) -> dict[str, object]:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM decisions WHERE station_id=? AND event_id=?",
                             (station_id, event_id)).fetchone()
        if row is None:
            raise ValueError("durable decision evidence is missing")
        return json.loads(row[0])

    def acknowledge(self, station_id: str, event_id: str) -> None:
        with self.connect() as db:
            if db.execute("UPDATE decisions SET delivered_at=? WHERE station_id=? AND event_id=?",
                          (datetime.now(timezone.utc).isoformat(), station_id, event_id)).rowcount != 1:
                raise ValueError("acknowledged decision evidence is missing")

    def latest_pending(self, station_id: str, limit: int = 20) -> list[dict[str, object]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM decisions WHERE station_id=? AND delivered_at IS NULL ORDER BY occurred_at DESC,ordinal DESC LIMIT ?",
                (station_id, max(1, limit)),
            ).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def migrate_legacy(self, path: Path, station_ids: Iterable[str], *, archive: bool = True) -> int:
        if not path.is_file():
            return 0
        station_ids = tuple(station_ids)
        try:
            import ijson
        except ImportError:
            ijson = None
        inserted = 0
        for station_id in station_ids:
            if ijson is None:
                events = json.loads(path.read_text(encoding="utf-8-sig")).get(station_id, [])
                inserted += self._migrate_events(station_id, events)
            else:
                with path.open("rb") as stream:
                    if stream.read(3) != b"\xef\xbb\xbf":
                        stream.seek(0)
                    inserted += self._migrate_events(station_id, ijson.items(stream, station_id + ".item", use_float=True))
        if archive:
            # Preserve the original backlog after the transactional migration.
            target = path.with_name(path.stem + ".legacy-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + path.suffix)
            path.replace(target)
        return inserted

    def _migrate_events(self, station_id: str, events: Iterable[dict[str, object]]) -> int:
        count = 0
        with self.connect() as db:
            for event in events:
                if not isinstance(event, dict) or event.get("station_id") != station_id or not event.get("event_id") or not event.get("occurred_at"):
                    raise ValueError("legacy decision backlog contains an invalid event")
                count += db.execute(
                    "INSERT OR IGNORE INTO decisions(station_id,event_id,occurred_at,payload) VALUES(?,?,?,?)",
                    (station_id, event["event_id"], event["occurred_at"], self.encode(event)),
                ).rowcount
                if count and count % 20 == 0:
                    db.commit()
        return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--legacy", type=Path, required=True)
    parser.add_argument("--station", action="append", required=True)
    args = parser.parse_args()
    try:
        import os, psutil
        if os.name == "nt":
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except ImportError:
        pass
    store = SelectionOutboxStore(args.db)
    count = store.migrate_legacy(args.legacy, args.station, archive=False)
    print(json.dumps({"inserted": count, "pending": {key: len(value) for key, value in store.references(args.station).items()}}))
