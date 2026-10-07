from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
MODULE_PATH = SCRIPTS / "run_ai_quality_supervisor.py"
SPEC = importlib.util.spec_from_file_location("run_ai_quality_supervisor", MODULE_PATH)
assert SPEC and SPEC.loader
quality = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = quality
SPEC.loader.exec_module(quality)

from run_ai_stream_supervisor import StationConfig, SupervisorConfig


def test_selection_api_payload_normalizes_choice_ids_without_changing_trace() -> None:
    raw_response = '{"answers":{"song_choice":{"choice":"candidate_01"}}}'
    model_input = {"state": {"ordered_candidates": [{"choice_id": 1}]}}
    event = {
        "candidate_tracks": [{"id": 1, "track_id": "track-a", "title": "Song A"}],
        "model_input": model_input,
        "model_output_raw_json": raw_response,
    }

    payload = quality.PublicApiSync._selection_event_for_api(event)

    assert payload["candidate_tracks"] == [
        {"choice_id": 1, "track_id": "track-a", "title": "Song A"}
    ]
    assert event["candidate_tracks"][0]["id"] == 1
    assert payload["model_input"] == model_input
    assert payload["model_output_raw_json"] == raw_response


def _station() -> StationConfig:
    return StationConfig(
        "radiotedu-en", "/ai", "English", (Path("C:/music"),), (".flac",), 8765,
        public_stream_url="https://stream.radiotedu.com/en", public_mount="/en",
    )


def _config(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        "127.0.0.1", 8000, "source", tmp_path / "vault", "credential://test", None,
        tmp_path, tmp_path / "ffmpeg.exe", tmp_path / "state.json", 192, (_station(),),
    )


