"""Deterministic, source-backed track announcements for the temporary station."""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


UNKNOWN_ARTISTS = {
    "",
    "unknown",
    "unknown artist",
    "various artists",
}
NOISY_TITLE_MARKERS = (
    "lyrics",
    "official video",
    "official visualizer",
    "knotyou.com",
    "spotdown.org",
)
TRUSTED_FACT_HOSTS = {
    "archive.blondie.net",
    "blondie.net",
    "www.blondie.net",
    "madonna.com",
    "www.madonna.com",
    "muse.mu",
    "www.muse.mu",
    "musicbrainz.org",
    "www.musicbrainz.org",
    "wikidata.org",
    "www.wikidata.org",
}


def identity(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def metadata_is_announceable(title: object, artist: object) -> bool:
    clean_title = " ".join(str(title or "").split())
    clean_artist = identity(artist)
    return bool(
        clean_title
        and clean_artist not in UNKNOWN_ARTISTS
        and not any(marker in clean_title.casefold() for marker in NOISY_TITLE_MARKERS)
        and not clean_artist.isdigit()
    )


def cache_key(station_id: str, text: str) -> str:
    payload = json.dumps(
        {
            "schema": 1,
            "station_id": station_id,
            "text": " ".join(text.split()),
            "model": "Qwen3-TTS-12Hz-0.6B-CustomVoice",
            "speaker": "ryan",
            "finishing": "radiotedu-track-intro-v1",
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedFact:
    track_title: str
    track_artist: str
    text_en: str
    text_fr: str
    source_url: str
    source_title: str
    verified_at: str
    evidence: str

    @classmethod
    def from_dict(cls, value: dict) -> "VerifiedFact":
        fact = cls(
            track_title=" ".join(str(value.get("track_title") or "").split()),
            track_artist=" ".join(str(value.get("track_artist") or "").split()),
            text_en=" ".join(str(value.get("text_en") or "").split()),
            text_fr=" ".join(str(value.get("text_fr") or "").split()),
            source_url=" ".join(str(value.get("source_url") or "").split()),
            source_title=" ".join(str(value.get("source_title") or "").split()),
            verified_at=" ".join(str(value.get("verified_at") or "").split()),
            evidence=" ".join(str(value.get("evidence") or "").split()),
        )
        fact.validate()
        return fact

    def validate(self) -> None:
        parsed = urlparse(self.source_url)
        if parsed.scheme != "https" or parsed.hostname not in TRUSTED_FACT_HOSTS:
            raise ValueError("fact source must be an allow-listed HTTPS host")
        if not all(
            (
                self.track_title,
                self.track_artist,
                self.text_en,
                self.text_fr,
                self.source_title,
                self.verified_at,
                self.evidence,
            )
        ):
            raise ValueError("verified fact fields cannot be empty")
        if len(self.text_en.split()) > 30 or len(self.text_fr.split()) > 36:
            raise ValueError("verified fact is too long for an intro")
        combined = f"{self.text_en} {self.text_fr} {self.evidence}".casefold()
        if "lyrics" in combined or "transcript" in combined:
            raise ValueError("lyrics and transcripts cannot be used as fact material")

    def matches(self, title: str, artist: str) -> bool:
        return identity(title) == identity(self.track_title) and identity(artist) == identity(
            self.track_artist
        )


def _weighted_variant_index(key: str, weights: tuple[int, ...]) -> int:
    total = sum(weights)
    roll = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big") % total
    cumulative = 0
    for index, weight in enumerate(weights):
        cumulative += weight
        if roll < cumulative:
            return index
    return len(weights) - 1


def render_intro(
    language: str,
    title: str,
    artist: str,
    fact: VerifiedFact | None = None,
    *,
    variant_key: str | None = None,
) -> str:
    if language not in {"en", "fr"}:
        raise ValueError(f"unsupported language: {language}")
    if not metadata_is_announceable(title, artist):
        raise ValueError("track metadata is not safe to announce")
    if fact is not None and not fact.matches(title, artist):
        raise ValueError("fact identity does not match the track")
    if language == "en":
        variants = (
            f"On Radio TED U, this is {title} by {artist}.",
            f"You're with Radio TED U. Here is {artist} with {title}.",
            f"Next on Radio TED U: {title}, from {artist}.",
            f"{artist} on Radio TED U. This is {title}.",
        )
        index = (
            0
            if variant_key is None
            else _weighted_variant_index(f"en:{variant_key}", (34, 27, 22, 17))
        )
        base = variants[index]
        return f"{base} {fact.text_en}" if fact else base
    variants = (
        f"Sur Radio TED U, voici {title}, par {artist}.",
        f"Vous êtes sur Radio TED U. Voici {artist}, avec {title}.",
        f"À suivre sur Radio TED U : {title}, de {artist}.",
        f"{artist} sur Radio TED U. Voici {title}.",
    )
    index = (
        0
        if variant_key is None
        else _weighted_variant_index(f"fr:{variant_key}", (34, 27, 22, 17))
    )
    base = variants[index]
    return f"{base} {fact.text_fr}" if fact else base


def load_verified_facts(path: Path) -> list[VerifiedFact]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported verified fact schema")
    return [VerifiedFact.from_dict(item) for item in payload.get("facts", [])]


@dataclass(frozen=True, slots=True)
class ReadyAnnouncement:
    path: Path
    duration_seconds: float
    text: str
    fact_source_url: str | None


class TrackAnnouncementAssetLibrary:
    """Read only complete, validated Qwen assets; otherwise return no result."""

    def __init__(self, root: Path, station_id: str) -> None:
        self.root = root.resolve()
        self.station_id = station_id
        self.manifest_path = self.root / f"{station_id}.json"
        self._manifest_mtime_ns = -1
        self._ready: dict[int, ReadyAnnouncement] = {}
        self.queued_count = 0

    @property
    def ready_count(self) -> int:
        self._reload_if_changed()
        return len(self._ready)

    def ready_track_ids(self) -> frozenset[int]:
        self._reload_if_changed()
        return frozenset(self._ready)

    def resolve(self, track_id: int | None) -> ReadyAnnouncement | None:
        self._reload_if_changed()
        return self._ready.get(int(track_id)) if track_id is not None else None

    def _reload_if_changed(self) -> None:
        try:
            mtime_ns = self.manifest_path.stat().st_mtime_ns
        except OSError:
            self._ready = {}
            self.queued_count = 0
            self._manifest_mtime_ns = -1
            return
        if mtime_ns == self._manifest_mtime_ns:
            return
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if (
            payload.get("schema_version") != 1
            or payload.get("station_id") != self.station_id
            or payload.get("live_web_requests") is not False
        ):
            raise ValueError("invalid track announcement manifest")
        ready: dict[int, ReadyAnnouncement] = {}
        queued = 0
        for entry in payload.get("entries", []):
            if entry.get("state") != "ready":
                queued += 1
                continue
            text = " ".join(str(entry.get("text") or "").split())
            expected_key = cache_key(self.station_id, text)
            if entry.get("cache_key") != expected_key:
                continue
            candidate = (self.root / str(entry.get("asset_path") or "")).resolve()
            try:
                candidate.relative_to(self.root)
            except ValueError:
                continue
            if candidate.name != f"{expected_key}.wav" or not candidate.is_file():
                continue
            try:
                with wave.open(str(candidate), "rb") as wav:
                    duration = wav.getnframes() / float(wav.getframerate())
                    channels = wav.getnchannels()
            except (OSError, EOFError, wave.Error, ZeroDivisionError):
                continue
            if channels != 1 or not 0.25 <= duration <= 30:
                continue
            fact = entry.get("fact")
            ready[int(entry["track_id"])] = ReadyAnnouncement(
                path=candidate,
                duration_seconds=duration,
                text=text,
                fact_source_url=(
                    str(fact.get("source_url"))
                    if isinstance(fact, dict) and fact.get("source_url")
                    else None
                ),
            )
        self._ready = ready
        self.queued_count = queued
        self._manifest_mtime_ns = mtime_ns
