from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


SUPPORTED_AUDIO = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
STATIONS = ("radiotedu-en", "radiotedu-fr")
CANONICAL_GENRES = (
    "Blues", "Classical", "Electronic", "Folk", "Hip-Hop",
    "Jazz", "Lo-Fi", "Pop", "Rock",
)

FOLDER_GENRES = {
    "5+ wochen": "Pop",
    "alternative and punk": "Rock",
    "alternative metal": "Rock",
    "alternative pop_rock": "Rock",
    "alternative punk": "Rock",
    "alternative rock": "Rock",
    "ambient": "Lo-Fi",
    "art pop": "Pop",
    "blues": "Blues",
    "contemporary country": "Folk",
    "dance-pop": "Pop",
    "dark wave": "Electronic",
    "electronic": "Electronic",
    "emo": "Rock",
    "funk": "Pop",
    "gothic metal": "Rock",
    "heavy metal": "Rock",
    "indie rock": "Rock",
    "jazz": "Jazz",
    "nu metal": "Rock",
    "pop": "Pop",
    "pop rock": "Rock",
    "punk": "Rock",
    "rock": "Rock",
    "rockabilly": "Rock",
}

OTHER_ARTIST_GENRES = {
    "alcoholic faith mission": "Rock",
    "corina": "Pop",
    "crew 7": "Electronic",
    "dartmouth college aires": "Pop",
    "kyf brewer": "Rock",
    "metro boomin & future feat. don toliver": "Hip-Hop",
    "miguel and the living dead": "Rock",
    "the ghastly ones": "Rock",
    "the kids picks singers": "Pop",
}

# Strong Turkish-language signals only. Characters shared with French or German
# (such as c-cedilla, o-umlaut and u-umlaut) are deliberately not used.
TURKISH_SPECIFIC = frozenset("ğĞıİşŞ")
REJECT_TERMS = (
    "the kids picks singers",  # low-quality children's cover collection
)


@dataclass(frozen=True)
class Candidate:
    source: str
    genre: str
    artist: str
    title: str
    duration_seconds: float
    sha256: str


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def classify_genre(source_folder: str, artist: str, title: str) -> str | None:
    artist_key = _normalize(artist)
    title_key = _normalize(title)
    if artist_key == "madonna" and title_key == "vogue":
        return "Pop"
    if _normalize(source_folder) == "other":
        return OTHER_ARTIST_GENRES.get(artist_key)
    return FOLDER_GENRES.get(_normalize(source_folder))


def looks_turkish(*values: str, language_tag: str = "") -> bool:
    language = _normalize(language_tag).replace("_", "-")
    if language == "tr" or language.startswith("tr-") or language == "tur":
        return True
    return any(character in TURKISH_SPECIFIC for value in values for character in value)


def _safe_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "-", value).strip(" .-")
    return cleaned[:120] or fallback


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fallback_title(path: Path) -> str:
    return re.sub(r"^\s*\d+[ _.-]+", "", path.stem).strip() or path.stem


