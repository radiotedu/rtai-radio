from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from mutagen import File as MutagenFile


SUPPORTED_AUDIO = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
LOSSY_AUDIO = {".aac", ".m4a", ".mp3", ".ogg", ".opus", ".wma"}
STATIONS = ("radiotedu-en", "radiotedu-fr")
CANONICAL_GENRES = (
    "Blues", "Classical", "Electronic", "Folk", "Hip-Hop",
    "Jazz", "Lo-Fi", "Pop", "Rock",
)

UNKNOWN_VALUES = {
    "", "unknown", "unknown artist", "unspecified", "various artists",
    "track", "audio", "music", "pop rising", "none", "n/a",
}
TURKISH_SPECIFIC = frozenset("ğĞıİşŞ")
TURKISH_ARTISTS = (
    "ajda pekkan", "athena", "barış manço", "ben fero", "bülent ortaçgil",
    "cem adrian", "cem karaca", "ceza", "dedublüman", "derya uluğ", "duman",
    "emre aydın", "erkin koray", "ezhel", "feridun düzağaç", "gazapizm",
    "göksel", "gripin", "hande yener", "hadise", "kayahan", "kenan doğulu",
    "leman sam", "maNga", "madrigal", "mabel matiz", "mfö", "model",
    "mor ve ötesi", "müslüm gürses", "neşet ertaş", "nil karaibrahimgil",
    "pinhani", "pilli bebek", "redd", "sagopa kajmer", "sertab erener",
    "sezen aksu", "sıla", "şebnem ferah", "tarkan", "teoman", "yalın",
    "yıldız tilbe", "zeynep bastık",
)
TURKISH_WORDS = {
    "ask", "aşk", "bana", "ben", "beni", "bir", "biz", "böyle", "çok",
    "değil", "dünya", "gece", "gel", "git", "gibi", "göz", "hayat",
    "kalp", "neden", "sen", "seni", "sev", "şarkı", "yalnız", "yine",
    "yok",
}
JUNK_TERMS = (
    "audiobook", "audio book", "interview", "meeting recording", "podcast",
    "radio jingle", "ringtone", "screen recording", "sound effect", "speech",
    "station id", "test tone", "the kids picks singers", "voice message",
    "vlog", "whatsapp audio", "zoom recording",
)

# First match wins. Specific styles precede broad pop/rock terms.
GENRE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Lo-Fi", ("lo-fi", "lofi", "chillhop", "ambient", "downtempo")),
    ("Hip-Hop", ("hip-hop", "hip hop", "rap", "trap", "grime")),
    ("Blues", ("blues",)),
    ("Classical", ("classical", "baroque", "chamber music", "opera", "symphony")),
    ("Jazz", ("jazz", "bebop", "swing")),
    ("Folk", ("folk", "country", "bluegrass", "traditional", "world music")),
    (
        "Electronic",
        (
            "electronic", "electronica", "edm", "house", "techno", "trance",
            "synthwave", "dark wave", "darkwave", "industrial", "dubstep",
            "drum and bass",
        ),
    ),
    ("Rock", ("rock", "metal", "punk", "emo", "grunge", "hardcore", "gothic")),
    ("Pop", ("pop", "r&b", "rnb", "soul", "disco", "funk", "k-pop", "j-pop")),
)

