from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.shadow_music_director import (
    DirectorTrack,
    RecentPlay,
    ShadowMusicDirector,
    status_payload,
)


def track(tmp_path: Path, track_id: int, title: str, artist: str, genre: str):
    path = tmp_path / f"{track_id}.mp3"
    path.write_bytes(b"test")
    return DirectorTrack(
        track_id=track_id,
        title=title,
        artist=artist,
        genre=genre,
        mood=None,
        bpm=110,
        duration_seconds=210,
        file_path=str(path),
        metadata_status="accepted",
    )


def test_shadow_plan_is_deterministic_and_unique(tmp_path):
    tracks = [
        track(tmp_path, 1, "One", "Artist A", "rock"),
        track(tmp_path, 2, "Two", "Artist B", "pop"),
        track(tmp_path, 3, "Three", "Artist C", "jazz"),
        track(tmp_path, 4, "Four", "Artist D", "other"),
    ]
    now = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    director = ShadowMusicDirector("radiotedu-en")
    first = director.recommend(tracks, [], program="Campus Flow", now=now, count=4)
    second = director.recommend(tracks, [], program="Campus Flow", now=now, count=4)
    assert first == second
    assert len({item.track_id for item in first}) == len(first)
    assert len({item.artist for item in first}) == len(first)


def test_recent_track_and_artist_are_hard_exclusions(tmp_path):
    tracks = [
        track(tmp_path, 1, "Recently Played", "Artist A", "rock"),
        track(tmp_path, 2, "Different Song", "Artist A", "pop"),
        track(tmp_path, 3, "Safe Song", "Artist B", "jazz"),
    ]
    now = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    recent = [
        RecentPlay(
            title="Recently Played",
            artist="Artist A",
            played_at=now - timedelta(minutes=10),
        )
    ]
    result = ShadowMusicDirector("radiotedu-en").recommend(
        tracks, recent, program="Campus Flow", now=now, count=3
    )
    assert [item.track_id for item in result] == [3]


def test_missing_files_and_current_track_are_excluded(tmp_path):
    safe = track(tmp_path, 1, "Safe", "Artist A", "rock")
    missing = DirectorTrack(
        track_id=2,
        title="Missing",
        artist="Artist B",
        genre="pop",
        mood=None,
        bpm=None,
        duration_seconds=200,
        file_path=str(tmp_path / "missing.mp3"),
    )
    result = ShadowMusicDirector("radiotedu-en").recommend(
        [safe, missing],
        [],
        program="Campus Flow",
        now=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
        current_title="Missing",
        count=2,
    )
    assert [item.track_id for item in result] == [1]


def test_status_explicitly_denies_live_control():
    payload = status_payload(
        "radiotedu-en",
        program="Campus Flow",
        generated_at=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
        recommendations=[],
        actual_next=[],
        recent_play_count=0,
        track_count=10,
        current={},
    )
    assert payload["state"] == "shadow"
    assert payload["read_only"] is True
    assert payload["controls_live_playout"] is False
    assert payload["constraints"]["generative_model_can_override"] is False
