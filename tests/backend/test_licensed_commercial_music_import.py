from __future__ import annotations

from scripts.import_licensed_commercial_music import classify_genre, looks_turkish


def test_source_folders_map_to_transition_liner_genres() -> None:
    assert classify_genre("Ambient", "Blue Foundation", "Eyes on Fire") == "Lo-Fi"
    assert classify_genre("Contemporary Country", "Billy Dean", "Saturday Night") == "Folk"
    assert classify_genre("Alternative Metal", "Three Days Grace", "Never Too Late") == "Rock"
    assert classify_genre("other", "Metro Boomin & Future feat. Don Toliver", "Too Many Nights") == "Hip-Hop"
    assert classify_genre("funk", "Earth, Wind & Fire", "Let's Groove") == "Pop"


def test_known_misclassified_commercial_track_is_corrected() -> None:
    assert classify_genre("jazz", "Madonna", "Vogue") == "Pop"


def test_turkish_language_signals_are_rejected_without_blocking_french() -> None:
    assert looks_turkish("Bir ışık", "Sanatçı") is True
    assert looks_turkish("Bonjour ça va", "Françoise") is False
    assert looks_turkish("English title", "Artist", language_tag="tr") is True
