from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from import_full_commercial_library import (
    CANONICAL_GENRES,
    STATIONS,
    artist_genre_override,
    repair_presenter_metadata,
    track_genre_override,
)


def expected_genre(track: dict[str, object]) -> str:
    title, artist = repair_presenter_metadata(
        str(track.get("title") or ""),
        str(track.get("artist") or ""),
    )
    current = str(track.get("genre") or "")
    return track_genre_override(artist, title) or artist_genre_override(artist) or current


def build_plan(catalog_path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    catalog_path = catalog_path.resolve()
    catalog_root = catalog_path.parent
    payload = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        raise RuntimeError("catalog tracks must be a list")
    plan: list[dict[str, object]] = []
    for track in tracks:
        if not isinstance(track, dict):
            raise RuntimeError("catalog track must be an object")
        old_genre = str(track.get("genre") or "")
        new_genre = expected_genre(track)
        old_title = str(track.get("title") or "")
        old_artist = str(track.get("artist") or "")
        new_title, new_artist = repair_presenter_metadata(old_title, old_artist)
        if old_genre == new_genre and (old_title, old_artist) == (new_title, new_artist):
            continue
        if old_genre not in CANONICAL_GENRES or new_genre not in CANONICAL_GENRES:
            raise RuntimeError(f"non-canonical genre change: {old_genre} -> {new_genre}")
        destinations = track.get("destinations")
        if not isinstance(destinations, dict):
            raise RuntimeError("catalog destinations must be an object")
        moves: dict[str, dict[str, str]] = {}
        for station in STATIONS:
            old_path = Path(str(destinations.get(station) or "")).resolve()
            station_root = (catalog_root / station).resolve()
            relative = old_path.relative_to(station_root)
            if not relative.parts or relative.parts[0] != old_genre:
                raise RuntimeError(f"destination genre mismatch: {old_path}")
            new_path = (station_root / new_genre / old_path.name).resolve()
            moves[station] = {"old": str(old_path), "new": str(new_path)}
        plan.append(
            {
                "track": track,
                "artist": str(track.get("artist") or ""),
                "title": str(track.get("title") or ""),
                "new_artist": new_artist,
                "new_title": new_title,
                "old_genre": old_genre,
                "new_genre": new_genre,
                "moves": moves,
            }
        )
    return payload, plan


def apply_plan(
    catalog_path: Path,
    payload: dict[str, object],
    plan: list[dict[str, object]],
    backup_root: Path,
) -> Path:
    catalog_path = catalog_path.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = (backup_root.resolve() / f"{stamp}-genre-audit").resolve()
    backup_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(catalog_path, backup_dir / catalog_path.name)
    moved: list[tuple[Path, Path]] = []
    try:
        for change in plan:
            track = change["track"]
            assert isinstance(track, dict)
            destinations = track["destinations"]
            assert isinstance(destinations, dict)
            moves = change["moves"]
            assert isinstance(moves, dict)
            for station, value in moves.items():
                assert isinstance(value, dict)
                old_path = Path(value["old"])
                new_path = Path(value["new"])
                if not old_path.is_file():
                    raise RuntimeError(f"source destination is missing: {old_path}")
                if new_path.exists():
                    raise RuntimeError(f"new destination already exists: {new_path}")
                new_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_path), str(new_path))
                moved.append((old_path, new_path))
                destinations[station] = str(new_path)
            track["genre"] = change["new_genre"]
            track["artist"] = change["new_artist"]
            track["title"] = change["new_title"]
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()
        payload["genre_audit"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "corrected_tracks": len(plan),
            "policy": "operator-reviewed canonical genre overrides",
        }
        temporary = catalog_path.with_suffix(catalog_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, catalog_path)
        report = {
            "catalog": str(catalog_path),
            "backup": str(backup_dir / catalog_path.name),
            "corrected_tracks": len(plan),
            "transitions": dict(
                Counter(
                    f"{change['old_genre']} -> {change['new_genre']}"
                    for change in plan
                )
            ),
            "changes": [
                {key: value for key, value in change.items() if key != "track"}
                for change in plan
            ],
        }
        report_path = backup_dir / "genre-audit-report.json"
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return report_path
    except Exception:
        for old_path, new_path in reversed(moved):
            if new_path.exists() and not old_path.exists():
                old_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(new_path), str(old_path))
        shutil.copy2(backup_dir / catalog_path.name, catalog_path)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit and safely repair installed EN/FR AI catalog genre folders."
    )
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--backup-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    payload, plan = build_plan(args.catalog)
    summary = {
        "mode": "apply" if args.apply else "dry-run",
        "catalog": str(args.catalog.resolve()),
        "corrected_tracks": len(plan),
        "transitions": dict(
            Counter(f"{item['old_genre']} -> {item['new_genre']}" for item in plan)
        ),
        "tracks": [
            {
                "artist": item["artist"],
                "title": item["title"],
                "old_genre": item["old_genre"],
                "new_genre": item["new_genre"],
                "new_artist": item["new_artist"],
                "new_title": item["new_title"],
            }
            for item in plan
        ],
    }
    if args.apply and plan:
        summary["report"] = str(
            apply_plan(args.catalog, payload, plan, args.backup_root)
        )
    print(json.dumps(summary, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
