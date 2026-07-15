from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend.audio.segue_policy import CueSource, SegueDecision, SegueKind
from backend.audio.talkover_renderer import (
    TalkOverRenderError,
    TalkOverRenderer,
    build_talkover_command,
)
from backend.config import Settings
from backend.playback import PlaybackController, QueueItem


def decision() -> SegueDecision:
    return SegueDecision(
        kind=SegueKind.TALK_OVER,
        overlap_seconds=5.0,
        speech_start_seconds=0.25,
        speech_end_seconds=5.25,
        cue_source=CueSource.DEFAULT,
        duck_db=-11.0,
        reason="test default opening",
    )


def test_ffmpeg_command_ducks_music_and_preserves_track_duration(tmp_path: Path):
    command = build_talkover_command(
        speech_path=tmp_path / "speech.wav",
        track_path=tmp_path / "track.wav",
        output_path=tmp_path / "mixed.wav",
        speech_start_seconds=0.25,
        speech_end_seconds=5.25,
        duck_db=-11.0,
    )

    rendered = " ".join(command)
    assert "volume" in rendered and "-11.0dB" in rendered
    assert "adelay=250" in rendered
    assert "amix=inputs=2:duration=first" in rendered


def test_renderer_runs_without_shell_and_validates_full_track_duration(tmp_path: Path):
    speech = tmp_path / "speech.wav"
    track = tmp_path / "track.wav"
    output = tmp_path / "mixed.wav"
    speech.write_bytes(b"speech")
    track.write_bytes(b"track")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[-1]).write_bytes(b"mixed")
        return SimpleNamespace(returncode=0, stderr="")

    def analyzer(path):
        return SimpleNamespace(duration_seconds=240.0)

    rendered = TalkOverRenderer(runner=runner, analyzer=analyzer).render(
        speech,
        track,
        output,
        decision(),
    )

    assert rendered == output
    assert output.is_file()
    assert calls[0][1].get("shell") is False


def test_renderer_failure_preserves_sequential_speech_then_track(tmp_path: Path):
    settings = Settings(
        database_path=str(tmp_path / "radio.db"),
        music_dir=str(tmp_path / "music"),
        static_dir=str(tmp_path / "static"),
        playback_backend="simulate",
    )
    playback = PlaybackController(settings)
    speech = QueueItem("tts", "Radio TED U", str(tmp_path / "speech.wav"), 5.0)
    track = QueueItem(
        "track",
        "Levitating",
        str(tmp_path / "track.wav"),
        240.0,
        artist="Dua Lipa",
        track_id=7,
    )

    class FailingRenderer:
        def render(self, *_args, **_kwargs):
            raise TalkOverRenderError("ffmpeg failed")

    mixed = playback.queue_talkover(
        speech,
        track,
        output_path=tmp_path / "mixed.wav",
        decision=decision(),
        renderer=FailingRenderer(),
    )

    assert mixed is False
    assert [item.item_type for item in playback.queue] == ["tts", "track"]
    assert playback.queue[1].file_path == track.file_path


def test_renderer_success_queues_one_full_duration_composite(tmp_path: Path):
    settings = Settings(
        database_path=str(tmp_path / "radio.db"),
        music_dir=str(tmp_path / "music"),
        static_dir=str(tmp_path / "static"),
        playback_backend="simulate",
    )
    playback = PlaybackController(settings)
    speech = QueueItem("tts", "Radio TED U", str(tmp_path / "speech.wav"), 5.0)
    track = QueueItem("track", "Levitating", str(tmp_path / "track.wav"), 240.0)
    output = tmp_path / "mixed.wav"

    class SuccessfulRenderer:
        def render(self, *_args, **_kwargs):
            output.write_bytes(b"mixed")
            return output

    mixed = playback.queue_talkover(
        speech,
        track,
        output_path=output,
        decision=decision(),
        renderer=SuccessfulRenderer(),
    )

    assert mixed is True
    assert len(playback.queue) == 1
    assert playback.queue[0].item_type == "talkover_composite"
    assert playback.queue[0].duration_seconds == track.duration_seconds
