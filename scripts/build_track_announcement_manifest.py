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
    INTRO_VARIANT_WEIGHTS,
    cache_key,
    identity,
    intro_variants,
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
LIVE_ROOT = ROOT / "data" / "runtime" / "temporary-dual-station"
VARIANTS_PER_TRACK = 2


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def priority_identities(station_id: str) -> dict[tuple[str, str], int]:
    status = read_json(LIVE_ROOT / station_id / "status.json")
    ordered = [status.get("now_playing") or {}, *list(status.get("next") or [])]
    return {
        (identity(item.get("title")), identity(item.get("artist"))): index
        for index, item in enumerate(ordered)
        if identity(item.get("title"))
    }


def build(station_id: str) -> dict:
    language = STATIONS[station_id]
    facts = load_verified_facts(FACTS_PATH)
    existing_by_track_id: dict[int, list[dict]] = {}
    existing_manifest = OUTPUT_ROOT / f"{station_id}.json"
    if existing_manifest.is_file():
        existing_payload = json.loads(existing_manifest.read_text(encoding="utf-8"))
        if (
            existing_payload.get("schema_version") == 1
            and existing_payload.get("station_id") == station_id
        ):
            for entry in existing_payload.get("entries", []):
                if entry.get("track_id") is not None:
                    existing_by_track_id.setdefault(int(entry["track_id"]), []).append(
                        entry
                    )
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
    priority = priority_identities(station_id)
    rows = sorted(
        rows,
        key=lambda row: (
            priority.get(
                (identity(row["title"]), identity(row["artist"])),
                100_000,
            ),
            int(row["id"]),
        ),
    )

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
        rendered_variants = [
            render_intro(
                language,
                title,
                artist,
                fact,
                variant_index=index,
            )
            for index in range(len(intro_variants(language, title, artist)))
        ]
        existing_texts: list[str] = []
        for existing in existing_by_track_id.get(int(row["id"]), []):
            text = " ".join(str(existing.get("text") or "").split())
            key = cache_key(station_id, text)
            if (
                text
                and existing.get("cache_key") == key
                and identity(existing.get("title")) == identity(title)
                and identity(existing.get("artist")) == identity(artist)
                and text not in existing_texts
            ):
                existing_texts.append(text)
        candidate_indexes = sorted(
            range(len(rendered_variants)),
            key=lambda index: (
                rendered_variants[index] in existing_texts,
                -INTRO_VARIANT_WEIGHTS[index],
                index,
            ),
        )
        desired_texts = list(existing_texts[:VARIANTS_PER_TRACK])
        if len(desired_texts) < VARIANTS_PER_TRACK:
            for index in candidate_indexes:
                text = rendered_variants[index]
                if text not in desired_texts:
                    desired_texts.append(text)
                if len(desired_texts) >= VARIANTS_PER_TRACK:
                    break

        fact_payload = (
            {
                "source_url": fact.source_url,
                "source_title": fact.source_title,
                "verified_at": fact.verified_at,
                "evidence": fact.evidence,
            }
            if fact
            else None
        )
        for text in desired_texts:
            try:
                variant_id = rendered_variants.index(text)
            except ValueError:
                variant_id = int(
                    next(
                        (
                            entry.get("variant_id")
                            for entry in existing_by_track_id.get(int(row["id"]), [])
                            if " ".join(str(entry.get("text") or "").split()) == text
                        ),
                        0,
                    )
                    or 0
                )
            key = cache_key(station_id, text)
            relative_asset = f"{station_id}/{key}.wav"
            asset = OUTPUT_ROOT / relative_asset
            entries.append(
                {
                    "track_id": int(row["id"]),
                    "variant_id": variant_id,
                    "weight": INTRO_VARIANT_WEIGHTS[variant_id],
                    "title": title,
                    "artist": artist,
                    "language": language,
                    "text": text,
                    "cache_key": key,
                    "asset_path": relative_asset,
                    "state": "ready" if asset.is_file() else "queued",
                    "audio_sha256": sha256_file(asset) if asset.is_file() else None,
                    "metadata_source_url": str(row["source_url"] or ""),
                    "fact": fact_payload,
                }
            )
    return {
        "schema_version": 1,
        "station_id": station_id,
        "language": language,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "live_web_requests": False,
        "fallback_asset_policy": "existing_qwen_station_ids",
        "selection_policy": {
            "mode": "weighted_random_per_play",
            "anti_repeat": True,
            "variants_per_track": VARIANTS_PER_TRACK,
            "template_weights": list(INTRO_VARIANT_WEIGHTS),
        },
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
