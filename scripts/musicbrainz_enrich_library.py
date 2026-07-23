from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import httpx


DEFAULT_LIBRARY = Path.home() / "Desktop" / "RadioTEDU Music Library"
API_URL = "https://musicbrainz.org/ws/2/recording"
USER_AGENT = "RadioTEDU-Metadata-Enricher/1.0 (https://radiotedu.com/ai)"
REQUEST_INTERVAL_SECONDS = 1.1

NOISE = re.compile(
    r"[\[(](?:(?:official\s+)?(?:music\s+)?video|official\s+audio|lyrics?|"
    r"audio|visuali[sz]er|hd|hq|4k|remastered(?:\s+\d{4})?)[\])]",
    re.IGNORECASE,
)
NUMBER_PREFIX = re.compile(r"^\s*\d{1,3}\s*[—–-]\s*")


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def clean_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = NUMBER_PREFIX.sub("", text)
    text = NOISE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -–—_")
    return text


def normalized(value: object) -> str:
    text = clean_text(value).casefold()
    text = "".join(
        character if character.isalnum() else " "
        for character in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", text).strip()


def known_artist(value: object) -> bool:
    artist = normalized(value)
    return artist not in {"", "unknown artist"} and not artist.isdigit()


def effective_identity(track: dict) -> tuple[str, str]:
    title = clean_text(track.get("title"))
    artist = clean_text(track.get("artist"))
    if known_artist(artist):
        return title, artist
    for separator in (" - ", " – ", " — "):
        if separator not in title:
            continue
        possible_artist, possible_title = title.split(separator, 1)
        possible_artist = possible_artist.strip()
        possible_title = possible_title.strip()
        if (
            possible_artist
            and possible_title
            and len(possible_artist) <= 100
            and known_artist(possible_artist)
        ):
            return possible_title, possible_artist
    return title, artist


def lucene_phrase(value: str) -> str:
    return value.replace("\\", " ").replace('"', " ").strip()


def artist_credit(recording: dict) -> tuple[str, list[str]]:
    names = []
    ids = []
    for credit in recording.get("artist-credit") or []:
        if isinstance(credit, str):
            continue
        artist = credit.get("artist") or {}
        name = credit.get("name") or artist.get("name")
        if name:
            names.append(str(name))
        if artist.get("id"):
            ids.append(str(artist["id"]))
        join = credit.get("joinphrase")
        if join and names:
            names[-1] += str(join)
    return "".join(names).strip(), ids


def similarity(left: object, right: object) -> float:
    return SequenceMatcher(None, normalized(left), normalized(right)).ratio()


def score_candidate(track: dict, candidate: dict) -> dict:
    candidate_artist, artist_ids = artist_credit(candidate)
    effective_title, effective_artist = effective_identity(track)
    title_score = similarity(effective_title, candidate.get("title"))
    has_known_artist = known_artist(effective_artist)
    artist_score = (
        similarity(effective_artist, candidate_artist) if has_known_artist else 0.7
    )
    duration = candidate.get("length")
    duration_delta = (
        abs(float(track.get("duration_seconds") or 0) - float(duration) / 1000.0)
        if duration
        else None
    )
    duration_score = (
        0.45
        if duration_delta is None
        else max(0.0, 1.0 - min(duration_delta, 30.0) / 30.0)
    )
    search_score = min(1.0, max(0.0, float(candidate.get("score") or 0) / 100.0))
    confidence = (
        title_score * 0.34
        + artist_score * 0.28
        + duration_score * 0.25
        + search_score * 0.13
    )
    if has_known_artist and artist_score < 0.72:
        confidence *= 0.55
    if title_score < 0.72:
        confidence *= 0.55
    if duration_delta is not None and duration_delta > 12:
        confidence *= 0.62
    return {
        "recording": candidate,
        "candidate_artist": candidate_artist,
        "artist_ids": artist_ids,
        "title_score": round(title_score, 4),
        "artist_score": round(artist_score, 4),
        "duration_delta_seconds": round(duration_delta, 3) if duration_delta is not None else None,
        "search_score": round(search_score, 4),
        "confidence": round(confidence, 4),
    }


def select_release(recording: dict) -> dict | None:
    releases = list(recording.get("releases") or [])
    if not releases:
        return None

    def rank(release: dict) -> tuple:
        release_artist, _ = artist_credit(release)
        group = release.get("release-group") or {}
        primary = str(group.get("primary-type") or "")
        official = str(release.get("status") or "").casefold() == "official"
        not_compilation = "compilation" not in {
            str(item).casefold() for item in (group.get("secondary-types") or [])
        }
        artist_specific = normalized(release_artist) not in {"", "various artists"}
        date = str(release.get("date") or "9999")
        return (
            not official,
            not artist_specific,
            not not_compilation,
            primary not in {"Album", "Single", "EP"},
            date,
        )

    return sorted(releases, key=rank)[0]


def tag_names(recording: dict) -> list[str]:
    weighted = {}
    for item in list(recording.get("genres") or []) + list(recording.get("tags") or []):
        name = normalized(item.get("name"))
        if not name:
            continue
        weighted[name] = max(weighted.get(name, -999), int(item.get("count") or 0))
    return [name for name, _ in sorted(weighted.items(), key=lambda pair: (-pair[1], pair[0]))]


def broad_genre(tags: list[str], fallback: str | None) -> str:
    joined = " | ".join(tags)
    rules = (
        ("jazz", ("jazz", "bebop", "swing")),
        ("classical", ("classical", "orchestral", "baroque", "romantic era")),
        ("rock", ("rock", "metal", "punk", "grunge", "emo")),
        ("pop", ("pop", "dance", "disco", "synthpop", "new wave", "electropop")),
    )
    for broad, markers in rules:
        if any(marker in joined for marker in markers):
            return broad
    current = normalized(fallback)
    return current if current in {"pop", "rock", "jazz", "classical"} else "other"


def summarize(track: dict, response: dict) -> dict:
    scored = sorted(
        (score_candidate(track, candidate) for candidate in response.get("recordings") or []),
        key=lambda item: item["confidence"],
        reverse=True,
    )
    if not scored:
        return {
            "track_id": track["track_id"],
            "relative_path": track["relative_path"],
            "status": "no_match",
            "confidence": 0.0,
            "source": "MusicBrainz",
        }
    top_confidence = float(scored[0]["confidence"])
    comparable = [
        item
        for item in scored
        if top_confidence - float(item["confidence"]) <= 0.025
        and item["title_score"] >= 0.9
        and item["artist_score"] >= 0.9
        and item["duration_delta_seconds"] is not None
        and item["duration_delta_seconds"] <= 5.0
    ]

    def canonical_rank(item: dict) -> tuple:
        recording = item["recording"]
        first_date = str(recording.get("first-release-date") or "9999")
        disambiguation = normalized(recording.get("disambiguation"))
        alternate_mix = any(
            marker in disambiguation
            for marker in ("live", "acoustic", "demo", "remix", "dolby atmos", "karaoke")
        )
        return (
            alternate_mix,
            first_date,
            float(item["duration_delta_seconds"] or 999),
            -float(item["confidence"]),
        )

    best = sorted(comparable, key=canonical_rank)[0] if comparable else scored[0]
    runner_up = scored[1]["confidence"] if len(scored) > 1 else 0.0
    candidate = best["recording"]
    release = select_release(candidate)
    release_group = (release or {}).get("release-group") or {}
    tags = tag_names(candidate)
    confidence = float(best["confidence"])
    gap = confidence - float(runner_up)
    duration_delta = best["duration_delta_seconds"]
    exact_enough = (
        confidence >= 0.88
        and best["title_score"] >= 0.82
        and best["artist_score"] >= 0.82
        and duration_delta is not None
        and duration_delta <= 8.0
    )
    _, effective_artist = effective_identity(track)
    local_artist_unknown = not known_artist(effective_artist)
    unknown_candidates = [
        item
        for item in scored
        if item["title_score"] >= 0.97
        and item["search_score"] >= 0.95
        and item["duration_delta_seconds"] is not None
        and item["duration_delta_seconds"] <= 2.0
    ]
    unknown_candidate_artists = {
        normalized(item["candidate_artist"])
        for item in unknown_candidates
        if normalized(item["candidate_artist"])
    }
    unknown_artist_exact = (
        local_artist_unknown
        and bool(unknown_candidates)
        and len(unknown_candidate_artists) == 1
        and normalized(best["candidate_artist"]) in unknown_candidate_artists
    )
    core_exact = (
        best["title_score"] >= 0.97
        and best["artist_score"] >= 0.97
        and best["search_score"] >= 0.95
    )
    status = (
        "accepted"
        if exact_enough or unknown_artist_exact
        else "accepted_core"
        if core_exact
        else "review"
    )
    return {
        "track_id": track["track_id"],
        "relative_path": track["relative_path"],
        "status": status,
        "confidence": round(confidence, 4),
        "runner_up_gap": round(gap, 4),
        "source": "MusicBrainz",
        "local": {
            "title": track.get("title"),
            "artist": track.get("artist"),
            "album": track.get("album"),
            "genre": track.get("genre"),
            "duration_seconds": track.get("duration_seconds"),
        },
        "match": {
            "recording_id": candidate.get("id"),
            "title": candidate.get("title"),
            "artist": best["candidate_artist"],
            "artist_ids": best["artist_ids"],
            "duration_ms": candidate.get("length"),
            "duration_delta_seconds": duration_delta,
            "first_release_date": candidate.get("first-release-date"),
            "isrcs": candidate.get("isrcs") or [],
            "release_id": (release or {}).get("id"),
            "album": (release or {}).get("title"),
            "release_date": (release or {}).get("date"),
            "release_group_id": release_group.get("id"),
            "release_group_type": release_group.get("primary-type"),
            "tags": tags[:20],
            "broad_genre": broad_genre(tags, track.get("genre")),
            "musicbrainz_url": (
                f"https://musicbrainz.org/recording/{candidate.get('id')}"
                if candidate.get("id")
                else None
            ),
        },
        "scores": {
            "title": best["title_score"],
            "artist": best["artist_score"],
            "duration_delta_seconds": duration_delta,
            "search": best["search_score"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library-root", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    library_root = args.library_root.resolve()
    manifest_path = library_root / "library-manifest.original.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tracks = list(manifest["items"])
    if args.limit:
        tracks = tracks[: args.limit]
    work_root = library_root / "MusicBrainz"
    response_root = work_root / "responses"
    result_root = work_root / "results"
    progress_path = work_root / "progress.json"
    response_root.mkdir(parents=True, exist_ok=True)
    result_root.mkdir(parents=True, exist_ok=True)
    completed = 0
    accepted = 0
    accepted_core = 0
    review = 0
    failures = 0
    last_request_at = 0.0
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        for index, track in enumerate(tracks, start=1):
            track_id = int(track["track_id"])
            response_path = response_root / f"{track_id:04d}.json"
            result_path = result_root / f"{track_id:04d}.json"
            try:
                if response_path.exists():
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                else:
                    effective_title, effective_artist = effective_identity(track)
                    title = lucene_phrase(effective_title)
                    artist = lucene_phrase(effective_artist)
                    query = f'recording:"{title}"'
                    if known_artist(artist):
                        query += f' AND artist:"{artist}"'
                    elapsed = time.monotonic() - last_request_at
                    if elapsed < REQUEST_INTERVAL_SECONDS:
                        time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)
                    for attempt in range(5):
                        api_response = client.get(
                            API_URL,
                            params={"query": query, "fmt": "json", "limit": 10},
                        )
                        last_request_at = time.monotonic()
                        if api_response.status_code not in {429, 503}:
                            api_response.raise_for_status()
                            break
                        time.sleep(min(30.0, 2.0 ** (attempt + 1)))
                    else:
                        raise RuntimeError("MusicBrainz remained rate limited")
                    response = api_response.json()
                    atomic_json(response_path, response)
                result = summarize(track, response)
                atomic_json(result_path, result)
                accepted += result["status"] == "accepted"
                accepted_core += result["status"] == "accepted_core"
                review += result["status"] not in {"accepted", "accepted_core"}
            except Exception as exc:
                failures += 1
                atomic_json(
                    result_path,
                    {
                        "track_id": track_id,
                        "relative_path": track["relative_path"],
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "source": "MusicBrainz",
                    },
                )
            completed += 1
            atomic_json(
                progress_path,
                {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "state": "running" if index < len(tracks) else "complete",
                    "completed": completed,
                    "total": len(tracks),
                    "accepted": accepted,
                    "accepted_core": accepted_core,
                    "review": review,
                    "failures": failures,
                    "rate_limit_seconds": REQUEST_INTERVAL_SECONDS,
                    "credentials_logged": False,
                },
            )
            print(
                f"{completed}/{len(tracks)} accepted={accepted} "
                f"accepted_core={accepted_core} review={review} failures={failures}",
                flush=True,
            )
    results = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(result_root.glob("*.json"))
    ]
    atomic_json(
        work_root / "musicbrainz-report.json",
        {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "source": "https://musicbrainz.org/ws/2/recording",
            "user_agent": USER_AGENT,
            "request_interval_seconds": REQUEST_INTERVAL_SECONDS,
            "tracks": len(results),
            "accepted": sum(item.get("status") == "accepted" for item in results),
            "accepted_core": sum(
                item.get("status") == "accepted_core" for item in results
            ),
            "review": sum(item.get("status") == "review" for item in results),
            "no_match": sum(item.get("status") == "no_match" for item in results),
            "errors": sum(item.get("status") == "error" for item in results),
            "items": results,
        },
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
