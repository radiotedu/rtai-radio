from __future__ import annotations

import os
import shutil
import signal
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .audio.models import BROADCAST_AUDIO_POLICY
from .audio.processing import ProcessingProfile
from .config import Settings


_STATION_LIQUIDSOAP = {
    "radiotedu-en": {
        "mount": "/radiotedu-en",
        "credentials_environment": "RADIOTEDU_EN_SOURCE_CREDENTIALS",
    },
    "radiotedu-fr": {
        "mount": "/radiotedu-fr",
        "credentials_environment": "RADIOTEDU_FR_SOURCE_CREDENTIALS",
    },
}


def liquidsoap_pid_path(settings: Settings) -> Path:
    return Path(settings.liquidsoap_script_path).with_suffix(".pid")


def liquidsoap_log_paths(settings: Settings) -> dict[str, Path]:
    script_path = Path(settings.liquidsoap_script_path)
    return {
        "stdout": script_path.with_suffix(".out.log"),
        "stderr": script_path.with_suffix(".err.log"),
    }


def station_liquidsoap_template_path(station_id: str) -> Path:
    if station_id not in _STATION_LIQUIDSOAP:
        raise ValueError(f"unsupported station Liquidsoap template: {station_id}")
    return (
        Path(__file__).resolve().parents[1]
        / "config"
        / "deployment"
        / "liquidsoap"
        / f"{station_id}.liq.template"
    )


def _station_template(settings: Settings) -> tuple[dict[str, str], Path] | None:
    station_id = getattr(settings, "station_id", "radiotedu-en")
    station = _STATION_LIQUIDSOAP.get(station_id)
    script_path = Path(settings.liquidsoap_script_path)
    mount = settings.liquidsoap_mount if settings.liquidsoap_mount.startswith("/") else f"/{settings.liquidsoap_mount}"
    if station is None or mount != station["mount"] or script_path.stem != station_id:
        return None
    return station, station_liquidsoap_template_path(station_id)


def _processing_block(processing_profile: ProcessingProfile) -> str:
    return f"""# Measured playout guards: T17 transition decisions are cue-aware.  Generic
# crossfades remain zero-length unless a decision supplies liq_cross_duration,
# so speech, jingles, and unverified intros stay sequential.
radio = crossfade(
  duration=0.0,
  fade_in=0.0,
  fade_out=0.0,
  smart=true,
  conservative=true,
  assume_autocue=true,
  radio
)

# A continuous source below -60 dBFS is degraded after 1.0 second and skipped
# at 1.5 seconds, allowing the next queued music/fallback item to take over.
radio = blank.detect(
  max_blank=1.0,
  min_noise=0.05,
  threshold=-60.0,
  track_sensitive=false,
  fun () -> log("RadioTEDU primary source degraded after 1.0s below -60 dBFS"),
  radio
)
radio = blank.skip(
  threshold=-60.0,
  max_blank=1.5,
  min_noise=0.05,
  track_sensitive=false,
  radio
)

# input level control
radio = amplify({processing_profile.input_gain_factor:.6f}, radio)

# gentle wideband AGC
radio = normalize(target={processing_profile.target_lufs:.1f}, lufs=true, gain_min=-{processing_profile.wideband_agc_max_gain_db:.1f}, gain_max={processing_profile.wideband_agc_max_gain_db:.1f}, radio)

# restrained multiband dynamics
radio = compress.multiband(radio, [
  {{frequency=250., attack=25., release=250., ratio={processing_profile.multiband_ratio:.1f}, threshold=-18., gain=0.}},
  {{frequency=2500., attack=20., release=200., ratio={processing_profile.multiband_ratio:.1f}, threshold=-16., gain=0.}},
  {{frequency=12000., attack=15., release=150., ratio={processing_profile.multiband_ratio:.1f}, threshold=-14., gain=0.}}
])

# final true-peak limiter
radio = limit(threshold={processing_profile.true_peak_ceiling_dbtp:.1f}, radio)"""