def _full_catalog_event(candidate_count: int = 1000) -> dict[str, object]:
    candidates = []
    for choice_id in range(1, candidate_count + 1):
        candidates.append(
            {
                "choice_id": choice_id,
                "track_id": f"track-{choice_id:04d}",
                "title": f"Song {choice_id:04d}",
                "artist": f"Artist {choice_id % 30:02d}",
                "genre": "Jazz",
                "shortlist_cosine_similarity": 1.0 - choice_id / (candidate_count + 1),
                "shortlist_rank": choice_id if choice_id <= 64 else None,
            }
        )
    basic_pool = [
        {key: item[key] for key in ("choice_id", "track_id", "title", "artist", "genre")}
        for item in candidates
    ]
    pool_sha256 = hashlib.sha256(
        json.dumps(
            basic_pool,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    finalists = list(range(1, min(candidate_count, 64) + 1))
    probability = 1.0 / len(finalists)
    candidate_probabilities = [
        {
            "choice_id": choice_id,
            "track_id": candidates[choice_id - 1]["track_id"],
            "probability": probability,
        }
        for choice_id in finalists
    ]
    return {
        "protocol": "radiotedu-platform/v1",
        "schema_version": 1,
        "event_id": "selection-radiotedu-en-full-catalog-test",
        "station_id": "radiotedu-en",
        "event_type": "song.selection.completed",
        "occurred_at": "2026-10-01T10:20:00+00:00",
        "language": "en",
        "program_id": "program-test",
        "program_name": "Test Program",
        "selection_mode": "laya_library_shortlist_probability_ranked_batch",
        "selector_version": "laya-full-catalog-shortlist-v1",
        "selector_policy_version": "laya-music-full-catalog-policy-v1",
        "selector_policy_sha256": "policy-hash",
        "model_provider": "laya",
        "model_version": "convaiinnovations/laya-multilingual@revision",
        "model_package_version": "0.3.22",
        "decision_schema_version": "radio-song-choice-library-shortlist-batch-v1",
        "prefilter_rules": {"random_candidate_sampling": False},
        "recent_tracks": [],
        "catalog_candidate_count": candidate_count,
        "catalog_ordered_pool_sha256": pool_sha256,
        "candidate_tracks": candidates,
        "shortlist_evidence": {
            "method": "laya.shortlist.predict_shortlist_cosine_similarity",
            "score_type": "cosine_similarity_not_probability",
            "candidate_count": candidate_count,
            "ordered_pool_sha256": pool_sha256,
            "shortlist_size": len(finalists),
            "choice_ids_by_similarity_rank": finalists,
            "query_text": "captured Laya shortlist query",
            "candidate_option_texts": [
                {"choice_id": item["choice_id"], "text": f"candidate_{item['choice_id']:04d}: {item['title']}"}
                for item in candidates
            ],
        },
        "finalist_choice_ids": finalists,
        "model_input": {
            "state": {
                "catalog_pool": {
                    "candidate_count": candidate_count,
                    "ordered_pool_sha256": pool_sha256,
                }
            },
            "questions": {
                "song_choice": {
                    "type": "choice",
                    "instructions": "Choose one supplied finalist",
                    "criteria": {
                        f"candidate_{choice_id:04d}": f"Song {choice_id:04d}"
                        for choice_id in finalists
                    },
                }
            },
        },
        "model_output_raw_json": "{\"answers\":{\"song_choice\":{\"type\":\"choice\"}}}",
        "model_output": {
            "choice_id": 1,
            "choice_key": "candidate_0001",
            "candidate_probabilities": candidate_probabilities,
            "confidence": probability,
            "answer_confidence": probability,
            "action": None,
            "usage": None,
            "routing": None,
        },
        "program_selection": [
            {
                "position": 1,
                "choice_id": 1,
                "track_id": "track-0001",
                "selection_basis": "typed_model_choice",
                "probability": probability,
            }
        ],
        "selected_track_id": "track-0001",
        "validation": {"fallback": False},
    }


def test_full_catalog_selection_event_over_32k_is_durably_queued(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sync = quality.PublicApiSync(
        config,
        quality.DurableStatus(tmp_path / "status.json"),
        threading.Event(),
    )
    event = _full_catalog_event()
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(encoded) > 32 * 1024
    assert sync.record_selection_decision(event)
    assert sync.pending_selection_decision_count("radiotedu-en") == 1


def test_single_live_laya_choice_event_is_durably_queued(tmp_path: Path) -> None:
    sync = quality.PublicApiSync(
        _config(tmp_path),
        quality.DurableStatus(tmp_path / "status.json"),
        threading.Event(),
    )
    event = _full_catalog_event()
    event.update(
        {
            "selection_mode": "laya_library_shortlist_single_next_track",
            "selector_version": "laya-live-next-track-v1",
            "selector_policy_version": "laya-live-next-track-policy-v1",
            "decision_schema_version": "radio-song-choice-library-shortlist-single-v1",
            "live_selection_queue": {
                "mode": "laya-live-next-track",
                "current_track_id": "track-current",
                "queued_track_ids": ["track-queued"],
                "queued_track_count": 1,
                "queue_target_depth": 5,
                "candidate_pool_excludes_queued_tracks": True,
                "selection_timing": "rolling_while_current_track_plays",
            },
        }
    )

    assert sync.record_selection_decision(event)
    assert sync.pending_selection_decision_count("radiotedu-en") == 1


def test_live_laya_song_queue_makes_one_traced_choice_while_music_plays(
    tmp_path: Path, monkeypatch
) -> None:
    station = _station()
    catalog = [tmp_path / f"track-{index}.wav" for index in range(8)]
    initial = catalog[:5]
    stop = threading.Event()
    calls: list[dict[str, object]] = []
    events: list[dict[str, object]] = []

    def choose_one(files, _station, **kwargs):
        selected = files[0]
        call = {
            "files": list(files),
            "kwargs": kwargs,
            "selected": selected,
        }
        calls.append(call)
        kwargs["decision_sink"](
            {
                "event_id": f"event-{len(calls)}",
                "selected_track_id": quality.public_track_id(selected),
                "live_selection_queue": kwargs["selection_context"][
                    "live_selection_queue"
                ],
            }
        )
        return [selected]

    monkeypatch.setattr(quality, "autonomous_rotation", choose_one)
    queue = quality.LiveLayaSongQueue(
        station=station,
        catalog=catalog,
        initial_tracks=initial,
        selection_context={"id": "test"},
        context_provider=lambda: {"id": "test"},
        decision_sink=events.append,
        stop=stop,
        target_depth=5,
    )
    queue.start()
    try:
        assert queue.next_track(timeout=1) == initial[0]
        deadline = time.monotonic() + 3
        while queue.snapshot()["queue_depth"] < 5 and time.monotonic() < deadline:
            time.sleep(0.01)

        assert queue.snapshot()["queue_depth"] == 5
        assert len(calls) == 1
        assert len(events) == 1
        kwargs = calls[0]["kwargs"]
        assert kwargs["max_tracks"] == 1
        assert kwargs["one_choice_per_track"] is True
        assert kwargs["recent_track_paths"] == [initial[0]]
        queue_state = kwargs["selection_context"]["live_selection_queue"]
        assert queue_state["current_track_id"] == quality.public_track_id(initial[0])
        assert queue_state["queued_track_count"] == 4
        assert events[0]["selected_track_id"] == quality.public_track_id(calls[0]["selected"])
        assert calls[0]["selected"] not in initial
    finally:
        stop.set()
        queue.close()


def test_full_catalog_selection_rejects_a_pool_hash_mismatch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sync = quality.PublicApiSync(
        config,
        quality.DurableStatus(tmp_path / "status.json"),
        threading.Event(),
    )
    event = _full_catalog_event()
    event["catalog_ordered_pool_sha256"] = "wrong"
    event["shortlist_evidence"]["ordered_pool_sha256"] = "wrong"

    with pytest.raises(ValueError, match="pool hash"):
        sync.record_selection_decision(event)


def test_fallback_metadata_removes_hash_and_recovers_parent_artist():
    title, artist = quality.fallback_track_metadata(
        Path(
            "F:/RadioTEDU Songs/AI/rights-cleared/radiotedu-fr/Other/Membeth/"
            "51c1fb4740dbe94b-Bach.Aria.Goldberg-Variationen.WerckmeisterIII.ogg"
        )
    )

    assert title == "Bach Aria Goldberg-Variationen Werckmeister III"
    assert artist == "Membeth"


def test_fallback_metadata_recovers_dash_separated_artist_and_title():
    title, artist = quality.fallback_track_metadata(
        Path("F:/RadioTEDU Songs/Voting/Rock/Alpha Hydrae/de22c501c7d87fad-Alpha Hydrae - 04 - All you need is noise.ogg")
    )

    assert title == "All you need is noise"
    assert artist == "Alpha Hydrae"


def test_fallback_metadata_removes_repeated_artist_credit_from_title():
    title, artist = quality.fallback_track_metadata(
        Path(
            "F:/RadioTEDU Songs/Voting/Electronic/Mesostic/"
            "56c1fe2443541548-Mesostic - Stem 3 from Happy Hard Pop Core by Mesostic.ogg"
        )
    )

    assert title == "Stem 3 from Happy Hard Pop Core"
    assert artist == "Mesostic"


def test_track_genre_uses_the_operator_classified_root_folder(tmp_path):
    root = tmp_path / "Lo-Fi"
    track = root / "Artist" / "Song.wav"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    station = replace(_station(), music_roots=(root,))

    assert quality.track_genre(station, track) == "Lo-Fi"


def test_editorial_schedule_matches_weekday_and_weekend_contract():
    weekday = datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc)  # 18:00 Ankara
    weekend_night = datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc)  # 05:00 Ankara
    weekend_day = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)  # 11:00 Ankara

    assert quality.current_editorial_program("radiotedu-en", weekday)["name"] == "Jazz Lab"
    assert quality.current_editorial_program("radiotedu-fr", weekday)["name"] == "Laboratoire jazz"
    assert quality.current_editorial_program("radiotedu-en", weekday)["sound_tags"] == ["calm"]
    assert quality.current_editorial_program("radiotedu-en", weekend_night)["name"] == "Weekend Night Signal"
    assert quality.current_editorial_program("radiotedu-fr", weekend_day)["name"] == "Signal du week-end"