ARTIST_GENRE_OVERRIDES = {
    # Pop and mainstream R&B
    "5 seconds of summer": "Pop", "abba": "Pop", "bee gees": "Pop",
    "beyoncé": "Pop",
    "billie eilish": "Pop", "black eyed peas": "Pop", "britney spears": "Pop",
    "bruno mars": "Pop", "charli xcx": "Pop", "dua lipa": "Pop",
    "ed sheeran": "Pop", "gracie abrams": "Pop", "harry styles": "Pop",
    "justin timberlake": "Pop", "katy perry": "Pop", "lady gaga": "Pop",
    "lil nas x": "Hip-Hop", "madonna": "Pop", "maroon 5": "Pop",
    "mark ronson": "Pop", "michael jackson": "Pop", "nelly furtado": "Pop",
    "diana ross": "Pop", "olivia dean": "Pop", "olivia rodrigo": "Pop",
    "ornella vanoni": "Pop", "ravyn lenae": "Pop", "raye": "Pop", "rihanna": "Pop",
    "rita ora": "Pop", "sabrina carpenter": "Pop", "sam smith": "Pop",
    "shawn mendes": "Pop", "sia": "Pop", "taylor swift": "Pop",
    "the weeknd": "Pop", "tyla": "Pop", "zara larsson": "Pop",
    "aurora": "Pop", "benson boone": "Pop", "daniel powter": "Pop",
    "fitz and the tantrums": "Pop", "fun. featuring janelle monáe": "Pop",
    "gwen stefani featuring eve": "Pop", "james blunt": "Pop",
    "natasha bedingfield": "Pop", "philip bailey & phil collins": "Pop",
    "ruth b.": "Pop", "sophie ellis‐bextor": "Pop", "suki waterhouse": "Pop",
    "timbaland feat. onerepublic": "Pop", "tv girl": "Pop",
    "kirinji feat. yonyon": "Pop",
    # Hip-Hop / rap
    "lil tecca": "Hip-Hop", "madcon": "Hip-Hop", "nf": "Hip-Hop",
    "pitbull feat. t‐pain": "Hip-Hop", "rich brian": "Hip-Hop",
    "bryce vine": "Hip-Hop", "dj snake, sean paul": "Electronic",
    "dimitri vegas": "Electronic",
    # Electronic
    "a touch of class": "Electronic", "alan walker x a$ap rocky": "Electronic",
    "alphaville": "Electronic", "blue foundation": "Electronic",
    "daft punk feat. julian casablancas": "Electronic",
    "dna, suzanne vega, neal slateford & nick batt": "Electronic",
    "jax jones bebe rexha": "Electronic", "m83": "Electronic",
    "marshmello": "Electronic", "modjo": "Electronic",
    "pinkpantheress": "Electronic", "the chainsmokers illenium": "Electronic",
    # Folk / country
    "kelsea ballerini": "Folk", "lord huron": "Folk", "the lumineers": "Folk",
    # Rock and alternative
    "arctic monkeys": "Rock", "coldplay": "Rock", "crowded house": "Rock",
    "foster the people": "Rock",
    "machine gun kelly feat. yungblood travis barker": "Rock",
    "imagine dragons": "Rock", "linkin park": "Rock", "muse": "Rock",
    "paramore": "Rock", "panic! at the disco": "Rock",
    "red hot chili peppers": "Rock", "system of a down": "Rock",
    "the beatles": "Rock", "toto": "Rock",
    # Vocal jazz, funk/pop, and modern classical
    "frank sinatra": "Jazz",
    "earth, wind & fire": "Pop", "bobby caldwell": "Pop",
    "max richter": "Classical",
}

TRACK_GENRE_OVERRIDES = {
    ("coldplay, bts", "my universe"): "Pop",
    ("machine gun kelly feat. yungblood travis barker", "i think i'm okay"): "Rock",
    ("machine gun kelly feat. yungblood travis barker", "i think im okay"): "Rock",
    ("the rolling stones & purple disco machine", "mess it up"): "Electronic",
    ("the weeknd feat. daft punk", "i feel it coming"): "Pop",
}

INVERTED_PRESENTER_METADATA = {
    ("classic and perfect", "bryce vine"): ("Classic and Perfect", "Bryce Vine"),
    ("fuego", "dj snake, sean paul"): ("Fuego", "DJ Snake, Sean Paul"),
    ("instagram", "dimitri vegas"): ("Instagram", "Dimitri Vegas"),
}


@dataclass(frozen=True)
class Candidate:
    source: str
    genre: str
    artist: str
    title: str
    album: str | None
    duration_seconds: float
    bitrate_kbps: int | None
    sample_rate_hz: int | None
    sha256: str = ""


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[^\W\d_]+", value.casefold(), flags=re.UNICODE))


