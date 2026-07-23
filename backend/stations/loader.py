from __future__ import annotations

import json
import ntpath
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from .models import AudioProfile, PublicProfile, RuntimeProfile, StationProfile

STATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
LANGUAGE_LOCALES = {"en": "en-US", "fr": "fr-FR"}
FROZEN_IDENTITIES: dict[str, dict[str, Any]] = {
    "radiotedu-en": {
        "display_name": "RadioTEDU",
        "language": "en",
        "locale": "en-US",
        "timezone": "Europe/Istanbul",
        "public.route": "/ai",
        "public.compatibility_routes": (),
        "public.snapshot_endpoint": "/v1/radio/stations/radiotedu-en/snapshot",
        "public.status_endpoint": "/v1/radio/stations/radiotedu-en/status",
        "public.stream_url": "https://stream.radiotedu.com/en",
        "audio.stream_mount": "/en",
        "audio.source_host": "10.98.98.75",
        "audio.source_port": 11154,
        "audio.source_user": "source",
        "audio.encoder_profile": "aac_192",
        "audio.public_listing": True,
        "audio.loudness_lufs": -16,
        "audio.true_peak_dbtp": -1,
        "audio.minimum_qwen_buffer": 5,
        "runtime.music_root": "media/stations/radiotedu-en/music",
        "voice_pack": "radiotedu-en-voices-v1",
        "snapshot_secret_ref": "RADIOTEDU_EN_SNAPSHOT_SECRET",
    },
    "radiotedu-fr": {
        "display_name": "RadioTEDU Français",
        "language": "fr",
        "locale": "fr-FR",
        "timezone": "Europe/Istanbul",
        "public.route": "/ai",
        "public.compatibility_routes": (),
        "public.snapshot_endpoint": "/v1/radio/stations/radiotedu-fr/snapshot",
        "public.status_endpoint": "/v1/radio/stations/radiotedu-fr/status",
        "public.stream_url": "https://stream.radiotedu.com/fr",
        "audio.stream_mount": "/fr",
        "audio.source_host": "10.98.98.75",
        "audio.source_port": 11154,
        "audio.source_user": "source",
        "audio.encoder_profile": "aac_192",
        "audio.public_listing": True,
        "audio.loudness_lufs": -16,
        "audio.true_peak_dbtp": -1,
        "audio.minimum_qwen_buffer": 5,
        "runtime.music_root": "media/stations/radiotedu-fr/music",
        "voice_pack": "radiotedu-fr-voices-v1",
        "snapshot_secret_ref": "RADIOTEDU_FR_SNAPSHOT_SECRET",
    },
}
WRITABLE_PATH_FIELDS = ("database", "data_root", "music_root", "announcement_root", "cache_root", "log_root")


class StationProfileError(ValueError):
    pass


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StationProfileError(f"{label} must be an object")
    return value


def _keys(value: dict[str, Any], required: set[str], label: str) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required)
    if missing:
        raise StationProfileError(f"{label} missing keys: {', '.join(missing)}")
    if unknown:
        raise StationProfileError(f"{label} unknown keys: {', '.join(unknown)}")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise StationProfileError(f"{label} must be a string")
    return value


