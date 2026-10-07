from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import TIT2, TPE1

from run_ai_quality_supervisor import fallback_track_metadata
from run_ai_stream_supervisor import SUPPORTED_AUDIO_EXTENSIONS, load_config
from metadata_cleanup import clean_track_metadata
from dynamic_host_queue import atomic_text_replace


UNKNOWN_ARTISTS = frozenset({"", "unknown", "unknown artist", "unspecified", "n/a", "none"})
HASH_PREFIX = re.compile(r"^[0-9a-fA-F]{16,64}-")


def first(tags, key: str) -> str:
    if tags is None:
        return ""
    lookup = key
    # Mutagen exposes RIFF/WAV ID3 frames as TIT2/TPE1 even in easy mode;
    # normalize those keys so the audit does not report a tag that is present.
    if key == "title" and key not in tags:
        lookup = "TIT2"
    elif key == "artist" and key not in tags:
        lookup = "TPE1"
    values = tags.get(lookup, [])
    return str(values[0]).strip() if values else ""


def set_text_tag(media, path: Path, key: str, value: str, *, preserve_remaining: bool = False) -> None:
    """Write an EasyTag, with explicit ID3 frames for WAV/AIFF containers."""
    values = [value]
    if preserve_remaining and media.tags is not None:
        existing = media.tags.get(key, media.tags.get("TIT2" if key == "title" else "TPE1", []))
        values.extend(str(item) for item in getattr(existing, "text", existing)[1:])
    if path.suffix.casefold() in {".wav", ".aif", ".aiff"}:
        if media.tags is None:
            media.add_tags()
        frame_id, frame_type = ("TIT2", TIT2) if key == "title" else ("TPE1", TPE1)
        media.tags.delall(frame_id)
        media.tags.add(frame_type(encoding=3, text=values))
        return
    if media.tags is None:
        media.add_tags()
    media[key] = values


def active_files(config_path: Path, extra_roots: tuple[Path, ...] = ()) -> tuple[list[Path], list[str]]:
    config = load_config(config_path)
    roots: list[str] = []
    files: dict[str, Path] = {}
    for station in config.stations:
        for root in station.music_roots:
            roots.append(str(root))
    for root in extra_roots:
        roots.append(str(root))
    for root in [Path(root) for root in roots]:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.casefold() in SUPPORTED_AUDIO_EXTENSIONS:
                files[str(path.resolve()).casefold()] = path.resolve()
    return sorted(files.values(), key=lambda item: str(item).casefold()), sorted(set(roots))


def audit(
    config_path: Path,
    report_root: Path,
    write: bool,
    extra_roots: tuple[Path, ...] = (),
    clean_labels: bool = False,
) -> dict[str, object]:
    files, roots = active_files(config_path, extra_roots)
    counts = {
        "files": len(files),
        "readable": 0,
        "missing_title": 0,
        "missing_artist": 0,
        "musicbrainz_tagged": 0,
        "safe_fallbacks_written": 0,
        "write_errors": 0,
        "read_errors": 0,
        "video_labels_cleaned": 0,
    }
    changes: list[dict[str, object]] = []
    for path in files:
        try:
            media = MutagenFile(path, easy=True)
            if media is None:
                raise ValueError("unsupported or unreadable audio container")
            tags = media.tags
            title = first(tags, "title")
            artist = first(tags, "artist")
            counts["readable"] += 1
            if not title:
                counts["missing_title"] += 1
            if artist.casefold() in UNKNOWN_ARTISTS:
                counts["missing_artist"] += 1
            if first(tags, "musicbrainz_trackid") or first(tags, "musicbrainz_recordingid"):
                counts["musicbrainz_tagged"] += 1

            fallback_title, fallback_artist = fallback_track_metadata(path)
            title_is_raw = not title or bool(HASH_PREFIX.match(title))
            artist_is_missing = artist.casefold() in UNKNOWN_ARTISTS
            if clean_labels:
                clean_title, clean_artist = clean_track_metadata(title, artist)
                proposed_title = clean_title or title
                proposed_artist = clean_artist or artist
            else:
                proposed_title = fallback_title if title_is_raw else title
                proposed_artist = fallback_artist if artist_is_missing and fallback_artist else artist
            if proposed_title == title and proposed_artist == artist:
                continue

            entry: dict[str, object] = {
                "path": str(path),
                "before": {"title": title or None, "artist": artist or None},
                "after": {"title": proposed_title or None, "artist": proposed_artist or None},
                "written": False,
            }
            if write:
                try:
                    # Persist original values before modifying the media tags.
                    backup_root = report_root / "original-tags"
                    backup_root.mkdir(parents=True, exist_ok=True)
                    backup = backup_root / (hashlib.sha256(str(path).encode("utf-8")).hexdigest() + ".json")
                    if not backup.exists():
                        original_fields = {}
                        for key, frame in (("title", "TIT2"), ("artist", "TPE1")):
                            raw = tags.get(key, tags.get(frame, [])) if tags is not None else []
                            original_fields[key] = [str(value) for value in getattr(raw, "text", raw)]
                        atomic_text_replace(backup, json.dumps({"path": str(path), "original_fields": original_fields}, ensure_ascii=False, indent=2))
                    if proposed_title != title:
                        set_text_tag(media, path, "title", proposed_title, preserve_remaining=clean_labels)
                    if proposed_artist != artist and proposed_artist:
                        set_text_tag(media, path, "artist", proposed_artist, preserve_remaining=clean_labels)
                    media.save()
                    entry["written"] = True
                    counts["safe_fallbacks_written"] += 1
                    if clean_labels:
                        counts["video_labels_cleaned"] += 1
                except Exception as exc:  # individual files must not stop the library pass
                    counts["write_errors"] += 1
                    entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
            changes.append(entry)
        except Exception as exc:  # corrupt files remain visible in the report
            counts["read_errors"] += 1
            changes.append({
                "path": str(path),
                "written": False,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            })

    generated_at = datetime.now(timezone.utc).isoformat()
    payload: dict[str, object] = {
        "generated_at": generated_at,
        "mode": ("clean-video-labels" if clean_labels else "write-safe-fallbacks") if write else "audit-only",
        "roots": roots,
        "counts": counts,
        "changes": changes,
    }
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / ("safe-fallback-ledger.json" if write else "metadata-audit.json")
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(report_path)
    return {**payload, "report": str(report_path), "changes": len(changes)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit active RadioTEDU AI roots and optionally fill only missing/raw title and artist tags."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(r"C:\ProgramData\RadioTEDU\ServicesCompanion\services\RadioTEDU.AIStreams.json"),
    )
    parser.add_argument(
        "--report-root",
        type=Path,
        default=Path(r"F:\RadioTEDU Library\_Metadata\AI"),
    )
    parser.add_argument("--write-safe-fallbacks", action="store_true")
    parser.add_argument("--clean-video-labels", action="store_true")
    parser.add_argument("--write-clean-labels", action="store_true")
    parser.add_argument(
        "--extra-root",
        action="append",
        type=Path,
        default=[],
        help="Also scan this live audio root; repeat for multiple roots.",
    )
    args = parser.parse_args()
    result = audit(
        args.config,
        args.report_root,
        args.write_safe_fallbacks or args.write_clean_labels,
        tuple(args.extra_root),
        clean_labels=args.clean_video_labels or args.write_clean_labels,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
