from backend.editorial_research import EditorialResearchService
from backend.search.base import SearchResult


class RecordingProvider:
    def __init__(self, results=()):
        self.results = list(results)
        self.queries = []

    def search(self, query: str, limit: int = 5):
        self.queries.append((query, limit))
        return self.results[:limit]


def test_pop_never_calls_search_provider():
    provider = RecordingProvider()
    service = EditorialResearchService(provider)

    assert service.research(
        {"id": 1, "title": "Levitating", "artist": "Dua Lipa", "genre": "pop"},
        "en",
    ) is None
    assert provider.queries == []


def test_jazz_requires_title_and_artist_identity_and_keeps_provenance():
    provider = RecordingProvider(
        results=[
            SearchResult(
                title="Blue in Green by Miles Davis",
                url="https://music.example/blue-in-green",
                snippet="Miles Davis recorded Blue in Green for the album Kind of Blue.",
                source="searxng",
            )
        ]
    )

    card = EditorialResearchService(provider).research(
        {"id": 2, "title": "Blue in Green", "artist": "Miles Davis", "genre": "jazz"},
        "en",
    )

    assert card is not None
    assert card.track_id == 2
    assert card.url == "https://music.example/blue-in-green"
    assert card.source == "searxng"
    assert card.match_evidence == "title+artist:blue in green|miles davis"
    assert card.retrieved_at


def test_artist_only_collision_is_rejected():
    provider = RecordingProvider(
        results=[
            SearchResult(
                "Miles Davis biography",
                "https://music.example/miles",
                "Miles Davis was a trumpeter.",
                "searxng",
            )
        ]
    )

    assert EditorialResearchService(provider).research(
        {"id": 2, "title": "Blue in Green", "artist": "Miles Davis", "genre": "jazz"},
        "en",
    ) is None


def test_rejects_non_http_and_lyrics_results():
    provider = RecordingProvider(
        results=[
            SearchResult(
                "Blue in Green by Miles Davis lyrics",
                "file:///tmp/blue-in-green.txt",
                "Blue in Green Miles Davis lyrics and transcription.",
                "searxng",
            ),
            SearchResult(
                "Blue in Green by Miles Davis lyrics",
                "https://lyrics.example/blue-in-green",
                "Blue in Green Miles Davis lyrics and transcription.",
                "searxng",
            ),
        ]
    )

    assert EditorialResearchService(provider).research(
        {"id": 2, "title": "Blue in Green", "artist": "Miles Davis", "genre": "jazz"},
        "en",
    ) is None

