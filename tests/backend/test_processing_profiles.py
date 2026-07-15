from __future__ import annotations

from pathlib import Path

import pytest

from backend.audio.processing import ProcessingProfile
from backend.config import Settings
from backend.liquidsoap import render_liquidsoap_config
from backend.stations.models import AudioProfile


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=str(tmp_path / "radiotedu.db"),
        music_dir=str(tmp_path / "music"),
        static_dir=str(tmp_path / "static"),
        liquidsoap_queue_path=str(tmp_path / "queue.m3u"),
        liquidsoap_script_path=str(tmp_path / "radiotedu.liq"),
        liquidsoap_mount="/radiotedu",
        liquidsoap_host="127.0.0.1",
        liquidsoap_port=8000,
        liquidsoap_icecast_user="source",
        liquidsoap_icecast_password="test-password",
        liquidsoap_encoder_profile="aac_192",
        liquidsoap_public=True,
    )


def test_station_audio_profile_carries_a_safe_processing_preset() -> None:
    english = ProcessingProfile(name="english", input_gain_db=-0.5)
    french = ProcessingProfile(name="french", input_gain_db=0.0)
    audio = AudioProfile(
        stream_mount="/radiotedu-en",
        loudness_lufs=-16,
        true_peak_dbtp=-1,
        minimum_qwen_buffer=2,
        processing=english,
    )

    assert audio.processing is english
    assert english != french
    assert english.target_lufs == -16.0
    assert english.loudness_tolerance_lu == 1.0
    assert english.true_peak_ceiling_dbtp == -1.0
    assert english.stage_names == (
        "input_level_control",
        "gentle_wideband_agc",
        "restrained_multiband_dynamics",
        "true_peak_limiter",
        "encoder",
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"target_lufs": -18.0}, "target_lufs"),
        ({"true_peak_ceiling_dbtp": -0.5}, "true_peak_ceiling_dbtp"),
        ({"wideband_agc_max_gain_db": 7.0}, "wideband_agc_max_gain_db"),
        ({"multiband_ratio": 2.5}, "multiband_ratio"),
    ],
)
def test_processing_profile_rejects_out_of_policy_settings(
    kwargs: dict[str, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ProcessingProfile(**kwargs)


def test_rendered_liquidsoap_pipeline_uses_only_the_safe_processing_order(tmp_path: Path) -> None:
    rendered = render_liquidsoap_config(
        _settings(tmp_path),
        processing_profile=ProcessingProfile(name="english", input_gain_db=-0.5),
    )
    script = Path(rendered["script_path"]).read_text(encoding="utf-8")

    stages = (
        "input level control",
        "gentle wideband agc",
        "restrained multiband dynamics",
        "final true-peak limiter",
        "encoder",
    )
    positions = [script.lower().index(stage) for stage in stages]
    assert positions == sorted(positions)
    assert "target=-16.0" in script
    assert "gain_max=3.0" in script
    assert "ratio=1.5" in script
    assert "threshold=-1.0" in script
    assert "output.icecast(" in script
    for forbidden in ("hard clip", "stereo widen", "bass enhance", "exciter", "source-file normalization"):
        assert forbidden not in script.lower()
