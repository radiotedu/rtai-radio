from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import Settings
from backend.database import connect, init_db
from backend.fallback_playlist import FallbackPlaylistBuilder
from backend.imaging.library import ImagingError, ImagingLibrary
from backend.liquidsoap import (
    liquidsoap_encoder_preflight,
    liquidsoap_status,
    render_liquidsoap_config,
)
from backend.ollama_setup import check_ollama_setup
from backend.rundown import RundownPlanner
from backend.stations.context import (
    StationContext,
    build_station_context,
    ensure_station_runtime_dirs,
)
from backend.stations.loader import load_station_profiles
from backend.tts import build_tts_provider
from scripts.run_station_forever import (
    STATION_PORTS,
    build_process_specs,
    process_environment,
)


EXPECTED_STATIONS = ("radiotedu-en", "radiotedu-fr")
EXPECTED_MOUNTS = {"radiotedu-en": "/en", "radiotedu-fr": "/fr"}
SOURCE_SECRET_NAMES = {
    "radiotedu-en": "RADIOTEDU_EN_SOURCE_CREDENTIALS",
    "radiotedu-fr": "RADIOTEDU_FR_SOURCE_CREDENTIALS",
}
HMAC_SECRET_NAMES = (
    "RADIOTEDU_EN_SNAPSHOT_SECRET",
    "RADIOTEDU_FR_SNAPSHOT_SECRET",
)
EXPECTED_ENCODER = '%ffmpeg(format="adts", %audio(codec="aac", b="192k", ac=2, ar=48000))'


def _count(settings: Settings, query: str) -> int:
    with connect(settings) as conn:
        row = conn.execute(query).fetchone()
        return int(row[0] if row else 0)


def _api_status(base_url: str, station_id: str) -> dict:
    url = f"{base_url.rstrip('/')}/v1/radio/stations/{station_id}/status"
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "RadioTEDU-Broadcast-Smoke/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return {"reachable": True, "status": int(response.status), "url": url}
    except urllib.error.HTTPError as exc:
        return {"reachable": True, "status": int(exc.code), "url": url}
    except OSError as exc:
        return {
            "reachable": False,
            "status": None,
            "url": url,
            "error": type(exc).__name__,
        }


def _secret_isolation_contract() -> dict:
    inherited = {
        **{name: f"sentinel-{index}" for index, name in enumerate(SOURCE_SECRET_NAMES.values())},
        **{name: f"sentinel-hmac-{index}" for index, name in enumerate(HMAC_SECRET_NAMES)},
    }
    results: dict[str, dict] = {}
    specs = build_process_specs(ROOT)
    for spec in specs:
        station_id = next(
            station for station in EXPECTED_STATIONS if spec.name == f"backend-{station}"
        )
        child = process_environment(spec, inherited)
        own_source = SOURCE_SECRET_NAMES[station_id]
        other_sources = [name for name in SOURCE_SECRET_NAMES.values() if name != own_source]
        results[station_id] = {
            "own_source_present": child.get(own_source) == inherited[own_source],
            "other_source_absent": all(name not in child for name in other_sources),
            "hmac_secrets_absent": all(name not in child for name in HMAC_SECRET_NAMES),
        }
    return {
        "station_children": results,
        "ok": (
            len(specs) == 2
            and set(STATION_PORTS) == set(EXPECTED_STATIONS)
            and all(all(checks.values()) for checks in results.values())
        ),
    }


def _tts_health(context: StationContext) -> dict:
    try:
        provider = build_tts_provider(
            context,
            os.environ.get("QWEN_TTS_SERVICE_URL", "http://127.0.0.1:8090"),
        )
        return provider.health()
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "blocked", "reason": type(exc).__name__}


