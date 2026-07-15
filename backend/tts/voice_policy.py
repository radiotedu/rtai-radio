from __future__ import annotations

import json
import re
from pathlib import Path

from backend.stations.context import StationContext

from .contracts import AnnouncementLabel, VoiceSelection


def normalize_broadcast_text(text: str, language: str, locale: str) -> str:
    normalized = " ".join(text.strip().split())
    if not normalized:
        raise ValueError("broadcast text cannot be blank")
    if (language, locale) not in {("en", "en-US"), ("fr", "fr-FR")}:
        raise ValueError(f"unsupported station language/locale pair: {language}/{locale}")
    normalized = re.sub(r"\bRadioTEDU\b", "Radio TED U", normalized, flags=re.IGNORECASE)
    return normalized[:800]


class VoicePolicy:
    def __init__(self, context: StationContext, pack: dict) -> None:
        self.context = context
        self.pack = pack
        profile = context.profile
        if pack.get("pack_id") != profile.voice_pack:
            raise ValueError("voice pack ID does not match station profile")
        if (pack.get("station_id"), pack.get("language"), pack.get("locale")) != (
            profile.station_id,
            profile.language,
            profile.locale,
        ):
            raise ValueError("voice pack station, language, or locale mismatch")
        hosts = pack.get("hosts") or []
        if len(hosts) != 4:
            raise ValueError("a frozen station voice pack must contain four hosts")
        for host in hosts:
            host_id = str(host.get("host_id") or "unknown")
            for field in ("clone_prompt_path", "reference_audio_path", "reference_transcript"):
                value = host.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"host {host_id} missing {field}")
        self.hosts = {daypart: host for host in hosts for daypart in host["dayparts"]}

    @classmethod
    def from_context(
        cls,
        context: StationContext,
        voice_config_root: Path = Path("config/voices"),
    ) -> "VoicePolicy":
        path = voice_config_root / f"{context.profile.voice_pack}.json"
        return cls(context, json.loads(path.read_text(encoding="utf-8")))

    def select(
        self,
        *,
        program_id: str,
        daypart: str,
        announcement_label: AnnouncementLabel,
        text: str,
    ) -> tuple[str, VoiceSelection]:
        del program_id
        host = self.hosts.get(daypart)
        if host is None:
            raise ValueError(f"voice pack has no host for daypart: {daypart}")
        style_id = host["styles"].get(announcement_label)
        if not style_id or not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", style_id):
            raise ValueError(f"voice pack has no locked style for {announcement_label}")
        normalized = normalize_broadcast_text(
            text,
            self.context.profile.language,
            self.context.profile.locale,
        )
        return normalized, VoiceSelection(
            station_id=self.context.profile.station_id,
            language=self.context.profile.language,
            locale=self.context.profile.locale,
            voice_pack=self.context.profile.voice_pack,
            host_id=host["host_id"],
            style_id=style_id,
            clone_prompt_path=host["clone_prompt_path"],
            reference_audio_path=host["reference_audio_path"],
            reference_transcript=host["reference_transcript"],
            model_checksum=self.pack["model_checksum"],
        )
