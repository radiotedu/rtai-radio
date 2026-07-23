from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .database import connect, init_db, now_iso


PROTOCOL = "radiotedu-platform/v1"
STATIONS = frozenset({"radiotedu-en", "radiotedu-fr"})
LANGUAGES = {"radiotedu-en": "en", "radiotedu-fr": "fr"}
MOUNTS = {"radiotedu-en": "/en", "radiotedu-fr": "/fr"}
STREAM_URLS = {
    "radiotedu-en": "https://stream.radiotedu.com/en",
    "radiotedu-fr": "https://stream.radiotedu.com/fr",
}
SOUND_TAGS = frozenset({"warm", "bright", "calm", "focused", "energetic"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StationIdentity(StrictModel):
    id: Literal["radiotedu-en", "radiotedu-fr"]
    language: Literal["en", "fr"]
    display_name: str = Field(min_length=1, max_length=80)


class SpeechState(StrictModel):
    active: bool
    kind: Literal["music", "talking", "idle", "unknown"]


class PublicTrack(StrictModel):
    kind: Literal["music", "talking", "imaging", "unknown"]
    track_id: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=200)
    artist: str | None = Field(default=None, max_length=160)
    cover_id: str | None = Field(default=None, max_length=128)
    mood: str | None = Field(default=None, max_length=80)
    sound_tags: list[Literal["warm", "bright", "calm", "focused", "energetic"]] = Field(
        default_factory=list, max_length=5
    )
    started_at: datetime | None = None


class PublicProgram(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=160)
    vibe: str | None = Field(default=None, max_length=240)
    sound_tags: list[Literal["warm", "bright", "calm", "focused", "energetic"]] = Field(
        default_factory=list, max_length=5
    )


class PublicStream(StrictModel):
    url: str = Field(min_length=1, max_length=256)
    mount: Literal["/en", "/fr"]
    status: Literal["live", "degraded", "offline", "unknown"]
    codec: Literal["AAC-LC"]
    bitrate_kbps: Literal[192]
    public: Literal[True]


class EditorialMetadata(StrictModel):
    sound_tags: list[Literal["warm", "bright", "calm", "focused", "energetic"]] = Field(
        default_factory=list, max_length=5
    )


class SnapshotV2(StrictModel):
    protocol: Literal["radiotedu-platform/v1"]
    schema_version: Literal[2]
    station: StationIdentity
    sequence: int = Field(ge=1)
    generated_at: datetime
    expires_at: datetime | None = None
    operational_state: Literal["live", "degraded", "offline", "starting", "unknown"]
    speech_state: SpeechState
    now_playing: PublicTrack | None = None
    current_program: PublicProgram | None = None
    next_program: PublicProgram | None = None
    stream: PublicStream
    editorial: EditorialMetadata


class PlayEventEnvelope(StrictModel):
    protocol: Literal["radiotedu-platform/v1"]
    schema_version: Literal[1]
    event_id: str = Field(min_length=1, max_length=128)
    station_id: Literal["radiotedu-en", "radiotedu-fr"]
    event_type: Literal["play.completed"]
    occurred_at: datetime
    classification: Literal["music", "talking", "silence", "unknown"]
    duration_ms: int = Field(ge=0, le=86_400_000)
    track_id: str | None = Field(default=None, max_length=128)
    track_title: str | None = Field(default=None, max_length=200)
    artist: str | None = Field(default=None, max_length=160)
    program_id: str | None = Field(default=None, max_length=128)
    program_name: str | None = Field(default=None, max_length=160)
    cover_id: str | None = Field(default=None, max_length=128)
    sound_tags: list[Literal["warm", "bright", "calm", "focused", "energetic"]] = Field(
        default_factory=list, max_length=5
    )


class PublicSession(StrictModel):
    session_id: str = Field(min_length=16, max_length=128)


class HandshakeRequest(StrictModel):
    protocol: Literal["radiotedu-platform/v1"]
    schema_version: Literal[1]
    station_id: Literal["radiotedu-en", "radiotedu-fr"]
    agent_id: Literal["school-radio-pc"]
    client_nonce: str = Field(min_length=16, max_length=128)


def _secret_for_station(settings: Settings, station_id: str) -> str:
    if station_id == "radiotedu-en":
        return settings.platform_hmac_secret_en
    if station_id == "radiotedu-fr":
        return settings.platform_hmac_secret_fr
    return ""


def _body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _canonical_request(
    *,
    method: str,
    path: str,
    agent_id: str,
    station_id: str,
    timestamp: str,
    nonce: str,
    idempotency_key: str,
    correlation_id: str,
    body_hash: str,
) -> bytes:
    fields = (
        method.upper(),
        path,
        agent_id,
        station_id,
        timestamp,
        nonce,
        idempotency_key,
        correlation_id,
        body_hash,
    )
    return "\n".join(fields).encode("utf-8")


def sign_platform_headers(
    settings: Settings,
    *,
    method: str,
    path: str,
    station_id: str,
    body: bytes,
    timestamp: str,
    nonce: str,
    idempotency_key: str,
    correlation_id: str,
    agent_id: str = "school-radio-pc",
) -> dict[str, str]:
    digest = _body_hash(body)
    canonical = _canonical_request(
        method=method,
        path=path,
        agent_id=agent_id,
        station_id=station_id,
        timestamp=timestamp,
        nonce=nonce,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        body_hash=digest,
    )
    signature = hmac.new(
        _secret_for_station(settings, station_id).encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-RadioTEDU-Agent-ID": agent_id,
        "X-RadioTEDU-Timestamp": timestamp,
        "X-RadioTEDU-Nonce": nonce,
        "X-RadioTEDU-Signature": f"sha256={signature}",
        "Idempotency-Key": idempotency_key,
        "X-Correlation-ID": correlation_id,
    }


def handshake_response_signature(
    settings: Settings,
    *,
    station_id: str,
    agent_id: str,
    client_nonce: str,
    server_nonce: str,
    server_timestamp: str,
    correlation_id: str,
) -> str:
    """Return the proof used by the broadcast computer to authenticate the website."""

    fields = (
        PROTOCOL,
        "handshake-response",
        station_id,
        agent_id,
        client_nonce,
        server_nonce,
        server_timestamp,
        correlation_id,
    )
    canonical = "\n".join(fields).encode("utf-8")
    secret = _secret_for_station(settings, station_id)
    return "sha256=" + hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def _correlation_id(request: Request) -> str:
    value = (request.headers.get("X-Correlation-ID") or "").strip()
    if _SAFE_ID.fullmatch(value):
        return value
    return str(uuid.uuid4())


def _error(status: int, code: str, message: str, correlation_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {"code": code, "message": message},
            "correlation_id": correlation_id,
        },
    )