def _imaging_status(context: StationContext) -> dict:
    try:
        library = ImagingLibrary.open(
            context.settings.imaging_release_root,
            context.profile.station_id,
        )
    except (ImagingError, OSError, ValueError) as exc:
        return {"valid": False, "asset_count": 0, "reason": type(exc).__name__}
    paths = library.asset_paths()
    return {
        "valid": bool(paths) and all(path.is_file() for path in paths),
        "asset_count": len(paths),
        "all_jingles": all(asset.category == "jingle" for asset in library.assets),
    }


def _liquidsoap_contract(context: StationContext) -> tuple[dict, dict]:
    settings = context.settings
    rendered = render_liquidsoap_config(settings)
    script_path = Path(settings.liquidsoap_script_path)
    script = script_path.read_text(encoding="utf-8")
    expected_secret = SOURCE_SECRET_NAMES[context.profile.station_id]
    checks = {
        "host": rendered.get("source_host") == "10.98.98.75",
        "port": rendered.get("source_port") == 11154,
        "mount": rendered.get("mount") == EXPECTED_MOUNTS[context.profile.station_id],
        "source_user": rendered.get("source_user") == "source",
        "encoder_profile": rendered.get("encoder_profile") == "aac_192",
        "codec": rendered.get("codec") == "AAC-LC",
        "bitrate_kbps": rendered.get("bitrate_kbps") == 192,
        "public_listing": rendered.get("public_listing") is True,
        "exact_encoder": EXPECTED_ENCODER in script,
        "station_secret_reference": f'environment.get("{expected_secret}")' in script,
        "no_literal_password": 'password="' not in script and "password='" not in script,
    }
    return {
        "checks": checks,
        "config_ok": all(checks.values()),
        "script_path": str(script_path),
    }, rendered


def _station_report(context: StationContext, public_sync_url: str) -> dict:
    settings = context.settings
    ensure_station_runtime_dirs(context)
    init_db(context)
    fallback = FallbackPlaylistBuilder(context).rebuild()
    coverage = RundownPlanner(
        context,
        fallback_seconds_provider=lambda: fallback.coverage_seconds,
    ).coverage(datetime.now(timezone.utc))
    config, _ = _liquidsoap_contract(context)
    stream = liquidsoap_status(settings)
    preflight = liquidsoap_encoder_preflight(settings)
    return {
        "station_id": context.profile.station_id,
        "language": context.profile.language,
        "music_library": {
            "track_count": _count(settings, "select count(*) from tracks"),
            "ready_announcements": _count(
                settings,
                "select count(*) from announcement_queue where status='ready'",
            ),
        },
        "coverage": {
            "planned_seconds": coverage.planned_seconds,
            "rendered_seconds": coverage.rendered_seconds,
            "fallback_seconds": fallback.coverage_seconds,
            "planned_required_seconds": settings.rundown_planned_seconds,
            "rendered_required_seconds": settings.rundown_rendered_seconds,
            "fallback_required_seconds": settings.fallback_coverage_seconds,
            "needs_refill": coverage.needs_refill,
            "air_ready": coverage.air_ready,
        },
        "imaging": _imaging_status(context),
        "tts": _tts_health(context),
        "liquidsoap": {
            **stream,
            "encoder_preflight": preflight,
            "rendered_contract": config,
            "source_credentials_configured": bool(
                os.environ.get(SOURCE_SECRET_NAMES[context.profile.station_id])
            ),
        },
        "platform_api": _api_status(public_sync_url, context.profile.station_id),
    }


