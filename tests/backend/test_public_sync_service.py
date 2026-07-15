from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from backend.config import Settings
from backend.public_sync import PublicSyncService, snapshot_state_from_operator_status


class Clock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class RecordingTransport:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.requests: list[dict] = []

    def __call__(self, **request) -> int:
        self.requests.append(request)
        if self.failures:
            self.failures -= 1
            raise OSError("website unavailable")
        return 201


def _settings() -> Settings:
    return Settings(
        public_sync_url="https://api.radiotedu.com",
        platform_hmac_secret_en="english-test-secret",
        platform_hmac_secret_fr="french-test-secret",
    )


def _snapshot_state(station_id: str, title: str) -> dict:
    language = "en" if station_id.endswith("-en") else "fr"
    mount = "/en" if language == "en" else "/fr"
    return {
        "operational_state": "live",
        "speech_state": {"active": False, "kind": "music"},
        "now_playing": {
            "kind": "music",
            "track_id": "track-1",
            "title": title,
            "artist": "The Signals",
            "cover_id": "track-1",
            "mood": "warm",
            "sound_tags": ["warm"],
            "started_at": "2026-07-15T08:00:00+00:00",
        },
        "current_program": {
            "id": "campus-flow",
            "name": "Campus Flow",
            "vibe": "warm focused jazz",
            "sound_tags": ["warm", "focused"],
        },
        "next_program": None,
        "stream": {
            "url": f"https://stream.radiotedu.com{mount}",
            "mount": mount,
            "status": "live",
            "codec": "AAC-LC",
            "bitrate_kbps": 192,
            "public": True,
        },
        "editorial": {"sound_tags": ["warm", "focused"]},
    }


def _play_event(event_id: str = "play-1") -> dict:
    return {
        "protocol": "radiotedu-platform/v1",
        "schema_version": 1,
        "event_id": event_id,
        "station_id": "radiotedu-en",
        "event_type": "play.completed",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "classification": "music",
        "duration_ms": 180_000,
        "track_id": "track-1",
        "track_title": "Blue Campus",
        "artist": "The Signals",
        "program_id": "campus-flow",
        "program_name": "Campus Flow",
        "cover_id": "track-1",
        "sound_tags": ["warm"],
    }


def test_unsent_snapshots_are_coalesced_to_newest_state(tmp_path: Path) -> None:
    service = PublicSyncService(_settings(), tmp_path / "public-sync.db", clock=Clock())

    first = service.publish_snapshot("radiotedu-en", _snapshot_state("radiotedu-en", "First"))
    second = service.publish_snapshot("radiotedu-en", _snapshot_state("radiotedu-en", "Newest"))
    pending = service.pending_records()

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert len(pending) == 1
    assert pending[0]["kind"] == "snapshot"
    assert json.loads(pending[0]["body"])["now_playing"]["title"] == "Newest"
    assert json.loads(pending[0]["body"])["sequence"] == 2


def test_play_events_survive_outage_restart_and_use_full_jitter_backoff(tmp_path: Path) -> None:
    clock = Clock()
    transport = RecordingTransport(failures=1)
    database = tmp_path / "public-sync.db"
    service = PublicSyncService(
        _settings(),
        database,
        transport=transport,
        clock=clock,
        random_value=lambda: 0.5,
    )
    service.enqueue_play_event("radiotedu-en", _play_event())

    failed = service.flush_once()
    pending_after_failure = service.pending_records()[0]
    clock.value += 0.5
    restarted = PublicSyncService(
        _settings(),
        database,
        transport=transport,
        clock=clock,
        random_value=lambda: 0.5,
    )
    sent = restarted.flush_once()

    assert failed == {"sent": False, "reason": "transport_error", "retry_in_seconds": 0.5}
    assert pending_after_failure["attempts"] == 1
    assert sent["sent"] is True
    assert restarted.pending_records() == []
    request = transport.requests[-1]
    assert request["path"] == "/v1/radio/stations/radiotedu-en/plays"
    assert request["headers"]["X-RadioTEDU-Agent-ID"] == "school-radio-pc"
    assert request["headers"]["X-RadioTEDU-Signature"].startswith("sha256=")
    assert "Sync-Token" not in " ".join(request["headers"])


def test_one_missing_station_secret_does_not_disable_the_other_station(tmp_path: Path) -> None:
    settings = _settings()
    settings.platform_hmac_secret_fr = ""
    transport = RecordingTransport()
    service = PublicSyncService(settings, tmp_path / "public-sync.db", transport=transport, clock=Clock())
    service.publish_snapshot("radiotedu-fr", _snapshot_state("radiotedu-fr", "Bonjour"))
    service.publish_snapshot("radiotedu-en", _snapshot_state("radiotedu-en", "Hello"))

    sent = service.flush_once()
    pending = service.pending_records()

    assert service.configured() is True
    assert service.station_configured("radiotedu-en") is True
    assert service.station_configured("radiotedu-fr") is False
    assert sent["sent"] is True
    assert sent["station_id"] == "radiotedu-en"
    assert [record["station_id"] for record in pending] == ["radiotedu-fr"]
    assert transport.requests[0]["path"] == "/v1/radio/stations/radiotedu-en/snapshot"