def test_jazz_lab_is_strictly_jazz_and_never_falls_back(tmp_path):
    jazz = tmp_path / "Jazz" / "Artist" / "Jazz Song.wav"
    second_jazz = tmp_path / "Jazz" / "Other" / "Second Jazz Song.wav"
    rock = tmp_path / "Rock" / "Artist" / "Rock Song.wav"
    for track in (jazz, second_jazz, rock):
        track.parent.mkdir(parents=True, exist_ok=True)
        track.write_bytes(b"audio")
    station = replace(_station(), music_roots=(tmp_path / "Jazz", tmp_path / "Rock"))
    block = quality.current_editorial_program(
        "radiotedu-en",
        datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc),
    )

    selected = quality.program_for_editorial_block(
        [rock, jazz, second_jazz], station, block
    )

    assert selected == [jazz, second_jazz]
    assert {quality.track_genre(station, item) for item in selected} == {"Jazz"}


def test_jazz_lab_accepts_one_track_so_rotation_can_restart_from_the_beginning(tmp_path):
    jazz = tmp_path / "Jazz" / "Artist" / "Only Jazz Song.wav"
    rock = tmp_path / "Rock" / "Artist" / "Rock Song.wav"
    for track in (jazz, rock):
        track.parent.mkdir(parents=True, exist_ok=True)
        track.write_bytes(b"audio")
    station = replace(_station(), music_roots=(tmp_path / "Jazz", tmp_path / "Rock"))
    block = quality.current_editorial_program(
        "radiotedu-en",
        datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc),
    )

    selected = quality.program_for_editorial_block([rock, jazz], station, block)

    assert selected == [jazz]
    assert selected[0 % len(selected)] == selected[1 % len(selected)]