def looks_turkish(*values: str, language_tag: str = "") -> bool:
    language = _normalize(language_tag).replace("_", "-")
    if language == "tr" or language.startswith("tr-") or language == "tur":
        return True
    combined = " ".join(values)
    if any(character in TURKISH_SPECIFIC for character in combined):
        return True
    normalized = _normalize(combined)
    if any(artist.casefold() in normalized for artist in TURKISH_ARTISTS):
        return True
    return len(_tokens(combined) & TURKISH_WORDS) >= 2


def canonical_genre(*signals: str) -> str | None:
    normalized = " | ".join(_normalize(signal).replace("_", " ") for signal in signals if signal)
    for genre, terms in GENRE_RULES:
        if any(re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", normalized) for term in terms):
            return genre
    return None


def artist_genre_override(artist: str) -> str | None:
    normalized = _normalize(artist)
    direct = ARTIST_GENRE_OVERRIDES.get(normalized)
    if direct:
        return direct
    for known_artist, genre in ARTIST_GENRE_OVERRIDES.items():
        if normalized.startswith(f"{known_artist};") or normalized.startswith(
            f"{known_artist},"
        ):
            return genre
    return None


def track_genre_override(artist: str, title: str) -> str | None:
    artist_key = _normalize(artist)
    title_key = _normalize(title)
    for (known_artist, known_title), genre in TRACK_GENRE_OVERRIDES.items():
        if artist_key == known_artist and title_key.startswith(known_title):
            return genre
    return None


def repair_presenter_metadata(title: str, artist: str) -> tuple[str, str]:
    """Repair the common `song title - artist` files whose tags were inverted."""
    title_key = _normalize(title)
    artist_key = _normalize(artist)
    explicit = INVERTED_PRESENTER_METADATA.get((artist_key, title_key))
    if explicit:
        return explicit
    presenter_noise = (
        "official", "music video", "lyric", "audio", "4k", "hd video",
    )
    if title_key in ARTIST_GENRE_OVERRIDES and any(
        marker in artist_key for marker in presenter_noise
    ):
        return artist.strip(), title.strip()
    return title.strip(), artist.strip()


def _first(tags, *names: str) -> str:
    if tags is None:
        return ""
    for name in names:
        value = tags.get(name)
        if isinstance(value, (list, tuple)) and value:
            return str(value[0]).strip()
        if value:
            return str(value).strip()
    return ""


def _fallback_title_artist(path: Path) -> tuple[str, str]:
    stem = re.sub(r"^\s*\d+[ _.-]+", "", path.stem).strip()
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return title.strip() or stem, artist.strip() or ""
    parts = path.parts
    artist = ""
    if "[standalone recordings]" in parts:
        index = parts.index("[standalone recordings]")
        if index:
            artist = parts[index - 1]
    return stem, artist


def _is_junk(*values: str) -> bool:
    normalized = _normalize(" ".join(values))
    return any(term in normalized for term in JUNK_TERMS)


def broadcast_metadata_is_safe(title: str, artist: str) -> bool:
    title = re.sub(r"\s+", " ", title).strip()
    artist = re.sub(r"\s+", " ", artist).strip()
    if not title or not artist or len(title) > 160 or len(artist) > 120:
        return False
    if any("\uff00" <= character <= "\uffef" for character in title + artist):
        return False
    if any(ord(character) < 32 for character in title + artist):
        return False
    return any(character.isalpha() for character in title) and any(
        character.isalpha() for character in artist
    )


def inspect_path(
    path: Path,
    *,
    minimum_seconds: float,
    maximum_seconds: float,
    minimum_bitrate_kbps: int,
) -> tuple[Candidate | None, str | None]:
    try:
        if path.stat().st_size < 512_000:
            return None, "file smaller than 500 KiB"
        media = MutagenFile(path, easy=True)
        if media is None or getattr(media, "info", None) is None:
            return None, "unreadable audio container"
        duration = float(getattr(media.info, "length", 0) or 0)
        if duration < minimum_seconds or duration > maximum_seconds:
            return None, f"duration {duration:.1f}s outside range"
        bitrate_raw = int(getattr(media.info, "bitrate", 0) or 0)
        bitrate_kbps = round(bitrate_raw / 1000) if bitrate_raw else None
        if path.suffix.casefold() in LOSSY_AUDIO and bitrate_kbps and bitrate_kbps < minimum_bitrate_kbps:
            return None, f"bitrate {bitrate_kbps} kbps below minimum"
        sample_rate = int(getattr(media.info, "sample_rate", 0) or 0) or None
        if sample_rate and sample_rate < 32_000:
            return None, f"sample rate {sample_rate} Hz below minimum"

        tags = media.tags
        fallback_title, fallback_artist = _fallback_title_artist(path)
        title = _first(tags, "title") or fallback_title
        artist = _first(tags, "artist", "albumartist") or fallback_artist
        title, artist = repair_presenter_metadata(title, artist)
        album = _first(tags, "album") or None
        genre_tag = _first(tags, "genre")
        language = _first(tags, "language", "lang")
        if _normalize(title) in UNKNOWN_VALUES or _normalize(artist) in UNKNOWN_VALUES:
            return None, "missing reliable title or artist"
        if not broadcast_metadata_is_safe(title, artist):
            return None, "unsafe presenter metadata"
        if _is_junk(str(path), artist, title, album or ""):
            return None, "junk/non-song signal"
        if looks_turkish(str(path), artist, title, album or "", language_tag=language):
            return None, "Turkish-language signal"

        genre = track_genre_override(artist, title) or artist_genre_override(artist) or canonical_genre(
            genre_tag, str(path.parent), album or ""
        ) or ""
        return (
            Candidate(
                source=str(path.resolve()),
                genre=genre,
                artist=artist.strip(),
                title=title.strip(),
                album=album.strip() if album else None,
                duration_seconds=round(duration, 3),
                bitrate_kbps=bitrate_kbps,
                sample_rate_hz=sample_rate,
            ),
            None,
        )
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"[:300]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


TITLE_NOISE = re.compile(
    r"[\[(][^\])]*(?:official|video|audio|lyrics?|visuali[sz]er|pseudo|4k|hd|shortened\s+version|single\s+version|feat\.?|ft\.?)[^\])]*[\])]",
    flags=re.IGNORECASE,
)


def semantic_key(candidate: Candidate) -> tuple[str, str]:
    def clean(value: str, *, title: bool = False) -> str:
        value = value.replace("�", "'").replace("’", "'").replace("‐", "-")
        if title:
            value = TITLE_NOISE.sub(" ", value)
            value = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]\s*$", " ", value)
            value = re.sub(
                r"\b(?:official\s+music\s+video|official\s+video|official\s+audio|lyrics?|music\s+video)\b",
                " ",
                value,
                flags=re.IGNORECASE,
            )
            value = re.sub(r"\s+(?:ft\.?|feat\.?)\s+.*$", " ", value, flags=re.IGNORECASE)
        value = "".join(
            character
            for character in unicodedata.normalize("NFKD", value)
            if not unicodedata.combining(character)
        )
        return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

    return clean(candidate.artist), clean(candidate.title, title=True)