def _authenticate(
    settings: Settings,
    request: Request,
    station_id: str,
    body: bytes,
) -> tuple[dict[str, str] | None, JSONResponse | None]:
    correlation_id = _correlation_id(request)
    if station_id not in STATIONS:
        return None, _error(404, "unknown_station", "station is not available", correlation_id)
    agent_id = (request.headers.get("X-RadioTEDU-Agent-ID") or "").strip()
    if agent_id != settings.platform_agent_id:
        return None, _error(403, "unknown_agent", "service identity is not authorized", correlation_id)
    if settings.platform_agent_scope != "agent:playout":
        return None, _error(403, "insufficient_scope", "service identity scope is not authorized", correlation_id)
    timestamp = (request.headers.get("X-RadioTEDU-Timestamp") or "").strip()
    nonce = (request.headers.get("X-RadioTEDU-Nonce") or "").strip()
    signature = (request.headers.get("X-RadioTEDU-Signature") or "").strip()
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not all((timestamp, nonce, signature, idempotency_key, correlation_id)):
        return None, _error(401, "authentication_failed", "request authentication failed", correlation_id)
    if not _SAFE_ID.fullmatch(nonce) or not _SAFE_ID.fullmatch(idempotency_key):
        return None, _error(401, "authentication_failed", "request authentication failed", correlation_id)
    try:
        timestamp_value = int(timestamp)
    except ValueError:
        return None, _error(401, "authentication_failed", "request authentication failed", correlation_id)
    if abs(int(time.time()) - timestamp_value) > int(settings.platform_timestamp_skew_seconds):
        return None, _error(401, "stale_timestamp", "request timestamp is outside the allowed window", correlation_id)
    secret = _secret_for_station(settings, station_id)
    if not secret:
        return None, _error(503, "authentication_unavailable", "request authentication is not configured", correlation_id)
    digest = _body_hash(body)
    canonical = _canonical_request(
        method=request.method,
        path=request.url.path,
        agent_id=agent_id,
        station_id=station_id,
        timestamp=timestamp,
        nonce=nonce,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        body_hash=digest,
    )
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None, _error(401, "authentication_failed", "request authentication failed", correlation_id)
    return {
        "agent_id": agent_id,
        "nonce": nonce,
        "idempotency_key": idempotency_key,
        "correlation_id": correlation_id,
        "body_hash": digest,
    }, None


