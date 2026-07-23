from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.config import Settings
from backend.public_app import create_public_app


def _settings(tmp_path: Path, *, compatibility: bool = True) -> Settings:
    return Settings(
        database_path=str(tmp_path / "public-platform.db"),
        static_dir=str(tmp_path / "static"),
        rss_feeds_path=str(tmp_path / "rss.json"),
        liquidsoap_queue_path=str(tmp_path / "liquidsoap" / "queue.m3u"),
        liquidsoap_script_path=str(tmp_path / "liquidsoap" / "radiotedu.liq"),
        platform_hmac_secret_en="english-test-secret",
        platform_hmac_secret_fr="french-test-secret",
        public_compatibility_enabled=compatibility,
    )


def _frontend(tmp_path: Path) -> Path:
    frontend = tmp_path / "dist" / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "brand").mkdir(parents=True)
    (frontend / "programs").mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>RadioTEDU</title>", encoding="utf-8")
    (frontend / "assets" / "listener.js").write_text("export {};", encoding="utf-8")
    (frontend / "brand" / "radiotedu.png").write_bytes(b"brand")
    (frontend / "programs" / "night_lab.png").write_bytes(b"cover")
    return frontend


def test_public_app_serves_single_bilingual_listener_route_and_only_public_api(tmp_path: Path) -> None:
    client = TestClient(create_public_app(_settings(tmp_path), frontend_dist=_frontend(tmp_path)))

    response = client.get("/ai")
    assert response.status_code == 200
    assert "RadioTEDU" in response.text
    assert client.get("/ai/en").status_code == 404
    assert client.get("/ai/fr").status_code == 404

    assert client.get("/assets/listener.js").status_code == 200
    assert client.get("/brand/radiotedu.png").status_code == 200
    assert client.get("/programs/night_lab.png").status_code == 200
    schema = client.get("/openapi.json").json()
    paths = " ".join(schema["paths"]).lower()
    assert "/v1/radio/stations/{station_id}/status" in schema["paths"]
    for forbidden in (
        "/api/status",
        "/api/air",
        "/api/control",
        "contact",
        "message",
        "purchase",
        "wallet",
        "reward",
        "vote",
        "social",
        "playout",
    ):
        assert forbidden not in paths


def test_english_compatibility_status_reads_canonical_storage_and_is_flagged(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_public_app(settings, frontend_dist=_frontend(tmp_path)))

    canonical = client.get("/v1/radio/stations/radiotedu-en/status")
    compatibility = client.get("/api/public/status")
    assert compatibility.status_code == 200
    assert compatibility.json() == canonical.json()
    assert compatibility.headers["Deprecation"] == "true"
    assert compatibility.headers["Sunset"]
    assert "successor-version" in compatibility.headers["Link"]

    disabled = _settings(tmp_path / "disabled", compatibility=False)
    disabled_client = TestClient(create_public_app(disabled, frontend_dist=_frontend(tmp_path / "disabled")))
    assert disabled_client.get("/api/public/status").status_code == 404


def test_compatibility_sessions_remain_station_scoped_and_snapshot_writes_require_v1(tmp_path: Path) -> None:
    client = TestClient(create_public_app(_settings(tmp_path), frontend_dist=_frontend(tmp_path)))
    body = {"session_id": "session_english_123456"}

    assert client.post("/api/public/session/start", json=body).status_code == 200
    status = client.get("/v1/radio/stations/radiotedu-en/status").json()
    assert status["metrics"]["active_website_listeners"] == 1
    assert client.post("/api/public/session/end", json=body).status_code == 200
    assert client.post("/api/public/snapshot", json={}).status_code == 404