def _recording_preference(candidate: Candidate) -> tuple[int, int, int, int]:
    text = f"{candidate.title} {candidate.source}".casefold()
    noisy = int(any(term in text for term in ("official music video", "lyrics", "lyric video")))
    organized = int("organized by genre" in candidate.source.casefold())
    clean_unicode = int("�" not in candidate.title and "�" not in candidate.artist)
    return (-noisy, organized, clean_unicode, candidate.bitrate_kbps or 0)


def _decode_start(path: Path, ffmpeg: Path) -> str | None:
    try:
        process = subprocess.run(
            [
                str(ffmpeg), "-nostdin", "-v", "error", "-i", str(path),
                "-map", "0:a:0", "-t", "15", "-f", "null", "-",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if process.returncode:
            return process.stderr.decode("utf-8", errors="replace").strip()[:300] or "decode failed"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"[:300]
    return None


def discover_audio(roots: tuple[Path, ...], excluded_roots: tuple[Path, ...]) -> list[Path]:
    excluded = tuple(root.resolve() for root in excluded_roots if root.exists())
    found: dict[str, Path] = {}
    for root in roots:
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            continue
        for path in resolved_root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in SUPPORTED_AUDIO:
                continue
            resolved = path.resolve()
            if any(exclusion == resolved or exclusion in resolved.parents for exclusion in excluded):
                continue
            found[str(resolved).casefold()] = resolved
    return sorted(found.values(), key=lambda item: str(item).casefold())


def curate(
    paths: list[Path],
    *,
    ffmpeg: Path,
    workers: int,
    minimum_seconds: float,
    maximum_seconds: float,
    minimum_bitrate_kbps: int,
) -> tuple[list[Candidate], list[dict[str, str]], dict[str, int]]:
    rejected: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        inspected = list(
            pool.map(
                lambda path: inspect_path(
                    path,
                    minimum_seconds=minimum_seconds,
                    maximum_seconds=maximum_seconds,
                    minimum_bitrate_kbps=minimum_bitrate_kbps,
                ),
                paths,
            )
        )
    provisional: list[Candidate] = []
    for path, (candidate, reason) in zip(paths, inspected):
        if candidate is None:
            rejected.append({"source": str(path), "reason": reason or "rejected"})
        else:
            provisional.append(candidate)

    artist_votes: dict[str, Counter[str]] = defaultdict(Counter)
    album_votes: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for candidate in provisional:
        if not candidate.genre:
            continue
        artist_key = _normalize(candidate.artist)
        artist_votes[artist_key][candidate.genre] += 1
        if candidate.album:
            album_votes[(artist_key, _normalize(candidate.album))][candidate.genre] += 1

    def confident(votes: Counter[str] | None) -> str | None:
        if not votes:
            return None
        ranked = votes.most_common()
        if len(ranked) == 1:
            return ranked[0][0]
        if ranked[0][1] >= ranked[1][1] * 2:
            return ranked[0][0]
        return None

    prelim: list[Candidate] = []
    for candidate in provisional:
        if candidate.genre:
            prelim.append(candidate)
            continue
        artist_key = _normalize(candidate.artist)
        inferred = None
        if candidate.album:
            inferred = confident(album_votes.get((artist_key, _normalize(candidate.album))))
        inferred = inferred or confident(artist_votes.get(artist_key))
        if inferred:
            prelim.append(replace(candidate, genre=inferred))
        else:
            rejected.append({"source": candidate.source, "reason": "no reliable canonical genre"})

    # Avoid hashing duplicate hardlinks more than once.
    inode_candidates: dict[tuple[int, int], Candidate] = {}
    for candidate in prelim:
        stat = Path(candidate.source).stat()
        key = (int(stat.st_dev), int(stat.st_ino))
        inode_candidates.setdefault(key, candidate)
    unique_inodes = list(inode_candidates.values())
    with ThreadPoolExecutor(max_workers=max(2, workers // 2)) as pool:
        hashes = list(pool.map(lambda item: _sha256(Path(item.source)), unique_inodes))
    hashed = [replace(candidate, sha256=checksum) for candidate, checksum in zip(unique_inodes, hashes)]

    content_candidates: dict[str, Candidate] = {}
    for candidate in hashed:
        content_candidates.setdefault(candidate.sha256, candidate)
    content_deduplicated = list(content_candidates.values())
    recording_candidates: dict[tuple[str, str], Candidate] = {}
    for candidate in content_deduplicated:
        key = semantic_key(candidate)
        existing = recording_candidates.get(key)
        if existing is None or _recording_preference(candidate) > _recording_preference(existing):
            recording_candidates[key] = candidate
    deduplicated = list(recording_candidates.values())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        decode_errors = list(pool.map(lambda item: _decode_start(Path(item.source), ffmpeg), deduplicated))
    accepted: list[Candidate] = []
    for candidate, error in zip(deduplicated, decode_errors):
        if error:
            rejected.append({"source": candidate.source, "reason": f"decode check: {error}"})
        else:
            accepted.append(candidate)
    accepted.sort(key=lambda item: (item.genre, item.artist.casefold(), item.title.casefold(), item.sha256))
    stats = {
        "inspected": len(paths),
        "quality_eligible": len(provisional),
        "classified": len(prelim),
        "unique_hardlinks": len(unique_inodes),
        "unique_content": len(content_deduplicated),
        "unique_recordings": len(deduplicated),
        "accepted": len(accepted),
    }
    return accepted, rejected, stats


def _safe_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "-", value).strip(" .-")
    return cleaned[:120] or fallback


def install(candidates: list[Candidate], target_root: Path, rejected: list[dict[str, str]], stats: dict[str, int]) -> dict[str, object]:
    target_root.mkdir(parents=True, exist_ok=True)
    for station in STATIONS:
        for genre in CANONICAL_GENRES:
            (target_root / station / genre).mkdir(parents=True, exist_ok=True)
    tracks: list[dict[str, object]] = []
    for candidate in candidates:
        source = Path(candidate.source)
        filename = (
            f"{candidate.sha256[:16]}-{_safe_name(candidate.artist, 'Unknown Artist')} - "
            f"{_safe_name(candidate.title, 'Track')}{source.suffix.casefold()}"
        )
        destinations: dict[str, str] = {}
        for station in STATIONS:
            destination = target_root / station / candidate.genre / filename
            if destination.exists() and _sha256(destination) != candidate.sha256:
                raise RuntimeError(f"checksum collision at {destination}")
            if not destination.exists():
                try:
                    os.link(source, destination)
                except OSError:
                    shutil.copy2(source, destination)
            destinations[station] = str(destination.resolve())
        tracks.append({**asdict(candidate), "destinations": destinations})
    generated_at = datetime.now(timezone.utc).isoformat()
    catalog = {
        "version": 1,
        "generated_at": generated_at,
        "rights_policy": "operator-asserted-mgm-muyap-commercial-broadcast-authorization",
        "language_policy": "heuristically non-Turkish catalog shared by en/fr",
        "tracks": tracks,
    }
    report = {
        "version": 1,
        "generated_at": generated_at,
        "stats": stats,
        "genres": {genre: sum(1 for item in candidates if item.genre == genre) for genre in CANONICAL_GENRES},
        "rejected": rejected,
    }
    catalog_path = target_root / "licensed-commercial-catalog.json"
    report_path = target_root / "curation-report.json"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"catalog": str(catalog_path), "report": str(report_path), "tracks_per_station": len(candidates)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Curate the full local MGM/MUYAP commercial library for AI Live.")
    parser.add_argument("--source-root", action="append", required=True, type=Path)
    parser.add_argument("--exclude-root", action="append", default=[], type=Path)
    parser.add_argument("--target-root", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--minimum-seconds", type=float, default=90)
    parser.add_argument("--maximum-seconds", type=float, default=360)
    parser.add_argument("--minimum-bitrate-kbps", type=int, default=96)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    roots = tuple(path.resolve() for path in args.source_root)
    target = args.target_root.resolve()
    if any(root == target or root in target.parents for root in roots):
        raise SystemExit("target must be outside every source tree")
    paths = discover_audio(roots, tuple(args.exclude_root))
    candidates, rejected, stats = curate(
        paths,
        ffmpeg=args.ffmpeg.resolve(),
        workers=max(1, args.workers),
        minimum_seconds=args.minimum_seconds,
        maximum_seconds=args.maximum_seconds,
        minimum_bitrate_kbps=args.minimum_bitrate_kbps,
    )
    summary: dict[str, object] = {
        "mode": "apply" if args.apply else "dry-run",
        "source_roots": [str(root) for root in roots],
        "target_root": str(target),
        "stats": stats,
        "genres": {genre: sum(1 for item in candidates if item.genre == genre) for genre in CANONICAL_GENRES},
        "rejected": len(rejected),
    }
    if args.apply:
        summary["installation"] = install(candidates, target, rejected, stats)
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0 if candidates else 2


if __name__ == "__main__":
    raise SystemExit(main())
