import importlib.util
import sys
from pathlib import Path

import pytest

from backend.track_announcements import (
    TrackAnnouncementAssetLibrary,
    VerifiedFact,
    load_verified_facts,
    metadata_is_announceable,
    render_intro,
)


ROOT = Path(__file__).resolve().parents[2]


def load_temporary_station_module():
    path = ROOT / "scripts" / "run_temporary_station.py"
    spec = importlib.util.spec_from_file_location("temporary_station_probability_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_unknown_or_noisy_metadata_is_not_announced():
    assert not metadata_is_announceable("Uptown Funk", "Unknown Artist")
    assert not metadata_is_announceable("Song [Official Video]", "Artist")
    assert not metadata_is_announceable("Song", "094")
    assert metadata_is_announceable("Heart of Glass", "Blondie")


def test_fact_must_match_exact_track_identity():
    fact = load_verified_facts(
        ROOT / "data" / "editorial" / "verified-track-facts.json"
    )[0]
    with pytest.raises(ValueError, match="does not match"):
        render_intro("en", "Vogue", "Madonna", fact)


def test_fact_sources_are_allow_listed():
    with pytest.raises(ValueError, match="allow-listed"):
        VerifiedFact.from_dict(
            {
                "track_title": "Song",
                "track_artist": "Artist",
                "text_en": "A short fact.",
                "text_fr": "Un fait court.",
                "source_url": "https://example.com/post",
                "source_title": "Post",
                "verified_at": "2026-07-23T00:00:00Z",
                "evidence": "Exact identity match.",
            }
        )


def test_bilingual_intros_are_deterministic_and_source_backed():
    facts = load_verified_facts(
        ROOT / "data" / "editorial" / "verified-track-facts.json"
    )
    heart = next(fact for fact in facts if fact.track_title == "Heart of Glass")
    en = render_intro("en", "Heart of Glass", "Blondie", heart)
    fr = render_intro("fr", "Heart of Glass", "Blondie", heart)
    assert en.startswith("On Radio TED U, this is Heart of Glass by Blondie.")
    assert "The Disco Song" in en
    assert fr.startswith("Sur Radio TED U, voici Heart of Glass, par Blondie.")
    assert "The Disco Song" in fr


def test_live_mix_contract_is_frozen():
    source = (ROOT / "scripts" / "run_temporary_station.py").read_text(
        encoding="utf-8"
    )
    assert "voice_delay_seconds = 0.35" in source
    assert "duck_gain = 0.2512" in source
    assert "music_full_seconds = music_release_seconds + 2.5" in source
    assert "amix=inputs=2:duration=first:dropout_transition=0:" in source


def test_fallback_announcements_are_weighted_and_rate_limited(tmp_path):
    station = load_temporary_station_module()

    class NoPreparedAnnouncements:
        ready_count = 0
        queued_count = 0

        @staticmethod
        def resolve(track_id):
            del track_id
            return None

    tracks = [
        station.Item(
            path=tmp_path / f"track-{index}.wav",
            kind="music",
            title=f"Track {index}",
            artist="Artist",
            duration_seconds=180,
            source="test",
            track_id=index,
        )
        for index in range(1, 121)
    ]
    qwen = [
        station.Item(
            path=tmp_path / "en-radio-id.wav",
            kind="talking",
            title="Station ID",
            artist="RTAI",
            duration_seconds=7,
            source="qwen",
        ),
        station.Item(
            path=tmp_path / "en-continuity-v2.wav",
            kind="talking",
            title="Continuity",
            artist="RTAI",
            duration_seconds=8,
            source="qwen",
        ),
    ]
    rotation = station.Rotation(tracks, [], qwen, NoPreparedAnnouncements(), seed=7301)
    planned = [rotation.next() for _ in range(100)]

    voiced = [item for item in planned if item.voice_path]
    continuity_indexes = [
        index
        for index, item in enumerate(planned)
        if item.voice_path and "continuity" in item.voice_path.stem
    ]
    assert len(voiced) == 100
    assert all(item.voice_path is not None for item in planned)
    assert 30 <= len(continuity_indexes) <= 60
    assert all(
        later - earlier >= station.FALLBACK_TALKOVER_POLICY["continuity"]["cooldown_tracks"]
        for earlier, later in zip(continuity_indexes, continuity_indexes[1:])
    )

    longest_music_only_run = 0
    current_music_only_run = 0
    for item in planned:
        if item.voice_path:
            current_music_only_run = 0
        else:
            current_music_only_run += 1
            longest_music_only_run = max(longest_music_only_run, current_music_only_run)
    assert longest_music_only_run <= station.MAX_TRACKS_WITHOUT_TALKOVER
    assert rotation.ready_items() == station.TARGET_ANNOUNCEMENT_BUFFER


def test_missing_or_queued_audio_never_replaces_safe_fallback(tmp_path):
    (tmp_path / "radiotedu-en.json").write_text(
        """{
          "schema_version": 1,
          "station_id": "radiotedu-en",
          "live_web_requests": false,
          "entries": [{
            "track_id": 1,
            "text": "On Radio TED U, this is Song by Artist.",
            "cache_key": "not-a-real-key",
            "asset_path": "radiotedu-en/missing.wav",
            "state": "queued"
          }]
        }""",
        encoding="utf-8",
    )
    library = TrackAnnouncementAssetLibrary(tmp_path, "radiotedu-en")
    assert library.resolve(1) is None
    assert library.ready_count == 0
    assert library.queued_count == 1