def _probe(path: Path, ffprobe: Path) -> dict[str, object]:
    process = subprocess.run(
        [
            str(ffprobe), "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name:stream_tags=language,title,artist,genre",
            "-show_entries", "format=duration:format_tags=language,title,artist,genre",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise ValueError((process.stderr or "ffprobe failed").strip())
    payload = json.loads(process.stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise ValueError("no audio stream")
    stream = streams[0]
    tags = {
        str(key).casefold(): str(value)
        for section in (payload.get("format", {}).get("tags", {}), stream.get("tags", {}))
        for key, value in section.items()
    }
    return {
        "duration": float(payload.get("format", {}).get("duration") or 0),
        "codec": str(stream.get("codec_name") or ""),
        "tags": tags,
    }


def _decode_check(path: Path, ffmpeg: Path) -> None:
    process = subprocess.run(
        [str(ffmpeg), "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"],
        capture_output=True,
        timeout=90,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(detail or "ffmpeg decode failed")


def inspect_catalog(
    source_root: Path,
    *,
    ffprobe: Path,
    ffmpeg: Path,
    minimum_seconds: float,
    maximum_seconds: float,
) -> tuple[list[Candidate], list[dict[str, str]]]:
    accepted: list[Candidate] = []
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()
    paths = sorted(
        (path for path in source_root.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO),
        key=lambda path: str(path).casefold(),
    )
    for path in paths:
        relative = path.relative_to(source_root)
        source_folder = relative.parts[0] if relative.parts else ""
        fallback_artist = relative.parts[1] if len(relative.parts) > 1 else "Unknown Artist"
        try:
            probe = _probe(path, ffprobe)
            tags = probe["tags"]
            assert isinstance(tags, dict)
            title = str(tags.get("title") or _fallback_title(path)).strip()
            artist = str(tags.get("artist") or fallback_artist).strip()
            language = str(tags.get("language") or "")
            duration = float(probe["duration"])
            if duration < minimum_seconds or duration > maximum_seconds:
                raise ValueError(
                    f"duration {duration:.1f}s outside {minimum_seconds:.0f}-{maximum_seconds:.0f}s"
                )
            if looks_turkish(str(relative), artist, title, language_tag=language):
                raise ValueError("Turkish-language signal")
            combined = _normalize(f"{relative} {artist} {title}")
            if any(term in combined for term in REJECT_TERMS):
                raise ValueError("operator quality exclusion")
            genre = classify_genre(source_folder, artist, title)
            if genre not in CANONICAL_GENRES:
                raise ValueError(f"no canonical jingle genre for {source_folder!r}")
            _decode_check(path, ffmpeg)
            checksum = _sha256(path)
            if checksum in seen:
                raise ValueError("duplicate audio content")
            seen.add(checksum)
            accepted.append(
                Candidate(
                    source=str(path.resolve()),
                    genre=genre,
                    artist=artist,
                    title=title,
                    duration_seconds=round(duration, 3),
                    sha256=checksum,
                )
            )
        except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            rejected.append({"source": str(path.resolve()), "reason": str(exc)})
    return accepted, rejected


def install_catalog(candidates: list[Candidate], target_root: Path) -> dict[str, object]:
    target_root.mkdir(parents=True, exist_ok=True)
    for station in STATIONS:
        for genre in CANONICAL_GENRES:
            (target_root / station / genre).mkdir(parents=True, exist_ok=True)
    tracks: list[dict[str, object]] = []
    counts = {station: 0 for station in STATIONS}
    for candidate in candidates:
        source = Path(candidate.source)
        filename = (
            f"{candidate.sha256[:16]}-"
            f"{_safe_name(candidate.artist, 'Unknown Artist')} - "
            f"{_safe_name(candidate.title, 'Track')}{source.suffix.lower()}"
        )
        destinations: dict[str, str] = {}
        for station in STATIONS:
            destination_dir = target_root / station / candidate.genre
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / filename
            if destination.exists() and _sha256(destination) != candidate.sha256:
                raise RuntimeError(f"checksum collision at {destination}")
            if not destination.exists():
                try:
                    os.link(source, destination)
                except OSError:
                    shutil.copy2(source, destination)
            destinations[station] = str(destination.resolve())
            counts[station] += 1
        tracks.append({**asdict(candidate), "destinations": destinations})

    catalog = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rights_policy": "operator-asserted-mgm-muyap-commercial-broadcast-authorization",
        "language_policy": "non-Turkish catalog shared by radiotedu-en and radiotedu-fr",
        "tracks": tracks,
    }
    catalog_path = target_root / "licensed-commercial-catalog.json"
    catalog_path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"catalog": str(catalog_path.resolve()), "counts": counts, "tracks": len(candidates)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Curate locally licensed commercial music for both non-Turkish AI Live streams."
    )
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--target-root", required=True, type=Path)
    parser.add_argument("--ffprobe", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--minimum-seconds", type=float, default=90)
    parser.add_argument("--maximum-seconds", type=float, default=360)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    target_root = args.target_root.resolve()
    if not source_root.is_dir():
        raise SystemExit(f"source root does not exist: {source_root}")
    if "rights-cleared" in {part.casefold() for part in source_root.parts}:
        raise SystemExit("CC0/rights-cleared source roots are forbidden for this commercial catalog")
    if source_root == target_root or source_root in target_root.parents:
        raise SystemExit("target root must be outside the source tree")

    accepted, rejected = inspect_catalog(
        source_root,
        ffprobe=args.ffprobe.resolve(),
        ffmpeg=args.ffmpeg.resolve(),
        minimum_seconds=args.minimum_seconds,
        maximum_seconds=args.maximum_seconds,
    )
    result: dict[str, object] = {
        "mode": "apply" if args.apply else "dry-run",
        "source_root": str(source_root),
        "target_root": str(target_root),
        "accepted": len(accepted),
        "rejected": rejected,
        "genres": {
            genre: sum(1 for item in accepted if item.genre == genre)
            for genre in CANONICAL_GENRES
        },
    }
    if args.apply:
        result["installation"] = install_catalog(accepted, target_root)
        report_path = target_root / "curation-report.json"
        report_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        result["report"] = str(report_path.resolve())
    # Keep CLI output compatible with Windows service consoles using cp1254.
    # The persisted catalog/report remain UTF-8 and retain the original names.
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
