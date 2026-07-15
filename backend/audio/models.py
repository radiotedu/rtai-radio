"""Typed contracts for immutable broadcast-audio catalog analysis."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final


class AudioValidationStatus(StrEnum):
    """Catalog eligibility for an analyzed audio asset."""

    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class AudioAnalysis:
    """Persistable facts about one source asset; analysis never edits that source."""

    source_path: Path
    duration_seconds: float
    integrated_lufs: float | None
    true_peak_dbtp: float | None
    leading_silence_seconds: float | None
    trailing_silence_seconds: float | None
    cue_in_seconds: float | None
    cue_out_seconds: float | None
    bpm: float | None
    sample_rate_hz: int
    channels: int
    codec: str
    checksum_sha256: str
    validation_status: AudioValidationStatus


@dataclass(frozen=True, slots=True)
class BroadcastAudioPolicy:
    """Frozen qualification inputs for later playout and processing work orders."""

    integrated_lufs_target: float = -16.0
    integrated_lufs_tolerance_lu: float = 1.0
    true_peak_ceiling_dbtp: float = -1.0
    silence_threshold_dbfs: float = -60.0
    silence_degraded_primary_seconds: float = 1.0
    silence_fallback_seconds: float = 1.5
    listener_visible_silence_limit_seconds: float = 2.0
    talk_over_minimum_intro_confidence: float = 0.65
    talk_over_minimum_instrumental_intro_seconds: float = 3.0
    talk_over_duck_db: float = -11.0
    talk_over_default_start_seconds: float = 0.25
    talk_over_default_window_seconds: float = 6.0
    talk_over_max_estimated_lyric_overlap_seconds: float = 2.0
    speech_target_before_intro_end_seconds: float = 0.5
    speech_target_before_intro_end_min_seconds: float = 0.3
    speech_target_before_intro_end_max_seconds: float = 0.7


BROADCAST_AUDIO_POLICY: Final = BroadcastAudioPolicy()


class AudioAnalysisError(RuntimeError):
    """Base error for assets which cannot be cataloged safely."""


class AudioAssetMissingError(AudioAnalysisError):
    """The requested asset is absent or is not a regular file."""


class UnsupportedAudioFormatError(AudioAnalysisError):
    """The source extension is outside the offline catalog's allowed formats."""


class UnreadableAudioError(AudioAnalysisError):
    """ffprobe could not obtain valid metadata from the source asset."""


class AudioStreamMissingError(UnreadableAudioError):
    """ffprobe found a container but no usable audio stream."""


class AudioAnalyzerUnavailableError(AudioAnalysisError):
    """The required offline ffprobe executable is unavailable."""
