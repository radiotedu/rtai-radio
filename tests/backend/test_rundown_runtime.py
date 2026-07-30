from __future__ import annotations

import json
import wave
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.config import Settings
from backend.database import connect, init_db, now_iso
from backend.imaging.library import ImagingLibrary, import_imaging
from backend.orchestrator import AutonomousOrchestrator
from backend.radio_agent import JINGLE_TITLE, RadioAgent
from backend.rundown import CoverageStatus
from backend.scheduler import current_program


def runtime_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        database_path=str(tmp_path / "radio.db"),
        music_dir=str(tmp_path / "music"),
        static_dir=str(tmp_path / "static"),
        liquidsoap_queue_path=str(tmp_path / "liquidsoap" / "queue.m3u"),
        playback_backend="simulate",
        min_ready_announcements=0,
        max_ready_announcements=1,
        rundown_planned_seconds=240,
        rundown_rendered_seconds=120,
        rundown_refill_seconds=120,
        fallback_coverage_seconds=180,
    )
    init_db(settings)
    return settings


def insert_tracks(settings: Settings, count: int = 4, duration: float = 120.0) -> None:
    timestamp = now_iso()
    with connect(settings) as conn:
        for index in range(count):
            path = settings.music_path / f"Artist {index} - Track {index}.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"RIFF-ready")
            conn.execute(
                """
                insert into tracks (
                    title, artist, album, genre, mood, bpm, duration_seconds,
                    file_path, cover_path, last_played_at, play_count, created_at, updated_at
                ) values (?, ?, null, 'pop', 'bright', 112, ?, ?, null, null, 0, ?, ?)
                """,
                (f"Track {index}", f"Artist {index}", duration, str(path), timestamp, timestamp),
            )
        conn.commit()