def _validate_snapshot_identity(snapshot: SnapshotV2, station_id: str) -> bool:
    return (
        snapshot.station.id == station_id
        and snapshot.station.language == LANGUAGES[station_id]
        and snapshot.stream.mount == MOUNTS[station_id]
        and snapshot.stream.url == STREAM_URLS[station_id]
    )


def _existing_idempotent_response(conn, auth: dict[str, str], method: str, path: str):
    row = conn.execute(
        "select method, path, body_hash, status_code, response_json from public_idempotency_records where agent_id=? and idempotency_key=?",
        (auth["agent_id"], auth["idempotency_key"]),
    ).fetchone()
    if row is None:
        return None
    if row["method"] != method or row["path"] != path or row["body_hash"] != auth["body_hash"]:
        return "conflict"
    return int(row["status_code"]), json.loads(row["response_json"])


def _register_nonce(conn, auth: dict[str, str]) -> bool:
    seen = conn.execute(
        "select 1 from public_agent_nonces where agent_id=? and nonce=?",
        (auth["agent_id"], auth["nonce"]),
    ).fetchone()
    if seen is not None:
        return False
    conn.execute(
        "insert into public_agent_nonces(agent_id, nonce, seen_at) values (?, ?, ?)",
        (auth["agent_id"], auth["nonce"], now_iso()),
    )
    return True


