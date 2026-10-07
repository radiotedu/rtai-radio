"""Local, non-generative Laya choice model for RadioTEDU song selection."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from pathlib import Path
from typing import Any


LAYA_MODEL_ID = "convaiinnovations/laya-multilingual"
LAYA_MODEL_REVISION = "e4e9ddf21a7b1903b7acffd8814ad4307bf63a67"
LAYA_MODEL_SHA256 = "9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204"
LAYA_PACKAGE_VERSION = "0.3.22"
LAYA_LIBRARY_SHORTLIST_SIZE = 64
LAYA_CHOICE_MAX_LEN = 8192
LAYA_CHOICE_HEAD_MAX_LEN = 2048
LAYA_EMBEDDING_MAX_LENGTH = 512

_AGENT: Any | None = None
_AGENT_KEY: tuple[str, str, str] | None = None
_AGENT_LOCK = threading.Lock()
_EMBED_FN: Any | None = None
_EMBED_FN_KEY: tuple[str, str, str] | None = None
_POLICY_TEXT = (
    "Choose one supplied song using its title, artist and genre, the program "
    "preferences, recent listening context and upcoming songs. Program genres are preferences."
)
SELECTION_POLICY_SHA256 = hashlib.sha256(_POLICY_TEXT.encode("utf-8")).hexdigest()


def choice_key(choice_id: int) -> str:
    """Return the stable option label used in current high-cardinality events."""
    return f"candidate_{int(choice_id):04d}"


def _load_agent(model_id: str, revision: str, cache_root: Path) -> Any:
    global _AGENT, _AGENT_KEY
    cache_root = cache_root.expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    key = (model_id, revision, str(cache_root))
    with _AGENT_LOCK:
        if _AGENT is not None and _AGENT_KEY == key:
            return _AGENT

        # Keep the model cache explicit so Windows service-account defaults do
        # not put the weights under System32's profile.
        os.environ["HF_HOME"] = str(cache_root)
        os.environ["HF_HUB_CACHE"] = str(cache_root / "hub")
        os.environ.setdefault("OMP_NUM_THREADS", "3")
        os.environ.setdefault("MKL_NUM_THREADS", "3")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

        from audio_thread_priority import set_broadcast_process_priority

        set_broadcast_process_priority()

        import torch
        from laya import load

        torch.set_num_threads(3)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            # Another in-process torch user may already have initialized its
            # inter-op pool; the bounded intra-op setting still applies.
            pass

        agent = load(
            model_id,
            device="cpu",
            revision=revision,
            expected_sha256={"model.safetensors": LAYA_MODEL_SHA256},
        )
        _AGENT = agent
        _AGENT_KEY = key
        return agent


def load_agent(model_id: str, revision: str, cache_root: Path) -> Any:
    """Load the pinned local checkpoint once and keep it resident in memory."""
    return _load_agent(model_id, revision, cache_root)


def _stable_pool_sha256(candidates: list[dict[str, object]]) -> str:
    encoded = json.dumps(
        candidates,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _all_cosine_scores(matrix: Any) -> list[float]:
    """Mirror the pinned laya.shortlist cosine calculation for its exact vectors."""
    import numpy as np

    values = np.asarray(matrix, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] < 2:
        raise RuntimeError("Laya shortlist embedding did not include the query and all options")
    query = values[0]
    options = values[1:]
    query_norm = float(np.linalg.norm(query))
    option_norms = np.linalg.norm(options, axis=1)
    denominator = option_norms * query_norm
    scores = np.zeros(options.shape[0], dtype=np.float64)
    valid = denominator > 0.0
    if query_norm > 0.0 and np.any(valid):
        scores[valid] = np.clip(
            np.dot(options[valid], query) / denominator[valid], -1.0, 1.0
        )
    if not np.isfinite(scores).all():
        raise RuntimeError("Laya shortlist produced a non-finite similarity score")
    return [float(score) for score in scores]


def persistent_embedding_fn(embed_fn: Any, cache_file: Path) -> Any:
    """Reuse exact encoder rows across restarts; each new typed choice still runs."""
    import sqlite3
    import numpy as np

    cache_file.parent.mkdir(parents=True, exist_ok=True)

    def embed(texts: list[str]) -> Any:
        keys = [str(text) for text in texts]
        if not keys:
            return np.zeros((0, 0), dtype=np.float32)
        rows: dict[str, Any] = {}
        with sqlite3.connect(cache_file, timeout=30) as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS embeddings (text TEXT PRIMARY KEY, dim INTEGER, vector BLOB)"
            )
            for key in dict.fromkeys(keys):
                record = database.execute(
                    "SELECT dim, vector FROM embeddings WHERE text = ?", (key,)
                ).fetchone()
                if record is not None and record[0] > 0 and len(record[1]) == record[0] * 4:
                    vector = np.frombuffer(record[1], dtype=np.float32).copy()
                    if np.isfinite(vector).all():
                        rows[key] = vector
            missing = [key for key in dict.fromkeys(keys) if key not in rows]
            # The first input is the long live-state query. Encode it alone
            # so it cannot pad 31 short song options to its 512-token length.
            chunks = ([missing[:1]] if missing else []) + [
                missing[start:start + 32] for start in range(1, len(missing), 32)
            ]
            for chunk in chunks:
                fresh = np.asarray(embed_fn(chunk), dtype=np.float32)
                if fresh.ndim != 2 or fresh.shape[0] != len(chunk) or fresh.shape[1] < 1 or not np.isfinite(fresh).all():
                    raise RuntimeError("invalid encoder rows for persistent cache")
                for key, vector in zip(chunk, fresh):
                    database.execute(
                        "INSERT OR REPLACE INTO embeddings (text, dim, vector) VALUES (?, ?, ?)",
                        (key, len(vector), vector.tobytes()),
                    )
                    rows[key] = vector
                database.commit()  # retain progress even during a cold-start interruption
            database.execute(
                "DELETE FROM embeddings WHERE rowid NOT IN (SELECT rowid FROM embeddings ORDER BY rowid DESC LIMIT 8192)"
            )
        return np.stack([rows[key] for key in keys])

    return embed


def choose_song(
    *,
    station_id: str,
    station_name: str,
    language: str,
    candidates: list[dict[str, object]],
    recent_tracks: list[dict[str, str]],
    program_context: dict[str, object],
    cache_root: Path,
    model_id: str = LAYA_MODEL_ID,
    revision: str = LAYA_MODEL_REVISION,
) -> dict[str, object]:
    """Run Laya over the full pool, then make one typed choice among its finalists."""
    if not candidates:
        raise RuntimeError("Laya received no song candidates")
    if language not in {"en", "fr"}:
        raise ValueError("Laya song selection supports only en and fr stations")

    ordered_candidates: list[dict[str, object]] = []
    criteria: dict[str, str] = {}
    choice_to_id: dict[str, int] = {}
    seen_track_ids: set[str] = set()
    for candidate in candidates:
        choice_id = int(candidate["id"])
        if choice_id < 1:
            raise ValueError("Laya song candidate ids must be positive")
        key = choice_key(choice_id)
        if key in choice_to_id:
            raise ValueError("Laya song candidates contain a duplicate choice id")
        title = str(candidate.get("title") or "Untitled")[:200]
        artist = str(candidate.get("artist") or "Unknown artist")[:160]
        genre = str(candidate.get("genre") or "Unspecified")[:64]
        track_id = str(candidate.get("track_id") or "")[:128]
        if not track_id or track_id in seen_track_ids:
            raise ValueError("Laya song candidates require unique opaque track ids")
        seen_track_ids.add(track_id)
        item = {
            "choice_id": choice_id,
            "track_id": track_id,
            "title": title,
            "artist": artist,
            "genre": genre,
        }
        ordered_candidates.append(item)
        choice_to_id[key] = choice_id
        # Keep each typed option short: repeated prose made the 64-option
        # CPU decision slower without supplying additional track facts.
        criteria[key] = f"{title} | {artist} | {genre}"

    pool_sha256 = _stable_pool_sha256(ordered_candidates)
    state: dict[str, object] = {
        "station_name": station_name,
        "language": "English" if language == "en" else "French",
        "program": {
            "name": str(program_context.get("name") or station_name)[:160],
            "description": str(program_context.get("description") or program_context.get("vibe") or "")[:240],
            "preferred_genres": [
                str(value)[:64]
                for value in program_context.get("required_genres", [])
                if str(value).strip()
            ][:12],
        },
        "recent_tracks": [
            {
                "title": str(item.get("title") or "Unknown title")[:200],
                "artist": str(item.get("artist") or "Unknown artist")[:160],
            }
            for item in recent_tracks[-6:]
        ],
        "catalog_pool": {
            "candidate_count": len(ordered_candidates),
            "ordered_pool_sha256": pool_sha256,
        },
        "selection_policy": _POLICY_TEXT,
    }
    if isinstance(program_context.get("live_selection_queue"), dict):
        # IDs, timestamps, hashes and scheduling flags remain in the event's
        # exact queue evidence. Give the encoder musical facts rather than
        # hundreds of tokens of opaque operational identifiers.
        state["upcoming_tracks"] = [
            {
                "title": str(item.get("title") or "Unknown title")[:200],
                "artist": str(item.get("artist") or "Unknown artist")[:160],
                "genre": str(item.get("genre") or "")[:64],
            }
            for item in program_context.get("upcoming_tracks", [])
            if isinstance(item, dict)
        ]
        current_id = program_context["live_selection_queue"].get("current_track_id")
        current = next((item for item in reversed(recent_tracks)
                        if current_id and item.get("track_id") == current_id), None)
        if current is not None:
            state["current_track"] = {
                "title": str(current.get("title") or "Unknown title")[:200],
                "artist": str(current.get("artist") or "Unknown artist")[:160],
            }
    instructions = (
        "Choose the one supplied track that best fits RadioTEDU's current "
        "program and recent listening context. Compare only the provided title, "
        "artist, and genre. The ordered candidate list is authoritative. Select "
        "exactly one listed candidate and do not invent another track."
        if language == "en"
        else "Choisissez le titre fourni qui convient le mieux au programme "
        "actuel de RadioTEDU et aux \u00e9coutes r\u00e9centes. Comparez uniquement le "
        "titre, l'artiste et le genre fournis. La liste ordonn\u00e9e fait foi. "
        "Choisissez exactement une option fournie et n'inventez aucun titre."
    )
    question = {
        "type": "choice",
        "instructions": instructions,
        "criteria": criteria,
    }
    questions = {"song_choice": question}

    agent = _load_agent(model_id, revision, cache_root)
    try:
        from laya.shortlist import embed_fn_from_agent, predict_shortlist
    except ImportError as exc:
        raise RuntimeError("installed Laya package lacks high-cardinality shortlist support") from exc

    global _EMBED_FN, _EMBED_FN_KEY
    cache_key = (model_id, revision, str(cache_root.expanduser().resolve()))
    with _AGENT_LOCK:
        if _EMBED_FN is None or _EMBED_FN_KEY != cache_key:
            from laya.shortlist import cached_embed_fn

            _EMBED_FN = cached_embed_fn(
                persistent_embedding_fn(
                    embed_fn_from_agent(
                        agent, max_length=LAYA_EMBEDDING_MAX_LENGTH, batch_size=32,
                    ),
                    cache_root / (
                        "encoder-" + hashlib.sha256(
                            f"{model_id}|{revision}|{LAYA_MODEL_SHA256}|mean-pool-v1|{LAYA_EMBEDDING_MAX_LENGTH}".encode()
                        ).hexdigest()[:24] + ".sqlite3"
                    ),
                ),
                maxsize=8192,
            )
            _EMBED_FN_KEY = cache_key
        embed = _EMBED_FN

    captured: dict[str, Any] = {}

    def capture_embedding_inputs(texts: list[str]) -> Any:
        rows = [str(text) for text in texts]
        vectors = embed(rows)
        captured["texts"] = rows
        captured["vectors"] = vectors
        return vectors

    response = predict_shortlist(
        agent,
        state,
        questions,
        capture_embedding_inputs,
        k=LAYA_LIBRARY_SHORTLIST_SIZE,
        lang=language,
        max_len=LAYA_CHOICE_MAX_LEN,
        head_max_len=LAYA_CHOICE_HEAD_MAX_LEN,
    )
    shortlist_meta = response.get("shortlist")
    shortlist_meta = shortlist_meta.get("song_choice") if isinstance(shortlist_meta, dict) else None
    if not isinstance(shortlist_meta, dict):
        raise RuntimeError("Laya did not return shortlist evidence for its song-choice question")
    shortlisted_keys = shortlist_meta.get("labels")
    if not isinstance(shortlisted_keys, list) or not shortlisted_keys:
        raise RuntimeError("Laya returned an empty song shortlist")
    if any(str(key) not in choice_to_id for key in shortlisted_keys):
        raise RuntimeError("Laya shortlist contains an option outside the full candidate pool")
    shortlisted_keys = [str(key) for key in shortlisted_keys]
    shortlist_ids = [choice_to_id[key] for key in shortlisted_keys]
    shortlist_scores = shortlist_meta.get("scores")
    all_scores: list[float | None]
    option_texts: list[str]
    if shortlist_meta.get("passthrough") is True:
        if len(ordered_candidates) > LAYA_LIBRARY_SHORTLIST_SIZE:
            raise RuntimeError("Laya unexpectedly bypassed full-library shortlist reduction")
        all_scores = [None] * len(ordered_candidates)
        option_texts = []
        method = "laya_typed_choice_over_entire_pool"
    else:
        if len(ordered_candidates) <= LAYA_LIBRARY_SHORTLIST_SIZE:
            raise RuntimeError("Laya unexpectedly shortened a pool that fits its configured shortlist")
        texts = captured.get("texts")
        vectors = captured.get("vectors")
        if not isinstance(texts, list) or len(texts) != len(ordered_candidates) + 1:
            raise RuntimeError("Laya encoder did not receive the query and every catalog candidate")
        if texts[0] is None:
            raise RuntimeError("Laya encoder shortlist query was not captured")
        option_texts = [str(value) for value in texts[1:]]
        all_scores = _all_cosine_scores(vectors)
        if len(all_scores) != len(ordered_candidates):
            raise RuntimeError("Laya encoder shortlist did not score every catalog candidate")
        if not isinstance(shortlist_scores, list) or len(shortlist_scores) != len(shortlisted_keys):
            raise RuntimeError("Laya shortlist omitted its finalist similarity scores")
        score_by_id = {
            int(item["choice_id"]): float(score)
            for item, score in zip(ordered_candidates, all_scores)
        }
        returned_scores = [float(score) for score in shortlist_scores]
        for choice_id, reported_score in zip(shortlist_ids, returned_scores):
            if not math.isclose(
                score_by_id[choice_id], reported_score, rel_tol=1e-7, abs_tol=1e-7
            ):
                raise RuntimeError("Laya shortlist similarity evidence failed local validation")
        candidate_order = {
            int(item["choice_id"]): index
            for index, item in enumerate(ordered_candidates)
        }
        expected_order = sorted(
            ordered_candidates,
            key=lambda item: (
                -score_by_id[int(item["choice_id"])],
                candidate_order[int(item["choice_id"])],
            ),
        )[:LAYA_LIBRARY_SHORTLIST_SIZE]
        if [int(item["choice_id"]) for item in expected_order] != shortlist_ids:
            raise RuntimeError("Laya shortlist labels do not match its full-pool similarity ranking")
        method = "laya.shortlist.predict_shortlist_cosine_similarity"

    answers = response.get("answers")
    answer = answers.get("song_choice") if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise RuntimeError("Laya returned no typed song-choice answer")

    selected_key = str(answer.get("choice") or "")
    if selected_key not in shortlisted_keys:
        raise RuntimeError("Laya selected an option outside its recorded shortlist")
    probabilities = answer.get("probabilities")
    final_criteria = {key: criteria[key] for key in shortlisted_keys}
    if not isinstance(probabilities, dict) or set(probabilities) != set(final_criteria):
        raise RuntimeError("Laya did not return one probability for every shortlisted candidate")
    numeric_probabilities: dict[str, float] = {}
    for key in shortlisted_keys:
        probability = float(probabilities[key])
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise RuntimeError("Laya returned an invalid shortlist candidate probability")
        numeric_probabilities[key] = probability
    if not 0.99 <= sum(numeric_probabilities.values()) <= 1.01:
        raise RuntimeError("Laya shortlisted-candidate probabilities do not form a distribution")

    raw_response = {key: value for key, value in response.items() if key != "shortlist"}
    usage = raw_response.get("usage")
    if isinstance(usage, dict):
        if bool(usage.get("truncated")) or int(usage.get("state_tokens_dropped") or 0) > 0:
            raise RuntimeError("Laya truncated the typed song-choice input")
        option_usage = usage.get("options")
        option_usage = option_usage.get("song_choice") if isinstance(option_usage, dict) else None
        if isinstance(option_usage, dict):
            if int(option_usage.get("total", -1)) != len(shortlisted_keys):
                raise RuntimeError("Laya typed-choice head did not receive every finalist")
            if int(option_usage.get("distinct", -1)) != len(shortlisted_keys):
                raise RuntimeError("Laya typed-choice head collapsed two or more finalist options")

    raw_response_json = json.dumps(
        raw_response,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    score_by_id = {
        int(item["choice_id"]): score
        for item, score in zip(ordered_candidates, all_scores)
    }
    rank_by_id = {choice_id: rank for rank, choice_id in enumerate(shortlist_ids, start=1)}
    candidate_evidence = [
        {
            **item,
            "shortlist_cosine_similarity": score_by_id[int(item["choice_id"])],
            "shortlist_rank": rank_by_id.get(int(item["choice_id"])),
        }
        for item in ordered_candidates
    ]
    shortlist_evidence = {
        "method": method,
        "score_type": "cosine_similarity_not_probability",
        "candidate_count": len(ordered_candidates),
        "ordered_pool_sha256": pool_sha256,
        "shortlist_size": len(shortlisted_keys),
        "choice_ids_by_similarity_rank": shortlist_ids,
        "query_text": str(captured["texts"][0]) if captured.get("texts") else None,
        "candidate_option_texts": [
            {"choice_id": int(item["choice_id"]), "text": text}
            for item, text in zip(ordered_candidates, option_texts)
        ],
    }
    request_evidence = {
        "state": state,
        "questions": {
            "song_choice": {
                **question,
                "criteria": final_criteria,
            }
        },
    }
    return {
        "choice_id": choice_to_id[selected_key],
        "choice_key": selected_key,
        "probabilities": numeric_probabilities,
        "confidence": answer.get("confidence"),
        "answer_confidence": answer.get("answer_confidence"),
        "action": answer.get("action"),
        "usage": usage,
        "routing": raw_response.get("routing"),
        "candidate_pool": candidate_evidence,
        "shortlist_evidence": shortlist_evidence,
        "finalist_choice_ids": shortlist_ids,
        "model_input": request_evidence,
        "model_output": answer,
        "model_output_raw_json": raw_response_json,
        "model_id": model_id,
        "model_revision": revision,
        "policy_sha256": SELECTION_POLICY_SHA256,
    }
