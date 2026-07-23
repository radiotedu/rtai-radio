from __future__ import annotations

import argparse
import hmac
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import Settings
from backend.platform_api import handshake_response_signature, sign_platform_headers


STATIONS = ("radiotedu-en", "radiotedu-fr")


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def handshake_station(settings: Settings, base_url: str, station_id: str) -> dict:
    client_nonce = uuid.uuid4().hex
    path = f"/v1/radio/stations/{station_id}/handshake"
    payload = {
        "protocol": "radiotedu-platform/v1",
        "schema_version": 1,
        "station_id": station_id,
        "agent_id": settings.platform_agent_id,
        "client_nonce": client_nonce,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = sign_platform_headers(
        settings,
        method="POST",
        path=path,
        station_id=station_id,
        body=body,
        timestamp=str(int(time.time())),
        nonce=client_nonce,
        idempotency_key=f"handshake-{uuid.uuid4().hex}",
        correlation_id=str(uuid.uuid4()),
        agent_id=settings.platform_agent_id,
    )
    request = Request(
        base_url.rstrip("/") + path,
        data=body,
        method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))
    expected = handshake_response_signature(
        settings,
        station_id=station_id,
        agent_id=result["agent_id"],
        client_nonce=result["client_nonce"],
        server_nonce=result["server_nonce"],
        server_timestamp=result["server_timestamp"],
        correlation_id=result["correlation_id"],
    )
    verified = (
        result.get("authenticated") is True
        and result.get("station_id") == station_id
        and result.get("agent_id") == settings.platform_agent_id
        and result.get("client_nonce") == client_nonce
        and hmac.compare_digest(str(result.get("server_signature", "")), expected)
    )
    if not verified:
        raise RuntimeError(f"{station_id} returned an invalid server proof")
    return {
        "station_id": station_id,
        "verified": True,
        "server_nonce": result["server_nonce"],
        "server_timestamp": result["server_timestamp"],
        "correlation_id": result["correlation_id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the RadioTEDU website's mutual HMAC handshake.")
    parser.add_argument("--base-url", default="https://api.radiotedu.com")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--status-file",
        type=Path,
        default=ROOT / "data" / "runtime" / "web-handshake.json",
    )
    args = parser.parse_args()
    settings = Settings.from_env(args.env_file)
    missing = []
    if not settings.platform_hmac_secret_en:
        missing.append("RADIOTEDU_EN_SNAPSHOT_SECRET")
    if not settings.platform_hmac_secret_fr:
        missing.append("RADIOTEDU_FR_SNAPSHOT_SECRET")
    if missing:
        result = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "state": "awaiting_secret_provisioning",
            "missing": missing,
            "credentials_logged": False,
        }
        atomic_json(args.status_file, result)
        print(json.dumps(result))
        return 2

    checks = []
    try:
        for station_id in STATIONS:
            checks.append(handshake_station(settings, args.base_url, station_id))
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError, RuntimeError) as exc:
        result = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "state": "failed",
            "error_type": type(exc).__name__,
            "credentials_logged": False,
        }
        atomic_json(args.status_file, result)
        print(json.dumps(result))
        return 1

    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "state": "verified",
        "base_url": args.base_url,
        "stations": checks,
        "credentials_logged": False,
    }
    atomic_json(args.status_file, result)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
