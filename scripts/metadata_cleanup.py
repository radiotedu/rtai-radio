"""Conservative cleanup of download labels; retain recording/version names."""

from __future__ import annotations

import html
import re

_VIDEO_LABEL = re.compile(
    r"\b(?:official\s+(?:(?:music|lyric(?:s)?)\s+)?(?:video|audio|visuali[sz]er)"
    r"|(?:music|lyric(?:s)?)\s+video|lyric(?:s)?(?:\s*/\s*letra)?"
    r"|official|audio|video|visuali[sz]er|video\s+clip|upgrade|letra(?:s)?|paroles|vr"
    r"|\d{3,4}p|4k|8k|hd|hq|uhd)\b",
    re.IGNORECASE,
)
_VIDEO_SUFFIX = re.compile(
    r"\s*(?:[-–—|:]\s*)?(?:official\s+(?:(?:music|lyric(?:s)?)\s+)?"
    r"(?:video|audio|visuali[sz]er)(?:\s+(?:hd|hq|uhd|4k|8k|\d{3,4}p))*"
    r"|(?:music|lyric(?:s)?)\s+video)\s*$",
    re.IGNORECASE,
)


def clean_catalog_name(value: str, *, artist: bool = False) -> str:
    """Remove explicit video labels, not meaningful acoustic/live/remix labels."""
    original = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()

    def bracket(match: re.Match[str]) -> str:
        square = match.group(1) is not None
        label = match.group(1) if square else match.group(2)
        label = label.strip()
        if square and re.fullmatch(r"[A-Za-z0-9_-]{11}", label):
            return " "
        cleaned = _VIDEO_LABEL.sub(" ", label)
        if _VIDEO_LABEL.search(label):
            cleaned = re.sub(r"\b360\b|°", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|,:/")
        if not cleaned:
            return " "
        return ("[" + cleaned + "]") if square else ("(" + cleaned + ")")

    text = original
    for _ in range(3):
        updated = re.sub(r"\[([^\[\]]*)\]|\(([^()]*)\)", bracket, text)
        updated = _VIDEO_SUFFIX.sub("", updated)
        if updated == text:
            break
        text = updated
    if artist:
        text = re.sub(r"\s*(?:[-–—|]\s*Topic|\s+VEVO)\s*$", "", text, flags=re.I)
    text = re.sub(
        r"(\b(?:ft\.?|feat\.?|featuring)\s+[^()]+?)\s+\1(?=\s*$|\))",
        r"\1", text, flags=re.I,
    )
    return re.sub(r"\s+", " ", text).strip(" -–—|_")


def clean_track_metadata(title: str, artist: str | None) -> tuple[str, str | None]:
    title = clean_catalog_name(title)
    artist = clean_catalog_name(artist or "", artist=True) or None
    if artist:
        prefix = re.compile(rf"^{re.escape(artist)}\s*[-–—]\s*", flags=re.I)
        while prefix.match(title):
            title = prefix.sub("", title, count=1)
    return title, artist
