from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ..config import Settings, ensure_runtime_dirs
from .models import AudioProfile, PublicProfile, RuntimeProfile, StationProfile


@dataclass(frozen=True, slots=True)
class StationContext:
    settings: Settings
    profile: StationProfile

    @property
    def data_root(self) -> Path:
        return self.settings.path(self.profile.runtime.data_root).resolve()

    @property
    def database_file(self) -> Path:
        return self.settings.database_file.resolve()

    @property
    def music_root(self) -> Path:
        return self.settings.music_path.resolve()

    @property
    def announcement_root(self) -> Path:
        return self.settings.path(self.profile.runtime.announcement_root).resolve()

    @property
    def cache_root(self) -> Path:
        return self.settings.path(self.profile.runtime.cache_root).resolve()

    @property
    def log_root(self) -> Path:
        return self.settings.path(self.profile.runtime.log_root).resolve()


@dataclass(frozen=True, slots=True)
class _EnglishCompatibilityContext(StationContext):
    pass


def build_station_context(settings: Settings, profile: StationProfile) -> StationContext:
    data_root = Path(profile.runtime.data_root)
    scoped = replace(
        settings,
        database_path=profile.runtime.database,
        music_dir=profile.runtime.music_root,
        static_dir=str(data_root / "public"),
        rss_feeds_path=str(data_root / "rss-feeds.json"),
        liquidsoap_queue_path=str(data_root / "liquidsoap" / "queue.m3u"),
        liquidsoap_script_path=str(data_root / "liquidsoap" / f"{profile.station_id}.liq"),
        liquidsoap_host=profile.audio.source_host,
        liquidsoap_port=profile.audio.source_port,
        liquidsoap_mount=profile.audio.stream_mount,
        liquidsoap_icecast_user=profile.audio.source_user,
        liquidsoap_encoder_profile=profile.audio.encoder_profile,
        liquidsoap_public=profile.audio.public_listing,
        public_dashboard_route=profile.public.route,
        public_stream_url=profile.public.stream_url,
        min_ready_announcements=profile.audio.minimum_qwen_buffer,
        max_ready_announcements=max(
            settings.max_ready_announcements,
            profile.audio.minimum_qwen_buffer,
        ),
        station_id=profile.station_id,
    )
    return StationContext(scoped, profile)


def _english_compatibility_profile(settings: Settings) -> StationProfile:
    return StationProfile(
        1,
        "radiotedu-en",
        "RadioTEDU",
        "en",
        "en-US",
        "Europe/Istanbul",
        PublicProfile(
            "/ai",
            (),
            "/v1/radio/stations/radiotedu-en/snapshot",
            "/v1/radio/stations/radiotedu-en/status",
            settings.public_stream_url or "https://stream.radiotedu.com/en",
        ),
        AudioProfile(
            "/en",
            -16,
            -1,
            max(5, settings.min_ready_announcements),
        ),
        RuntimeProfile(
            str(settings.database_file.parent),
            settings.database_path,
            settings.music_dir,
            str(settings.tts_path),
            str(settings.tts_path / "qwen-cache"),
            str(settings.database_file.parent / "logs"),
        ),
        "radiotedu-en-voices-v1",
        "RADIOTEDU_EN_SNAPSHOT_SECRET",
    )


def english_compatibility_context(settings: Settings) -> StationContext:
    scoped = replace(settings, station_id="radiotedu-en")
    return _EnglishCompatibilityContext(scoped, _english_compatibility_profile(scoped))


def coerce_station_context(value: Settings | StationContext) -> StationContext:
    return value if isinstance(value, StationContext) else english_compatibility_context(value)


def _strict_descendant(path: Path, parent: Path) -> bool:
    return path != parent and parent in path.parents


def _resolved_runtime_directories(context: StationContext) -> dict[str, Path]:
    settings = context.settings
    return {
        "database": settings.database_file.resolve().parent,
        "music": settings.music_path.resolve(),
        "static": settings.static_path.resolve(),
        "covers": settings.covers_path.resolve(),
        "tts": settings.tts_path.resolve(),
        "clips": settings.clips_path.resolve(),
        "rss": settings.path(settings.rss_feeds_path).resolve().parent,
        "liquidsoap queue": settings.path(settings.liquidsoap_queue_path).resolve().parent,
        "liquidsoap script": settings.path(settings.liquidsoap_script_path).resolve().parent,
        "data_root": context.data_root,
        "announcement": context.announcement_root,
        "cache": context.cache_root,
        "log": context.log_root,
    }


def _validate_legacy_runtime_paths(
    context: StationContext,
    deployment_root: Path,
    targets: dict[str, Path],
) -> None:
    del context
    for label, target in targets.items():
        if not _strict_descendant(target, deployment_root):
            raise ValueError(f"{label} runtime path must resolve within deployment root")


def _validate_canonical_runtime_paths(
    context: StationContext,
    deployment_root: Path,
    targets: dict[str, Path],
) -> None:
    data_root = targets["data_root"]
    if not _strict_descendant(data_root, deployment_root):
        raise ValueError("data_root must resolve strictly within deployment root")

    database_file = context.database_file
    if not _strict_descendant(database_file, data_root):
        raise ValueError("database runtime path must resolve strictly within data_root")

    expected_music_root = (
        deployment_root / "media" / "stations" / context.profile.station_id / "music"
    ).resolve()
    if not _strict_descendant(expected_music_root, deployment_root):
        raise ValueError("music runtime path must resolve within deployment root")
    if context.music_root != expected_music_root or targets["music"] != expected_music_root:
        raise ValueError("music runtime path must match the station-specific media root")

    for label in ("announcement", "cache", "log"):
        if not _strict_descendant(targets[label], data_root):
            raise ValueError(f"{label} runtime path must resolve strictly within data_root")

    for label in (
        "static",
        "covers",
        "tts",
        "clips",
        "rss",
        "liquidsoap queue",
        "liquidsoap script",
    ):
        target = targets[label]
        if target != data_root and not _strict_descendant(target, data_root):
            raise ValueError(f"{label} runtime path must resolve within data_root")


def _validate_station_runtime_paths(context: StationContext) -> None:
    deployment_root = Path.cwd().resolve()
    targets = _resolved_runtime_directories(context)
    if isinstance(context, _EnglishCompatibilityContext):
        _validate_legacy_runtime_paths(context, deployment_root, targets)
    else:
        _validate_canonical_runtime_paths(context, deployment_root, targets)


def ensure_station_runtime_dirs(context: StationContext) -> None:
    _validate_station_runtime_paths(context)
    ensure_runtime_dirs(context.settings)
    for path in (
        context.data_root,
        context.announcement_root,
        context.cache_root,
        context.log_root,
    ):
        path.mkdir(parents=True, exist_ok=True)
