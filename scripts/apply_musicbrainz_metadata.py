from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from mutagen.id3 import ID3, ID3NoHeaderError, TCON, TIT2, TPE1, TSRC, TXXX, UFID

from musicbrainz_enrich_library import DEFAULT_LIBRARY, atomic_json


def audio_payload_hash(path: Path) -> str:
    data = path.read_bytes()
    start = 0
    end = len(data)
    if data.startswith(b"ID3") and len(data) >= 10:
        size = (
            (data[6] & 0x7F) << 21
            | (data[7] & 0x7F) << 14
            | (data[8] & 0x7F) << 7
            | (data[9] & 0x7F)
        )
        start = min(end, 10 + size)
    if end - start >= 128 and data[end - 128 : end - 125] == b"TAG":
        end -= 128
    return hashlib.sha256(data[start:end]).hexdigest()


def replace_txxx(tags: ID3, description: str, values: list[str]) -> None:
    tags.delall(f"TXXX:{description}")
    cleaned = [
        str(value).strip()
        for value in values
        if value is not None and str(value).strip()
    ]
    if cleaned:
        tags.add(TXXX(encoding=3, desc=description, text=cleaned))


def apply_one(path: Path, result: dict) -> dict:
    before_file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before_audio_hash = audio_payload_hash(path)
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    match = result["match"]
    tags.delall("TIT2")
    tags.add(TIT2(encoding=3, text=[str(match["title"])]))
    tags.delall("TPE1")
    tags.add(TPE1(encoding=3, text=[str(match["artist"])]))
    tags.delall("TCON")
    tags.add(TCON(encoding=3, text=[str(match["broad_genre"])]))
    if result["status"] == "accepted":
        tags.delall("UFID:http://musicbrainz.org")
        tags.add(
            UFID(
                owner="http://musicbrainz.org",
                data=str(match["recording_id"]).encode("ascii"),
            )
        )
        replace_txxx(tags, "MusicBrainz Recording Id", [match["recording_id"]])
    replace_txxx(tags, "MusicBrainz Artist Id", list(match.get("artist_ids") or []))
    replace_txxx(tags, "MusicBrainz Genres", list(match.get("tags") or []))
    replace_txxx(tags, "MusicBrainz Source", [match.get("musicbrainz_url")])
    replace_txxx(tags, "MusicBrainz Match Confidence", [result["confidence"]])
    isrcs = list(match.get("isrcs") or [])
    tags.delall("TSRC")
    if isrcs:
        tags.add(TSRC(encoding=3, text=isrcs))
    tags.save(path, v2_version=3)
    after_file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    after_audio_hash = audio_payload_hash(path)
    if before_audio_hash != after_audio_hash:
        raise RuntimeError(f"audio payload changed while tagging: {path}")
    return {
        "track_id": result["track_id"],
        "relative_path": result["relative_path"],
        "before_file_sha256": before_file_hash,
        "after_file_sha256": after_file_hash,
        "audio_payload_sha256": after_audio_hash,
        "audio_payload_unchanged": True,
        "recording_id": (
            match["recording_id"] if result["status"] == "accepted" else None
        ),
        "match_status": result["status"],
        "title": match["title"],
        "artist": match["artist"],
        "broad_genre": match["broad_genre"],
        "detail_tags": list(match.get("tags") or []),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library-root", type=Path, default=DEFAULT_LIBRARY)
    args = parser.parse_args()
    library_root = args.library_root.resolve()
    audio_root = (library_root / "Audio").resolve()
    report_path = library_root / "MusicBrainz" / "musicbrainz-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    applied = []
    skipped = []
    for item in report["items"]:
        if item.get("status") not in {"accepted", "accepted_core"}:
            skipped.append(
                {
                    "track_id": item.get("track_id"),
                    "relative_path": item.get("relative_path"),
                    "reason": item.get("status"),
                }
            )
            continue
        path = (audio_root / item["relative_path"]).resolve()
        try:
            path.relative_to(audio_root)
        except ValueError as exc:
            raise RuntimeError("metadata target escaped the desktop audio root") from exc
        if not path.is_file():
            raise FileNotFoundError(path)
        applied.append(apply_one(path, item))
        if len(applied) % 25 == 0:
            print(f"tagged_and_audio_verified={len(applied)}", flush=True)
    final = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "library_root": str(library_root),
        "accepted_and_tagged": len(applied),
        "skipped_for_review": len(skipped),
        "all_audio_payloads_unchanged": all(
            item["audio_payload_unchanged"] for item in applied
        ),
        "items": applied,
        "skipped": skipped,
    }
    atomic_json(library_root / "metadata-application-report.json", final)
    print(
        json.dumps(
            {
                "accepted_and_tagged": len(applied),
                "skipped_for_review": len(skipped),
                "all_audio_payloads_unchanged": final[
                    "all_audio_payloads_unchanged"
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
