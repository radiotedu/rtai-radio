from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path


def _env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _get(name: str, default: str, env_file: dict[str, str]) -> str:
    return os.environ.get(name, env_file.get(name, default))


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


ALLOWED_STATION_IDS = frozenset({"radiotedu-en", "radiotedu-fr"})


@dataclass
class Settings:
    station_id: str = "radiotedu-en"
    station_profiles_dir: str = "config/stations"
    database_path: str = "data/radiotedu.db"
    music_dir: str = "data/music"
    static_dir: str = "backend/static"
    llm_provider: str = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:0.5b-instruct"
    ollama_timeout_seconds: int = 30
    tts_provider: str = "qwen"
    qwen_tts_command: str = ""
    piper_tts_command: str = ""
    fallback_tts_provider: str = "dummy"
    search_provider: str = "rss"
    searxng_url: str = "http://localhost:8080"
    rss_feeds_path: str = "data/rss_feeds.json"
    playback_backend: str = "simulate"
    mpv_path: str = "mpv"
    ffplay_path: str = "ffplay"
    web_search_interval_minutes: int = 30
    weather_enabled: bool = False
    weather_provider: str = "open_meteo"
    weather_location: str = "Ankara"
    weather_latitude: str = ""
    weather_longitude: str = ""
    weather_interval_minutes: int = 30
    song_repeat_hours: int = 6
    artist_repeat_minutes: int = 90
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    frontend_port: int = 5173
    autonomy_enabled: bool = False
    autonomy_tick_seconds: int = 30
    strategy_interval_minutes: int = 240
    min_ready_announcements: int = 5
    max_ready_announcements: int = 8
    rundown_planned_seconds: int = 14_400
    rundown_rendered_seconds: int = 3_600
    rundown_refill_seconds: int = 7_200
    fallback_coverage_seconds: int = 21_600
    jingle_enabled: bool = False
    jingle_interval_tracks: int = 3
    imaging_release_root: str = "."
    liquidsoap_enabled: bool = False
    liquidsoap_queue_path: str = "data/liquidsoap/queue.m3u"
    liquidsoap_script_path: str = "data/liquidsoap/radiotedu.liq"
    liquidsoap_command: str = "liquidsoap"
    liquidsoap_host: str = "127.0.0.1"
    liquidsoap_port: int = 8001
    liquidsoap_mount: str = "/ai"
    liquidsoap_icecast_user: str = "source"
    liquidsoap_icecast_password: str = ""
    liquidsoap_encoder_profile: str = "aac_192"
    liquidsoap_public: bool = True
    public_dashboard_enabled: bool = False
    public_dashboard_route: str = "/ai"
    public_stream_url: str = ""
    public_sync_url: str = ""
    public_sync_token: str = ""
    public_sync_interval_seconds: int = 10
    snapshot_ttl_seconds: int = 30
    platform_agent_id: str = "school-radio-pc"
    platform_agent_scope: str = "agent:playout"
    platform_hmac_secret_en: str = ""
    platform_hmac_secret_fr: str = ""
    platform_snapshot_max_bytes: int = 262144
    platform_timestamp_skew_seconds: int = 60
    public_compatibility_enabled: bool = True
    admin_api_token: str = ""
    news_enabled: bool = False
    news_interval_minutes: int = 60
    news_max_age_hours: int = 24

    def __post_init__(self) -> None:
        if self.station_id not in ALLOWED_STATION_IDS:
            raise ValueError("station_id must be radiotedu-en or radiotedu-fr")
        if self.liquidsoap_encoder_profile != "aac_192":
            raise ValueError("liquidsoap_encoder_profile must be aac_192")
        if min(
            self.rundown_planned_seconds,
            self.rundown_rendered_seconds,
            self.rundown_refill_seconds,
            self.fallback_coverage_seconds,
        ) <= 0:
            raise ValueError("rundown and fallback coverage seconds must be positive")
        if self.rundown_rendered_seconds > self.rundown_planned_seconds:
            raise ValueError("rendered rundown coverage cannot exceed planned coverage")
        if self.rundown_refill_seconds > self.rundown_planned_seconds:
            raise ValueError("rundown refill threshold cannot exceed planned coverage")
        if self.jingle_interval_tracks <= 0:
            raise ValueError("jingle_interval_tracks must be positive")

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "Settings":
        env_file = _env_file_values(Path(env_path))
        values: dict[str, object] = {}
        key_map = {
            "station_id": "STATION_ID",
            "station_profiles_dir": "STATION_PROFILES_DIR",
            "database_path": "DATABASE_PATH",
            "music_dir": "MUSIC_DIR",
            "static_dir": "STATIC_DIR",
            "llm_provider": "LLM_PROVIDER",
            "ollama_url": "OLLAMA_URL",
            "ollama_model": "OLLAMA_MODEL",
            "ollama_timeout_seconds": "OLLAMA_TIMEOUT_SECONDS",
            "tts_provider": "TTS_PROVIDER",
            "qwen_tts_command": "QWEN_TTS_COMMAND",
            "piper_tts_command": "PIPER_TTS_COMMAND",
            "fallback_tts_provider": "FALLBACK_TTS_PROVIDER",
            "search_provider": "SEARCH_PROVIDER",
            "searxng_url": "SEARXNG_URL",
            "rss_feeds_path": "RSS_FEEDS_PATH",
            "playback_backend": "PLAYBACK_BACKEND",
            "mpv_path": "MPV_PATH",
            "ffplay_path": "FFPLAY_PATH",
            "web_search_interval_minutes": "WEB_SEARCH_INTERVAL_MINUTES",
            "weather_enabled": "WEATHER_ENABLED",
            "weather_provider": "WEATHER_PROVIDER",
            "weather_location": "WEATHER_LOCATION",
            "weather_latitude": "WEATHER_LATITUDE",
            "weather_longitude": "WEATHER_LONGITUDE",
            "weather_interval_minutes": "WEATHER_INTERVAL_MINUTES",
            "song_repeat_hours": "SONG_REPEAT_HOURS",
            "artist_repeat_minutes": "ARTIST_REPEAT_MINUTES",
            "api_host": "API_HOST",
            "api_port": "API_PORT",
            "frontend_port": "FRONTEND_PORT",
            "autonomy_enabled": "AUTONOMY_ENABLED",
            "autonomy_tick_seconds": "AUTONOMY_TICK_SECONDS",
            "strategy_interval_minutes": "STRATEGY_INTERVAL_MINUTES",
            "min_ready_announcements": "MIN_READY_ANNOUNCEMENTS",
            "max_ready_announcements": "MAX_READY_ANNOUNCEMENTS",
            "rundown_planned_seconds": "RUNDOWN_PLANNED_SECONDS",
            "rundown_rendered_seconds": "RUNDOWN_RENDERED_SECONDS",
            "rundown_refill_seconds": "RUNDOWN_REFILL_SECONDS",
            "fallback_coverage_seconds": "FALLBACK_COVERAGE_SECONDS",
            "jingle_enabled": "JINGLE_ENABLED",
            "jingle_interval_tracks": "JINGLE_INTERVAL_TRACKS",
            "imaging_release_root": "IMAGING_RELEASE_ROOT",
            "liquidsoap_enabled": "LIQUIDSOAP_ENABLED",
            "liquidsoap_queue_path": "LIQUIDSOAP_QUEUE_PATH",
            "liquidsoap_script_path": "LIQUIDSOAP_SCRIPT_PATH",
            "liquidsoap_command": "LIQUIDSOAP_COMMAND",
            "liquidsoap_host": "LIQUIDSOAP_HOST",
            "liquidsoap_port": "LIQUIDSOAP_PORT",
            "liquidsoap_mount": "LIQUIDSOAP_MOUNT",
            "liquidsoap_icecast_user": "LIQUIDSOAP_ICECAST_USER",
            "liquidsoap_icecast_password": "LIQUIDSOAP_ICECAST_PASSWORD",
            "liquidsoap_encoder_profile": "LIQUIDSOAP_ENCODER_PROFILE",
            "liquidsoap_public": "LIQUIDSOAP_PUBLIC",
            "public_dashboard_enabled": "PUBLIC_DASHBOARD_ENABLED",
            "public_dashboard_route": "PUBLIC_DASHBOARD_ROUTE",
            "public_stream_url": "PUBLIC_STREAM_URL",
            "public_sync_url": "PUBLIC_SYNC_URL",
            "public_sync_token": "PUBLIC_SYNC_TOKEN",
            "public_sync_interval_seconds": "PUBLIC_SYNC_INTERVAL_SECONDS",
            "snapshot_ttl_seconds": "SNAPSHOT_TTL_SECONDS",
            "platform_agent_id": "RADIOTEDU_AGENT_ID",
            "platform_agent_scope": "RADIOTEDU_AGENT_SCOPE",
            "platform_hmac_secret_en": "RADIOTEDU_EN_SNAPSHOT_SECRET",
            "platform_hmac_secret_fr": "RADIOTEDU_FR_SNAPSHOT_SECRET",
            "platform_snapshot_max_bytes": "RADIOTEDU_SNAPSHOT_MAX_BYTES",
            "platform_timestamp_skew_seconds": "RADIOTEDU_TIMESTAMP_SKEW_SECONDS",
            "public_compatibility_enabled": "PUBLIC_COMPATIBILITY_ENABLED",
            "admin_api_token": "ADMIN_API_TOKEN",
            "news_enabled": "NEWS_ENABLED",
            "news_interval_minutes": "NEWS_INTERVAL_MINUTES",
            "news_max_age_hours": "NEWS_MAX_AGE_HOURS",
        }
        for field in fields(cls):
            env_name = key_map[field.name]
            raw = _get(env_name, str(field.default), env_file)
            if field.name == "liquidsoap_command":
                raw = os.environ.get("LIQUIDSOAP_PATH", env_file.get("LIQUIDSOAP_PATH", raw))
            elif field.name == "liquidsoap_script_path":
                raw = os.environ.get("LIQUIDSOAP_SCRIPT", env_file.get("LIQUIDSOAP_SCRIPT", raw))
            elif field.name == "liquidsoap_host":
                raw = os.environ.get("ICECAST_HOST", env_file.get("ICECAST_HOST", raw))
            elif field.name == "liquidsoap_port":
                raw = os.environ.get("ICECAST_PORT", env_file.get("ICECAST_PORT", raw))
            elif field.name == "liquidsoap_mount":
                raw = os.environ.get("ICECAST_MOUNT", env_file.get("ICECAST_MOUNT", raw))
            elif field.name == "liquidsoap_icecast_user":
                raw = os.environ.get("ICECAST_SOURCE_USERNAME", env_file.get("ICECAST_SOURCE_USERNAME", raw))
            elif field.name == "liquidsoap_icecast_password":
                raw = os.environ.get("ICECAST_PASSWORD", env_file.get("ICECAST_PASSWORD", raw))
            elif field.name == "liquidsoap_encoder_profile":
                raw = os.environ.get("ICECAST_ENCODER_PROFILE", env_file.get("ICECAST_ENCODER_PROFILE", raw))
            elif field.name == "liquidsoap_public":
                raw = os.environ.get("ICECAST_PUBLIC", env_file.get("ICECAST_PUBLIC", raw))
            if field.type in (int, "int"):
                values[field.name] = int(raw)
            elif field.type in (bool, "bool"):
                values[field.name] = _as_bool(raw)
            else:
                values[field.name] = raw
        return cls(**values)

    def path(self, value: str) -> Path:
        return Path(value).expanduser()

    @property
    def station_profiles_path(self) -> Path:
        return self.path(self.station_profiles_dir)

    @property
    def database_file(self) -> Path:
        return self.path(self.database_path)

    @property
    def music_path(self) -> Path:
        return self.path(self.music_dir)

    @property
    def static_path(self) -> Path:
        return self.path(self.static_dir)

    @property
    def covers_path(self) -> Path:
        return self.static_path / "generated" / "covers"

    @property
    def tts_path(self) -> Path:
        return self.static_path / "generated" / "tts"

    @property
    def clips_path(self) -> Path:
        return self.static_path / "generated" / "clips"


def ensure_runtime_dirs(settings: Settings) -> None:
    for path in (
        settings.database_file.parent,
        settings.music_path,
        settings.covers_path,
        settings.tts_path,
        settings.clips_path,
        Path(settings.rss_feeds_path).parent,
        Path(settings.liquidsoap_queue_path).parent,
        Path(settings.liquidsoap_script_path).parent,
    ):
        path.mkdir(parents=True, exist_ok=True)
