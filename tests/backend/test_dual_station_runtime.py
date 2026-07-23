from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.config import Settings
from backend.stations.context import build_station_context
from backend.stations.loader import load_station_profiles


class RecordingAgent:
    def __init__(self, context) -> None:
        self.context = context
        self.starts = 0
        self.stops = 0

    def start(self) -> dict:
        self.starts += 1
        return {"started": True, "source": "music"}

    def stop(self) -> dict:
        self.stops += 1
        return {"stopped": True}


class FailingAutonomy:
    def __init__(self, context) -> None:
        self.context = context
        self.starts = 0
        self.stops = 0

    def start_background(self) -> dict:
        self.starts += 1
        raise RuntimeError("AI is unavailable")

    def stop_background(self) -> dict:
        self.stops += 1
        return {"running": False}


class RecordingPusher:
    def __init__(self, context) -> None:
        self.context = context
        self.starts = 0
        self.stops = 0

    def start_background(self) -> dict:
        self.starts += 1
        return {"running": True}

    def stop_background(self) -> dict:
        self.stops += 1
        return {"running": False}


def station_context(tmp_path: Path, station_id: str = "radiotedu-en"):
    project_root = Path(__file__).resolve().parents[2]
    profile = load_station_profiles(project_root / "config" / "stations")[station_id]
    data_root = tmp_path / "data" / "stations" / station_id
    runtime = replace(
        profile.runtime,
        data_root=str(data_root),
        database=str(data_root / "radio.db"),
        music_root=str(tmp_path / "media" / "stations" / station_id / "music"),
        announcement_root=str(data_root / "announcements"),
        cache_root=str(data_root / "cache"),
        log_root=str(data_root / "logs"),
    )
    return build_station_context(
        Settings(static_dir=str(data_root / "public"), autonomy_enabled=False),
        replace(profile, runtime=runtime),
    )


def test_station_runtime_keeps_music_available_when_ai_lifecycle_fails() -> None:
    from backend.runtime.station_runtime import StationRuntime

    context = SimpleNamespace(profile=SimpleNamespace(station_id="radiotedu-en"))
    agent = RecordingAgent(context)
    autonomy = FailingAutonomy(context)
    pusher = RecordingPusher(context)
    runtime = StationRuntime(context, agent, autonomy, pusher)

    started = runtime.start()
    stopped = runtime.stop()

    assert started["station_id"] == "radiotedu-en"
    assert started["music"] == {"started": True, "source": "music"}
    assert started["autonomy"] == {"running": False, "reason": "unavailable"}
    assert agent.starts == 1
    assert autonomy.starts == 1
    assert pusher.starts == 1
    assert stopped["music"] == {"stopped": True}
    assert agent.stops == 1
    assert autonomy.stops == 1
    assert pusher.stops == 1


def test_station_runtime_starts_and_stops_each_owned_component_once() -> None:
    from backend.runtime.station_runtime import StationRuntime

    context = SimpleNamespace(profile=SimpleNamespace(station_id="radiotedu-fr"))
    agent = RecordingAgent(context)
    autonomy = RecordingPusher(context)
    pusher = RecordingPusher(context)
    runtime = StationRuntime(context, agent, autonomy, pusher)

    runtime.start()
    runtime.start()
    runtime.stop()
    runtime.stop()

    assert agent.starts == 1
    assert autonomy.starts == 1
    assert pusher.starts == 1
    assert agent.stops == 1
    assert autonomy.stops == 1
    assert pusher.stops == 1


def test_app_delegates_public_state_lifecycle_to_its_station_runtime(tmp_path: Path, monkeypatch) -> None:
    from backend.app import create_app

    monkeypatch.chdir(tmp_path)
    context = station_context(tmp_path)
    pusher = RecordingPusher(context)
    app = create_app(station_context=context, snapshot_pusher=pusher)

    with TestClient(app):
        assert app.state.runtime.context is context

    assert pusher.starts == 1
    assert pusher.stops == 1


def test_station_liquidsoap_templates_keep_en_and_fr_runtime_artifacts_isolated(tmp_path: Path) -> None:
    from backend.liquidsoap import render_liquidsoap_config

    en = render_liquidsoap_config(station_context(tmp_path, "radiotedu-en").settings)
    fr = render_liquidsoap_config(station_context(tmp_path, "radiotedu-fr").settings)

    assert en["mount"] == "/en"
    assert fr["mount"] == "/fr"
    assert en["credentials_environment"] == "RADIOTEDU_EN_SOURCE_CREDENTIALS"
    assert fr["credentials_environment"] == "RADIOTEDU_FR_SOURCE_CREDENTIALS"
    assert en["source_ids"] == {
        "primary": "radiotedu-en-primary",
        "fallback": "radiotedu-en-fallback",
    }
    assert fr["source_ids"] == {
        "primary": "radiotedu-fr-primary",
        "fallback": "radiotedu-fr-fallback",
    }

    for selector in ("queue_path", "fallback_queue_path", "script_path", "pid_path"):
        assert en[selector] != fr[selector]
    assert set(en["log_paths"].values()).isdisjoint(fr["log_paths"].values())

    for rendered in (en, fr):
        script = Path(rendered["script_path"]).read_text(encoding="utf-8")
        assert rendered["source_host"] == "10.98.98.75"
        assert rendered["source_port"] == 11154
        assert rendered["source_user"] == "source"
        assert rendered["encoder_profile"] == "aac_192"
        assert rendered["codec"] == "AAC-LC"
        assert rendered["bitrate_kbps"] == 192
        assert rendered["public_listing"] is True
        assert rendered["credentials_environment"] in script
        assert "environment.get(" in script
        assert "hackme" not in script
        assert '%ffmpeg(format="adts", %audio(codec="aac", b="192k", ac=2, ar=48000))' in script
        assert 'user="source"' in script
        assert "public=true" in script
        assert "%mp3" not in script
        assert Path(rendered["queue_path"]).as_posix() in script
        assert Path(rendered["fallback_queue_path"]).as_posix() in script
        for source_id in rendered["source_ids"].values():
            assert source_id in script
        assert "crossfade(" in script
        assert "blank.detect(" in script
        assert "blank.skip(" in script
        assert Path(rendered["fallback_queue_path"]).exists()
        assert rendered["fallback_coverage_seconds"] == 0
        assert rendered["fallback_required_seconds"] == 21_600
        assert rendered["fallback_air_ready"] is False


def test_aac_192_preflight_rejects_liquidsoap_without_ffmpeg() -> None:
    from backend.liquidsoap import liquidsoap_encoder_preflight

    missing = liquidsoap_encoder_preflight(
        Settings(liquidsoap_encoder_profile="aac_192"),
        build_config="AAC: yes\nFDK-AAC: yes\nFFmpeg: no",
    )
    supported = liquidsoap_encoder_preflight(
        Settings(liquidsoap_encoder_profile="aac_192"),
        build_config="AAC: no\nFDK-AAC: no\nFFmpeg: yes",
    )

    assert missing == {
        "encoder_profile": "aac_192",
        "encoder_supported": False,
        "reason": "ffmpeg_unavailable",
    }
    assert supported == {
        "encoder_profile": "aac_192",
        "encoder_supported": True,
        "reason": None,
    }
