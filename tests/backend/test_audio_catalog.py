from __future__ import annotations

import hashlib
import json
import sys
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.audio.catalog_analyzer import analyze_audio
from backend.audio.models import (
    AudioAssetMissingError,
    AudioValidationStatus,
    BROADCAST_AUDIO_POLICY,
    UnreadableAudioError,
    UnsupportedAudioFormatError,
)
from backend.config import Settings
from backend.database import connect
from backend.music_library import scan_music
from backend.programming.separation import PlayedTrack, SeparationPolicy, TrackCandidate
from backend.stations.context import StationContext, build_station_context
from backend.stations.models import (
    AudioProfile,
    PublicProfile,
    RuntimeProfile,
    StationProfile,
)


def _write_wave(path: Path) -> None:
    with wave.open(str(path), "wb") as audio_file:
        audio_file.setnchannels(1)
        audio_file.setsampwidth(2)
        audio_file.setframerate(8_000)
        audio_file.writeframes(b"\x00\x00" * 800)


def _write_ffprobe_fixture(
    path: Path, duration: str, *, codec: str = "pcm_s16le"
) -> tuple[str, str]:
    metadata = {
        "format": {"duration": duration},
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": codec,
                "sample_rate": "8000",
                "channels": 1,
            }
        ],
    }
    path.write_text(
        f"print({json.dumps(metadata)!r})\n",
        encoding="utf-8",
    )
    return (sys.executable, str(path))


def _station_context(
    station_id: str, station_root: Path, music_root: Path
) -> StationContext:
    data_root = station_root / "data"
    database = data_root / "radio.db"
    settings = Settings(
        station_id=station_id,
        database_path=str(database),
        music_dir=str(music_root),
    )
    profile = StationProfile(
        profile_version=1,
        station_id=station_id,
        display_name=station_id,
        language="en" if station_id.endswith("-en") else "fr",
        locale="en-US" if station_id.endswith("-en") else "fr-FR",
        timezone="Europe/Istanbul",
        public=PublicProfile(
            route=f"/ai/{station_id}",
            compatibility_routes=(),
            snapshot_endpoint=f"/api/{station_id}/snapshot",
            status_endpoint=f"/api/{station_id}/status",
            stream_url=f"https://example.test/{station_id}",
        ),
        audio=AudioProfile(
            stream_mount=f"/{station_id}",
            loudness_lufs=-16,
            true_peak_dbtp=-1,
            minimum_qwen_buffer=5,
        ),
        runtime=RuntimeProfile(
            data_root=str(data_root),
            database=str(database),
            music_root=str(music_root),
            announcement_root=str(data_root / "announcements"),
            cache_root=str(data_root / "cache"),
            log_root=str(data_root / "logs"),
        ),
        voice_pack=f"{station_id}-voices",
        snapshot_secret_ref=f"{station_id}-secret",
    )
    return build_station_context(settings, profile)


def _catalog_rows(context: StationContext) -> list[tuple[object, ...]]:
    with connect(context) as conn:
        rows = conn.execute(
            "select id, title, artist, duration_seconds, file_path "
            "from tracks order by file_path"
        ).fetchall()
    return [tuple(row) for row in rows]


def test_broadcast_audio_policy_freezes_professional_qualification_inputs() -> None:
    assert BROADCAST_AUDIO_POLICY.integrated_lufs_target == -16.0
    assert BROADCAST_AUDIO_POLICY.integrated_lufs_tolerance_lu == 1.0
    assert BROADCAST_AUDIO_POLICY.true_peak_ceiling_dbtp == -1.0
    assert BROADCAST_AUDIO_POLICY.silence_threshold_dbfs == -60.0
    assert BROADCAST_AUDIO_POLICY.silence_degraded_primary_seconds == 1.0
    assert BROADCAST_AUDIO_POLICY.silence_fallback_seconds == 1.5
    assert BROADCAST_AUDIO_POLICY.listener_visible_silence_limit_seconds == 2.0
    assert BROADCAST_AUDIO_POLICY.talk_over_minimum_intro_confidence == 0.65
    assert BROADCAST_AUDIO_POLICY.talk_over_minimum_instrumental_intro_seconds == 3.0
    assert BROADCAST_AUDIO_POLICY.speech_target_before_intro_end_seconds == 0.5
    assert BROADCAST_AUDIO_POLICY.speech_target_before_intro_end_min_seconds == 0.3
    assert BROADCAST_AUDIO_POLICY.speech_target_before_intro_end_max_seconds == 0.7


def test_analyze_audio_returns_deterministic_metadata_without_changing_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "station-id.wav"
    _write_wave(source)
    original_bytes = source.read_bytes()

    analysis = analyze_audio(source)

    assert analysis.source_path == source.resolve()
    assert analysis.duration_seconds == pytest.approx(0.1)
    assert analysis.sample_rate_hz == 8_000
    assert analysis.channels == 1
    assert analysis.codec == "pcm_s16le"
    assert analysis.checksum_sha256 == hashlib.sha256(original_bytes).hexdigest()
    assert analysis.validation_status is AudioValidationStatus.VALID
    assert source.read_bytes() == original_bytes


@pytest.mark.parametrize("duration", ["inf", "-inf", "nan"])
def test_analyze_audio_rejects_non_finite_ffprobe_duration(
    tmp_path: Path, duration: str
) -> None:
    source = tmp_path / "station-id.wav"
    fixture = tmp_path / "ffprobe_fixture.py"
    _write_wave(source)
    command = _write_ffprobe_fixture(fixture, duration)

    with pytest.raises(UnreadableAudioError, match="positive duration"):
        analyze_audio(source, ffprobe_binary=command)