def _render_station_template(
    template_path: Path,
    station: dict[str, str],
    queue_path: Path,
    fallback_queue_path: Path,
    settings: Settings,
    processing_profile: ProcessingProfile,
) -> tuple[str, dict[str, object]]:
    template = template_path.read_text(encoding="utf-8")
    credentials_environment = station["credentials_environment"]
    if f'environment.get("{credentials_environment}")' not in template:
        raise ValueError(f"station template must reference {credentials_environment}")
    logs = liquidsoap_log_paths(settings)
    source_ids = {
        "primary": f"{settings.station_id}-primary",
        "fallback": f"{settings.station_id}-fallback",
    }
    replacements = {
        "queue_path": queue_path.as_posix(),
        "fallback_queue_path": fallback_queue_path.as_posix(),
        "primary_source_id": source_ids["primary"],
        "fallback_source_id": source_ids["fallback"],
        "log_path": logs["stdout"].as_posix(),
        "host": settings.liquidsoap_host,
        "port": str(settings.liquidsoap_port),
        "mount": station["mount"],
        "processing": _processing_block(processing_profile),
    }
    for key, value in replacements.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    if "{{" in template or "}}" in template:
        raise ValueError(f"unresolved placeholder in station Liquidsoap template: {template_path}")
    return template, {
        "station_id": settings.station_id,
        "fallback_queue_path": str(fallback_queue_path),
        "pid_path": str(liquidsoap_pid_path(settings)),
        "log_paths": {name: str(path) for name, path in logs.items()},
        "credentials_environment": credentials_environment,
        "source_ids": source_ids,
    }


