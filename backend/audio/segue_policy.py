"""Deterministic and auditable decisions for station segues."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from backend.audio.models import BROADCAST_AUDIO_POLICY


class MediaKind(StrEnum):
    """The on-air role of an item participating in a transition."""

    MUSIC = "music"
    SPEECH = "speech"
    FULL_JINGLE = "full_jingle"
    SWEEPER = "sweeper"


class Genre(StrEnum):
    """Genres with an approved deterministic transition preset."""

    CLASSICAL = "classical"
    JAZZ = "jazz"
    POP = "pop"
    ROCK = "rock"
    OTHER = "other"


class SegueKind(StrEnum):
    """The small set of playout-safe transition intents."""

    SEQUENTIAL = "sequential"
    HARD_CUT = "hard_cut"
    FADE = "fade"
    SMART_CROSSFADE = "smart_crossfade"
    TALK_OVER = "talk_over"
    IMAGING_TRANSITION = "imaging_transition"


class CueSource(StrEnum):
    """Provenance for a talk-over opening decision."""

    CURATED = "curated"
    ESTIMATED = "estimated"
    DEFAULT = "default"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class CueMetadata:
    """Measured, curated, or explicitly absent cue and vocal-boundary facts."""

    cue_in_seconds: float | None = None
    cue_out_seconds: float | None = None
    intro_end_seconds: float | None = None
    intro_confidence: float | None = None
    overlap_validated: bool = False
    cue_source: CueSource = CueSource.NONE
    immediate_loud_vocal: bool = False


@dataclass(frozen=True, slots=True)
class SegueItem:
    """One item in the previous/current/next selection window."""

    media_kind: MediaKind
    duration_seconds: float
    genre: Genre = Genre.OTHER
    cue: CueMetadata = field(default_factory=CueMetadata)
    explicit_imaging_mix: bool = False


@dataclass(frozen=True, slots=True)
class SegueDecision:
    """A playout-ready intent with timing and cue provenance."""

    kind: SegueKind
    overlap_seconds: float = 0.0
    outgoing_gain_curve: str = "none"
    incoming_gain_curve: str = "none"
    uses_measured_cues: bool = False
    speech_start_seconds: float | None = None
    speech_end_seconds: float | None = None
    speech_end_before_intro_seconds: float | None = None
    time_stretch_ratio: float = 1.0
    speaks_over_vocals: bool = False
    cue_source: CueSource = CueSource.NONE
    duck_db: float | None = None
    estimated_lyric_overlap_seconds: float = 0.0
    reason: str = ""


class SeguePolicy:
    """Choose the transition from the ending ``current`` item to ``next``."""

    _CLASSICAL_FADE_SECONDS = 0.6
    _JAZZ_CROSSFADE_SECONDS = 1.75
    _POP_CROSSFADE_SECONDS = 3.0
    _ROCK_CROSSFADE_SECONDS = 2.0

    def choose(
        self,
        previous: SegueItem | None,
        current: SegueItem,
        next: SegueItem,
    ) -> SegueDecision:
        """Return a conservative decision without changing source-audio timing.

        ``previous`` is part of the frozen selection interface.  This first
        policy makes each decision from the pair that would actually overlap
        (``current`` and ``next``), avoiding invented context from an earlier
        item.
        """

        del previous

        if current.media_kind is MediaKind.SPEECH:
            return self._speech_decision(current, next)

        if self._is_explicit_sweeper_mix(current, next):
            return SegueDecision(
                kind=SegueKind.IMAGING_TRANSITION,
                reason="explicit short-form imaging transition",
            )

        if current.media_kind is not MediaKind.MUSIC or next.media_kind is not MediaKind.MUSIC:
            return self._sequential("non-music items do not inherit music presets")

        if not self._has_measured_music_cues(current, next):
            return self._sequential("music overlap requires measured cue-in and cue-out")

        if current.genre is Genre.CLASSICAL:
            return self._music_overlap(
                SegueKind.FADE,
                self._CLASSICAL_FADE_SECONDS,
                "equal_power",
                "classical uses a short equal-power fade",
            )

        if not (current.cue.overlap_validated and next.cue.overlap_validated):
            return self._sequential("smart crossfade requires validated cue and level analysis")

        if current.genre is Genre.JAZZ:
            return self._music_overlap(
                SegueKind.SMART_CROSSFADE,
                self._JAZZ_CROSSFADE_SECONDS,
                "smart",
                "jazz smart segue",
            )
        if current.genre is Genre.POP:
            return self._music_overlap(
                SegueKind.SMART_CROSSFADE,
                self._POP_CROSSFADE_SECONDS,
                "smart",
                "pop smart crossfade",
            )
        if current.genre is Genre.ROCK:
            return self._music_overlap(
                SegueKind.SMART_CROSSFADE,
                self._ROCK_CROSSFADE_SECONDS,
                "controlled",
                "rock controlled crossfade",
            )

        return self._sequential("no approved genre preset")

    def _speech_decision(self, speech: SegueItem, incoming: SegueItem) -> SegueDecision:
        """Prefer cue metadata, then use a bounded default opening when appropriate."""

        cue = incoming.cue
        if incoming.media_kind is not MediaKind.MUSIC or speech.duration_seconds <= 0.0:
            return self._sequential("talk-over requires positive speech and incoming music")
        if cue.immediate_loud_vocal:
            return self._sequential("immediate loud vocal evidence requires sequential speech")

        source = CueSource(cue.cue_source)
        cue_in = cue.cue_in_seconds
        intro_end = cue.intro_end_seconds
        confidence = cue.intro_confidence
        target_before_intro = self._speech_end_before_intro_seconds()

        if source in {CueSource.NONE, CueSource.DEFAULT}:
            return self._default_opening_decision(speech)
        if cue_in is None or intro_end is None:
            return self._sequential("curated or estimated talk-over requires an intro boundary")
        if source is CueSource.ESTIMATED and (
            confidence is None
            or confidence < BROADCAST_AUDIO_POLICY.talk_over_minimum_intro_confidence
        ):
            return self._sequential("estimated intro confidence is below 0.65")

        instrumental_intro_seconds = intro_end - cue_in
        if instrumental_intro_seconds < BROADCAST_AUDIO_POLICY.talk_over_minimum_instrumental_intro_seconds:
            return self._sequential("instrumental opening is too short for talk-over")

        speech_end = intro_end - target_before_intro
        speech_start = speech_end - speech.duration_seconds
        if speech_start < cue_in:
            speech_start = max(0.0, cue_in)
            speech_end = speech_start + speech.duration_seconds
        lyric_overlap = max(0.0, speech_end - intro_end)
        if lyric_overlap > BROADCAST_AUDIO_POLICY.talk_over_max_estimated_lyric_overlap_seconds:
            return self._sequential("speech would exceed the two-second lyric-overlap allowance")

        return SegueDecision(
            kind=SegueKind.TALK_OVER,
            overlap_seconds=speech.duration_seconds,
            outgoing_gain_curve="duck",
            incoming_gain_curve="hold",
            uses_measured_cues=True,
            speech_start_seconds=speech_start,
            speech_end_seconds=speech_end,
            speech_end_before_intro_seconds=intro_end - speech_end,
            time_stretch_ratio=1.0,
            speaks_over_vocals=lyric_overlap > 0.0,
            cue_source=source,
            duck_db=BROADCAST_AUDIO_POLICY.talk_over_duck_db,
            estimated_lyric_overlap_seconds=lyric_overlap,
            reason=(
                "speech ends before the curated vocal boundary"
                if lyric_overlap == 0.0
                else "best-effort cue permits bounded lyric overlap"
            ),
        )

    @staticmethod
    def _default_opening_decision(speech: SegueItem) -> SegueDecision:
        start = BROADCAST_AUDIO_POLICY.talk_over_default_start_seconds
        end = start + speech.duration_seconds
        if end > BROADCAST_AUDIO_POLICY.talk_over_default_window_seconds:
            return SeguePolicy._sequential("speech does not fit the six-second default opening")
        return SegueDecision(
            kind=SegueKind.TALK_OVER,
            overlap_seconds=speech.duration_seconds,
            outgoing_gain_curve="duck",
            incoming_gain_curve="hold",
            uses_measured_cues=False,
            speech_start_seconds=start,
            speech_end_seconds=end,
            time_stretch_ratio=1.0,
            speaks_over_vocals=True,
            cue_source=CueSource.DEFAULT,
            duck_db=BROADCAST_AUDIO_POLICY.talk_over_duck_db,
            reason="best-effort six-second default opening without a cue",
        )

    @staticmethod
    def _has_measured_music_cues(current: SegueItem, next: SegueItem) -> bool:
        return (
            current.cue.cue_out_seconds is not None
            and current.cue.cue_out_seconds >= 0.0
            and current.cue.cue_out_seconds <= current.duration_seconds
            and next.cue.cue_in_seconds is not None
            and next.cue.cue_in_seconds >= 0.0
            and next.cue.cue_in_seconds <= next.duration_seconds
        )

    @staticmethod
    def _is_explicit_sweeper_mix(current: SegueItem, next: SegueItem) -> bool:
        return (
            current.media_kind is MediaKind.SWEEPER
            and current.explicit_imaging_mix
        ) or (next.media_kind is MediaKind.SWEEPER and next.explicit_imaging_mix)

    @staticmethod
    def _music_overlap(
        kind: SegueKind,
        overlap_seconds: float,
        gain_curve: str,
        reason: str,
    ) -> SegueDecision:
        return SegueDecision(
            kind=kind,
            overlap_seconds=overlap_seconds,
            outgoing_gain_curve=gain_curve,
            incoming_gain_curve=gain_curve,
            uses_measured_cues=True,
            reason=reason,
        )

    @staticmethod
    def _sequential(reason: str) -> SegueDecision:
        return SegueDecision(
            kind=SegueKind.SEQUENTIAL,
            time_stretch_ratio=1.0,
            speaks_over_vocals=False,
            reason=reason,
        )

    @staticmethod
    def _speech_end_before_intro_seconds() -> float:
        """Keep the frozen 0.5-second target inside its 0.3--0.7-second bound."""

        return min(
            max(
                BROADCAST_AUDIO_POLICY.speech_target_before_intro_end_seconds,
                BROADCAST_AUDIO_POLICY.speech_target_before_intro_end_min_seconds,
            ),
            BROADCAST_AUDIO_POLICY.speech_target_before_intro_end_max_seconds,
        )