def test_origin_probe_timeout_keeps_last_success_during_bounded_grace():
    assert quality._origin_probe_effective_ready(
        False, 100.0, 100.0 + quality.ORIGIN_PROBE_GRACE_SECONDS
    )
    assert not quality._origin_probe_effective_ready(
        False, 100.0, 100.0 + quality.ORIGIN_PROBE_GRACE_SECONDS + 0.01
    )
    assert quality._origin_probe_effective_ready(False, None, 100.0) is False


def _bridge(tmp_path: Path) -> Path:
    path = tmp_path / "quality.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "channels": [
                    {
                        "channel_id": "ai",
                        "outputs": [
                            {
                                "enabled": True,
                                "quality": "low",
                                "mount": "/ai-low",
                                "stream_codec_profile": "aac_lc_96",
                                "stream_bitrate_kbps": 96,
                                "icecast_public": True,
                            },
                            {
                                "enabled": True,
                                "quality": "normal",
                                "mount": "/ai-normal",
                                "stream_codec_profile": "aac_lc_128",
                                "stream_bitrate_kbps": 128,
                            },
                            {
                                "enabled": True,
                                "quality": "high",
                                "mount": "/ai-high",
                                "stream_codec_profile": "aac_lc_320",
                                "stream_bitrate_kbps": 320,
                            },
                            {
                                "enabled": True,
                                "quality": "flac",
                                "mount": "/ai-flac",
                                "stream_codec_profile": "ogg_flac_lossless",
                                "stream_bitrate_kbps": 0,
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_bridge_adds_four_canonical_outputs_and_keeps_legacy(tmp_path):
    outputs = quality._quality_specs(_bridge(tmp_path), _station())
    assert [item.mount for item in outputs] == [
        "/ai", "/ai-low", "/ai-normal", "/ai-high", "/ai-flac"
    ]
    assert [item.codec_profile for item in outputs[1:]] == [
        "aac_lc_96", "aac_lc_128", "aac_lc_320", "ogg_flac_lossless"
    ]


def test_native_aac_and_flac_commands_are_exact(tmp_path):
    config = _config(tmp_path)
    outputs = quality._quality_specs(_bridge(tmp_path), _station())
    aac = quality.build_encoder_command(config, outputs[1])
    flac = quality.build_encoder_command(config, outputs[-1])
    assert ["-c:a", "aac"] == aac[aac.index("-c:a") : aac.index("-c:a") + 2]
    assert "aac_low" in aac and "96k" in aac and "adts" in aac
    assert "flac" in flac and "ogg" in flac and "-b:a" not in flac
    assert not any("password" in item.lower() for item in aac + flac)


def test_noncanonical_mount_is_rejected(tmp_path):
    path = _bridge(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["channels"][0]["outputs"][0]["mount"] = "/wrong-low"
    path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        quality._quality_specs(path, _station())
    except ValueError as exc:
        assert "non-canonical" in str(exc)
    else:
        raise AssertionError("non-canonical mount was accepted")


def test_full_pcm_queue_applies_lossless_backpressure(tmp_path):
    worker = quality.OutputWorker(
        _config(tmp_path), _station(), quality._quality_specs(_bridge(tmp_path), _station())[1],
        "secret", quality.DurableStatus(tmp_path / "state.json"), threading.Event(),
    )
    for index in range(worker.pcm.maxsize):
        worker.pcm.put_nowait(bytes([index % 256]))
    producer = threading.Thread(target=worker.offer, args=(b"next",))
    producer.start()
    threading.Event().wait(0.05)
    assert worker.pcm.qsize() == worker.pcm.maxsize
    assert producer.is_alive()
    assert worker.dropped_chunks == 0
    assert worker.pcm.get_nowait() == bytes([0])
    producer.join(timeout=1)
    assert not producer.is_alive()
    assert worker.pcm.get_nowait() == bytes([1])


def test_quality_supervisor_reuses_warmed_ai_program_and_reports_autonomy():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "playlist_generation=playlist.name" in source
    assert source.count("program, skipped_metadata = build_safe_program(") == 1
    assert 'f"{station.station_id}-autonomous-program.json"' in source
    assert 'scheduled_offsets = host_snapshot["scheduled_song_offsets"]' in source
    assert "build_track_decoder_command(self.config, item)" in source
    assert '"prebaked-transition-host"' in source
    assert "transition_library.choose(" in source
    assert "queue_preview(" in source
    assert "autonomous_selection=autonomous_selection_status" in source
    assert "host_queue.after_song(" in source
    assert "expected_song_context={" in source
    assert "host_queue.played(host_clip)" in source
    assert "_wait_for_all_station_warmups(station, workers, host_queue)" in source
    assert '"state": "waiting-for-all-stations"' in source
    assert "encoded audio sender stalled" in source


def test_prebaked_program_never_probes_or_calls_ollama(tmp_path, monkeypatch):
    first = tmp_path / "Rock" / "Artist One" / "First Song.flac"
    second = tmp_path / "Lo-Fi" / "Artist Two" / "Second Song.flac"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    station = replace(
        _station(),
        music_roots=(tmp_path / "Rock", tmp_path / "Lo-Fi"),
        prebaked_transition_host=True,
    )
    monkeypatch.setattr(quality, "discover_audio", lambda _station: [first, second])
    monkeypatch.setattr(
        quality,
        "track_metadata",
        lambda path: (path.stem.split(" ", 1)[-1], path.parent.name),
    )
    monkeypatch.setattr(
        quality,
        "assert_autonomous_song_ai_ready",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Ollama was probed")),
    )
    monkeypatch.setattr(
        quality,
        "autonomous_rotation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Ollama rotation was called")
        ),
    )

    selected, skipped = quality.build_safe_program(station)

    assert set(selected) == {first.resolve(), second.resolve()}
    assert skipped == 0


def test_validated_autonomous_program_survives_service_restart(tmp_path, monkeypatch):
    first = tmp_path / "Artist One - First Song.flac"
    second = tmp_path / "Artist Two - Second Song.flac"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    station = replace(_station(), music_roots=(tmp_path,))
    cache = tmp_path / "state" / "radiotedu-en-autonomous-program.json"
    monkeypatch.setattr(quality, "discover_audio", lambda _station: [first, second])
    monkeypatch.setattr(
        quality,
        "track_metadata",
        lambda path: (path.stem.split(" - ", 1)[1], path.stem.split(" - ", 1)[0]),
    )
    monkeypatch.setattr(
        quality,
        "autonomous_rotation",
        lambda *_args, **_kwargs: [second, first],
    )
    monkeypatch.setattr(quality, "assert_autonomous_song_ai_ready", lambda *_args: None)
    restored = []
    monkeypatch.setattr(
        quality,
        "mark_autonomous_program_restored",
        lambda *_args, **kwargs: restored.append(kwargs["program_items"]),
    )

    selected, skipped = quality.build_safe_program(station, cache)
    assert selected == [second, first]
    assert skipped == 0 and cache.is_file()

    def must_not_reselect(*_args, **_kwargs):
        raise AssertionError("valid saved AI program was unnecessarily regenerated")

    monkeypatch.setattr(quality, "autonomous_rotation", must_not_reselect)
    selected_again, skipped_again = quality.build_safe_program(station, cache)
    assert selected_again == [second.resolve(), first.resolve()]
    assert skipped_again == 0
    assert restored == [2]


import threading


def test_concurrent_durable_status_updates_do_not_share_a_temp_file(tmp_path):
    status = quality.DurableStatus(tmp_path / "state.json")
    errors = []

    def update(index):
        try:
            for sequence in range(20):
                status.update("station", f"branch-{index}", sequence=sequence)
        except Exception as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=update, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    payload = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert len(payload["stations"]["station"]["outputs"]) == 4
    assert list(tmp_path.glob("*.tmp")) == []


def test_status_write_retries_windows_sharing_violation(tmp_path, monkeypatch):
    real_replace = quality.os.replace
    attempts = 0

    def briefly_locked(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError("sharing violation")
            error.winerror = 5
            raise error
        return real_replace(source, target)

    monkeypatch.setattr(quality.os, "replace", briefly_locked)
    status = quality.DurableStatus(tmp_path / "state.json")
    status.update("station", "legacy", state="streaming")

    payload = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert payload["stations"]["station"]["outputs"]["legacy"]["state"] == "streaming"
    assert attempts == 3
    assert list(tmp_path.glob("*.tmp")) == []


def test_public_snapshot_matches_live_website_contract(tmp_path):
    config = _config(tmp_path)
    status = quality.DurableStatus(tmp_path / "state.json")
    status.update(
        "radiotedu-en",
        "legacy",
        state="streaming",
        audio_sent_at="2026-08-14T12:00:00Z",
        origin_listener_ready=True,
        now_playing={
            "kind": "music",
            "title": "Test song",
            "artist": None,
            "started_at": "2026-08-14T12:00:00+00:00",
        },
    )
    sync = quality.PublicApiSync(config, status, threading.Event())

    snapshot = sync._snapshot(
        _station(),
        datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc),
    )

    assert snapshot["stream"] == {
        "url": "https://stream.radiotedu.com/en",
        "mount": "/en",
        "status": "live",
        "codec": "MP3",
        "bitrate_kbps": 192,
        "public": True,
    }
    assert snapshot["station"]["id"] == "radiotedu-en"
    assert snapshot["now_playing"]["title"] == "Test song"
    assert snapshot["current_program"]["name"] == "Jazz Lab"
    assert "C:\\" not in json.dumps(snapshot)


def test_public_snapshot_localizes_live_program_for_french(tmp_path):
    config = _config(tmp_path)
    status = quality.DurableStatus(tmp_path / "state.json")
    sync = quality.PublicApiSync(config, status, threading.Event())
    sync.sequences["radiotedu-fr"] = sync.sequences["radiotedu-en"]
    station = replace(
        _station(),
        station_id="radiotedu-fr",
        mount="/fr",
        name="RadioTEDU FranÃƒÂ§ais",
        health_port=8766,
        public_mount="/fr",
        public_stream_url="https://stream.radiotedu.com/fr",
    )

    snapshot = sync._snapshot(
        station,
        datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc),
    )

    assert snapshot["current_program"]["name"] == "Laboratoire jazz"


def test_public_play_events_are_durable_and_include_platform_contract(tmp_path):
    config = _config(tmp_path)
    status = quality.DurableStatus(tmp_path / "state.json")
    sync = quality.PublicApiSync(config, status, threading.Event())

    sync.record_play(
        station_id="radiotedu-en",
        classification="music",
        duration_ms=123_000,
        occurred_at="2026-08-24T12:00:00+00:00",
        track_id="track-test",
        title="Warm Night",
        artist="RadioTEDU",
        genre="Pop",
        program_name="AI Music Rotation",
    )

    event = sync.play_outbox["radiotedu-en"][0]
    assert event["protocol"] == "radiotedu-platform/v1"
    assert event["event_type"] == "play.completed"
    assert event["classification"] == "music"
    assert event["duration_ms"] == 123_000
    assert event["genre"] == "Pop"
    persisted = json.loads((tmp_path / "public-sync-play-outbox.json").read_text())
    assert persisted["radiotedu-en"][0]["event_id"] == event["event_id"]


def test_public_play_queue_flushes_in_order_and_clears_durable_outbox(tmp_path, monkeypatch):
    config = _config(tmp_path)
    status = quality.DurableStatus(tmp_path / "state.json")
    sync = quality.PublicApiSync(config, status, threading.Event())
    sync.secrets["radiotedu-en"] = "secret"
    for title in ("First", "Second"):
        sync.record_play(
            station_id="radiotedu-en",
            classification="music",
            duration_ms=1000,
            occurred_at="2026-08-24T12:00:00+00:00",
            track_id=title.lower(),
            title=title,
            artist="Artist",
            genre="Pop",
            program_name="AI Music Rotation",
        )
    sent = []

    def fake_post(station_id, path, payload, **kwargs):
        sent.append((station_id, path, payload["track_title"]))
        return {"stored": True}

    monkeypatch.setattr(sync, "_post", fake_post)
    sync._flush_plays(_station())

    assert [item[2] for item in sent] == ["First", "Second"]
    assert sync.pending_play_count("radiotedu-en") == 0
    persisted = json.loads((tmp_path / "public-sync-play-outbox.json").read_text())
    assert persisted["radiotedu-en"] == []


def test_public_spoken_segments_are_durable_and_flush_in_order(tmp_path, monkeypatch):
    config = _config(tmp_path)
    status = quality.DurableStatus(tmp_path / "state.json")
    sync = quality.PublicApiSync(config, status, threading.Event())
    sync.secrets["radiotedu-en"] = "secret"
    now = datetime.now(timezone.utc).isoformat()
    for transcript in ("First Radio Ted You link.", "Hope you have a lovely day."):
        sync.record_spoken_segment(
            station_id="radiotedu-en",
            transcript=transcript,
            occurred_at=now,
            completed_at=now,
            duration_ms=3_200,
            language="en",
            segment_type="track_link",
            well_wish=transcript.startswith("Hope"),
            program_id="campus-flow",
            program_name="Campus Flow",
            preceding_track={"track_id": "track-a", "title": "Before", "artist": "Artist A"},
            following_track={"track_id": "track-b", "title": "After", "artist": "Artist B"},
            text_model="qwen3:0.6b",
            tts_engine="kokoro",
            tts_model="Kokoro-82M",
            voice_design="en-day",
            source="ai_song_context",
        )

    pending = json.loads((tmp_path / "public-sync-spoken-outbox.json").read_text())
    assert [item["transcript"] for item in pending["radiotedu-en"]] == [
        "First Radio Ted You link.",
        "Hope you have a lovely day.",
    ]
    assert pending["radiotedu-en"][1]["well_wish"] is True

    restored = quality.PublicApiSync(config, status, threading.Event())
    restored.secrets["radiotedu-en"] = "secret"
    sent = []

    def fake_post(station_id, path, payload, **kwargs):
        sent.append((station_id, path, payload["transcript"]))
        return {"stored": True}

    monkeypatch.setattr(restored, "_post", fake_post)
    restored._flush_spoken_segments(_station())

    assert sent == [
        (
            "radiotedu-en",
            "/v1/radio/stations/radiotedu-en/spoken-segments",
            "First Radio Ted You link.",
        ),
        (
            "radiotedu-en",
            "/v1/radio/stations/radiotedu-en/spoken-segments",
            "Hope you have a lovely day.",
        ),
    ]
    assert restored.pending_spoken_segment_count("radiotedu-en") == 0
    persisted = json.loads((tmp_path / "public-sync-spoken-outbox.json").read_text())
    assert persisted["radiotedu-en"] == []


def test_restart_restores_only_real_single_laya_choices(tmp_path):
    catalog = [tmp_path / f"song-{i}.wav" for i in range(6)]
    events = []
    for i, path in enumerate(catalog):
        events.append({
            "station_id": "radiotedu-en", "program_id": "live",
            "decision_schema_version": "radio-song-choice-library-shortlist-single-v1",
            "selection_mode": "laya_library_shortlist_single_next_track",
            "event_id": f"real-{i}", "model_output_raw_json": "{actual-model-output}",
            "selected_track_id": quality.public_track_id(path),
            "program_selection": [{"track_id": quality.public_track_id(path), "selection_basis": "typed_model_choice"}],
        })
    events[-1]["program_selection"][0]["selection_basis"] = "probability_ranked_batch"
    restored = quality.restore_laya_startup_tracks(catalog, "radiotedu-en", "live", events)
    # The first song plus four future songs all retain real single-choice evidence.
    assert restored == [path.resolve() for path in catalog[:5]]
    assert quality.restore_laya_startup_tracks(catalog, "radiotedu-fr", "live", events) == []
    assert quality.restore_laya_startup_tracks(catalog, "radiotedu-en", "other", events) == []


def test_editorial_preference_change_preserves_live_safety_queue(tmp_path):
    catalog = [tmp_path / f"song-{i}.wav" for i in range(6)]
    queue = quality.LiveLayaSongQueue(
        station=_station(), catalog=catalog, initial_tracks=catalog[:4],
        selection_context={"id": "old"}, context_provider=lambda: {"id": "new"},
        decision_sink=lambda event: True, stop=threading.Event(),
    )
    current = queue.next_track(timeout=0)
    queue.update_context({"id": "new"})
    assert queue.peek() == [path.resolve() for path in catalog[1:4]]
    assert queue.snapshot()["current_track_id"] == quality.public_track_id(current)


def test_live_queue_uses_cached_track_ids_without_catalog_filesystem_work(tmp_path, monkeypatch):
    catalog = [tmp_path / f"song-{i}.wav" for i in range(100)]
    queue = quality.LiveLayaSongQueue(
        station=_station(), catalog=catalog, initial_tracks=catalog[:5],
        selection_context={"id": "live"}, context_provider=lambda: {"id": "live"},
        decision_sink=lambda event: True, stop=threading.Event(),
    )
    queue.next_track(timeout=0)
    def forbid_resolving_catalog(_path):
        raise AssertionError("live queue must use precomputed opaque IDs")
    monkeypatch.setattr(quality, "public_track_id", forbid_resolving_catalog)
    monkeypatch.setattr(quality, "autonomous_rotation", lambda files, _station, **kwargs: [files[0]])
    queue._choose_next()
    assert queue.snapshot()["queue_depth"] == 4
    assert queue.snapshot()["last_error"] == ""
