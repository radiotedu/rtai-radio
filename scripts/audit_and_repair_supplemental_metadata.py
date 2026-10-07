from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.oggvorbis import OggVorbis

from run_ai_quality_supervisor import fallback_track_metadata


EXTENSIONS = frozenset({".oga", ".wma"})
UNKNOWN_ARTISTS = frozenset({"", "unknown", "unknown artist", "unspecified", "n/a", "none"})


def first(tags, *keys: str) -> str:
    if tags is None:
        return ""
    for key in keys:
        try:
            values = tags.get(key, [])
        except AttributeError:
            try:
                values = tags[key]
            except (KeyError, TypeError):
                values = []
        if values:
            return str(values[0]).strip()
    return ""


def load_media(path: Path):
    if path.suffix.casefold() == ".oga":
        return OggVorbis(path)
    media = MutagenFile(path, easy=True)
    if media is None:
        raise ValueError("unsupported or unreadable audio container")
    return media


def read_title_artist(media, path: Path) -> tuple[str, str]:
    if path.suffix.casefold() == ".wma":
        return first(media.tags, "Title", "WM/Title"), first(media.tags, "Author", "WM/AlbumArtist")
    return first(media.tags, "title"), first(media.tags, "artist")


def write_title_artist(media, path: Path, title: str, artist: str) -> None:
    if media.tags is None:
        media.add_tags()
    if path.suffix.casefold() == ".wma":
        media.tags["Title"] = title
        if artist:
            media.tags["Author"] = artist
    else:
        media.tags["title"] = [title]
        if artist:
            media.tags["artist"] = [artist]
    media.save()


def run(root: Path, report_root: Path, write: bool) -> dict[str, object]:
    root = root.resolve()
    files = sorted(
        (path.resolve() for path in root.rglob("*") if path.is_file() and path.suffix.casefold() in EXTENSIONS),
        key=lambda path: str(path).casefold(),
    )
    counts = {
        "files": len(files),
        "readable": 0,
        "missing_title": 0,
        "missing_artist": 0,
        "safe_fallbacks_written": 0,
        "write_errors": 0,
        "read_errors": 0,
    }
    entries: list[dict[str, object]] = []
    for path in files:
        try:
            media = load_media(path)
            title, artist = read_title_artist(media, path)
            counts["readable"] += 1
            if not title:
                counts["missing_title"] += 1
            if artist.casefold() in UNKNOWN_ARTISTS:
                counts["missing_artist"] += 1

            fallback_title, fallback_artist = fallback_track_metadata(path)
            proposed_title = title or fallback_title
            proposed_artist = artist if artist.casefold() not in UNKNOWN_ARTISTS else fallback_artist
            entry: dict[str, object] = {
                "path": str(path),
                "before": {"title": title or None, "artist": artist or None},
                "after": {"title": proposed_title or None, "artist": proposed_artist or None},
                "written": False,
            }
            if proposed_title != title or proposed_artist != artist:
                if write:
                    try:
                        write_title_artist(media, path, proposed_title, proposed_artist)
                        entry["written"] = True
                        counts["safe_fallbacks_written"] += 1
                    except Exception as exc:
                        counts["write_errors"] += 1
                        entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
                entries.append(entry)
        except Exception as exc:
            counts["read_errors"] += 1
            entries.append(
                {
                    "path": str(path),
                    "written": False,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
            )

    payload: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "write-safe-fallbacks" if write else "audit-only",
        "root": str(root),
        "extensions": sorted(EXTENSIONS),
        "counts": counts,
        "changes": entries,
    }
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / ("supplemental-safe-fallback-ledger.json" if write else "supplemental-metadata-audit.json")
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(report_path)
    return {**payload, "report": str(report_path), "changes": len(entries)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit and safely fill missing WMA/OGA title and artist tags.")
    parser.add_argument("--root", type=Path, default=Path(r"F:\RadioTEDU Library"))
    parser.add_argument(
        "--report-root",
        type=Path,
        default=Path(r"F:\RadioTEDU Library\_Metadata\Full Library"),
    )
    parser.add_argument("--write-safe-fallbacks", action="store_true")
    args = parser.parse_args()
    result = run(args.root, args.report_root, args.write_safe_fallbacks)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
