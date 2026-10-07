from __future__ import annotations

import json
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.run_ai_stream_supervisor import (
    IcecastSource,
    OutputPacer,
    autonomous_rotation,
    _is_streaming_status,
    _autonomous_track_id,
    build_ffmpeg_command,
    deterministic_rotation,
    discover_audio,
    load_config,
    probabilistic_rotation,
    read_cached_playlist,
    resolve_source_password,
    write_playlist,
)


def test_streaming_health_rejects_stale_state() -> None:
    fresh = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
    assert _is_streaming_status({"outputs": {"legacy": {"state": "streaming", "updated_at": fresh}}})
    assert not _is_streaming_status({
        "outputs": {"legacy": {"state": "streaming", "updated_at": "2020-01-01T00:00:00Z"}}
    })
    assert not _is_streaming_status({
        "outputs": {"legacy": {
            "state": "streaming", "audio_sent_at": fresh,
            "updated_at": fresh, "origin_listener_ready": False,
        }}
    })


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def settimeout(self, _timeout: float) -> None:
        pass

    def sendall(self, value: bytes) -> None:
        self.sent.append(value)

    def recv(self, _size: int) -> bytes:
        return b"HTTP/1.0 200 OK\r\n\r\n"

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        pass


def _config(tmp_path: Path) -> Path:
    for name in ("en", "fr"):
        root = tmp_path / name
        root.mkdir()
        (root / f"{name}.mp3").write_bytes(b"audio")
    payload = {
        "version": 1,
        "icecast_host": "127.0.0.1",
        "icecast_port": 8000,
        "icecast_user": "source",
        "credential_store": str(tmp_path / "vault.json"),
        "credential_reference": "credential://user/station/2/icecast",
        "onair_source_root": str(tmp_path / "onair"),
        "ffmpeg": str(tmp_path / "ffmpeg.exe"),
        "state_file": str(tmp_path / "state.json"),
        "bitrate_kbps": 192,
        "stations": [
            {
                "station_id": "radiotedu-en",
                "mount": "/en",
                "public_mount": "/en",
                "public_stream_url": "https://stream.radiotedu.com/en",
                "name": "RadioTEDU English",
                "music_roots": [str(tmp_path / "en")],
                "preferred_extensions": ["mp3"],
            },
            {
                "station_id": "radiotedu-fr",
                "mount": "/fr",
                "public_mount": "/fr",
                "public_stream_url": "https://stream.radiotedu.com/fr",
                "name": "RadioTEDU FranÃ§ais",
                "music_roots": [str(tmp_path / "fr")],
                "preferred_extensions": ["mp3"],
            },
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_config_accepts_only_canonical_ai_mounts(tmp_path: Path) -> None:
    path = _config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stations"][0]["mount"] = "/bad/path"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported public AI mount"):
        load_config(path)


def test_config_accepts_windows_powershell_utf8_bom(tmp_path: Path) -> None:
    path = _config(tmp_path)
    payload = path.read_bytes()
    path.write_bytes(b"\xef\xbb\xbf" + payload)

    config = load_config(path)

    assert [station.mount for station in config.stations] == ["/en", "/fr"]
    assert [station.public_mount for station in config.stations] == ["/en", "/fr"]
    assert [station.health_port for station in config.stations] == [8765, 8766]


def test_config_rejects_deterministic_playout_mode(tmp_path: Path) -> None:
    path = _config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["playout_mode"] = "deterministic"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="autonomous-local-ai"):
        load_config(path)


def test_config_accepts_prebaked_transition_host_without_ollama(tmp_path: Path) -> None:
    path = _config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["playout_mode"] = "prebaked-transition-host"
    liner_root = tmp_path / "liners"
    for station in payload["stations"]:
        station.update(
            {
                "dynamic_host_enabled": False,
                "transition_liner_root": str(liner_root),
                "transition_liner_genres": ["Lo-Fi", "Rock"],
                "transition_liner_min_variants": 5,
                "transition_liner_timezone": "Europe/Istanbul",
            }
        )
    path.write_text(json.dumps(payload), encoding="utf-8")

    config = load_config(path)

    assert config.playout_mode == "prebaked-transition-host"
    assert all(station.prebaked_transition_host for station in config.stations)
    assert all(not station.dynamic_host_enabled for station in config.stations)


