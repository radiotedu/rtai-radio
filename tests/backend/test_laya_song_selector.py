from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import laya_song_selector as selector


class FakeAgent:
    def __init__(self, *, truncated: bool = False) -> None:
        self.truncated = truncated
        self.seen_count = 0

    def predict(self, state, questions, **kwargs):
        question = questions["song_choice"]
        criteria = question["criteria"]
        self.seen_count = len(criteria)
        assert state["catalog_pool"]["candidate_count"] == 100
        assert kwargs["max_len"] == selector.LAYA_CHOICE_MAX_LEN
        assert kwargs["head_max_len"] == selector.LAYA_CHOICE_HEAD_MAX_LEN
        keys = list(criteria)
        probability = 1.0 / len(keys)
        return {
            "model": selector.LAYA_MODEL_ID,
            "answers": {
                "song_choice": {
                    "type": "choice",
                    "choice": keys[0],
                    "probabilities": {key: probability for key in keys},
                    "confidence": probability,
                    "answer_confidence": probability,
                    "action": {"act_probability": 1.0},
                }
            },
            "usage": {
                "truncated": int(self.truncated),
                "state_tokens_dropped": int(self.truncated),
                "options": {
                    "song_choice": {"total": len(keys), "distinct": len(keys)}
                },
            },
            "routing": None,
        }


def _catalog() -> list[dict[str, object]]:
    return [
        {
            "id": index,
            "track_id": f"track-{index:04d}",
            "title": f"Song {index}",
            "artist": f"Artist {index % 17}",
            "genre": "Jazz",
        }
        for index in range(1, 101)
    ]


def _prepare(monkeypatch, *, truncated: bool = False):
    agent = FakeAgent(truncated=truncated)
    monkeypatch.setattr(selector, "_load_agent", lambda *_args: agent)
    monkeypatch.setattr(selector, "_EMBED_FN", None)
    monkeypatch.setattr(selector, "_EMBED_FN_KEY", None)
    monkeypatch.setattr(selector, "persistent_embedding_fn", lambda embed, _path: embed)
    seen_texts: list[str] = []

    def fake_embed_fn(_agent, *, max_length, batch_size):
        assert max_length == selector.LAYA_EMBEDDING_MAX_LENGTH
        assert batch_size == 32

        def embed(texts):
            seen_texts.extend(texts)
            vectors = []
            for text in texts:
                if text.startswith("candidate_"):
                    candidate_id = int(text.split(":", 1)[0].removeprefix("candidate_"))
                    vectors.append([candidate_id / 100.0, 1.0])
                else:
                    vectors.append([1.0, 0.0])
            return np.asarray(vectors, dtype=np.float32)

        return embed

    import laya.shortlist

    monkeypatch.setattr(laya.shortlist, "embed_fn_from_agent", fake_embed_fn)
    monkeypatch.setattr(
        laya.shortlist,
        "cached_embed_fn",
        lambda embed_fn, maxsize: embed_fn,
    )
    return agent, seen_texts


def _choose() -> dict[str, object]:
    return selector.choose_song(
        station_id="radiotedu-en",
        station_name="RadioTEDU English",
        language="en",
        candidates=_catalog(),
        recent_tracks=[],
        program_context={"id": "test-program", "name": "Test Program"},
        cache_root=Path("C:/temp/laya-selector-test"),
    )


def test_laya_shortlists_from_every_catalog_candidate_without_random_sampling(
    monkeypatch,
) -> None:
    agent, embedded_texts = _prepare(monkeypatch)

    decision = _choose()

    assert agent.seen_count == 64
    assert len(embedded_texts) == 101
    assert len(decision["candidate_pool"]) == 100
    assert decision["shortlist_evidence"]["candidate_count"] == 100
    assert len(decision["shortlist_evidence"]["candidate_option_texts"]) == 100
    assert decision["shortlist_evidence"]["score_type"] == "cosine_similarity_not_probability"
    assert decision["finalist_choice_ids"] == list(range(100, 36, -1))
    assert set(decision["probabilities"]) == {
        selector.choice_key(index) for index in range(37, 101)
    }
    assert len(decision["model_input"]["questions"]["song_choice"]["criteria"]) == 64
    assert all(item["shortlist_cosine_similarity"] is not None for item in decision["candidate_pool"])


def test_laya_rejects_truncated_finalist_choice_input(monkeypatch) -> None:
    _prepare(monkeypatch, truncated=True)

    with pytest.raises(RuntimeError, match="truncated"):
        _choose()


def test_encoder_cache_reuses_exact_rows_after_restart(tmp_path):
    calls = []
    def encode(texts):
        calls.append(list(texts))
        return np.array([[len(text), sum(map(ord, text))] for text in texts], dtype=np.float32)
    cache = tmp_path / "encoder.sqlite3"
    first = selector.persistent_embedding_fn(encode, cache)
    actual = first(["song-a", "song-b", "song-a"])
    restarted = selector.persistent_embedding_fn(encode, cache)
    assert np.array_equal(restarted(["song-a", "song-b", "song-a"]), actual)
    assert calls == [["song-a"], ["song-b"]]
    restarted(["new-query", "song-a"])
    assert calls[-1] == ["new-query"]


def test_encoder_cache_rejects_nonfinite_new_rows(tmp_path):
    cached = selector.persistent_embedding_fn(
        lambda texts: np.array([[np.nan, 1]], dtype=np.float32),
        tmp_path / "encoder.sqlite3",
    )
    with pytest.raises(RuntimeError, match="invalid encoder rows"):
        cached(["song"])
