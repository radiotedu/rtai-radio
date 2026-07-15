from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from .catalog_analyzer import analyze_audio
from .segue_policy import SegueDecision, SegueKind


class TalkOverRenderError(RuntimeError):
    """A composite could not be rendered and sequential playout must be used."""


def build_talkover_command(
    *,
    speech_path: str | Path,
    track_path: str | Path,
    output_path: str | Path,
    speech_start_seconds: float,
    speech_end_seconds: float,
    duck_db: float,
) -> list[str]:
    delay_ms = round(speech_start_seconds * 1000)
    gain = f"{duck_db:.1f}dB"
    filter_graph = (
        f"[0:a]volume='{gain}':enable='between(t,{speech_start_seconds:.3f},{speech_end_seconds:.3f})'[music];"
        f"[1:a]adelay={delay_ms}|{delay_ms}[voice];"
        "[music][voice]amix=inputs=2:duration=first:dropout_transition=0[mix]"
    )
    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-i",
        str(track_path),
        "-i",
        str(speech_path),
        "-filter_complex",
        filter_graph,
        "-map",
        "[mix]",
        str(output_path),
    ]


class TalkOverRenderer:
    def __init__(
        self,
        *,
        ffmpeg_command: str = "ffmpeg",
        runner: Callable = subprocess.run,
        analyzer: Callable = analyze_audio,
    ) -> None:
        self.ffmpeg_command = ffmpeg_command
        self.runner = runner
        self.analyzer = analyzer

    def render(
        self,
        speech_path: str | Path,
        track_path: str | Path,
        output_path: str | Path,
        decision: SegueDecision,
    ) -> Path:
        speech = Path(speech_path).expanduser().resolve()
        track = Path(track_path).expanduser().resolve()
        output = Path(output_path).expanduser().resolve()
        if not speech.is_file() or not track.is_file():
            raise TalkOverRenderError("talk-over source file is missing")
        if (
            decision.kind is not SegueKind.TALK_OVER
            or decision.speech_start_seconds is None
            or decision.speech_end_seconds is None
            or decision.duck_db is None
        ):
            raise TalkOverRenderError("talk-over decision is incomplete")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.stem}.partial{output.suffix}")
        command = build_talkover_command(
            speech_path=speech,
            track_path=track,
            output_path=temporary,
            speech_start_seconds=decision.speech_start_seconds,
            speech_end_seconds=decision.speech_end_seconds,
            duck_db=decision.duck_db,
        )
        command[0] = self.ffmpeg_command
        try:
            completed = self.runner(
                command,
                capture_output=True,
                check=False,
                text=True,
                shell=False,
            )
            if completed.returncode != 0 or not temporary.is_file():
                raise TalkOverRenderError("ffmpeg failed to create the talk-over composite")
            track_analysis = self.analyzer(track)
            rendered_analysis = self.analyzer(temporary)
            tolerance = max(0.25, float(track_analysis.duration_seconds) * 0.001)
            if abs(
                float(rendered_analysis.duration_seconds) - float(track_analysis.duration_seconds)
            ) > tolerance:
                raise TalkOverRenderError("talk-over composite did not preserve track duration")
            temporary.replace(output)
            return output
        except TalkOverRenderError:
            temporary.unlink(missing_ok=True)
            raise
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            temporary.unlink(missing_ok=True)
            raise TalkOverRenderError("talk-over rendering failed") from error
