"""Build deterministic Qwen work manifests without touching the live playout process."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.track_announcements import (
    cache_key,
    load_verified_facts,
    metadata_is_announceable,
    render_intro,
)


STATIONS = {
    "radiotedu-en": "en",
    "radiotedu-fr": "fr",
}
FACTS_PATH = ROOT / "data" / "editorial" / "verified-track-facts.json"
OUTPUT_ROOT = ROOT / "data" / "runtime" / "qwen-track-announcements"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build(station_id: str) -> dict:
    language = STATIONS[station_id]
    facts = load_verified_facts(FACTS_PATH)
    database = ROOT / "data" / "stations" / station_id / "radio.db"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        select t.id, t.title, t.artist, m.match_status, m.source_url
        from tracks t
        left join track_musicbrainz_metadata m on m.track_id = t.id
        order by t.id
        """
    ).fetchall()
    connection.close()

    entries: list[dict] = []
    for row in rows:
        title = " ".join(str(row["title"] or "").split())
        artist = " ".join(str(row["artist"] or "").split())
        if (
            row["match_status"] not in {"accepted", "accepted_core"}
            or not metadata_is_announceable(title, artist)
        ):
            continue
        fact = next((item for item in facts if item.matches(title, artist)), None)
        text = render_intro(language, title, artist, fact)
        key = cache_key(station_id, text)
        relative_asset = f"{station_id}/{key}.wav"
        asset = OUTPUT_ROOT / relative_asset
        entry = {
            "track_id": int(row["id"]),
            "title": title,
            "artist": artist,
            "language": language,
            "text": text,
            "cache_key": key,
            "asset_path": relative_asset,
            "state": "ready" if asset.is_file() else "queued",
            "audio_sha256": sha256_file(asset) if asset.is_file() else None,
            "metadata_source_url": str(row["source_url"] or ""),
            "fact": (
                {
                    "source_url": fact.source_url,
                    "source_title": fact.source_title,
                    "verified_at": fact.verified_at,
                    "evidence": fact.evidence,
                }
                if fact
                else None
            ),
        }
        entries.append(entry)
    return {
        "schema_version": 1,
        "station_id": station_id,
        "language": language,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "live_web_requests": False,
        "fallback_asset_policy": "existing_qwen_station_ids",
        "entries": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--station", choices=tuple(STATIONS))
    args = parser.parse_args()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    station_ids = (args.station,) if args.station else tuple(STATIONS)
    for station_id in station_ids:
        payload = build(station_id)
        target = OUTPUT_ROOT / f"{station_id}.json"
        temporary = target.with_suffix(".json.partial")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)
        print(
            json.dumps(
                {
                    "station_id": station_id,
                    "manifest": str(target),
                    "entries": len(payload["entries"]),
                    "with_fact": sum(1 for item in payload["entries"] if item["fact"]),
                    "ready": sum(1 for item in payload["entries"] if item["state"] == "ready"),
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
