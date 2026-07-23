from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from backend.config import Settings
from backend.platform_api import (
    handshake_response_signature,
    install_platform_routes,
    sign_platform_headers,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=str(tmp_path / "public-platform.db"),
        static_dir=str(tmp_path / "static"),
        rss_feeds_path=str(tmp_path / "rss.json"),
        liquidsoap_queue_path=str(tmp_path / "liquidsoap" / "queue.m3u"),
        liquidsoap_script_path=str(tmp_path / "liquidsoap" / "radiotedu.liq"),
        platform_hmac_secret_en="english-test-secret",
        platform_hmac_secret_fr="french-test-secret",
    )


def _client(settings: Settings) -> TestClient:
    app = FastAPI()
    install_platform_routes(app, settings)
    return TestClient(app)


def _snapshot(station_id: str = "radiotedu-en", sequence: int = 1) -> dict:
    language = "en" if station_id.endswith("-en") else "fr"
    mount = "/en" if language == "en" else "/fr"
    return {
        "protocol": "radiotedu-platform/v1",
        "schema_version": 2,
        "station": {
            "id": station_id,
            "language": language,
            "display_name": "RadioTEDU" if language == "en" else "RadioTEDU Français",
        },
        "sequence": sequence,
        "generated_at": "2026-07-15T08:00:00+00:00",
        "expires_at": "2026-07-15T08:01:00+00:00",
        "operational_state": "live",
        "speech_state": {"active": False, "kind": "music"},
        "now_playing": {
            "kind": "music",
            "track_id": "track-1",
            "title": "Blue Campus",
            "artist": "The Signals",
            "cover_id": "track-1",
            "mood": "warm",
            "sound_tags": ["warm", "focused"],
            "started_at": "2026-07-15T07:58:00+00:00",
        },
        "current_program": {
            "id": "campus-flow",
            "name": "Campus Flow",
            "vibe": "warm focused jazz",
            "sound_tags": ["warm", "focused"],
        },
        "next_program": {
            "id": "night-lab",
            "name": "Night Lab",
            "vibe": "calm late-night jazz",
            "sound_tags": ["calm"],
        },
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


def _play_event(
    event_id: str,
    classification: str,
    duration_ms: int,
    occurred_at: datetime,
    station_id: str = "radiotedu-en",
) -> dict:
    return {
        "protocol": "radiotedu-platform/v1",
        "schema_version": 1,
        "event_id": event_id,
        "station_id": station_id,
        "event_type": "play.completed",
        "occurred_at": occurred_at.isoformat(),
        "classification": classification,
        "duration_ms": duration_ms,
        "track_id": f"track-{event_id}",
        "track_title": f"Track {event_id}",
        "artist": "RadioTEDU",
        "program_id": "campus-flow",
        "program_name": "Campus Flow",
        "cover_id": f"cover-{event_id}",
        "sound_tags": ["warm"],
    }


def _signed_post(
    client: TestClient,
    settings: Settings,
    path: str,
    payload: dict,
    *,
    station_id: str = "radiotedu-en",
    nonce: str | None = None,
    idempotency_key: str | None = None,
    timestamp: str | None = None,
    agent_id: str = "school-radio-pc",
):
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    headers = sign_platform_headers(
        settings,
        method="POST",
        path=path,
        station_id=station_id,
        body=body,
        agent_id=agent_id,
        timestamp=timestamp or str(int(time.time())),
        nonce=nonce or uuid.uuid4().hex,
        idempotency_key=idempotency_key or uuid.uuid4().hex,
        correlation_id=str(uuid.uuid4()),
    )
    return client.post(path, content=body, headers={**headers, "Content-Type": "application/json"})


def test_valid_hmac_snapshot_is_stored_and_returned_by_station_status(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    path = "/v1/radio/stations/radiotedu-en/snapshot"

    stored = _signed_post(client, settings, path, _snapshot())
    status = client.get("/v1/radio/stations/radiotedu-en/status")

    assert stored.status_code == 201
    assert stored.json()["stored"] is True
    assert stored.json()["station_id"] == "radiotedu-en"
    assert stored.json()["sequence"] == 1
    assert stored.json()["correlation_id"]
    assert status.status_code == 200
    assert status.json()["snapshot"]["now_playing"]["title"] == "Blue Campus"
    assert status.json()["snapshot"]["stream"]["url"] == "https://stream.radiotedu.com/en"


def test_mutual_handshake_authenticates_broadcast_agent_and_website(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    station_id = "radiotedu-en"
    path = f"/v1/radio/stations/{station_id}/handshake"
    client_nonce = uuid.uuid4().hex
    payload = {
        "protocol": "radiotedu-platform/v1",
        "schema_version": 1,
        "station_id": station_id,
        "agent_id": "school-radio-pc",
        "client_nonce": client_nonce,
    }

    response = _signed_post(
        client,
        settings,
        path,
        payload,
        nonce=client_nonce,
        idempotency_key=f"handshake-{uuid.uuid4().hex}",
    )
    result = response.json()
    expected_proof = handshake_response_signature(
        settings,
        station_id=station_id,
        agent_id=result["agent_id"],
        client_nonce=result["client_nonce"],
        server_nonce=result["server_nonce"],
        server_timestamp=result["server_timestamp"],
        correlation_id=result["correlation_id"],
    )

    assert response.status_code == 200
    assert result["authenticated"] is True
    assert result["station_id"] == station_id
    assert result["client_nonce"] == client_nonce
    assert result["expires_in_seconds"] == 60
    assert result["server_signature"] == expected_proof

    replay = _signed_post(
        client,
        settings,
        path,
        payload,
        nonce=client_nonce,
        idempotency_key=f"handshake-{uuid.uuid4().hex}",
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "replayed_nonce"


def test_hmac_binds_agent_station_path_replay_fields_and_body(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    path = "/v1/radio/stations/radiotedu-en/snapshot"
    body = json.dumps(_snapshot(), separators=(",", ":")).encode()
    nonce = uuid.uuid4().hex
    headers = sign_platform_headers(
        settings,
        method="POST",
        path=path,
        station_id="radiotedu-en",
        body=body,
        agent_id="school-radio-pc",
        timestamp=str(int(time.time())),
        nonce=nonce,
        idempotency_key="idem-auth-binding",
        correlation_id=str(uuid.uuid4()),
    )

    tampered = client.post(path, content=body + b" ", headers={**headers, "Content-Type": "application/json"})
    valid = client.post(path, content=body, headers={**headers, "Content-Type": "application/json"})
    replay_headers = sign_platform_headers(
        settings,
        method="POST",
        path=path,
        station_id="radiotedu-en",
        body=body,
        agent_id="school-radio-pc",
        timestamp=str(int(time.time())),
        nonce=nonce,
        idempotency_key="a-new-idempotency-key",
        correlation_id=str(uuid.uuid4()),
    )
    replay = client.post(
        path,
        content=body,
        headers={**replay_headers, "Content-Type": "application/json"},
    )

    assert tampered.status_code == 401
    assert tampered.json()["error"]["code"] == "authentication_failed"
    assert valid.status_code == 201
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "replayed_nonce"
    assert "secret" not in json.dumps(replay.json()).lower()


def test_unknown_agent_scope_wrong_station_stale_timestamp_and_private_payload_are_rejected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    path = "/v1/radio/stations/radiotedu-en/snapshot"

    unknown_agent = _signed_post(client, settings, path, _snapshot(), agent_id="other-computer")
    stale = _signed_post(
        client,
        settings,
        path,
        _snapshot(),
        timestamp=str(int(time.time()) - 61),
    )
    private = _snapshot()
    private["local_path"] = "C:/private/music/file.wav"
    private_payload = _signed_post(client, settings, path, private)
    wrong_station = _signed_post(
        client,
        settings,
        "/v1/radio/stations/radiotedu-fr/snapshot",
        _snapshot("radiotedu-en"),
        station_id="radiotedu-en",
    )

    assert unknown_agent.status_code == 403
    assert unknown_agent.json()["error"]["code"] == "unknown_agent"
    assert stale.status_code == 401
    assert stale.json()["error"]["code"] == "stale_timestamp"
    assert private_payload.status_code == 422
    assert private_payload.json()["error"]["code"] == "invalid_payload"
    assert wrong_station.status_code == 401
    assert wrong_station.json()["error"]["code"] == "authentication_failed"


def test_snapshot_sequence_idempotency_size_and_correlation_contract(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    path = "/v1/radio/stations/radiotedu-en/snapshot"

    first = _signed_post(client, settings, path, _snapshot(sequence=2), idempotency_key="same-operation")
    duplicate = _signed_post(client, settings, path, _snapshot(sequence=2), idempotency_key="same-operation")
    out_of_order = _signed_post(client, settings, path, _snapshot(sequence=1))
    oversized_body = b"{" + (b" " * (256 * 1024)) + b"}"
    oversized_headers = sign_platform_headers(
        settings,
        method="POST",
        path=path,
        station_id="radiotedu-en",
        body=oversized_body,
        timestamp=str(int(time.time())),
        nonce=uuid.uuid4().hex,
        idempotency_key=uuid.uuid4().hex,
        correlation_id=str(uuid.uuid4()),
    )
    oversized = client.post(path, content=oversized_body, headers=oversized_headers)

    assert first.status_code == 201
    assert duplicate.status_code == 201
    assert duplicate.json() == first.json()
    assert out_of_order.status_code == 409
    assert out_of_order.json()["error"]["code"] == "out_of_order_sequence"
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "payload_too_large"
    for response in (out_of_order, oversized):
        assert response.json()["correlation_id"]


def test_chunked_snapshot_is_capped_without_buffering_the_entire_request(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    settings.platform_snapshot_max_bytes = 16
    client = _client(settings)
    path = "/v1/radio/stations/radiotedu-en/snapshot"
    body = b'{"padding":"' + (b"x" * 64) + b'"}'
    headers = sign_platform_headers(
        settings,
        method="POST",
        path=path,
        station_id="radiotedu-en",
        body=body,
        timestamp=str(int(time.time())),
        nonce=uuid.uuid4().hex,
        idempotency_key=uuid.uuid4().hex,
        correlation_id=str(uuid.uuid4()),
    )

    async def forbidden_body(_request):
        raise AssertionError("platform uploads must use the bounded streaming reader")

    monkeypatch.setattr(StarletteRequest, "body", forbidden_body)
    response = client.post(path, content=iter((body[:8], body[8:24], body[24:])), headers=headers)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_public_openapi_has_status_only_and_no_remote_playout_or_engagement_capabilities(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))
    paths = set(client.get("/openapi.json").json()["paths"])
    rendered = " ".join(sorted(paths)).lower()

    assert "/v1/radio/stations/{station_id}/snapshot" in paths
    assert "/v1/radio/stations/{station_id}/handshake" in paths
    assert "/v1/radio/stations/{station_id}/plays" in paths
    assert "/v1/radio/stations/{station_id}/covers/{cover_id}" in paths
    assert "/v1/radio/stations/{station_id}/status" in paths
    for forbidden in ("admin", "contact", "message", "purchase", "wallet", "reward", "vote", "social", "playout", "control"):
        assert forbidden not in rendered


def test_play_events_are_idempotent_and_drive_rolling_14_day_airtime_split(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    path = "/v1/radio/stations/radiotedu-en/plays"
    now = datetime.now(timezone.utc)

    events = (
        _play_event("music", "music", 2_000, now - timedelta(days=2)),
        _play_event("talk", "talking", 1_000, now - timedelta(days=1)),
        _play_event("silence", "silence", 90_000, now - timedelta(hours=1)),
        _play_event("unknown", "unknown", 90_000, now - timedelta(hours=1)),
        _play_event("old", "music", 999_000, now - timedelta(days=15)),
    )
    responses = [_signed_post(client, settings, path, event) for event in events]
    duplicate = _signed_post(client, settings, path, events[0], idempotency_key="duplicate-event-request")
    status = client.get("/v1/radio/stations/radiotedu-en/status").json()
    french = client.get("/v1/radio/stations/radiotedu-fr/status").json()

    assert all(response.status_code == 201 for response in responses)
    assert duplicate.status_code == 201
    assert status["metrics"]["airtime"] == {
        "window_days": 14,
        "classified_duration_ms": 3_000,
        "music_percent": 67,
        "talking_percent": 33,
    }
    assert french["metrics"]["airtime"] == {
        "window_days": 14,
        "classified_duration_ms": 0,
        "music_percent": None,
        "talking_percent": None,
    }


def test_cover_upload_and_station_scoped_listener_sessions_store_no_browser_identity(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    cover_path = "/v1/radio/stations/radiotedu-en/covers/track-1"
    cover = b"\x89PNG\r\nRadioTEDU"
    headers = sign_platform_headers(
        settings,
        method="PUT",
        path=cover_path,
        station_id="radiotedu-en",
        body=cover,
        timestamp=str(int(time.time())),
        nonce=uuid.uuid4().hex,
        idempotency_key=uuid.uuid4().hex,
        correlation_id=str(uuid.uuid4()),
    )

    uploaded = client.put(cover_path, content=cover, headers={**headers, "Content-Type": "image/png"})
    fetched = client.get(cover_path)
    session_id = "session_1234567890abcdef"
    started_en = client.post(
        "/v1/radio/stations/radiotedu-en/sessions/start",
        json={"session_id": session_id},
        headers={"User-Agent": "must-not-be-stored"},
    )
    status_en = client.get("/v1/radio/stations/radiotedu-en/status").json()
    status_fr = client.get("/v1/radio/stations/radiotedu-fr/status").json()
    ended_en = client.post(
        "/v1/radio/stations/radiotedu-en/sessions/end",
        json={"session_id": session_id},
    )

    assert uploaded.status_code == 201
    assert fetched.content == cover
    assert fetched.headers["content-type"] == "image/png"
    assert started_en.status_code == 200
    assert status_en["metrics"]["active_website_listeners"] == 1
    assert status_fr["metrics"]["active_website_listeners"] == 0
    assert ended_en.json()["active_website_listeners"] == 0
    assert "user-agent" not in json.dumps(status_en).lower()


def test_service_scope_is_restricted_to_agent_playout(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.platform_agent_scope = "agent:read"
    client = _client(settings)
    response = _signed_post(
        client,
        settings,
        "/v1/radio/stations/radiotedu-en/snapshot",
        _snapshot(),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"