def test_state_changes_publish_immediately_and_unchanged_state_gets_ten_second_heartbeat(tmp_path: Path) -> None:
    clock = Clock()
    state = {"title": "First"}
    service = PublicSyncService(
        _settings(),
        tmp_path / "public-sync.db",
        snapshot_providers={
            "radiotedu-en": lambda: _snapshot_state("radiotedu-en", state["title"]),
            "radiotedu-fr": lambda: _snapshot_state("radiotedu-fr", "Bonjour"),
        },
        clock=clock,
    )

    first = service.collect_snapshots_once()
    clock.value += 1
    unchanged = service.collect_snapshots_once()
    state["title"] = "Changed"
    changed = service.collect_snapshots_once()
    clock.value += 9
    before_heartbeat = service.collect_snapshots_once()
    clock.value += 1
    heartbeat = service.collect_snapshots_once()

    assert first == {"radiotedu-en": "changed", "radiotedu-fr": "changed"}
    assert unchanged == {"radiotedu-en": "waiting", "radiotedu-fr": "waiting"}
    assert changed["radiotedu-en"] == "changed"
    assert before_heartbeat == {"radiotedu-en": "waiting", "radiotedu-fr": "heartbeat"}
    assert heartbeat == {"radiotedu-en": "heartbeat", "radiotedu-fr": "waiting"}
    records = service.pending_records()
    en = next(item for item in records if item["station_id"] == "radiotedu-en")
    fr = next(item for item in records if item["station_id"] == "radiotedu-fr")
    assert json.loads(en["body"])["sequence"] == 3
    assert json.loads(fr["body"])["sequence"] == 2


def test_service_is_one_process_level_outbound_owner_with_no_playout_surface(tmp_path: Path) -> None:
    service = PublicSyncService(_settings(), tmp_path / "public-sync.db")

    assert service.station_ids == ("radiotedu-en", "radiotedu-fr")
    assert service.can_control_playout is False
    assert not hasattr(service, "play")
    assert not hasattr(service, "select_music")
    assert service.status()["outbound_only"] is True


def test_operator_status_adapter_exposes_only_sanitized_editorial_state() -> None:
    state = snapshot_state_from_operator_status(
        "radiotedu-en",
        {
            "channel": {"status": "live", "private_path": "C:/secret"},
            "now_playing": {
                "type": "track",
                "title": "Blue Campus",
                "artist": "The Signals",
                "started_at": "2026-07-15T08:00:00+00:00",
                "file_path": "C:/music/private.wav",
            },
            "current_program": {
                "id": "campus-flow",
                "name": "Campus Flow",
                "vibe": "warm focused jazz",
                "host_name": "private host identity",
            },
            "next_programs": [
                {"id": "night-lab", "name": "Night Lab", "vibe": "calm late-night jazz"}
            ],
            "liquidsoap": {"running": True, "mount_active": True, "command_path": "C:/liquidsoap.exe"},
            "configuration": {"ADMIN_AUTH": "enabled", "MUSIC_DIR": "C:/music"},
        },
    )

    rendered = json.dumps(state)
    assert state["now_playing"]["kind"] == "music"
    assert state["speech_state"] == {"active": False, "kind": "music"}
    assert state["current_program"]["sound_tags"] == ["warm", "focused"]
    assert state["next_program"]["sound_tags"] == ["calm"]
    assert state["stream"]["url"] == "https://stream.radiotedu.com/en"
    for private in ("C:/", "private_path", "file_path", "command_path", "ADMIN_AUTH", "host_name"):
        assert private not in rendered


def test_dual_station_supervisor_builds_exactly_one_process_level_sync_owner(tmp_path: Path) -> None:
    from scripts.run_station_forever import build_public_sync_service

    service = build_public_sync_service(
        tmp_path,
        settings=_settings(),
        transport=RecordingTransport(),
    )

    assert isinstance(service, PublicSyncService)
    assert set(service.snapshot_providers) == {"radiotedu-en", "radiotedu-fr"}
    assert service.database_path == tmp_path / "data" / "public-sync" / "public-sync.db"
    assert service.can_control_playout is False


def test_completed_music_and_speech_airtime_are_journaled_once_into_durable_play_outbox(tmp_path: Path) -> None:
    station_db = tmp_path / "station.db"
    with sqlite3.connect(station_db) as conn:
        conn.executescript(
            """
            create table tracks(id integer primary key, title text, artist text, mood text);
            create table programs(id text primary key, name text, vibe text);
            create table play_history(
                id integer primary key, track_id integer, program_id text,
                played_at text, duration_seconds real, source text
            );
            create table station_public_events(
                id integer primary key, event_type text, occurred_at text,
                classification text, duration_seconds real, program_id text,
                title text, metadata_json text
            );
            insert into tracks values (1, 'Blue Campus', 'The Signals', 'warm');
            insert into programs values ('campus-flow', 'Campus Flow', 'warm focused jazz');
            insert into play_history values (
                1, 1, 'campus-flow', '2026-07-15T08:00:00+00:00', 180.0, 'local_file'
            );
            insert into station_public_events values (
                1, 'play.completed', '2026-07-15T07:59:55+00:00',
                'talking', 5.0, 'campus-flow', 'RadioTEDU DJ', '{}'
            );
            """
        )
    service = PublicSyncService(
        _settings(),
        tmp_path / "public-sync.db",
        station_databases={"radiotedu-en": station_db},
    )

    first = service.collect_play_events_once()
    second = service.collect_play_events_once()
    plays = [json.loads(item["body"]) for item in service.pending_records() if item["kind"] == "play"]

    assert first == {"radiotedu-en": 2}
    assert second == {"radiotedu-en": 0}
    assert {event["classification"] for event in plays} == {"music", "talking"}
    assert {event["duration_ms"] for event in plays} == {180_000, 5_000}
    assert all(event["event_type"] == "play.completed" for event in plays)
