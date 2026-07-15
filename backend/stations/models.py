from __future__ import annotations

from dataclasses import dataclass, field

from ..audio.processing import ProcessingProfile


@dataclass(frozen=True, slots=True)
class PublicProfile:
    route: str
    compatibility_routes: tuple[str, ...]
    snapshot_endpoint: str
    status_endpoint: str
    stream_url: str


@dataclass(frozen=True, slots=True)
class AudioProfile:
    stream_mount: str
    loudness_lufs: int
    true_peak_dbtp: int
    minimum_qwen_buffer: int
    source_host: str = "10.98.98.75"
    source_port: int = 11154
    source_user: str = "source"
    encoder_profile: str = "aac_192"
    public_listing: bool = True
    processing: ProcessingProfile = field(default_factory=ProcessingProfile)


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    data_root: str
    database: str
    music_root: str
    announcement_root: str
    cache_root: str
    log_root: str


@dataclass(frozen=True, slots=True)
class StationProfile:
    profile_version: int
    station_id: str
    display_name: str
    language: str
    locale: str
    timezone: str
    public: PublicProfile
    audio: AudioProfile
    runtime: RuntimeProfile
    voice_pack: str
    snapshot_secret_ref: str