def test_config_rejects_announcement_spacing_outside_supported_range(tmp_path: Path) -> None:
    path = _config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stations"][0].update(
        {
            "dynamic_host_enabled": True,
            "host_model": "qwen3:0.6b",
            "host_voice_design": "en-day",
            "host_night_voice_design": "en-night",
            "host_lead_songs_min": 10,
            "host_lead_songs_max": 12,
            "host_queue_target": 13,
            "host_queue_start_min": 0,
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid dynamic host lead"):
        load_config(path)


def test_config_rejects_host_queue_start_after_queue_target(tmp_path: Path) -> None:
    path = _config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stations"][0].update(
        {
            "dynamic_host_enabled": True,
            "host_model": "qwen3:0.6b",
            "host_voice_design": "en-day",
            "host_night_voice_design": "en-night",
            "host_lead_songs_min": 3,
            "host_lead_songs_max": 4,
            "host_queue_target": 10,
            "host_queue_start_min": 11,
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid dynamic host startup buffer"):
        load_config(path)


def test_config_rejects_swapped_language_health_ports(tmp_path: Path) -> None:
    path = _config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stations"][0]["health_port"] = 8766
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported health port"):
        load_config(path)


def test_config_accepts_maximum_track_duration(tmp_path: Path) -> None:
    path = _config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stations"][0]["max_track_seconds"] = 600
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_config(path).stations[0].max_track_seconds == 600


def test_discover_audio_excludes_tracks_longer_than_station_limit(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "Pop"
    root.mkdir()
    short = root / "short.mp3"
    long = root / "long.mp3"
    short.write_bytes(b"short")
    long.write_bytes(b"long")
    station = load_config(_config(tmp_path)).stations[0]
    station = __import__("dataclasses").replace(
        station,
        music_roots=(root,),
        preferred_extensions=(),
        max_track_seconds=600,
    )
    monkeypatch.setattr(
        "scripts.run_ai_stream_supervisor.MutagenFile",
        lambda item: SimpleNamespace(
            info=SimpleNamespace(length=601 if Path(item).name == "long.mp3" else 180)
        ),
    )

    assert discover_audio(station) == [short.resolve()]


def test_discover_audio_can_require_safe_musicbrainz_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "Pop"
    root.mkdir()
    safe = root / "safe.mp3"
    unsafe = root / "anonymous.mp3"
    safe.write_bytes(b"safe")
    unsafe.write_bytes(b"unsafe")
    station = load_config(_config(tmp_path)).stations[0]
    station = __import__("dataclasses").replace(
        station,
        music_roots=(root,),
        preferred_extensions=(),
        require_musicbrainz_metadata=True,
    )

    def fake_mutagen(item, easy=False):
        artist = "Anonymous420" if Path(item).name == "anonymous.mp3" else "Example Artist"
        return SimpleNamespace(
            tags={
                "title": ["Example Song"],
                "artist": [artist],
                "musicbrainz_trackid": ["da8c2ae4-70f2-4703-ba54-8a3a359f3a36"],
            },
            info=SimpleNamespace(length=180),
        )

    monkeypatch.setattr(
        "scripts.run_ai_stream_supervisor.MutagenFile", fake_mutagen
    )

    assert discover_audio(station) == [safe.resolve()]


def test_probabilistic_rotation_keeps_all_tracks_and_separates_folders() -> None:
    files = [
        Path("C:/approved/jazz/alice/one.mp3"),
        Path("C:/approved/jazz/alice/two.mp3"),
        Path("C:/approved/rock/bob/three.mp3"),
        Path("C:/approved/rock/bob/four.mp3"),
    ]

    result = probabilistic_rotation(files, __import__("random").Random(7))

    assert set(result) == set(files)
    assert len(result) == len(files)
    assert all(left.parent != right.parent for left, right in zip(result, result[1:]))


def test_deterministic_rotation_repeats_exactly_for_same_seed() -> None:
    files = [
        Path("C:/approved/jazz/alice/one.mp3"),
        Path("C:/approved/jazz/alice/two.mp3"),
        Path("C:/approved/rock/bob/three.mp3"),
        Path("C:/approved/rock/bob/four.mp3"),
    ]
    first = deterministic_rotation(files, 42, "radiotedu-en")
    second = deterministic_rotation(list(reversed(files)), 42, "radiotedu-en")
    assert first == second


def test_autonomous_rotation_uses_only_valid_local_ai_choices(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("scripts.run_ai_stream_supervisor._RECENT_AUTONOMOUS_TRACKS", {})
    monkeypatch.setattr(
        "scripts.run_ai_stream_supervisor.assert_autonomous_song_ai_ready",
        lambda _station: None,
    )
    station = load_config(_config(tmp_path)).stations[0]
    files = [tmp_path / "Jazz" / f"Artist {index}" / f"Song {index}.mp3" for index in range(3)]
    files += [tmp_path / "Rock" / "Artist R" / "Song R.mp3"]
    received_genres = []
    queued_events = []

    def fake_choose(_station, candidates, _recent, _context):
        received_genres.append([str(item["genre"]) for item in candidates])
        return {
            "choice_id": 1,
            "choice_key": "candidate_0001",
            "probabilities": {
                f"candidate_{int(item['id']):04d}": (0.2 if int(item["id"]) == 1 else 0.6 if int(item["id"]) == 2 else 0.2)
                for item in candidates
            },
            "finalist_choice_ids": [int(item["id"]) for item in candidates],
            "candidate_pool": [
                {
                    "choice_id": int(item["id"]),
                    "track_id": str(item["track_id"]),
                    "title": str(item["title"]),
                    "artist": str(item["artist"]),
                    "genre": str(item["genre"]),
                    "shortlist_cosine_similarity": None,
                    "shortlist_rank": index,
                }
                for index, item in enumerate(candidates, start=1)
            ],
            "shortlist_evidence": {
                "method": "laya_typed_choice_over_entire_pool",
                "score_type": "cosine_similarity_not_probability",
                "candidate_count": len(candidates),
                "ordered_pool_sha256": "test-pool-hash",
                "shortlist_size": len(candidates),
                "choice_ids_by_similarity_rank": [int(item["id"]) for item in candidates],
                "query_text": None,
                "candidate_option_texts": [],
            },
            "answer_confidence": 0.2,
            "model_input": {},
            "model_output_raw_json": "{}",
            "model_id": "laya-test",
            "model_revision": "test-revision",
        }

    monkeypatch.setattr(
        "scripts.run_ai_stream_supervisor._choose_autonomous_candidate", fake_choose
    )
    result = autonomous_rotation(
        files,
        station,
        metadata_resolver=lambda path: (path.stem, path.parent.name),
        genre_resolver=lambda path: path.parent.parent.name,
        selection_context={"id": "jazz_lab", "required_genres": ["Jazz"]},
        decision_sink=lambda event: queued_events.append(event) or True,
    )

    assert len(result) == 3
    assert set(result) == {path.resolve() for path in files if "Jazz" in path.parts}
    assert all(genres and set(genres) == {"Jazz"} for genres in received_genres)
    assert len(queued_events) == 1
    event = queued_events[0]
    assert event["selection_mode"] == "laya_library_shortlist_probability_ranked_batch"
    assert event["decision_schema_version"] == "radio-song-choice-library-shortlist-batch-v1"
    assert [item["selection_basis"] for item in event["program_selection"]] == [
        "typed_model_choice",
        "highest_remaining_model_probability",
        "highest_remaining_model_probability",
    ]
    assert event["program_selection"][0]["choice_id"] == event["model_output"]["choice_id"]
    assert event["program_selection"][1]["probability"] == 0.6


def test_autonomous_rotation_sends_the_complete_catalog_to_laya_shortlisting(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("scripts.run_ai_stream_supervisor._RECENT_AUTONOMOUS_TRACKS", {})
    monkeypatch.setattr(
        "scripts.run_ai_stream_supervisor.assert_autonomous_song_ai_ready",
        lambda _station: None,
    )
    station = load_config(_config(tmp_path)).stations[0]
    files = [
        tmp_path / "Jazz" / f"Artist {index:03d}" / f"Song {index:03d}.mp3"
        for index in range(80)
    ]
    received_count: list[int] = []
    queued_events: list[dict[str, object]] = []

    def fake_choose(_station, candidates, _recent, _context):
        received_count.append(len(candidates))
        finalists = list(range(1, 65))
        pool = [
            {
                "choice_id": int(item["id"]),
                "track_id": str(item["track_id"]),
                "title": str(item["title"]),
                "artist": str(item["artist"]),
                "genre": str(item["genre"]),
                "shortlist_cosine_similarity": 1.0 - int(item["id"]) / 100.0,
                "shortlist_rank": int(item["id"]) if int(item["id"]) <= 64 else None,
            }
            for item in candidates
        ]
        return {
            "choice_id": 1,
            "choice_key": "candidate_0001",
            "probabilities": {f"candidate_{index:04d}": 1.0 / 64 for index in finalists},
            "finalist_choice_ids": finalists,
            "candidate_pool": pool,
            "shortlist_evidence": {
                "method": "laya.shortlist.predict_shortlist_cosine_similarity",
                "score_type": "cosine_similarity_not_probability",
                "candidate_count": len(candidates),
                "ordered_pool_sha256": "all-eighty-hash",
                "shortlist_size": 64,
                "choice_ids_by_similarity_rank": finalists,
                "query_text": "captured full-pool query",
                "candidate_option_texts": [
                    {
                        "choice_id": int(item["id"]),
                        "text": f"candidate_{int(item['id']):04d}: {item['title']}",
                    }
                    for item in candidates
                ],
            },
            "model_input": {
                "state": {"catalog_pool": {"candidate_count": len(candidates)}},
                "questions": {"song_choice": {"criteria": {}}},
            },
            "model_output_raw_json": "{}",
            "model_id": "laya-test",
            "model_revision": "test-revision",
        }

    monkeypatch.setattr(
        "scripts.run_ai_stream_supervisor._choose_autonomous_candidate", fake_choose
    )
    selected = autonomous_rotation(
        files,
        station,
        metadata_resolver=lambda path: (path.stem, path.parent.name),
        genre_resolver=lambda _path: "Jazz",
        decision_sink=lambda event: queued_events.append(event) or True,
    )

    assert received_count == [80]
    assert len(selected) == 13
    assert len(queued_events) == 1
    event = queued_events[0]
    assert len(event["candidate_tracks"]) == 80
    assert len(event["shortlist_evidence"]["candidate_option_texts"]) == 80
    assert event["finalist_choice_ids"] == list(range(1, 65))
    assert event["prefilter_rules"]["random_candidate_sampling"] is False


def test_live_laya_choice_uses_stable_catalog_option_ids(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("scripts.run_ai_stream_supervisor._RECENT_AUTONOMOUS_TRACKS", {})
    monkeypatch.setattr(
        "scripts.run_ai_stream_supervisor.assert_autonomous_song_ai_ready",
        lambda _station: None,
    )
    station = load_config(_config(tmp_path)).stations[0]
    files = [tmp_path / "Jazz" / f"Artist {index}" / f"Song {index}.mp3" for index in range(3)]
    catalog_ids = {path.resolve(): 101 + index * 17 for index, path in enumerate(files)}
    events: list[dict[str, object]] = []

    def fake_choose(_station, candidates, _recent, _context):
        ids = [int(item["id"]) for item in candidates]
        return {
            "choice_id": ids[0],
            "choice_key": f"candidate_{ids[0]:04d}",
            "probabilities": {f"candidate_{choice_id:04d}": 1.0 / len(ids) for choice_id in ids},
            "finalist_choice_ids": ids,
            "candidate_pool": [
                {
                    "choice_id": int(item["id"]),
                    "track_id": str(item["track_id"]),
                    "title": str(item["title"]),
                    "artist": str(item["artist"]),
                    "genre": str(item["genre"]),
                    "shortlist_cosine_similarity": None,
                    "shortlist_rank": None,
                }
                for item in candidates
            ],
            "shortlist_evidence": {
                "method": "laya_typed_choice_over_entire_pool",
                "score_type": "cosine_similarity_not_probability",
                "candidate_count": len(ids),
                "ordered_pool_sha256": "stable-id-test-pool",
                "shortlist_size": len(ids),
                "choice_ids_by_similarity_rank": ids,
                "query_text": None,
                "candidate_option_texts": [],
            },
            "answer_confidence": 1.0 / len(ids),
            "model_input": {},
            "model_output_raw_json": "{}",
            "model_id": "laya-test",
            "model_revision": "test-revision",
        }

    monkeypatch.setattr(
        "scripts.run_ai_stream_supervisor._choose_autonomous_candidate", fake_choose
    )
    selected = autonomous_rotation(
        files,
        station,
        metadata_resolver=lambda path: (path.stem, path.parent.name),
        genre_resolver=lambda path: "Jazz" if "0" in path.stem else "Rock",
        selection_context={"id": "jazz_lab", "required_genres": ["Jazz"]},
        decision_sink=lambda event: events.append(event) or True,
        max_tracks=1,
        one_choice_per_track=True,
        choice_id_resolver=lambda path: catalog_ids[path.resolve()],
    )

    assert len(selected) == 1
    assert len(events) == 1
    assert events[0]["catalog_candidate_count"] == len(files)
    assert events[0]["prefilter_rules"]["required_genre_filter_applied"] is False
    assert events[0]["prefilter_rules"]["genre_policy"] == "advisory"
    assert [item["choice_id"] for item in events[0]["candidate_tracks"]] == [
        catalog_ids[path.resolve()]
        for path in sorted(files, key=_autonomous_track_id)
    ]
    assert len(events[0]["program_selection"]) == 1
    assert events[0]["live_selection_queue"]["selection_timing"] == "startup_buffer_prefill"


def test_autonomous_rotation_has_no_deterministic_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("scripts.run_ai_stream_supervisor._RECENT_AUTONOMOUS_TRACKS", {})
    monkeypatch.setattr(
        "scripts.run_ai_stream_supervisor.assert_autonomous_song_ai_ready",
        lambda _station: None,
    )
    station = load_config(_config(tmp_path)).stations[0]
    files = [tmp_path / "Artist One" / "One.mp3", tmp_path / "Artist Two" / "Two.mp3"]

    monkeypatch.setattr(
        "scripts.run_ai_stream_supervisor._choose_autonomous_candidate",
        lambda *_args: {"choice_id": 999},
    )
    with pytest.raises(RuntimeError, match="outside the supplied candidate set"):
        autonomous_rotation(
            files,
            station,
            metadata_resolver=lambda path: (path.stem, path.parent.name),
        )


def test_ffmpeg_command_has_no_icecast_credential(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path))
    station = config.stations[0]
    playlist, count = write_playlist(
        station, tmp_path / "playlists", discover_audio(station)
    )
    command = build_ffmpeg_command(config, station, playlist)
    assert count == 1
    assert "credential://" not in " ".join(command)
    assert "icecast" not in " ".join(command).lower()
    assert command[-1] == "pipe:1"


def test_source_password_can_come_from_protected_service_env(tmp_path: Path) -> None:
    path = _config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    secret_file = tmp_path / "ai-streams.env"
    secret_file.write_text(
        "RADIOTEDU_AI_ICECAST_SOURCE_PASSWORD=protected-test-value\n",
        encoding="utf-8",
    )
    payload["credential_env_file"] = str(secret_file)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert resolve_source_password(load_config(path)) == "protected-test-value"


def test_playlist_is_ffconcat_and_contains_operator_root_audio(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path))
    station = config.stations[1]
    playlist, count = write_playlist(
        station, tmp_path / "playlists", discover_audio(station)
    )
    text = playlist.read_text(encoding="utf-8")
    assert count == 1
    assert text.startswith("ffconcat version 1.0\n")
    assert "fr.mp3" in text


def test_cached_playlist_can_start_without_rescanning_roots(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path))
    station = config.stations[0]
    state_root = tmp_path / "playlists"
    playlist, count = write_playlist(station, state_root, discover_audio(station))
    for root in station.music_roots:
        for item in root.iterdir():
            item.unlink()

    assert read_cached_playlist(station, state_root) == (playlist, count)


def test_playlist_refresh_uses_new_immutable_file_while_old_is_active(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path))
    station = config.stations[0]
    state_root = tmp_path / "playlists"
    original, original_count = write_playlist(
        station, state_root, discover_audio(station)
    )
    (station.music_roots[0] / "second.mp3").write_bytes(b"audio-2")

    refreshed, refreshed_count = write_playlist(
        station, state_root, discover_audio(station)
    )

    assert refreshed != original
    assert original.is_file()
    assert refreshed.is_file()
    assert original_count == 1
    assert refreshed_count == 2
    assert read_cached_playlist(station, state_root) == (refreshed, refreshed_count)


def test_output_pacer_splits_encoder_bursts_into_small_packets() -> None:
    class _Source:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def send(self, audio: bytes) -> None:
            self.sent.append(audio)

    source = _Source()
    pacer = OutputPacer(100_000_000, packet_bytes=1_024)
    pacer.send(source, b"x" * 2_500, __import__("threading").Event())  # type: ignore[arg-type]
    assert [len(packet) for packet in source.sent] == [1_024, 1_024, 452]


def test_icecast_source_sends_raw_audio_without_chunk_markers(monkeypatch) -> None:
    fake = _FakeSocket()
    monkeypatch.setattr(
        "scripts.run_ai_stream_supervisor.socket.create_connection",
        lambda *_args, **_kwargs: fake,
    )
    source = IcecastSource(
        host="127.0.0.1",
        port=8000,
        mount="/en",
        user="source",
        password="protected-test-value",
        name="RadioTEDU English",
    )
    source.send(b"MP3-FRAME")
    request = fake.sent[0]
    assert request.startswith(b"PUT /en HTTP/1.1\r\n")
    assert b"Expect: 100-continue\r\n" in request
    assert b"Transfer-Encoding" not in request
    assert fake.sent[-1] == b"MP3-FRAME"
    source.close()
