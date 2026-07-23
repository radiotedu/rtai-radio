from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
import uuid


STATIONS = ("radiotedu-en", "radiotedu-fr")
FORBIDDEN_PUBLIC_TERMS = (
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
)


def request_json(base_url: str, path: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "User-Agent": "RadioTEDU-Public-Smoke/2.0"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return {"ok": response.status < 400, "status": response.status, "json": json.loads(response.read())}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": exc.read(300).decode("utf-8", errors="replace")}
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "status": None, "error": str(exc)}


def request_text(base_url: str, path: str) -> dict:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        headers={"User-Agent": "RadioTEDU-Public-Smoke/2.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            text = response.read(2048).decode("utf-8", errors="replace")
            return {"ok": response.status < 400, "status": response.status, "has_html": "<html" in text.lower()}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "status": None, "error": str(exc)}


def run_smoke(base_url: str) -> dict:
    results: dict[str, object] = {
        "ai": request_text(base_url, "/ai"),
        "ai_en_absent": request_text(base_url, "/ai/en"),
        "ai_fr_absent": request_text(base_url, "/ai/fr"),
    }
    openapi = request_json(base_url, "/openapi.json")
    schema_paths = " ".join((openapi.get("json") or {}).get("paths", {})).lower()
    forbidden_paths = [term for term in FORBIDDEN_PUBLIC_TERMS if term in schema_paths]
    results["openapi"] = {"ok": bool(openapi.get("ok")) and not forbidden_paths, "forbidden_paths": forbidden_paths}

    station_results: dict[str, object] = {}
    for station_id in STATIONS:
        root = f"/v1/radio/stations/{station_id}"
        session_id = f"smoke_{uuid.uuid4()}"
        session = {"session_id": session_id}
        start = request_json(base_url, f"{root}/sessions/start", method="POST", payload=session)
        status = request_json(base_url, f"{root}/status")
        heartbeat = request_json(base_url, f"{root}/sessions/heartbeat", method="POST", payload=session)
        end = request_json(base_url, f"{root}/sessions/end", method="POST", payload=session)
        status_json = status.get("json") or {}
        metrics = status_json.get("metrics") or {}
        station_results[station_id] = {
            "ok": all(item.get("ok") for item in (start, status, heartbeat, end))
            and "active_website_listeners" in metrics,
            "status": status.get("status"),
            "online": status_json.get("online"),
            "stale": status_json.get("stale"),
            "active_website_listeners": metrics.get("active_website_listeners"),
        }
    results["stations"] = station_results
    absent_routes_ok = all(
        not results[key]["ok"] and results[key]["status"] == 404
        for key in ("ai_en_absent", "ai_fr_absent")
    )
    results["ok"] = results["ai"]["ok"] and absent_routes_ok and results["openapi"]["ok"] and all(
        station["ok"] for station in station_results.values()
    )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-check the RadioTEDU public-only website service.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when conformance checks fail.")
    args = parser.parse_args()

    report = run_smoke(args.base_url)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print(f"RadioTEDU public server: {'OK' if report['ok'] else 'FAILED'}")
        for station_id, station in report["stations"].items():
            print(f"- {station_id}: status={station['status']} online={station['online']} listeners={station['active_website_listeners']}")
        if report["openapi"]["forbidden_paths"]:
            print(f"- forbidden OpenAPI terms: {', '.join(report['openapi']['forbidden_paths'])}")
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
