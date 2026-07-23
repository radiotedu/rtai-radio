from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from musicbrainz_enrich_library import DEFAULT_LIBRARY


ROOT = Path(__file__).resolve().parents[1]
STATIONS = ("radiotedu-en", "radiotedu-fr")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library-root", type=Path, default=DEFAULT_LIBRARY)
    args = parser.parse_args()
    library_root = args.library_root.resolve()
    audio_root = (library_root / "Audio").resolve()
    report = json.loads(
        (library_root / "MusicBrainz" / "musicbrainz-report.json").read_text(
            encoding="utf-8"
        )
    )
    by_id = {int(item["track_id"]): item for item in report["items"]}
    manifest = json.loads(
        (library_root / "library-manifest.original.json").read_text(encoding="utf-8")
    )
    manifest_by_id = {
        int(item["track_id"]): item for item in manifest["items"]
    }
    paths = {
        int(item["track_id"]): (audio_root / item["relative_path"]).resolve()
        for item in manifest["items"]
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise RuntimeError(f"{len(missing)} desktop library files are missing")
    timestamp = datetime.now(timezone.utc).isoformat()
    backup_root = ROOT / "data" / "runtime" / "desktop-library-db-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    summary = []
    for station_id in STATIONS:
        database = ROOT / "data" / "stations" / station_id / "radio.db"
        backup = backup_root / f"{station_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        shutil.copy2(database, backup)
        connection = sqlite3.connect(database)
        try:
            connection.execute("begin immediate")
            connection.execute(
                """
                create table if not exists track_musicbrainz_metadata (
                    track_id integer primary key,
                    recording_id text,
                    artist_ids_json text not null,
                    isrcs_json text not null,
                    detail_tags_json text not null,
                    match_confidence real not null,
                    match_status text not null,
                    source_url text,
                    checked_at text not null
                )
                """
            )
            rows = connection.execute("select id from tracks order by id").fetchall()
            for (track_id,) in rows:
                result = by_id.get(int(track_id), {})
                match = result.get("match") or {}
                path = paths[int(track_id)]
                if result.get("status") in {"accepted", "accepted_core"}:
                    title = match.get("title")
                    artist = match.get("artist")
                    genre = match.get("broad_genre")
                    connection.execute(
                        """
                        update tracks
                        set title=?, artist=?, genre=?, file_path=?, updated_at=?
                        where id=?
                        """,
                        (title, artist, genre, str(path), timestamp, track_id),
                    )
                else:
                    local_artist = str(
                        (result.get("local") or {}).get("artist")
                        or manifest_by_id[int(track_id)].get("artist")
                        or ""
                    ).strip()
                    if local_artist.isdigit():
                        connection.execute(
                            """
                            update tracks
                            set artist='Unknown Artist', file_path=?, updated_at=?
                            where id=?
                            """,
                            (str(path), timestamp, track_id),
                        )
                    else:
                        connection.execute(
                            "update tracks set file_path=?, updated_at=? where id=?",
                            (str(path), timestamp, track_id),
                        )
                connection.execute(
                    """
                    insert into track_musicbrainz_metadata(
                        track_id, recording_id, artist_ids_json, isrcs_json,
                        detail_tags_json, match_confidence, match_status,
                        source_url, checked_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(track_id) do update set
                        recording_id=excluded.recording_id,
                        artist_ids_json=excluded.artist_ids_json,
                        isrcs_json=excluded.isrcs_json,
                        detail_tags_json=excluded.detail_tags_json,
                        match_confidence=excluded.match_confidence,
                        match_status=excluded.match_status,
                        source_url=excluded.source_url,
                        checked_at=excluded.checked_at
                    """,
                    (
                        track_id,
                        (
                            match.get("recording_id")
                            if result.get("status") == "accepted"
                            else None
                        ),
                        json.dumps(match.get("artist_ids") or []),
                        json.dumps(match.get("isrcs") or []),
                        json.dumps(match.get("tags") or []),
                        float(result.get("confidence") or 0),
                        str(result.get("status") or "not_checked"),
                        match.get("musicbrainz_url"),
                        timestamp,
                    ),
                )
            connection.commit()
            desktop_count = connection.execute(
                "select count(*) from tracks where file_path like ?",
                (str(audio_root) + "%",),
            ).fetchone()[0]
            metadata_count = connection.execute(
                "select count(*) from track_musicbrainz_metadata"
            ).fetchone()[0]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        summary.append(
            {
                "station_id": station_id,
                "database_backup": str(backup),
                "desktop_paths": desktop_count,
                "metadata_rows": metadata_count,
            }
        )
    output = {
        "switched_at": timestamp,
        "audio_root": str(audio_root),
        "stations": summary,
    }
    (library_root / "database-switch-report.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