def _store_idempotent_response(
    conn,
    auth: dict[str, str],
    method: str,
    path: str,
    status_code: int,
    payload: dict,
) -> None:
    conn.execute(
        """
        insert into public_idempotency_records(
            agent_id, idempotency_key, method, path, body_hash,
            status_code, response_json, created_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            auth["agent_id"],
            auth["idempotency_key"],
            method,
            path,
            auth["body_hash"],
            status_code,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            now_iso(),
        ),
    )


def _session_metrics(settings: Settings, station_id: str) -> dict[str, int]:
    cutoff = datetime.fromtimestamp(time.time() - 30, tz=timezone.utc).isoformat()
    with connect(settings) as conn:
        listeners = conn.execute(
            "select count(*) from public_station_sessions where station_id=? and ended_at is null and last_seen_at>=?",
            (station_id, cutoff),
        ).fetchone()[0]
    return {"active_website_listeners": int(listeners)}


def _airtime_metrics(settings: Settings, station_id: str) -> dict[str, int | None]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    with connect(settings) as conn:
        row = conn.execute(
            """
            select
                coalesce(sum(case when classification='music' then duration_ms else 0 end), 0) as music_ms,
                coalesce(sum(case when classification='talking' then duration_ms else 0 end), 0) as talking_ms
            from public_play_events
            where station_id=? and occurred_at>=? and classification in ('music', 'talking')
            """,
            (station_id, cutoff),
        ).fetchone()
    music_ms = int(row["music_ms"])
    talking_ms = int(row["talking_ms"])
    total = music_ms + talking_ms
    if total <= 0:
        return {
            "window_days": 14,
            "classified_duration_ms": 0,
            "music_percent": None,
            "talking_percent": None,
        }
    music_percent = int((music_ms * 100 / total) + 0.5)
    return {
        "window_days": 14,
        "classified_duration_ms": total,
        "music_percent": music_percent,
        "talking_percent": 100 - music_percent,
    }


def station_status_payload(settings: Settings, station_id: str):
    """Read the canonical public status shared by v1 and the legacy EN adapter."""
    if station_id not in STATIONS:
        return _error(404, "unknown_station", "station is not available", str(uuid.uuid4()))
    with connect(settings) as conn:
        row = conn.execute(
            "select sequence, received_at, payload_json from public_station_snapshots where station_id=?",
            (station_id,),
        ).fetchone()
    snapshot = json.loads(row["payload_json"]) if row is not None else None
    received_at = row["received_at"] if row is not None else None
    fresh = False
    if received_at:
        try:
            received = datetime.fromisoformat(received_at)
            fresh = datetime.now(timezone.utc) - received <= timedelta(seconds=settings.snapshot_ttl_seconds)
        except (TypeError, ValueError):
            fresh = False
    metrics = _session_metrics(settings, station_id)
    metrics["airtime"] = _airtime_metrics(settings, station_id)
    return {
        "protocol": PROTOCOL,
        "station_id": station_id,
        "online": bool(snapshot is not None and fresh),
        "stale": bool(snapshot is not None and not fresh),
        "received_at": received_at,
        "snapshot": snapshot,
        "metrics": metrics,
    }


def apply_session_operation(settings: Settings, station_id: str, session_id: str, operation: str):
    """Store station-scoped listener presence without IP or browser identity."""
    if station_id not in STATIONS:
        return _error(404, "unknown_station", "station is not available", str(uuid.uuid4()))
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return _error(422, "invalid_session", "session identifier is invalid", str(uuid.uuid4()))
    timestamp = now_iso()
    with connect(settings) as conn:
        if operation == "end":
            conn.execute(
                "update public_station_sessions set last_seen_at=?, ended_at=? where station_id=? and session_id=?",
                (timestamp, timestamp, station_id, session_id),
            )
        else:
            conn.execute(
                """
                insert into public_station_sessions(station_id, session_id, started_at, last_seen_at, ended_at)
                values (?, ?, ?, ?, null)
                on conflict(station_id, session_id) do update set
                    last_seen_at=excluded.last_seen_at,
                    ended_at=null
                """,
                (station_id, session_id, timestamp, timestamp),
            )
        conn.commit()
    return {"station_id": station_id, "session_id": session_id, **_session_metrics(settings, station_id)}


async def _read_limited_body(request: Request, max_bytes: int) -> bytes | None:
    """Read an upload incrementally and stop before retaining more than the cap."""

    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) < 0 or int(declared_length) > max_bytes:
                return None
        except ValueError:
            return None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            return None
        body.extend(chunk)
    return bytes(body)


def install_platform_routes(app: FastAPI, settings: Settings) -> None:
    init_db(settings)

    @app.post("/v1/radio/stations/{station_id}/handshake")
    async def handshake(station_id: str, request: Request):
        correlation_id = _correlation_id(request)
        body = await _read_limited_body(request, 16 * 1024)
        if body is None:
            return _error(413, "payload_too_large", "handshake exceeds the allowed size", correlation_id)
        auth, auth_error = _authenticate(settings, request, station_id, body)
        if auth_error is not None:
            return auth_error
        assert auth is not None
        try:
            envelope = HandshakeRequest.model_validate_json(body)
        except ValidationError:
            return _error(422, "invalid_payload", "handshake payload is invalid", correlation_id)
        if (
            envelope.station_id != station_id
            or envelope.agent_id != auth["agent_id"]
            or envelope.client_nonce != auth["nonce"]
            or not _SAFE_ID.fullmatch(envelope.client_nonce)
        ):
            return _error(422, "invalid_payload", "handshake identity is invalid", correlation_id)
        with connect(settings) as conn:
            conn.execute("begin immediate")
            if not _register_nonce(conn, auth):
                conn.rollback()
                return _error(409, "replayed_nonce", "request nonce has already been used", correlation_id)
            conn.commit()
        server_nonce = uuid.uuid4().hex
        server_timestamp = str(int(time.time()))
        proof = handshake_response_signature(
            settings,
            station_id=station_id,
            agent_id=auth["agent_id"],
            client_nonce=auth["nonce"],
            server_nonce=server_nonce,
            server_timestamp=server_timestamp,
            correlation_id=correlation_id,
        )
        return {
            "protocol": PROTOCOL,
            "schema_version": 1,
            "authenticated": True,
            "station_id": station_id,
            "agent_id": auth["agent_id"],
            "client_nonce": auth["nonce"],
            "server_nonce": server_nonce,
            "server_timestamp": server_timestamp,
            "expires_in_seconds": int(settings.platform_timestamp_skew_seconds),
            "correlation_id": correlation_id,
            "server_signature": proof,
        }

    @app.post("/v1/radio/stations/{station_id}/snapshot", status_code=201)
    async def store_snapshot(station_id: str, request: Request):
        correlation_id = _correlation_id(request)
        body = await _read_limited_body(request, int(settings.platform_snapshot_max_bytes))
        if body is None:
            return _error(413, "payload_too_large", "snapshot exceeds the allowed size", correlation_id)
        auth, auth_error = _authenticate(settings, request, station_id, body)
        if auth_error is not None:
            return auth_error
        assert auth is not None
        try:
            snapshot = SnapshotV2.model_validate_json(body)
        except ValidationError:
            return _error(422, "invalid_payload", "snapshot payload is invalid", correlation_id)
        if not _validate_snapshot_identity(snapshot, station_id):
            return _error(422, "invalid_payload", "snapshot identity is invalid", correlation_id)
        response = {
            "stored": True,
            "station_id": station_id,
            "sequence": snapshot.sequence,
            "correlation_id": correlation_id,
        }
        path = request.url.path
        with connect(settings) as conn:
            conn.execute("begin immediate")
            if not _register_nonce(conn, auth):
                conn.rollback()
                return _error(409, "replayed_nonce", "request nonce has already been used", correlation_id)
            existing = _existing_idempotent_response(conn, auth, "POST", path)
            if existing == "conflict":
                conn.rollback()
                return _error(409, "idempotency_conflict", "idempotency key conflicts with another request", correlation_id)
            if existing is not None:
                conn.commit()
                status_code, stored_response = existing
                return JSONResponse(status_code=status_code, content=stored_response)
            current = conn.execute(
                "select sequence from public_station_snapshots where station_id=?",
                (station_id,),
            ).fetchone()
            if current is not None and snapshot.sequence <= int(current["sequence"]):
                conn.rollback()
                return _error(409, "out_of_order_sequence", "snapshot sequence is not newer", correlation_id)
            payload = snapshot.model_dump(mode="json")
            conn.execute(
                """
                insert into public_station_snapshots(
                    station_id, sequence, generated_at, expires_at, received_at,
                    correlation_id, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?)
                on conflict(station_id) do update set
                    sequence=excluded.sequence,
                    generated_at=excluded.generated_at,
                    expires_at=excluded.expires_at,
                    received_at=excluded.received_at,
                    correlation_id=excluded.correlation_id,
                    payload_json=excluded.payload_json
                """,
                (
                    station_id,
                    snapshot.sequence,
                    payload["generated_at"],
                    payload.get("expires_at"),
                    now_iso(),
                    correlation_id,
                    json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                ),
            )
            _store_idempotent_response(conn, auth, "POST", path, 201, response)
            conn.commit()
        return JSONResponse(status_code=201, content=response)

    @app.post("/v1/radio/stations/{station_id}/plays", status_code=201)
    async def store_play(station_id: str, request: Request):
        correlation_id = _correlation_id(request)
        body = await _read_limited_body(request, int(settings.platform_snapshot_max_bytes))
        if body is None:
            return _error(413, "payload_too_large", "play event exceeds the allowed size", correlation_id)
        auth, auth_error = _authenticate(settings, request, station_id, body)
        if auth_error is not None:
            return auth_error
        assert auth is not None
        try:
            event = PlayEventEnvelope.model_validate_json(body)
        except ValidationError:
            return _error(422, "invalid_payload", "play event payload is invalid", correlation_id)
        if event.station_id != station_id:
            return _error(422, "invalid_payload", "play event identity is invalid", correlation_id)
        response = {
            "stored": True,
            "station_id": station_id,
            "event_id": event.event_id,
            "correlation_id": correlation_id,
        }
        path = request.url.path
        with connect(settings) as conn:
            conn.execute("begin immediate")
            if not _register_nonce(conn, auth):
                conn.rollback()
                return _error(409, "replayed_nonce", "request nonce has already been used", correlation_id)
            existing = _existing_idempotent_response(conn, auth, "POST", path)
            if existing == "conflict":
                conn.rollback()
                return _error(409, "idempotency_conflict", "idempotency key conflicts with another request", correlation_id)
            if existing is not None:
                conn.commit()
                status_code, stored_response = existing
                return JSONResponse(status_code=status_code, content=stored_response)
            payload = event.model_dump(mode="json")
            conn.execute(
                """
                insert into public_play_events(
                    station_id, event_id, occurred_at, classification, duration_ms,
                    payload_json, correlation_id, received_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(station_id, event_id) do nothing
                """,
                (
                    station_id,
                    event.event_id,
                    payload["occurred_at"],
                    event.classification,
                    event.duration_ms,
                    json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                    correlation_id,
                    now_iso(),
                ),
            )
            _store_idempotent_response(conn, auth, "POST", path, 201, response)
            conn.commit()
        return JSONResponse(status_code=201, content=response)

    @app.put("/v1/radio/stations/{station_id}/covers/{cover_id}", status_code=201)
    async def store_cover(station_id: str, cover_id: str, request: Request):
        correlation_id = _correlation_id(request)
        body = await _read_limited_body(request, 5 * 1024 * 1024)
        if body is None:
            return _error(413, "payload_too_large", "cover exceeds the allowed size", correlation_id)
        auth, auth_error = _authenticate(settings, request, station_id, body)
        if auth_error is not None:
            return auth_error
        assert auth is not None
        content_type = (request.headers.get("content-type") or "").split(";", 1)[0].lower()
        if not _SAFE_ID.fullmatch(cover_id) or content_type not in {"image/jpeg", "image/png", "image/webp"}:
            return _error(422, "invalid_payload", "cover payload is invalid", correlation_id)
        response = {
            "stored": True,
            "station_id": station_id,
            "cover_id": cover_id,
            "correlation_id": correlation_id,
        }
        path = request.url.path
        with connect(settings) as conn:
            conn.execute("begin immediate")
            if not _register_nonce(conn, auth):
                conn.rollback()
                return _error(409, "replayed_nonce", "request nonce has already been used", correlation_id)
            existing = _existing_idempotent_response(conn, auth, "PUT", path)
            if existing == "conflict":
                conn.rollback()
                return _error(409, "idempotency_conflict", "idempotency key conflicts with another request", correlation_id)
            if existing is not None:
                conn.commit()
                status_code, stored_response = existing
                return JSONResponse(status_code=status_code, content=stored_response)
            conn.execute(
                """
                insert into public_cover_assets(station_id, cover_id, content_type, body, correlation_id, updated_at)
                values (?, ?, ?, ?, ?, ?)
                on conflict(station_id, cover_id) do update set
                    content_type=excluded.content_type,
                    body=excluded.body,
                    correlation_id=excluded.correlation_id,
                    updated_at=excluded.updated_at
                """,
                (station_id, cover_id, content_type, body, correlation_id, now_iso()),
            )
            _store_idempotent_response(conn, auth, "PUT", path, 201, response)
            conn.commit()
        return JSONResponse(status_code=201, content=response)

    @app.get("/v1/radio/stations/{station_id}/covers/{cover_id}", include_in_schema=False)
    def get_cover(station_id: str, cover_id: str):
        if station_id not in STATIONS or not _SAFE_ID.fullmatch(cover_id):
            return Response(status_code=404)
        with connect(settings) as conn:
            row = conn.execute(
                "select content_type, body from public_cover_assets where station_id=? and cover_id=?",
                (station_id, cover_id),
            ).fetchone()
        if row is None:
            return Response(status_code=404)
        return Response(content=row["body"], media_type=row["content_type"])

    @app.get("/v1/radio/stations/{station_id}/status")
    def station_status(station_id: str):
        return station_status_payload(settings, station_id)

    @app.post("/v1/radio/stations/{station_id}/sessions/start")
    def session_start(station_id: str, session: PublicSession):
        return apply_session_operation(settings, station_id, session.session_id, "start")

    @app.post("/v1/radio/stations/{station_id}/sessions/heartbeat")
    def session_heartbeat(station_id: str, session: PublicSession):
        return apply_session_operation(settings, station_id, session.session_id, "heartbeat")

    @app.post("/v1/radio/stations/{station_id}/sessions/end")
    def session_end(station_id: str, session: PublicSession):
        return apply_session_operation(settings, station_id, session.session_id, "end")