def test_analyze_audio_rejects_unsupported_ffprobe_codec(tmp_path: Path) -> None:
    source = tmp_path / "station-id.wav"
    fixture = tmp_path / "ffprobe_fixture.py"
    _write_wave(source)
    command = _write_ffprobe_fixture(fixture, "0.1", codec="garbage")

    with pytest.raises(UnreadableAudioError, match="unsupported audio codec"):
        analyze_audio(source, ffprobe_binary=command)


def test_analyze_audio_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(AudioAssetMissingError):
        analyze_audio(tmp_path / "missing.wav")


def test_analyze_audio_rejects_malformed_audio(tmp_path: Path) -> None:
    source = tmp_path / "malformed.wav"
    source.write_bytes(b"not a wave file")

    with pytest.raises(UnreadableAudioError):
        analyze_audio(source)


def test_analyze_audio_rejects_unsupported_file_type(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("not audio", encoding="utf-8")

    with pytest.raises(UnsupportedAudioFormatError):
        analyze_audio(source)


def test_scan_music_persists_validated_station_catalogs_without_cross_station_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    en_music_root = tmp_path / "media" / "stations" / "radiotedu-en" / "music"
    fr_music_root = tmp_path / "media" / "stations" / "radiotedu-fr" / "music"
    en_music_root.mkdir(parents=True)
    fr_music_root.mkdir(parents=True)
    en_song = en_music_root / "English Artist - English Song.wav"
    en_broken = en_music_root / "broken.wav"
    fr_song = fr_music_root / "French Artist - French Song.wav"
    _write_wave(en_song)
    _write_wave(fr_song)
    en_broken.write_bytes(b"not a wave file")
    original_en_song = en_song.read_bytes()
    original_en_broken = en_broken.read_bytes()

    en_context = _station_context("radiotedu-en", tmp_path / "radiotedu-en", en_music_root)
    fr_context = _station_context("radiotedu-fr", tmp_path / "radiotedu-fr", fr_music_root)

    en_first_scan = scan_music(en_context)
    fr_first_scan = scan_music(fr_context)

    assert en_first_scan.tracks_found == 2
    assert en_first_scan.tracks_indexed == 1
    assert en_first_scan.music_dir == str(en_music_root)
    assert fr_first_scan.tracks_found == 1
    assert fr_first_scan.tracks_indexed == 1
    assert _catalog_rows(en_context) == [
        (1, "English Song", "English Artist", pytest.approx(0.1), str(en_song.resolve()))
    ]
    assert _catalog_rows(fr_context) == [
        (1, "French Song", "French Artist", pytest.approx(0.1), str(fr_song.resolve()))
    ]
    assert en_song.read_bytes() == original_en_song
    assert en_broken.read_bytes() == original_en_broken

    en_second_scan = scan_music(en_context)

    assert en_second_scan.tracks_found == 2
    assert en_second_scan.tracks_indexed == 1
    assert _catalog_rows(en_context) == [
        (1, "English Song", "English Artist", pytest.approx(0.1), str(en_song.resolve()))
    ]

    en_song.unlink()
    en_stale_scan = scan_music(en_context)

    assert en_stale_scan.tracks_found == 1
    assert en_stale_scan.tracks_indexed == 0
    assert _catalog_rows(en_context) == []
    assert _catalog_rows(fr_context) == [
        (1, "French Song", "French Artist", pytest.approx(0.1), str(fr_song.resolve()))
    ]


def test_station_separation_rejects_local_title_artist_album_and_track_cooldowns() -> None:
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
    policy = SeparationPolicy(
        title_cooldown=timedelta(hours=3),
        artist_cooldown=timedelta(hours=2),
        album_cooldown=timedelta(hours=4),
        track_cooldown=timedelta(hours=5),
    )
    candidates = (
        TrackCandidate("radiotedu-en", "title", "Shared Title", "New Artist", "New Album"),
        TrackCandidate("radiotedu-en", "artist", "New Title", "Shared Artist", "New Album"),
        TrackCandidate("radiotedu-en", "album", "New Title", "New Artist", "Shared Album"),
        TrackCandidate("radiotedu-en", "track", "New Title", "New Artist", "New Album"),
        TrackCandidate("radiotedu-en", "z-eligible", "Clear Z", "Clear Artist", "Clear Album"),
        TrackCandidate("radiotedu-en", "a-eligible", "Clear A", "Clear Artist", "Clear Album"),
    )
    local_history = (
        PlayedTrack("radiotedu-en", "old-title", "Shared Title", "Other", "Other", now - timedelta(hours=1)),
        PlayedTrack("radiotedu-en", "old-artist", "Other", "Shared Artist", "Other", now - timedelta(hours=1)),
        PlayedTrack("radiotedu-en", "old-album", "Other", "Other", "Shared Album", now - timedelta(hours=1)),
        PlayedTrack("radiotedu-en", "track", "Other", "Other", "Other", now - timedelta(hours=1)),
        PlayedTrack("radiotedu-fr", "a-eligible", "Clear A", "Clear Artist", "Clear Album", now - timedelta(minutes=1)),
    )

    selected = policy.select("radiotedu-en", candidates, local_history, now=now)

    assert selected is not None
    assert selected.track_id == "a-eligible"


def test_station_separation_returns_none_when_only_track_is_on_cooldown() -> None:
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
    candidate = TrackCandidate("radiotedu-en", "track-1", "Title", "Artist", "Album")
    policy = SeparationPolicy(track_cooldown=timedelta(minutes=30))

    selected = policy.select(
        "radiotedu-en",
        (candidate,),
        (PlayedTrack("radiotedu-en", "track-1", "Title", "Artist", "Album", now - timedelta(minutes=1)),),
        now=now,
    )

    assert selected is None
