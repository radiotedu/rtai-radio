from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import Settings, ensure_runtime_dirs
from backend.database import connect, init_db
from backend.liquidsoap import liquidsoap_status
from backend.ollama_setup import check_ollama_setup
from backend.stations.context import coerce_station_context
from backend.tts import build_tts_provider


def _track_count(settings: Settings) -> int:
    with connect(settings) as conn:
        row = conn.execute("select count(*) as count from tracks").fetchone()
        return int(row["count"] if row else 0)


def _ready_announcement_count(settings: Settings) -> int:
    with connect(settings) as conn:
        row = conn.execute("select count(*) as count from announcement_queue where status = 'ready'").fetchone()
        return int(row["count"] if row else 0)


def build_report(settings: Settings) -> dict:
    ensure_runtime_dirs(settings)
    init_db(settings)
    ollama = check_ollama_setup(settings)
    stream = liquidsoap_status(settings)
    public_sync_url = settings.public_sync_url
    public_sync_configured = bool(
        public_sync_url == "https://api.radiotedu.com"
        and settings.platform_agent_id == "school-radio-pc"
        and settings.platform_agent_scope == "agent:playout"
        and settings.platform_hmac_secret_en
        and settings.platform_hmac_secret_fr
    )
    tts = build_tts_provider(
        coerce_station_context(settings),
        os.environ.get("QWEN_TTS_SERVICE_URL", "http://127.0.0.1:8090"),
    ).health()
    music_library = {
        "path_configured": bool(settings.music_dir),
        "track_count": _track_count(settings),
        "ready_announcements": _ready_announcement_count(settings),
        "min_ready_announcements": settings.min_ready_announcements,
    }
    return {
        "ok": True,
        "music_library": music_library,
        "ollama": ollama,
        "tts": tts,
        "liquidsoap": stream,
        "public_sync": {
            "configured": public_sync_configured,
            "public_sync_url": public_sync_url,
            "agent_id": settings.platform_agent_id,
            "scope": settings.platform_agent_scope,
            "station_secrets_configured": {
                "radiotedu-en": bool(settings.platform_hmac_secret_en),
                "radiotedu-fr": bool(settings.platform_hmac_secret_fr),
            },
        },
    }


def print_report(report: dict) -> None:
    print("RadioTEDU broadcast smoke")
    print(f"- Music tracks: {report['music_library']['track_count']}")
    print(f"- Ready announcements: {report['music_library']['ready_announcements']}")
    print(f"- Ollama: {report['ollama'].get('status')}")
    print(f"- TTS: {report['tts'].get('status')}")
    print(f"- Liquidsoap: {report['liquidsoap'].get('health')}")
    print(f"- Public sync: {'configured' if report['public_sync']['configured'] else 'not configured'}")


def strict_failures(report: dict) -> list[str]:
    failures: list[str] = []
    if report["music_library"]["track_count"] <= 0:
        failures.append("music_library has no indexed tracks")
    if report["music_library"]["ready_announcements"] < report["music_library"]["min_ready_announcements"]:
        failures.append("announcement prebuffer is below minimum")
    if report["ollama"].get("status") not in {"ready", "ok"}:
        failures.append("ollama is not ready")
    if report["tts"].get("status") not in {"ready", "ok"}:
        failures.append("tts is not ready")
    if report["liquidsoap"].get("enabled") and report["liquidsoap"].get("health") == "missing":
        failures.append("liquidsoap is enabled but missing")
    if not report["public_sync"]["configured"]:
        failures.append("public snapshot sync is not configured")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-check the local RadioTEDU broadcast computer.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when required live-air checks fail.")
    args = parser.parse_args()

    report = build_report(Settings.from_env())
    failures = strict_failures(report) if args.strict else []
    report["strict_failures"] = failures
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print_report(report)
        for failure in failures:
            print(f"- FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
