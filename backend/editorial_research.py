from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from .editorial import research_allowed
from .search.base import SearchProvider, SearchResult


@dataclass(frozen=True, slots=True)
class FactCard:
    track_id: int
    language: str
    fact: str
    url: str
    source: str
    retrieved_at: str
    match_evidence: str


def clean_identity(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def sanitize_fact(value: object, *, max_words: int = 28) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    if not text or re.search(r"\blyrics?\b|\btranscript(?:ion)?\b", text, re.IGNORECASE):
        return ""
    return " ".join(text.split()[:max_words])


def _public_http_url(value: object) -> str:
    url = " ".join(str(value or "").split())
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def result_matches(result: SearchResult, title: str, artist: str) -> bool:
    haystack = clean_identity(f"{result.title} {result.snippet}")
    return bool(title and artist and title in haystack and artist in haystack)


class EditorialResearchService:
    def __init__(self, provider: SearchProvider) -> None:
        self.provider = provider

    def research(self, track: dict, language: str) -> FactCard | None:
        if not research_allowed(track.get("genre")):
            return None
        if language not in {"en", "fr"}:
            raise ValueError(f"unsupported editorial language: {language}")
        title = clean_identity(track.get("title"))
        artist = clean_identity(track.get("artist"))
        if not title or not artist:
            return None
        try:
            track_id = int(track["id"])
        except (KeyError, TypeError, ValueError):
            return None
        query = f'"{track.get("artist")}" "{track.get("title")}" music'
        for result in self.provider.search(query, limit=5):
            url = _public_http_url(result.url)
            source = " ".join(str(result.source or "").split())
            result_label = f"{result.title} {result.url}"
            if (
                not url
                or not source
                or re.search(r"\blyrics?\b|\btranscript(?:ion)?\b", result_label, re.IGNORECASE)
                or not result_matches(result, title, artist)
            ):
                continue
            fact = sanitize_fact(result.snippet)
            if fact:
                return FactCard(
                    track_id=track_id,
                    language=language,
                    fact=fact,
                    url=url,
                    source=source,
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    match_evidence=f"title+artist:{title}|{artist}",
                )
        return None

