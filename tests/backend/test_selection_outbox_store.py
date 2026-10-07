import json

import pytest

from scripts.selection_outbox_store import SelectionOutboxStore


def event(number):
    return {"station_id": "radiotedu-en", "event_id": f"decision-{number}",
            "occurred_at": f"2026-10-02T20:00:{number % 60:02d}Z",
            "candidate_tracks": [{"choice_id": 12, "title": "Sé", "score": 0.123456789}],
            "model_output_raw_json": '{"actual":"response"}', "evidence": "x" * 4096}


def test_spool_preserves_all_pending_events_and_uses_small_references(tmp_path):
    store = SelectionOutboxStore(tmp_path / "outbox.sqlite3")
    for number in range(505):
        assert store.enqueue(event(number))
    refs = store.references(("radiotedu-en",))["radiotedu-en"]
    assert len(refs) == 505
    assert refs[0]["event_id"] == "decision-0"
    assert set(refs[0]) == {"event_id", "station_id", "occurred_at"}
    assert store.event("radiotedu-en", "decision-0") == event(0)


def test_acknowledgement_survives_restart_and_retains_evidence(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    store = SelectionOutboxStore(path)
    store.enqueue(event(1))
    store.enqueue(event(2))
    store.acknowledge("radiotedu-en", "decision-1")
    restored = SelectionOutboxStore(path)
    refs = restored.references(("radiotedu-en",))["radiotedu-en"]
    assert [item["event_id"] for item in refs] == ["decision-2"]
    assert restored.event("radiotedu-en", "decision-1") == event(1)
    assert not restored.enqueue(event(1))


def test_legacy_migration_is_lossless_and_idempotent(tmp_path):
    legacy = tmp_path / "outbox.json"
    legacy.write_text(json.dumps({"radiotedu-en": [event(1), event(2)]}), encoding="utf-8-sig")
    store = SelectionOutboxStore(tmp_path / "outbox.sqlite3")
    assert store.migrate_legacy(legacy, ("radiotedu-en",), archive=False) == 2
    store.acknowledge("radiotedu-en", "decision-1")
    assert store.migrate_legacy(legacy, ("radiotedu-en",)) == 0
    assert not legacy.exists()
    assert len(list(tmp_path.glob("outbox.legacy-*.json"))) == 1
    assert store.event("radiotedu-en", "decision-2") == event(2)
    assert len(store.references(("radiotedu-en",))["radiotedu-en"]) == 1


def test_conflicting_event_cannot_replace_original_evidence(tmp_path):
    store = SelectionOutboxStore(tmp_path / "outbox.sqlite3")
    store.enqueue(event(1))
    modified = {**event(1), "model_output_raw_json": "fabricated"}
    with pytest.raises(ValueError, match="conflicts"):
        store.enqueue(modified)
    assert store.event("radiotedu-en", "decision-1") == event(1)