def _integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise StationProfileError(f"{label} must be an integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise StationProfileError(f"{label} must be a boolean")
    return value


def _lexical_path(value: str) -> PureWindowsPath:
    normalized = ntpath.normcase(ntpath.normpath(value.replace("/", "\\")))
    return PureWindowsPath(normalized)


def _strict_descendant(child: str, parent: str) -> bool:
    return _lexical_path(parent) in _lexical_path(child).parents


def _paths_overlap(first: str, second: str) -> bool:
    first_path = _lexical_path(first)
    second_path = _lexical_path(second)
    return first_path == second_path or first_path in second_path.parents or second_path in first_path.parents


def _validate(profile: StationProfile) -> None:
    if profile.profile_version != 1:
        raise StationProfileError("profile_version must equal 1")
    if not STATION_ID.fullmatch(profile.station_id):
        raise StationProfileError("invalid station_id")
    expected = FROZEN_IDENTITIES.get(profile.station_id)
    if expected is None:
        raise StationProfileError(f"unsupported station_id: {profile.station_id}")
    if LANGUAGE_LOCALES.get(profile.language) != profile.locale:
        raise StationProfileError("language and locale do not agree")
    if profile.timezone != "Europe/Istanbul":
        raise StationProfileError("timezone must be Europe/Istanbul")
    if profile.audio.minimum_qwen_buffer < 5:
        raise StationProfileError("minimum_qwen_buffer must be at least 5")
    if (profile.audio.loudness_lufs, profile.audio.true_peak_dbtp) != (-16, -1):
        raise StationProfileError("audio targets must be -16 LUFS and -1 dBTP")
    if profile.audio.source_port < 1 or profile.audio.source_port > 65535:
        raise StationProfileError("audio.source_port must be a valid TCP port")
    if not profile.public.route.startswith("/") or not profile.audio.stream_mount.startswith("/"):
        raise StationProfileError("public route and stream mount must be absolute URL paths")
    for name in ("database", "announcement_root", "cache_root", "log_root"):
        if not _strict_descendant(getattr(profile.runtime, name), profile.runtime.data_root):
            raise StationProfileError(f"runtime.{name} must stay strictly beneath runtime.data_root")
    actual = {
        "display_name": profile.display_name,
        "language": profile.language,
        "locale": profile.locale,
        "timezone": profile.timezone,
        "public.route": profile.public.route,
        "public.compatibility_routes": profile.public.compatibility_routes,
        "public.snapshot_endpoint": profile.public.snapshot_endpoint,
        "public.status_endpoint": profile.public.status_endpoint,
        "public.stream_url": profile.public.stream_url,
        "audio.stream_mount": profile.audio.stream_mount,
        "audio.source_host": profile.audio.source_host,
        "audio.source_port": profile.audio.source_port,
        "audio.source_user": profile.audio.source_user,
        "audio.encoder_profile": profile.audio.encoder_profile,
        "audio.public_listing": profile.audio.public_listing,
        "audio.loudness_lufs": profile.audio.loudness_lufs,
        "audio.true_peak_dbtp": profile.audio.true_peak_dbtp,
        "audio.minimum_qwen_buffer": profile.audio.minimum_qwen_buffer,
        "runtime.music_root": profile.runtime.music_root,
        "voice_pack": profile.voice_pack,
        "snapshot_secret_ref": profile.snapshot_secret_ref,
    }
    for field, expected_value in expected.items():
        if actual[field] != expected_value:
            raise StationProfileError(f"{profile.station_id} frozen identity mismatch: {field}")


def load_station_profile(path: str | Path) -> StationProfile:
    source = Path(path)
    try:
        raw = _mapping(json.loads(source.read_text(encoding="utf-8")), "profile")
    except (OSError, json.JSONDecodeError) as exc:
        raise StationProfileError(f"cannot load {source}: {exc}") from exc
    root_keys = {
        "profile_version",
        "station_id",
        "display_name",
        "language",
        "locale",
        "timezone",
        "public",
        "audio",
        "runtime",
        "voice_pack",
        "snapshot_secret_ref",
    }
    public_keys = {"route", "compatibility_routes", "snapshot_endpoint", "status_endpoint", "stream_url"}
    audio_keys = {
        "stream_mount",
        "loudness_lufs",
        "true_peak_dbtp",
        "minimum_qwen_buffer",
        "source_host",
        "source_port",
        "source_user",
        "encoder_profile",
        "public_listing",
    }
    runtime_keys = {"data_root", "database", "music_root", "announcement_root", "cache_root", "log_root"}
    _keys(raw, root_keys, "profile")
    public = _mapping(raw["public"], "public")
    audio = _mapping(raw["audio"], "audio")
    runtime = _mapping(raw["runtime"], "runtime")
    _keys(public, public_keys, "public")
    _keys(audio, audio_keys, "audio")
    _keys(runtime, runtime_keys, "runtime")
    if not isinstance(public["compatibility_routes"], list) or not all(
        isinstance(item, str) for item in public["compatibility_routes"]
    ):
        raise StationProfileError("public.compatibility_routes must be a string array")
    try:
        profile = StationProfile(
            profile_version=_integer(raw["profile_version"], "profile.profile_version"),
            station_id=_string(raw["station_id"], "profile.station_id"),
            display_name=_string(raw["display_name"], "profile.display_name"),
            language=_string(raw["language"], "profile.language"),
            locale=_string(raw["locale"], "profile.locale"),
            timezone=_string(raw["timezone"], "profile.timezone"),
            public=PublicProfile(
                _string(public["route"], "public.route"),
                tuple(public["compatibility_routes"]),
                _string(public["snapshot_endpoint"], "public.snapshot_endpoint"),
                _string(public["status_endpoint"], "public.status_endpoint"),
                _string(public["stream_url"], "public.stream_url"),
            ),
            audio=AudioProfile(
                _string(audio["stream_mount"], "audio.stream_mount"),
                _integer(audio["loudness_lufs"], "audio.loudness_lufs"),
                _integer(audio["true_peak_dbtp"], "audio.true_peak_dbtp"),
                _integer(audio["minimum_qwen_buffer"], "audio.minimum_qwen_buffer"),
                source_host=_string(audio["source_host"], "audio.source_host"),
                source_port=_integer(audio["source_port"], "audio.source_port"),
                source_user=_string(audio["source_user"], "audio.source_user"),
                encoder_profile=_string(audio["encoder_profile"], "audio.encoder_profile"),
                public_listing=_boolean(audio["public_listing"], "audio.public_listing"),
            ),
            runtime=RuntimeProfile(
                *(_string(runtime[name], f"runtime.{name}") for name in (
                    "data_root",
                    "database",
                    "music_root",
                    "announcement_root",
                    "cache_root",
                    "log_root",
                ))
            ),
            voice_pack=_string(raw["voice_pack"], "profile.voice_pack"),
            snapshot_secret_ref=_string(raw["snapshot_secret_ref"], "profile.snapshot_secret_ref"),
        )
    except (TypeError, ValueError) as exc:
        raise StationProfileError(f"invalid profile value: {exc}") from exc
    _validate(profile)
    return profile


def _validate_writable_isolation(profiles: list[StationProfile]) -> None:
    for index, profile in enumerate(profiles):
        for other in profiles[index + 1 :]:
            for field in WRITABLE_PATH_FIELDS:
                value = getattr(profile.runtime, field)
                for other_field in WRITABLE_PATH_FIELDS:
                    other_value = getattr(other.runtime, other_field)
                    if _paths_overlap(value, other_value):
                        raise StationProfileError(
                            "writable path overlap: "
                            f"{profile.station_id}.runtime.{field} and "
                            f"{other.station_id}.runtime.{other_field}"
                        )


def load_station_profiles(directory: str | Path) -> dict[str, StationProfile]:
    profiles = [load_station_profile(path) for path in sorted(Path(directory).glob("*.json"))]
    result = {profile.station_id: profile for profile in profiles}
    if len(result) != len(profiles):
        raise StationProfileError("duplicate station_id")
    if set(result) != set(FROZEN_IDENTITIES):
        expected = ", ".join(sorted(FROZEN_IDENTITIES))
        raise StationProfileError(f"profile set must contain exactly: {expected}")
    _validate_writable_isolation(profiles)
    unique_groups = {
        "snapshot_endpoint": [profile.public.snapshot_endpoint for profile in profiles],
        "status_endpoint": [profile.public.status_endpoint for profile in profiles],
        "stream_mount": [profile.audio.stream_mount for profile in profiles],
        "database": [str(_lexical_path(profile.runtime.database)) for profile in profiles],
        "data_root": [str(_lexical_path(profile.runtime.data_root)) for profile in profiles],
        "music_root": [str(_lexical_path(profile.runtime.music_root)) for profile in profiles],
        "announcement_root": [str(_lexical_path(profile.runtime.announcement_root)) for profile in profiles],
        "cache_root": [str(_lexical_path(profile.runtime.cache_root)) for profile in profiles],
        "log_root": [str(_lexical_path(profile.runtime.log_root)) for profile in profiles],
        "secret_ref": [profile.snapshot_secret_ref for profile in profiles],
    }
    for label, values in unique_groups.items():
        if len(values) != len(set(values)):
            raise StationProfileError(f"duplicate {label}")
    for profile in profiles:
        inspected = " ".join(
            (
                profile.public.snapshot_endpoint,
                profile.public.status_endpoint,
                profile.audio.stream_mount,
                profile.runtime.data_root,
                profile.runtime.database,
                profile.runtime.music_root,
                profile.runtime.announcement_root,
                profile.runtime.cache_root,
                profile.runtime.log_root,
                profile.snapshot_secret_ref,
            )
        )
        for other_id in result:
            if other_id != profile.station_id and other_id in inspected:
                raise StationProfileError(f"{profile.station_id} references {other_id}")
    return result
