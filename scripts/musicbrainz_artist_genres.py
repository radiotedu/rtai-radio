from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from musicbrainz_enrich_library import (
    DEFAULT_LIBRARY,
    REQUEST_INTERVAL_SECONDS,
    USER_AGENT,
    atomic_json,
    broad_genre,
    normalized,
)


def weighted_tags(payload: dict) -> list[str]:
    values = {}
    for item in list(payload.get("genres") or []) + list(payload.get("tags") or []):
        name = normalized(item.get("name"))
        if not name:
            continue
        values[name] = max(values.get(name, -999), int(item.get("count") or 0))
    return [name for name, _ in sorted(values.items(), key=lambda pair: (-pair[1], pair[0]))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library-root", type=Path, default=DEFAULT_LIBRARY)
    args = parser.parse_args()
    work_root = args.library_root.resolve() / "MusicBrainz"
    report_path = work_root / "musicbrainz-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    accepted = [
        item
        for item in report["items"]
        if item.get("status") in {"accepted", "accepted_core"}
    ]
    artist_ids = sorted(
        {
            artist_id
            for item in accepted
            for artist_id in (item.get("match") or {}).get("artist_ids") or []
        }
    )
    cache_root = work_root / "artist-responses"
    cache_root.mkdir(parents=True, exist_ok=True)
    progress_path = work_root / "artist-progress.json"
    last_request_at = 0.0
    failures = 0
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        for index, artist_id in enumerate(artist_ids, start=1):
            cache_path = cache_root / f"{artist_id}.json"
            try:
                if cache_path.exists():
                    payload = json.loads(cache_path.read_text(encoding="utf-8"))
                else:
                    elapsed = time.monotonic() - last_request_at
                    if elapsed < REQUEST_INTERVAL_SECONDS:
                        time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)
                    for attempt in range(5):
                        response = client.get(
                            f"https://musicbrainz.org/ws/2/artist/{artist_id}",
                            params={"fmt": "json", "inc": "genres+tags"},
                        )
                        last_request_at = time.monotonic()
                        if response.status_code not in {429, 503}:
                            response.raise_for_status()
                            break
                        time.sleep(min(30.0, 2.0 ** (attempt + 1)))
                    else:
                        raise RuntimeError("MusicBrainz remained rate limited")
                    payload = response.json()
                    atomic_json(cache_path, payload)
            except Exception as exc:
                failures += 1
                payload = {"id": artist_id, "error_type": type(exc).__name__}
                atomic_json(cache_path, payload)
            atomic_json(
                progress_path,
                {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "state": "running" if index < len(artist_ids) else "complete",
                    "completed": index,
                    "total": len(artist_ids),
                    "failures": failures,
                    "request_interval_seconds": REQUEST_INTERVAL_SECONDS,
                },
            )
            print(f"{index}/{len(artist_ids)} failures={failures}", flush=True)

    artist_metadata = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in cache_root.glob("*.json")
    }
    for item in report["items"]:
        match = item.get("match") or {}
        tags = list(match.get("tags") or [])
        artist_tags = []
        for artist_id in match.get("artist_ids") or []:
            artist_tags.extend(weighted_tags(artist_metadata.get(artist_id) or {}))
        artist_tags = list(dict.fromkeys(artist_tags))
        merged = list(dict.fromkeys(tags + artist_tags))
        match["artist_tags"] = artist_tags[:20]
        match["tags"] = merged[:30]
        match["broad_genre"] = broad_genre(merged, match.get("broad_genre"))
    report["artist_enrichment_completed_at"] = datetime.now(timezone.utc).isoformat()
    report["unique_artists_queried"] = len(artist_ids)
    report["artist_lookup_failures"] = failures
    atomic_json(report_path, report)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
