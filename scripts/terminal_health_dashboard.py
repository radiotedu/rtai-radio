"""Live terminal dashboard for the temporary English and French broadcasts."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "data" / "runtime" / "temporary-dual-station"
SHADOW_ROOT = ROOT / "data" / "runtime" / "shadow-music-director"
STATIONS = (
    ("radiotedu-en", "ENGLISH", "/ai"),
    ("radiotedu-fr", "FRANCAIS", "/event"),
)


def load_status(station_id: str) -> dict:
    path = RUNTIME_ROOT / station_id / "status.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_shadow_status(station_id: str) -> dict:
    path = SHADOW_ROOT / station_id / "status.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def icecast_status() -> dict[str, dict]:
    try:
        request = urllib.request.Request(
            "http://10.98.98.75:11154/status-json.xsl",
            headers={"User-Agent": "RadioTEDU-Terminal-Health/1.0"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        sources = payload.get("icestats", {}).get("source", [])
        if isinstance(sources, dict):
            sources = [sources]
        mounts = {}
        for source in sources:
            if not source.get("listenurl"):
                continue
            mount = "/" + str(source["listenurl"]).rsplit("/", 1)[-1]
            mounts[mount] = {
                "listeners": int(source.get("listeners") or 0),
                "name": str(source.get("server_name") or ""),
                "type": str(source.get("server_type") or ""),
            }
        return mounts
    except (OSError, ValueError):
        return {}


def age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        then = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - then).total_seconds())
    except ValueError:
        return None


def progress(elapsed: float, duration: float, width: int = 32) -> str:
    ratio = max(0.0, min(1.0, elapsed / duration)) if duration else 0.0
    filled = round(ratio * width)
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def duration(seconds: float | int | None) -> str:
    total = max(0, round(float(seconds or 0)))
    return f"{total // 60:02d}:{total % 60:02d}"


def render_station(station_id: str, label: str, mount: str, mounts: dict[str, dict]) -> list[str]:
    status = load_status(station_id)
    shadow = load_shadow_status(station_id)
    supervisor = {}
    try:
        supervisor = json.loads(
            (RUNTIME_ROOT / station_id / "supervisor-status.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        pass
    now = status.get("now_playing") or {}
    stats = status.get("session_stats") or {}
    library = status.get("library") or {}
    buffer = status.get("announcement_buffer") or {}
    audio_age = age_seconds(status.get("last_audio_write_at"))
    local_live = status.get("state") == "live" and audio_age is not None and audio_age < 8
    public_live = mount in mounts
    mount_status = mounts.get(mount) or {}
    health = "LIVE" if local_live and public_live else "RECOVERING"
    lines = [
        (
            f"{label}  {mount}  {health}  |  "
            f"listeners {int(mount_status.get('listeners') or 0)}"
        ),
        f"  Program : {status.get('program') or '—'}",
        (
            f"  Now     : [{str(now.get('kind') or 'idle').upper()}] "
            f"{now.get('artist') or '—'} — {now.get('title') or '—'}"
        ),
        (
            f"  Progress: {progress(float(now.get('elapsed_seconds') or 0), float(now.get('duration_seconds') or 0))} "
            f"{duration(now.get('elapsed_seconds'))} / {duration(now.get('duration_seconds'))}"
        ),
    ]
    if now.get("announcement_text"):
        lines.append(f"  Host    : {now['announcement_text']}")
    if now.get("fact_source_url"):
        lines.append(f"  Fact    : {now['fact_source_url']}")
    upcoming = status.get("next") or []
    for index, item in enumerate(upcoming[:5], start=1):
        lines.append(
            f"  Next {index:02d} : [{str(item.get('kind') or '').upper()}] "
            f"{item.get('artist') or '—'} — {item.get('title') or '—'}"
        )
    shadow_age = age_seconds(shadow.get("generated_at"))
    shadow_safe = (
        shadow.get("state") == "shadow"
        and shadow.get("read_only") is True
        and shadow.get("controls_live_playout") is False
        and shadow_age is not None
        and shadow_age < 150
    )
    lines.append(
        f"  AI Shadow: {'READY' if shadow_safe else 'OFFLINE'} | "
        "READ-ONLY | does not control live playout"
    )
    if shadow.get("state") == "error":
        lines.append(f"  AI Error : {shadow.get('error') or 'unknown'}")
    for index, item in enumerate((shadow.get("recommendations") or [])[:5], start=1):
        reasons = ", ".join(str(reason) for reason in item.get("reasons") or [])
        lines.append(
            f"  AI {index:02d}   : score {float(item.get('score') or 0):05.2f} | "
            f"{item.get('artist') or '-'} - {item.get('title') or '-'}"
            f" | {reasons}"
        )
    lines.extend(
        [
            (
                f"  Warmup  : {int(buffer.get('ready') or 0)}/"
                f"{int(buffer.get('required') or 0)} ready | "
                f"target {int(buffer.get('target') or 0)} | "
                f"{int(buffer.get('unique_qwen_assets') or 0)} unique Qwen assets | "
                f"{str(buffer.get('state') or 'unknown').upper()}"
            ),
            (
                f"  Intros  : {int(buffer.get('verified_track_intros_ready') or 0)} "
                f"verified ready | "
                f"{int(buffer.get('verified_track_intros_queued') or 0)} queued | "
                "live web disabled"
            ),
            (
                f"  Mix     : music {float(stats.get('music_percent') or 0):5.1f}% | "
                f"talking {float(stats.get('talking_percent') or 0):5.1f}% | "
                f"Qwen IDs played {int(float(stats.get('qwen_items_played') or 0))}"
            ),
            (
                f"  Library : {int(library.get('tracks') or 0)} songs / "
                f"{float(library.get('music_hours') or 0):.2f} h | "
                f"{int(library.get('validated_jingles') or 0)}/6 jingles | "
                f"{int(library.get('validated_qwen_ids') or 0)}/2 Qwen IDs"
            ),
            (
                f"  Health  : Icecast={'OK' if public_live else 'DOWN'} | "
                f"audio_age={audio_age:.1f}s | encoder={status.get('encoder_pid') or '—'} | "
                f"decoder={status.get('decoder_pid') or '—'}"
                if audio_age is not None
                else f"  Health  : Icecast={'OK' if public_live else 'DOWN'} | no audio heartbeat"
            ),
            (
                f"  Watchdog: {str(supervisor.get('state') or 'unknown').upper()} | "
                f"restarts {int(supervisor.get('restart_count') or 0)} | "
                f"worker {supervisor.get('worker_pid') or '—'} | "
                f"last {supervisor.get('last_restart_reason') or 'none'} | "
                f"suspect {int(supervisor.get('consecutive_failures') or 0)}/"
                f"{int(supervisor.get('consecutive_failures_required') or 3)}"
            ),
            f"  Error   : {status.get('last_error') or 'none'}",
        ]
    )
    return lines


def parse_args() -> object:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Render one health snapshot and exit (useful for verification).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        while True:
            mounts = icecast_status()
            if sys.stdout.isatty():
                os.system("cls" if os.name == "nt" else "clear")
            print("RadioTEDU AI Radio - live health dashboard")
            print(datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))
            print("=" * 92)
            for index, station in enumerate(STATIONS):
                if index:
                    print("-" * 92)
                print("\n".join(render_station(*station, mounts=mounts)))
            print("=" * 92)
            print("Public audio: https://stream.radiotedu.com/ai (EN) - /event (FR)")
            print("Press Ctrl+C to close this dashboard; broadcasting continues.")
            if args.once:
                return 0
            time.sleep(2)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
