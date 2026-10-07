from __future__ import annotations

from scripts.import_full_commercial_library import (
    Candidate,
    artist_genre_override,
    broadcast_metadata_is_safe,
    canonical_genre,
    looks_turkish,
    repair_presenter_metadata,
    semantic_key,
)


def test_genres_map_to_transition_liner_categories() -> None:
    assert canonical_genre("alternative metal") == "Rock"
    assert canonical_genre("ambient downtempo") == "Lo-Fi"
    assert canonical_genre("dance-pop") == "Pop"
    assert canonical_genre("trap / rap") == "Hip-Hop"
    assert canonical_genre("electronic house") == "Electronic"
    assert canonical_genre("funk") == "Pop"


def test_non_turkish_and_turkish_signals() -> None:
    assert looks_turkish("Sezen Aksu", "Gidemem") is True
    assert looks_turkish("Bir gece yine", "Artist") is True
    assert looks_turkish("Françoise Hardy", "Tous les garçons") is False
    assert looks_turkish("Coldplay", "Yellow", language_tag="en") is False


def test_semantic_key_collapses_video_and_lyrics_variants() -> None:
    common = dict(
        source="x.mp3", genre="Rock", artist="Crowded House", album=None,
        duration_seconds=220.0, bitrate_kbps=192, sample_rate_hz=44100,
    )
    plain = Candidate(title="Don't Dream It's Over", **common)
    video = Candidate(title="Don’t Dream It’s Over (Official Music Video)", **common)
    assert semantic_key(plain) == semantic_key(video)

    youtube = Candidate(title="Don't Dream It's Over [dQw4w9WgXcQ]", **common)
    assert semantic_key(plain) == semantic_key(youtube)


def test_presenter_metadata_rejects_numeric_and_fullwidth_titles() -> None:
    assert broadcast_metadata_is_safe("Yellow", "Coldplay") is True
    assert broadcast_metadata_is_safe("22", "Taylor Swift") is False
    assert broadcast_metadata_is_safe("Paramore： Decode", "music") is False


def test_inverted_known_artist_metadata_is_repaired() -> None:
    assert repair_presenter_metadata(
        "Linkin Park", "In The End [Official HD Music Video]"
    ) == ("In The End [Official HD Music Video]", "Linkin Park")
    assert repair_presenter_metadata("Yellow", "Coldplay") == ("Yellow", "Coldplay")
    assert repair_presenter_metadata("Bryce Vine", "Classic and perfect") == (
        "Classic and Perfect", "Bryce Vine"
    )
    assert repair_presenter_metadata("Dimitri Vegas", "Instagram") == (
        "Instagram", "Dimitri Vegas"
    )


def test_artist_override_handles_lead_artist_collaborations() -> None:
    assert artist_genre_override("RAYE, 070 Shake") == "Pop"
    assert artist_genre_override("Max Richter;Mari Samuelsen") == "Classical"
    assert artist_genre_override("Earth, Wind & Fire") == "Pop"
    assert artist_genre_override("Bobby Caldwell") == "Pop"