def write_wav(path: Path, duration_seconds: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_rate = 8_000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(frame_rate)
        output.writeframes(b"\0\0" * int(frame_rate * duration_seconds))


def drain_without_sleep(agent: RadioAgent, played: list) -> None:
    def play_next():
        if not agent.playback.queue:
            agent.playback.now_playing = None
            return None
        item = agent.playback.queue.pop(0)
        agent.playback.now_playing = item
        played.append(item)
        return item

    agent.playback.play_next = play_next


def import_test_jingles(tmp_path: Path, count: int = 6) -> Path:
    source = tmp_path / "jingle-source"
    source.mkdir()
    for index in range(count):
        write_wav(source / f"jingle-{index}.wav", duration_seconds=0.1 + index * 0.01)
    release = tmp_path / "imaging-release"
    import_imaging(source, release, station_id="radiotedu-en", category="jingle")
    return release


def insert_completed_track_plays(settings: Settings, count: int) -> None:
    with connect(settings) as conn:
        track_id = int(conn.execute("select id from tracks order by id limit 1").fetchone()[0])
        program = current_program(settings)
        for _ in range(count):
            conn.execute(
                """
                insert into play_history (
                    track_id, program_id, played_at, duration_seconds, source
                ) values (?, ?, ?, 120, 'local_file')
                """,
                (track_id, program["id"], now_iso()),
            )
        conn.commit()


def test_agent_maintains_duration_coverage_and_compatibility_readiness(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)
    insert_tracks(settings)
    agent = RadioAgent(settings)

    coverage = agent.maintain_rundown(max_render_items=0)
    readiness = agent.announcement_readiness()

    assert coverage.planned_seconds >= 240
    assert coverage.rendered_seconds >= 120
    assert coverage.fallback_seconds >= 180
    assert coverage.air_ready is True
    assert readiness["planned_seconds"] == coverage.planned_seconds
    assert readiness["rendered_seconds"] == coverage.rendered_seconds
    assert readiness["fallback_seconds"] == coverage.fallback_seconds
    assert readiness["air_ready"] is True
    assert readiness["ready_to_broadcast"] is True


def test_station_jingles_cycle_all_assets_after_each_three_completed_tracks(
    tmp_path: Path,
) -> None:
    settings = runtime_settings(tmp_path)
    settings.jingle_enabled = True
    settings.jingle_interval_tracks = 3
    settings.imaging_release_root = str(import_test_jingles(tmp_path))
    insert_tracks(settings, count=1)
    agent = RadioAgent(settings)
    library = ImagingLibrary.open(settings.imaging_release_root, "radiotedu-en")
    selected_paths: list[str] = []

    for _ in range(6):
        insert_completed_track_plays(settings, 3)
        selected = agent._next_jingle_item()
        assert selected is not None
        selected_paths.append(selected.file_path)
        with connect(settings) as conn:
            conn.execute(
                """
                insert into station_public_events (
                    event_type, occurred_at, classification, duration_seconds,
                    program_id, title, metadata_json
                ) values ('play.completed', ?, 'music', ?, null, ?, '{}')
                """,
                (now_iso(), selected.duration_seconds, JINGLE_TITLE),
            )
            conn.commit()

    assert selected_paths == [str(path) for path in library.asset_paths()]
    assert agent._next_jingle_item() is None


def test_due_jingle_plays_before_ready_music_and_is_publicly_classified(
    tmp_path: Path,
) -> None:
    settings = runtime_settings(tmp_path)
    settings.jingle_enabled = True
    settings.jingle_interval_tracks = 3
    settings.imaging_release_root = str(import_test_jingles(tmp_path, count=1))
    insert_tracks(settings, count=1)
    insert_completed_track_plays(settings, 3)
    agent = RadioAgent(settings)
    agent.maintain_rundown(max_render_items=0)
    played: list = []
    drain_without_sleep(agent, played)

    result = agent.queue_next_ready_rundown_item()

    assert result["started"] is True
    assert [item.item_type for item in played] == ["imaging", "track"]
    with connect(settings) as conn:
        event = conn.execute(
            """
            select classification, title from station_public_events
            where title=? order by id desc limit 1
            """,
            (JINGLE_TITLE,),
        ).fetchone()
    assert dict(event) == {"classification": "music", "title": JINGLE_TITLE}


def test_operator_observability_reports_station_duration_coverage(tmp_path: Path) -> None:
    from backend.app import observability

    settings = runtime_settings(tmp_path)
    insert_tracks(settings)
    agent = RadioAgent(settings)
    coverage = agent.maintain_rundown(max_render_items=0)

    status = observability(settings, agent)

    reported = status["rundown_coverage"]
    assert 0 <= coverage.planned_seconds - reported["planned_seconds"] <= 1
    assert 0 <= coverage.rendered_seconds - reported["rendered_seconds"] <= 1
    assert reported["fallback_seconds"] == coverage.fallback_seconds
    assert reported["needs_refill"] == coverage.needs_refill
    policy = agent.rundown_planner.policy
    assert reported["air_ready"] == (
        reported["planned_seconds"] >= policy.planned_seconds
        and reported["rendered_seconds"] >= policy.rendered_seconds
        and reported["fallback_seconds"] >= policy.fallback_seconds
    )


def test_orchestrator_maintains_one_render_item_then_plays_ready_rundown(
    tmp_path: Path,
) -> None:
    settings = runtime_settings(tmp_path)
    insert_tracks(settings)
    agent = RadioAgent(settings)
    orchestrator = AutonomousOrchestrator(settings, agent)
    coverage = CoverageStatus(240, 120, 180, False, True)
    render_limits: list[int] = []
    reconciliations: list[bool] = []

    def maintain_rundown(max_render_items: int = 1) -> CoverageStatus:
        render_limits.append(max_render_items)
        return coverage

    agent.maintain_rundown = maintain_rundown
    agent.reconcile_playing_rundown = lambda: reconciliations.append(True) or {"completed": False}
    agent.queue_next_ready_rundown_item = lambda: {"started": True, "item_type": "track"}
    orchestrator._maybe_update_strategy = lambda _track_count: False

    result = orchestrator.tick()

    assert result["played"] is True
    assert result["coverage"] == asdict(coverage)
    assert render_limits == [1]
    assert reconciliations == [True]


def test_ready_music_plays_when_search_llm_and_qwen_are_unavailable(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)
    insert_tracks(settings)
    agent = RadioAgent(settings)
    agent.maintain_rundown(max_render_items=0)
    played: list = []
    drain_without_sleep(agent, played)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("dependency unavailable")

    agent._web_context = unavailable
    agent._narrate = unavailable
    agent._synthesize_qwen = unavailable

    result = agent.queue_next_ready_rundown_item()

    assert result["started"] is True
    assert result["editorial_mode"] == "music_only"
    assert [item.item_type for item in played] == ["track"]


def test_talkover_render_failure_falls_back_to_speech_then_track(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)
    insert_tracks(settings)
    agent = RadioAgent(settings)
    agent.maintain_rundown(max_render_items=0)
    program = current_program(settings)
    speech_path = tmp_path / "speech.wav"
    write_wav(speech_path)
    with connect(settings) as conn:
        conn.execute(
            """
            insert into announcement_queue (
                text, file_path, status, program_id, source, created_at, metadata_json
            ) values ('Have a good day from Radio TED U.', ?, 'ready', ?, 'test', ?, '{}')
            """,
            (str(speech_path), program["id"], now_iso()),
        )
        conn.commit()

    class FailingRenderer:
        def render(self, *_args, **_kwargs):
            raise RuntimeError("ffmpeg unavailable")

    agent.talkover_renderer = FailingRenderer()
    played: list = []
    drain_without_sleep(agent, played)

    result = agent.queue_next_ready_rundown_item()

    assert result["started"] is True
    assert result["transition"] == "sequential"
    assert [item.item_type for item in played] == ["tts", "track"]
    with connect(settings) as conn:
        transition = conn.execute(
            "select transition_kind, cue_source, duck_db from rundown_transitions order by id desc limit 1"
        ).fetchone()
    assert dict(transition) == {
        "transition_kind": "sequential",
        "cue_source": "default",
        "duck_db": -11.0,
    }


def test_track_specific_announcement_cannot_attach_to_another_track(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)
    insert_tracks(settings)
    agent = RadioAgent(settings)
    agent.maintain_rundown(max_render_items=0)
    program = current_program(settings)
    speech_path = tmp_path / "bound-speech.wav"
    write_wav(speech_path)
    with connect(settings) as conn:
        target_track_id = conn.execute(
            "select track_id from rundown_items where state='ready' order by planned_start, id limit 1"
        ).fetchone()[0]
        other_track_id = conn.execute(
            "select id from tracks where id != ? order by id limit 1",
            (target_track_id,),
        ).fetchone()[0]
        track_ids = [target_track_id, other_track_id]
        for created_at, track_id in (
            ("2026-07-05T00:00:00+00:00", track_ids[1]),
            ("2026-07-05T00:00:01+00:00", track_ids[0]),
        ):
            conn.execute(
                """
                insert into announcement_queue (
                    text, file_path, status, program_id, source, created_at, metadata_json
                ) values ('Prepared track intro', ?, 'ready', ?, 'test', ?, ?)
                """,
                (str(speech_path), program["id"], created_at, json.dumps({"track_id": track_id})),
            )
        conn.commit()
    played: list = []
    drain_without_sleep(agent, played)

    result = agent.queue_next_ready_rundown_item()

    assert result["track_id"] == track_ids[0]
    with connect(settings) as conn:
        statuses = {
            json.loads(row["metadata_json"])["track_id"]: row["status"]
            for row in conn.execute(
                "select status, metadata_json from announcement_queue order by id"
            ).fetchall()
        }
    assert statuses[track_ids[0]] == "used"
    assert statuses[track_ids[1]] == "ready"


def test_runtime_uses_curated_track_cue_before_default_window(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)
    insert_tracks(settings)
    agent = RadioAgent(settings)
    agent.maintain_rundown(max_render_items=0)
    program = current_program(settings)
    speech_path = tmp_path / "curated-cue-speech.wav"
    write_wav(speech_path)
    with connect(settings) as conn:
        track_id = conn.execute(
            "select track_id from rundown_items where state='ready' order by planned_start, id limit 1"
        ).fetchone()[0]
        conn.execute(
            "update tracks set cue_source='curated', intro_end_seconds=5.0, intro_confidence=1.0 where id=?",
            (track_id,),
        )
        conn.execute(
            """
            insert into announcement_queue (
                text, file_path, status, program_id, source, created_at, metadata_json
            ) values ('Prepared curated intro', ?, 'ready', ?, 'test', ?, ?)
            """,
            (str(speech_path), program["id"], now_iso(), json.dumps({"track_id": track_id})),
        )
        conn.commit()

    class RecordingRenderer:
        decision = None

        def render(self, _speech, _track, output, decision):
            self.decision = decision
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_bytes(b"mixed")
            return Path(output)

    renderer = RecordingRenderer()
    agent.talkover_renderer = renderer
    played: list = []
    drain_without_sleep(agent, played)

    result = agent.queue_next_ready_rundown_item()

    assert result["transition"] == "talk_over"
    assert renderer.decision.cue_source.value == "curated"
    with connect(settings) as conn:
        transition = conn.execute(
            "select cue_source, cue_confidence from rundown_transitions order by id desc limit 1"
        ).fetchone()
    assert dict(transition) == {"cue_source": "curated", "cue_confidence": 1.0}


def test_liquidsoap_submission_stays_playing_until_duration_elapses(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import backend.playback as playback_module

    settings = runtime_settings(tmp_path)
    insert_tracks(settings)
    agent = RadioAgent(settings)
    agent.maintain_rundown(max_render_items=0)
    submitted: list[str] = []
    monkeypatch.setattr(
        playback_module,
        "append_liquidsoap_item",
        lambda _settings, file_path: submitted.append(file_path),
    )
    agent.playback.backend = "liquidsoap"

    first = agent.queue_next_ready_rundown_item()
    blocked = agent.queue_next_ready_rundown_item()
    with connect(settings) as conn:
        state_before = conn.execute(
            "select state from rundown_items where id=?", (first["rundown_item_id"],)
        ).fetchone()[0]
        plays_before = conn.execute("select count(*) from play_history").fetchone()[0]

    assert first["started"] is True
    assert submitted
    assert blocked == {"started": False, "reason": "rundown_item_playing"}
    assert state_before == "playing"
    assert plays_before == 0
    assert agent.playback.now_playing is not None

    completed = agent.reconcile_playing_rundown(
        datetime.now(timezone.utc) + timedelta(minutes=10)
    )
    with connect(settings) as conn:
        state_after = conn.execute(
            "select state from rundown_items where id=?", (first["rundown_item_id"],)
        ).fetchone()[0]
        plays_after = conn.execute("select count(*) from play_history").fetchone()[0]

    assert completed["completed"] is True
    assert state_after == "completed"
    assert plays_after == 1
    assert agent.playback.now_playing is None


def test_station_runtime_services_share_the_injected_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from backend.runtime.station_runtime import create_station_runtime
    from backend.stations.context import build_station_context
    from backend.stations.loader import load_station_profiles

    monkeypatch.chdir(tmp_path)
    settings = runtime_settings(tmp_path)
    profile = load_station_profiles(Path(__file__).resolve().parents[2] / "config" / "stations")["radiotedu-fr"]
    data_root = tmp_path / "stations" / "radiotedu-fr"
    profile = replace(
        profile,
        runtime=replace(
            profile.runtime,
            data_root=str(data_root),
            database=str(data_root / "radio.db"),
            music_root=str(tmp_path / "media" / "stations" / "radiotedu-fr" / "music"),
            announcement_root=str(data_root / "announcements"),
            cache_root=str(data_root / "cache"),
            log_root=str(data_root / "logs"),
        ),
    )
    context = build_station_context(
        replace(settings, station_id="radiotedu-fr"),
        profile,
    )
    monkeypatch.setattr(
        "backend.radio_agent.build_tts_provider",
        lambda _context: SimpleNamespace(provider_name="qwen"),
    )

    runtime = create_station_runtime(context)

    assert runtime.agent.rundown_planner.context is context
    assert runtime.agent.fallback_playlist.context is context
    assert runtime.agent.context is context
    assert runtime.orchestrator.context is context