def build_report(settings: Settings) -> dict:
    profiles = load_station_profiles(settings.station_profiles_path)
    stations = {
        station_id: _station_report(
            build_station_context(settings, profiles[station_id]),
            settings.public_sync_url,
        )
        for station_id in EXPECTED_STATIONS
    }
    public_sync_configured = bool(
        settings.public_sync_url == "https://api.radiotedu.com"
        and settings.platform_agent_id == "school-radio-pc"
        and settings.platform_agent_scope == "agent:playout"
        and settings.platform_hmac_secret_en
        and settings.platform_hmac_secret_fr
    )
    return {
        "ok": False,
        "mode": "dual-station-staging",
        "stations": stations,
        "ollama": check_ollama_setup(settings),
        "secret_isolation": _secret_isolation_contract(),
        "public_sync": {
            "configured": public_sync_configured,
            "public_sync_url": settings.public_sync_url,
            "agent_id": settings.platform_agent_id,
            "scope": settings.platform_agent_scope,
            "heartbeat_seconds": settings.public_sync_interval_seconds,
            "station_secrets_configured": {
                "radiotedu-en": bool(settings.platform_hmac_secret_en),
                "radiotedu-fr": bool(settings.platform_hmac_secret_fr),
            },
        },
    }


def strict_failures(report: dict) -> list[str]:
    failures: list[str] = []
    if report["ollama"].get("status") not in {"ready", "ok"}:
        failures.append("shared Ollama is not ready")
    if not report["secret_isolation"].get("ok"):
        failures.append("station child secret isolation contract failed")
    if not report["public_sync"].get("configured"):
        failures.append("canonical public sync or per-station HMAC secrets are not configured")
    for station_id, station in report["stations"].items():
        prefix = station_id
        if station["music_library"]["track_count"] <= 0:
            failures.append(f"{prefix} has no indexed music")
        coverage = station["coverage"]
        if coverage["planned_seconds"] < coverage["planned_required_seconds"]:
            failures.append(f"{prefix} planned coverage is below four hours")
        if coverage["rendered_seconds"] < coverage["rendered_required_seconds"]:
            failures.append(f"{prefix} rendered coverage is below one hour")
        if coverage["fallback_seconds"] < coverage["fallback_required_seconds"]:
            failures.append(f"{prefix} fallback coverage is below six hours")
        imaging = station["imaging"]
        if not imaging.get("valid") or imaging.get("asset_count") != 6 or not imaging.get("all_jingles"):
            failures.append(f"{prefix} does not have all six validated jingles")
        liquidsoap = station["liquidsoap"]
        if not liquidsoap["rendered_contract"].get("config_ok"):
            failures.append(f"{prefix} Liquidsoap render contract failed")
        if not liquidsoap["encoder_preflight"].get("encoder_supported"):
            failures.append(f"{prefix} Liquidsoap does not advertise FFmpeg encoding")
        if not liquidsoap.get("source_credentials_configured"):
            failures.append(f"{prefix} rotated source credential is not configured")
        if station["tts"].get("status") not in {"ready", "ok"}:
            failures.append(f"{prefix} approved Qwen TTS is not ready")
        if station["platform_api"].get("status") != 200:
            failures.append(f"{prefix} canonical platform status endpoint is unavailable")
    return failures


def print_report(report: dict) -> None:
    print("RadioTEDU dual-station broadcast smoke")
    for station_id, station in report["stations"].items():
        coverage = station["coverage"]
        print(
            f"- {station_id}: {station['music_library']['track_count']} tracks, "
            f"{station['imaging']['asset_count']} jingles, "
            f"{coverage['planned_seconds']}s planned, "
            f"{coverage['rendered_seconds']}s rendered, "
            f"{coverage['fallback_seconds']}s fallback, "
            f"mount {station['liquidsoap']['mount']}"
        )
    print(f"- Ollama: {report['ollama'].get('status')}")
    print(f"- Child secret isolation: {'ok' if report['secret_isolation']['ok'] else 'failed'}")
    print(f"- Public sync: {'configured' if report['public_sync']['configured'] else 'not configured'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-check the local RadioTEDU broadcast computer.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when required staging checks fail.")
    args = parser.parse_args()

    report = build_report(Settings.from_env(ROOT / ".env"))
    failures = strict_failures(report) if args.strict else []
    report["strict_failures"] = failures
    report["ok"] = not failures
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print_report(report)
        for failure in failures:
            print(f"- FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