def render_liquidsoap_config(
    settings: Settings, processing_profile: ProcessingProfile | None = None
) -> dict:
    processing_profile = processing_profile or ProcessingProfile()
    queue_path = Path(settings.liquidsoap_queue_path)
    script_path = Path(settings.liquidsoap_script_path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    if not queue_path.exists():
        queue_path.write_text("", encoding="utf-8")
    mount = settings.liquidsoap_mount if settings.liquidsoap_mount.startswith("/") else f"/{settings.liquidsoap_mount}"
    playout = {
        "silence_threshold_dbfs": BROADCAST_AUDIO_POLICY.silence_threshold_dbfs,
        "degraded_primary_seconds": BROADCAST_AUDIO_POLICY.silence_degraded_primary_seconds,
        "fallback_seconds": BROADCAST_AUDIO_POLICY.silence_fallback_seconds,
        "talk_over_minimum_intro_confidence": BROADCAST_AUDIO_POLICY.talk_over_minimum_intro_confidence,
        "talk_over_minimum_instrumental_intro_seconds": BROADCAST_AUDIO_POLICY.talk_over_minimum_instrumental_intro_seconds,
        "speech_end_before_intro_seconds": BROADCAST_AUDIO_POLICY.speech_target_before_intro_end_seconds,
        "speech_end_before_intro_range_seconds": (
            BROADCAST_AUDIO_POLICY.speech_target_before_intro_end_min_seconds,
            BROADCAST_AUDIO_POLICY.speech_target_before_intro_end_max_seconds,
        ),
        "time_stretch_ratio": 1.0,
        "speaks_over_vocals": False,
    }
    station_template = _station_template(settings)
    station_rendered: dict[str, object] = {}
    if station_template:
        station, template_path = station_template
        fallback_queue_path = queue_path.with_name("fallback.m3u")
        if not fallback_queue_path.exists():
            fallback_queue_path.write_text("", encoding="utf-8")
        script, station_rendered = _render_station_template(
            template_path,
            station,
            queue_path,
            fallback_queue_path,
            settings,
            processing_profile,
        )
        mount = station["mount"]
    else:
        script = f"""# RadioTEDU Liquidsoap configuration
set("log.stdout", true)
set("server.telnet", false)

radio = playlist(id="RadioTEDU", mode="normal", reload=1, reload_mode="watch", "{queue_path.as_posix()}")
radio = mksafe(radio)

# Measured playout guards: T17 transition decisions are cue-aware.  Generic
# crossfades remain zero-length unless a decision supplies liq_cross_duration,
# so speech, jingles, and unverified intros stay sequential.
radio = crossfade(
  duration=0.0,
  fade_in=0.0,
  fade_out=0.0,
  smart=true,
  conservative=true,
  assume_autocue=true,
  radio
)

# A continuous source below -60 dBFS is degraded after 1.0 second and skipped
# at 1.5 seconds, allowing the next queued music/fallback item to take over.
radio = blank.detect(
  max_blank=1.0,
  min_noise=0.05,
  threshold=-60.0,
  track_sensitive=false,
  fun () -> log("RadioTEDU primary source degraded after 1.0s below -60 dBFS"),
  radio
)
radio = blank.skip(
  threshold=-60.0,
  max_blank=1.5,
  min_noise=0.05,
  track_sensitive=false,
  radio
)

# input level control
radio = amplify({processing_profile.input_gain_factor:.6f}, radio)

# gentle wideband AGC
radio = normalize(target={processing_profile.target_lufs:.1f}, lufs=true, gain_min=-{processing_profile.wideband_agc_max_gain_db:.1f}, gain_max={processing_profile.wideband_agc_max_gain_db:.1f}, radio)

# restrained multiband dynamics
radio = compress.multiband(radio, [
  {{frequency=250., attack=25., release=250., ratio={processing_profile.multiband_ratio:.1f}, threshold=-18., gain=0.}},
  {{frequency=2500., attack=20., release=200., ratio={processing_profile.multiband_ratio:.1f}, threshold=-16., gain=0.}},
  {{frequency=12000., attack=15., release=150., ratio={processing_profile.multiband_ratio:.1f}, threshold=-14., gain=0.}}
])

# final true-peak limiter
radio = limit(threshold={processing_profile.true_peak_ceiling_dbtp:.1f}, radio)

# encoder
output.icecast(
  %mp3,
  host="{settings.liquidsoap_host}",
  port={settings.liquidsoap_port},
  password="{settings.liquidsoap_icecast_password}",
  mount="{mount}",
  name="RadioTEDU",
  description="RadioTEDU AI radio",
  genre="AI Radio",
  radio
)
"""
    script_path.write_text(script, encoding="utf-8")
    return {
        "queue_path": str(queue_path),
        "script_path": str(script_path),
        "mount": mount,
        "icecast_url": f"http://{settings.liquidsoap_host}:{settings.liquidsoap_port}{mount}",
        "processing_profile": processing_profile.name,
        "playout": playout,
        **station_rendered,
    }


def verify_liquidsoap_output(settings: Settings) -> dict:
    rendered = render_liquidsoap_config(settings)
    status = liquidsoap_status(settings)
    queue_path = Path(settings.liquidsoap_queue_path)
    script_path = Path(settings.liquidsoap_script_path)
    try:
        queue_path.open("r", encoding="utf-8").close()
        queue_readable = True
        queue_error = None
    except OSError as exc:
        queue_readable = False
        queue_error = str(exc)
    try:
        script_text = script_path.read_text(encoding="utf-8")
        script_references_queue = queue_path.as_posix() in script_text
    except OSError:
        script_references_queue = False
    return {
        **rendered,
        **status,
        "queue_readable": queue_readable,
        "queue_error": queue_error,
        "script_references_queue": script_references_queue,
        "verified": bool(queue_readable and script_references_queue and status["mount_active"]),
    }


def liquidsoap_status(settings: Settings, icecast_checker=None) -> dict:
    pid_path = liquidsoap_pid_path(settings)
    command_path = shutil.which(settings.liquidsoap_command)
    pid = _read_pid(pid_path)
    running = _pid_running(pid) if pid else False
    if pid and not running:
        pid_path.unlink(missing_ok=True)
    rendered = Path(settings.liquidsoap_script_path).exists()
    queue_path = Path(settings.liquidsoap_queue_path)
    queue_exists = queue_path.exists()
    queue_length = _queue_length(queue_path) if queue_exists else 0
    if not settings.liquidsoap_enabled:
        health = "disabled"
    elif running:
        health = "running"
    elif command_path:
        health = "ready"
    else:
        health = "missing"
    mount = settings.liquidsoap_mount if settings.liquidsoap_mount.startswith("/") else f"/{settings.liquidsoap_mount}"
    icecast_url = f"http://{settings.liquidsoap_host}:{settings.liquidsoap_port}{mount}"
    icecast = _check_icecast_mount(icecast_url, checker=icecast_checker)
    return {
        "enabled": settings.liquidsoap_enabled,
        "health": health,
        "command": settings.liquidsoap_command,
        "command_found": bool(command_path),
        "command_path": command_path,
        "running": running,
        "pid": pid if running else None,
        "rendered": rendered,
        "script_path": settings.liquidsoap_script_path,
        "queue_path": settings.liquidsoap_queue_path,
        "queue_exists": queue_exists,
        "queue_length": queue_length,
        "mount": mount,
        "icecast_url": icecast_url,
        "icecast_reachable": icecast["reachable"],
        "mount_active": icecast["mount_active"],
        "icecast_status": icecast["status"],
        "icecast_error": icecast.get("error"),
    }


def start_liquidsoap(settings: Settings) -> dict:
    rendered = render_liquidsoap_config(settings)
    status = liquidsoap_status(settings)
    if status["running"]:
        return {"started": True, "already_running": True, **status}
    command_path = status["command_path"]
    if not command_path:
        return {"started": False, "reason": "liquidsoap_missing", **status, **rendered}
    script_path = str(Path(settings.liquidsoap_script_path).resolve())
    out_path = Path(settings.liquidsoap_script_path).with_suffix(".out.log")
    err_path = Path(settings.liquidsoap_script_path).with_suffix(".err.log")
    with out_path.open("a", encoding="utf-8") as stdout, err_path.open("a", encoding="utf-8") as stderr:
        process = subprocess.Popen([command_path, script_path], stdout=stdout, stderr=stderr)
    liquidsoap_pid_path(settings).write_text(str(process.pid), encoding="utf-8")
    return {"started": True, "already_running": False, **liquidsoap_status(settings), **rendered}


def stop_liquidsoap(settings: Settings) -> dict:
    pid_path = liquidsoap_pid_path(settings)
    pid = _read_pid(pid_path)
    if not pid or not _pid_running(pid):
        pid_path.unlink(missing_ok=True)
        return {"stopped": True, "already_stopped": True, **liquidsoap_status(settings)}
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    pid_path.unlink(missing_ok=True)
    return {"stopped": True, "already_stopped": False, **liquidsoap_status(settings)}


def append_liquidsoap_item(settings: Settings, file_path: str) -> None:
    queue_path = Path(settings.liquidsoap_queue_path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with queue_path.open("a", encoding="utf-8") as handle:
        handle.write(str(Path(file_path).resolve()) + "\n")


def _queue_length(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def _check_icecast_mount(url: str, checker=None) -> dict:
    if checker:
        return checker(url, timeout=0.5)
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "RadioTEDU-Icecast-Check/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=0.5) as response:
            return {"reachable": True, "mount_active": response.status in {200, 206}, "status": response.status, "url": url}
    except urllib.error.HTTPError as exc:
        return {"reachable": True, "mount_active": False, "status": exc.code, "url": url, "error": str(exc)}
    except OSError as exc:
        return {"reachable": False, "mount_active": False, "status": None, "url": url, "error": str(exc)}


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
