from __future__ import annotations

import pytest

from backend.audio.segue_policy import (
    CueSource,
    CueMetadata,
    Genre,
    MediaKind,
    SegueItem,
    SegueKind,
    SeguePolicy,
)


def music(
    genre: Genre,
    *,
    cue_in_seconds: float | None = 0.0,
    cue_out_seconds: float | None = 220.0,
    intro_end_seconds: float | None = 5.0,
    intro_confidence: float | None = 0.95,
    overlap_validated: bool = True,
    cue_source: CueSource | str = CueSource.CURATED,
    immediate_loud_vocal: bool = False,
) -> SegueItem:
    return SegueItem(
        media_kind=MediaKind.MUSIC,
        duration_seconds=240.0,
        genre=genre,
        cue=CueMetadata(
            cue_in_seconds=cue_in_seconds,
            cue_out_seconds=cue_out_seconds,
            intro_end_seconds=intro_end_seconds,
            intro_confidence=intro_confidence,
            overlap_validated=overlap_validated,
            cue_source=CueSource(cue_source),
            immediate_loud_vocal=immediate_loud_vocal,
        ),
    )


def speech(duration_seconds: float) -> SegueItem:
    return SegueItem(
        media_kind=MediaKind.SPEECH,
        duration_seconds=duration_seconds,
    )


def test_classical_uses_a_short_equal_power_fade_from_measured_cues() -> None:
    decision = SeguePolicy().choose(None, music(Genre.CLASSICAL), music(Genre.CLASSICAL))

    assert decision.kind is SegueKind.FADE
    assert 0.2 <= decision.overlap_seconds <= 1.0
    assert decision.outgoing_gain_curve == "equal_power"
    assert decision.incoming_gain_curve == "equal_power"


@pytest.mark.parametrize(
    ("genre", "minimum", "maximum"),
    [
        (Genre.JAZZ, 1.0, 2.5),
        (Genre.POP, 2.0, 4.0),
        (Genre.ROCK, 1.0, 3.0),
    ],
)
def test_measured_music_genres_receive_their_own_smart_crossfade_preset(
    genre: Genre, minimum: float, maximum: float
) -> None:
    decision = SeguePolicy().choose(None, music(genre), music(genre))

    assert decision.kind is SegueKind.SMART_CROSSFADE
    assert minimum <= decision.overlap_seconds <= maximum
    assert decision.uses_measured_cues is True


def test_pop_without_validated_overlap_is_sequential() -> None:
    decision = SeguePolicy().choose(
        None,
        music(Genre.POP, overlap_validated=False),
        music(Genre.POP, overlap_validated=False),
    )

    assert decision.kind is SegueKind.SEQUENTIAL
    assert decision.overlap_seconds == 0.0


def test_music_overlap_rejects_cues_outside_the_measured_asset_duration() -> None:
    decision = SeguePolicy().choose(
        None,
        music(Genre.POP, cue_out_seconds=240.1),
        music(Genre.POP),
    )

    assert decision.kind is SegueKind.SEQUENTIAL
    assert decision.overlap_seconds == 0.0


def test_talk_over_ends_half_a_second_before_a_verified_vocal_boundary() -> None:
    incoming = music(Genre.POP, intro_end_seconds=4.0, intro_confidence=0.85)

    decision = SeguePolicy().choose(None, speech(3.0), incoming)

    assert decision.kind is SegueKind.TALK_OVER
    assert decision.speech_start_seconds == pytest.approx(0.5)
    assert decision.speech_end_seconds == pytest.approx(3.5)
    assert 0.3 <= decision.speech_end_before_intro_seconds <= 0.7
    assert decision.time_stretch_ratio == 1.0
    assert decision.speaks_over_vocals is False
    assert decision.cue_source is CueSource.CURATED
    assert decision.duck_db == -11.0


def test_estimated_cue_at_point_sixty_five_allows_talk_over() -> None:
    incoming = music(
        Genre.POP,
        intro_end_seconds=4.0,
        intro_confidence=0.65,
        cue_source="estimated",
    )

    decision = SeguePolicy().choose(None, speech(4.5), incoming)

    assert decision.kind is SegueKind.TALK_OVER
    assert decision.cue_source is CueSource.ESTIMATED
    assert decision.duck_db in {-10.0, -11.0, -12.0}
    assert decision.speaks_over_vocals is True
    assert 0.0 < decision.estimated_lyric_overlap_seconds <= 2.0


def test_unknown_cue_uses_six_second_default_window() -> None:
    incoming = music(
        Genre.POP,
        intro_end_seconds=None,
        intro_confidence=None,
        cue_source="none",
    )

    decision = SeguePolicy().choose(None, speech(5.0), incoming)

    assert decision.kind is SegueKind.TALK_OVER
    assert decision.cue_source is CueSource.DEFAULT
    assert decision.speech_start_seconds == pytest.approx(0.25)
    assert decision.speech_end_seconds <= 6.0
    assert decision.duck_db == -11.0


@pytest.mark.parametrize(
    "incoming",
    [
        music(Genre.POP, intro_confidence=0.64, cue_source="estimated"),
        music(
            Genre.POP,
            intro_end_seconds=None,
            intro_confidence=None,
            cue_source="none",
            immediate_loud_vocal=True,
        ),
    ],
)
def test_speech_is_sequential_when_minimum_evidence_rejects_talk_over(incoming: SegueItem) -> None:
    decision = SeguePolicy().choose(None, speech(1.0), incoming)

    assert decision.kind is SegueKind.SEQUENTIAL
    assert decision.overlap_seconds == 0.0
    assert decision.time_stretch_ratio == 1.0
    assert decision.speaks_over_vocals is False


def test_speech_is_sequential_when_its_measured_duration_does_not_fit_the_intro() -> None:
    decision = SeguePolicy().choose(
        None,
        speech(7.0),
        music(
            Genre.POP,
            intro_end_seconds=3.5,
            intro_confidence=0.8,
            cue_source="estimated",
        ),
    )

    assert decision.kind is SegueKind.SEQUENTIAL
    assert decision.overlap_seconds == 0.0
    assert decision.time_stretch_ratio == 1.0


def test_full_jingles_stay_sequential_but_explicit_sweeper_mix_uses_imaging_transition() -> None:
    full_jingle = SegueItem(media_kind=MediaKind.FULL_JINGLE, duration_seconds=8.0)
    sweeper = SegueItem(
        media_kind=MediaKind.SWEEPER,
        duration_seconds=1.0,
        explicit_imaging_mix=True,
    )
    policy = SeguePolicy()

    assert policy.choose(None, full_jingle, music(Genre.POP)).kind is SegueKind.SEQUENTIAL
    assert policy.choose(None, sweeper, music(Genre.POP)).kind is SegueKind.IMAGING_TRANSITION
