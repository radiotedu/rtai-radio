from __future__ import annotations

import wave
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from backend.config import Settings
from backend.database import connect, init_db, now_iso
from backend.orchestrator import AutonomousOrchestrator
from backend.radio_agent import RadioAgent
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
    assert reported["air_ready"] == coverage.air_ready


def test_orchestrator_maintains_one_render_item_then_plays_ready_rundown(
    tmp_path: Path,
) -> None:
    settings = runtime_settings(tmp_path)
    insert_tracks(settings)
    agent = RadioAgent(settings)
    orchestrator = AutonomousOrchestrator(settings, agent)
    coverage = CoverageStatus(240, 120, 180, False, True)
    render_limits: list[int] = []

    def maintain_rundown(max_render_items: int = 1) -> CoverageStatus:
        render_limits.append(max_render_items)
        return coverage

    agent.maintain_rundown = maintain_rundown
    agent.queue_next_ready_rundown_item = lambda: {"started": True, "item_type": "track"}
    orchestrator._maybe_update_strategy = lambda _track_count: False

    result = orchestrator.tick()

    assert result["played"] is True
    assert result["coverage"] == asdict(coverage)
    assert render_limits == [1]


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
