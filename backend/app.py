from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .art.cover_generator import generate_covers
from .config import Settings
from .fallback_playlist import FallbackPlaylistBuilder
from .database import connect, log_event, now_iso, rows_to_dicts
from .liquidsoap import liquidsoap_status, render_liquidsoap_config, start_liquidsoap, stop_liquidsoap, verify_liquidsoap_output
from .llm import ollama_runtime_status
from .maintenance import maintenance_summary, run_maintenance, watchdog_summary
from .models import ListenerFeedbackRequest, ProgramUpdateRequest, PublicSessionRequest, PublicSnapshotRequest, SayRequest, SearchRequest
from .music_library import scan_music
from .ollama_setup import check_ollama_setup
from .orchestrator import AutonomousOrchestrator
from .platform_api import install_platform_routes
from .public_dashboard import (
    PublicSnapshotPusher,
    public_session_end,
    public_session_heartbeat,
    public_session_start,
    public_status,
    store_public_snapshot,
)
from .radio_agent import RadioAgent
from .runtime.station_runtime import SnapshotPusher, create_station_runtime
from .scheduler import current_program, next_programs
from .search.rss import RSSSearchProvider
from .search.searxng import SearXNGSearchProvider
from .stations.context import StationContext, build_station_context
from .stations.loader import StationProfileError, load_station_profiles


STARTED_AT = datetime.now(timezone.utc)


def create_app(
    settings: Settings | None = None,
    station_context: StationContext | None = None,
    snapshot_pusher: SnapshotPusher | None = None,
) -> FastAPI:
    if settings is not None and station_context is not None:
        raise ValueError("pass settings or station_context, not both")

    if station_context is not None:
        runtime_source: Settings | StationContext = station_context
    elif settings is not None:
        runtime_source = settings
    else:
        base_settings = Settings.from_env()
        profiles = load_station_profiles(base_settings.station_profiles_path)
        try:
            profile = profiles[base_settings.station_id]
        except KeyError as exc:
            raise StationProfileError(f"unknown STATION_ID: {base_settings.station_id}") from exc
        runtime_source = build_station_context(base_settings, profile)

    runtime_settings = (
        runtime_source.settings
        if isinstance(runtime_source, StationContext)
        else runtime_source
    )
    generate_covers(runtime_settings)
    runtime = create_station_runtime(
        runtime_source,
        snapshot_pusher=snapshot_pusher,
    )
    context = runtime.context
    settings = context.settings
    agent = runtime.agent
    orchestrator = runtime.orchestrator
    public_snapshot_pusher = runtime.snapshot_pusher
    app = FastAPI(title=context.profile.display_name)
    app.state.station_context = context
    app.state.runtime = runtime
    app.state.settings = settings
    app.state.agent = agent
    app.state.orchestrator = orchestrator
    app.state.public_snapshot_pusher = public_snapshot_pusher
    install_platform_routes(app, settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://localhost:{settings.frontend_port}",
            f"http://127.0.0.1:{settings.frontend_port}",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.mount("/static", StaticFiles(directory=str(settings.static_path)), name="static")
    frontend_dist = Path(__file__).resolve().parents[1] / "dist" / "frontend"
    frontend_assets = frontend_dist / "assets"
    if frontend_assets.exists():
        app.mount("/assets", StaticFiles(directory=str(frontend_assets)), name="frontend_assets")

    @app.middleware("http")
    async def optional_admin_auth(request: Request, call_next):
        if _requires_admin_token(settings, request):
            if request.headers.get("X-RadioTEDU-Admin-Token") != settings.admin_api_token:
                return JSONResponse({"detail": "invalid admin token"}, status_code=401)
        return await call_next(request)

    @app.on_event("startup")
    def startup() -> None:
        runtime.start()

    @app.on_event("shutdown")
    def shutdown() -> None:
        runtime.stop()

    @app.get("/api/status")
    def status() -> dict:
        return build_status(settings, agent, orchestrator, public_snapshot_pusher)

    @app.get("/ai")
    @app.get("/ai/en")
    @app.get("/ai/fr")
    def public_ai_page():
        index_path = frontend_dist / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="frontend build is not available")
        return FileResponse(str(index_path), media_type="text/html")

    @app.get("/api/public/status")
    def public_status_endpoint() -> dict:
        if not settings.public_compatibility_enabled:
            raise HTTPException(status_code=404, detail="compatibility adapter is disabled")
        return public_status(settings)

    @app.post("/api/public/snapshot")
    def public_snapshot_endpoint(
        payload: PublicSnapshotRequest = Body(...),
        x_radiotedu_sync_token: str | None = Header(default=None),
    ) -> dict:
        if not settings.public_compatibility_enabled:
            raise HTTPException(status_code=404, detail="compatibility adapter is disabled")
        if not settings.public_sync_token or x_radiotedu_sync_token != settings.public_sync_token:
            raise HTTPException(status_code=401, detail="invalid sync token")
        snapshot = store_public_snapshot(settings, _model_to_dict(payload))
        return {"stored": True, "channel": snapshot["channel"]["id"]}

    @app.post("/api/public/session/start")
    def public_session_start_endpoint(request: PublicSessionRequest, raw: Request) -> dict:
        if not settings.public_compatibility_enabled:
            raise HTTPException(status_code=404, detail="compatibility adapter is disabled")
        try:
            return public_session_start(settings, request.session_id, raw.headers.get("user-agent"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/public/session/heartbeat")
    def public_session_heartbeat_endpoint(request: PublicSessionRequest) -> dict:
        if not settings.public_compatibility_enabled:
            raise HTTPException(status_code=404, detail="compatibility adapter is disabled")
        try:
            return public_session_heartbeat(settings, request.session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/public/session/end")
    def public_session_end_endpoint(request: PublicSessionRequest) -> dict:
        if not settings.public_compatibility_enabled:
            raise HTTPException(status_code=404, detail="compatibility adapter is disabled")
        try:
            return public_session_end(settings, request.session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/channel")
    def channel() -> dict:
        with connect(settings) as conn:
            return dict(conn.execute("select * from channels where id='radiotedu'").fetchone())

    @app.get("/api/programs")
    def programs() -> list[dict]:
        with connect(settings) as conn:
            return rows_to_dicts(conn.execute("select * from programs where channel_id='radiotedu' order by start_time").fetchall())

    @app.patch("/api/programs/{program_id}")
    def update_program(program_id: str, request: ProgramUpdateRequest) -> dict:
        try:
            return patch_program(settings, program_id, request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/schedule/week")
    def schedule_week() -> dict:
        return weekly_schedule(settings)

    @app.get("/api/fallback-playlist")
    def fallback_playlist() -> dict:
        return emergency_fallback_playlist(settings)

    @app.get("/api/tracks/top")
    def tracks_top() -> list[dict]:
        return top_songs(settings)

    @app.get("/api/genres/top")
    def genres_top() -> list[dict]:
        return top_genres(settings)

    @app.get("/api/logs")
    def logs() -> list[dict]:
        return recent_logs(settings)

    @app.get("/api/setup/ollama")
    def ollama_setup() -> dict:
        return check_ollama_setup(settings)

    @app.post("/api/control/start")
    def start() -> dict:
        return orchestrator.start_background()

    @app.post("/api/air/start")
    def air_start() -> dict:
        readiness = air_start_readiness(settings, agent)
        if not readiness["ready"]:
            with connect(settings) as conn:
                log_event(conn, "warning", "Run Air refused because local readiness checks failed.", readiness)
                conn.commit()
            return {"started": False, "reason": readiness["reason"], **readiness}
        stream = start_liquidsoap(settings) if settings.liquidsoap_enabled else {"started": True, "reason": "liquidsoap_disabled"}
        if settings.liquidsoap_enabled and not stream.get("started"):
            with connect(settings) as conn:
                log_event(conn, "error", "Run Air failed before broadcast start.", stream)
                conn.commit()
            return {"started": False, "stream": stream, "orchestrator": orchestrator.status()}
        runner = orchestrator.start_background()
        return {"started": bool(runner.get("running")), "stream": stream, "orchestrator": runner}

    @app.post("/api/air/stop")
    def air_stop() -> dict:
        runner = orchestrator.stop_background()
        stopped = agent.stop()
        stream = stop_liquidsoap(settings) if settings.liquidsoap_enabled else {"stopped": True, "reason": "liquidsoap_disabled"}
        return {"stopped": True, "stream": stream, "orchestrator": runner, "playback": stopped}

    @app.post("/api/control/stop")
    def stop() -> dict:
        orchestrator.stop_background()
        return agent.stop()

    @app.post("/api/control/skip")
    def skip() -> dict:
        return agent.skip()

    @app.post("/api/control/say")
    def say(request: SayRequest) -> dict:
        return agent.say(request.text)

    @app.post("/api/clips/latest")
    def clip_latest_segment() -> dict:
        return latest_segment_clip(settings)

    @app.post("/api/tts/test")
    def tts_test(payload: dict = Body(default_factory=dict)) -> dict:
        program_id = str(payload.get("program_id") or current_program(settings)["id"])
        return test_tts(settings, agent, program_id)

    @app.post("/api/control/search")
    def search(request: SearchRequest) -> dict:
        provider = SearXNGSearchProvider(settings.searxng_url) if settings.search_provider == "searxng" else RSSSearchProvider(settings.rss_feeds_path)
        results = [item.__dict__ for item in provider.search(request.query, limit=5)]
        return {"results": results}

    @app.post("/api/listener/feedback")
    def listener_feedback(request: ListenerFeedbackRequest) -> dict:
        return orchestrator.record_listener_feedback(request.text, request.source)

    @app.get("/api/listener/messages")
    def listener_messages() -> list[dict]:
        return recent_listener_messages(settings)

    @app.post("/api/autonomy/strategy")
    def refresh_strategy() -> dict:
        return orchestrator.maintain_long_horizon_strategy()

    @app.post("/api/autonomy/tick")
    def autonomy_tick() -> dict:
        return orchestrator.tick()

    @app.post("/api/music/rescan")
    def rescan() -> dict:
        result = scan_music(settings)
        return result.__dict__

    @app.post("/api/maintenance/run")
    def maintenance_run(payload: dict = Body(default_factory=dict)) -> dict:
        return run_maintenance(
            settings,
            clip_retention_days=int(payload.get("clip_retention_days", 7)),
            max_agent_logs=int(payload.get("max_agent_logs", 500)),
        )

    @app.post("/api/art/generate-program-covers")
    def covers() -> dict:
        paths = generate_covers(settings)
        with connect(settings) as conn:
            log_event(conn, "info", "Cover art generated.", {"count": len(paths)})
            conn.commit()
        return {"generated": paths}

    @app.post("/api/liquidsoap/render")
    def liquidsoap_render() -> dict:
        return render_liquidsoap_config(settings)

    @app.get("/api/liquidsoap/status")
    def liquidsoap_status_endpoint() -> dict:
        return liquidsoap_status(settings)

    @app.post("/api/liquidsoap/verify")
    def liquidsoap_verify_endpoint() -> dict:
        result = verify_liquidsoap_output(settings)
        with connect(settings) as conn:
            log_event(conn, "info" if result["verified"] else "warning", "Liquidsoap verification requested.", result)
            conn.commit()
        return result

    @app.post("/api/liquidsoap/start")
    def liquidsoap_start_endpoint() -> dict:
        result = start_liquidsoap(settings)
        with connect(settings) as conn:
            log_event(conn, "info" if result.get("started") else "warning", "Liquidsoap start requested.", result)
            conn.commit()
        return result

    @app.post("/api/liquidsoap/stop")
    def liquidsoap_stop_endpoint() -> dict:
        result = stop_liquidsoap(settings)
        with connect(settings) as conn:
            log_event(conn, "info", "Liquidsoap stop requested.", result)
            conn.commit()
        return result

    return app


def build_status(
    settings: Settings,
    agent: RadioAgent,
    orchestrator: AutonomousOrchestrator,
    public_snapshot_pusher: PublicSnapshotPusher | None = None,
) -> dict:
    with connect(settings) as conn:
        channel = dict(conn.execute("select * from channels where id='radiotedu'").fetchone())
        track_count = conn.execute("select count(*) from tracks").fetchone()[0]
        programs = rows_to_dicts(conn.execute("select * from programs where channel_id='radiotedu' order by start_time").fetchall())
    has_music = track_count > 0
    current = current_program(settings)
    if agent.playback.now_playing:
        now_playing = agent.playback.state()
    elif has_music:
        now_playing = {
            "type": "idle",
            "title": "Idle — ready to start RadioTEDU.",
            "artist": None,
            "started_at": None,
        }
    else:
        now_playing = {
            "type": "idle",
            "title": "Idle — waiting for music library.",
            "artist": None,
            "started_at": None,
        }
    if now_playing["type"] == "idle" and channel["status"] == "live":
        channel["status"] = "idle"
    liquidsoap = liquidsoap_status(settings)
    if settings.liquidsoap_enabled and not liquidsoap["running"] and channel["status"] == "live":
        channel["status"] = "idle"
    watchdog = watchdog_summary(settings)
    watchdog.update(agent.playback.watchdog_status())
    return {
        "channel": channel,
        "now_playing": now_playing,
        "queue": [item.__dict__ for item in agent.playback.queue],
        "current_program": current,
        "programs": programs,
        "next_programs": next_programs(settings),
        "orchestrator": orchestrator.status(),
        "metrics": metrics(settings),
        "top_songs": top_songs(settings),
        "top_genres": top_genres(settings),
        "listener_messages": recent_listener_messages(settings),
        "incidents": recent_incidents(settings),
        "autonomous_tasks": recent_autonomous_tasks(settings),
        "weather": agent.weather_provider.current_context().to_dict(),
        "observability": observability(settings, agent),
        "logs": recent_logs(settings),
        "health": health(settings, agent),
        "liquidsoap": liquidsoap,
        "music_library": music_library_status(settings),
        "air_readiness": air_start_readiness(settings, agent, prepare_prebuffer=False),
        "maintenance": maintenance_summary(settings),
        "watchdog": watchdog,
        "configuration": operator_configuration(settings),
        "website_sync": website_sync_health(settings, public_snapshot_pusher),
        "fallback_playlist": emergency_fallback_playlist(settings),
        "schedule_week": weekly_schedule(settings),
        "setup": {
            "has_music": has_music,
            "message": "" if has_music else "No music library found. Add audio files to data/music and click Rescan.",
        },
    }


def air_start_readiness(settings: Settings, agent: RadioAgent, prepare_prebuffer: bool = True) -> dict:
    checklist: dict[str, dict] = {}
    library = music_library_status(settings)
    checklist["music_library"] = _readiness_item(
        library["playable_track_count"] > 0,
        f"{library['playable_track_count']} playable tracks indexed.",
        "blocking",
    )
    if library["playable_track_count"] <= 0:
        return {"ready": False, "reason": "no_music", "music_library": library, "readiness": {"checklist": checklist}}
    program = current_program(settings)
    buffer_state = (
        agent.ensure_announcement_prebuffer(program["id"])
        if prepare_prebuffer
        else agent.announcement_readiness(program["id"])
    )
    checklist["announcement_prebuffer"] = _readiness_item(
        buffer_state["ready_to_broadcast"],
        (
            f"{buffer_state['rendered_seconds']} rendered seconds, "
            f"{buffer_state['planned_seconds']} planned seconds, and "
            f"{buffer_state['fallback_seconds']} fallback seconds."
        ),
        "blocking",
    )
    tts_provider = getattr(agent.tts, "provider_name", settings.tts_provider)
    checklist["tts"] = _readiness_item(
        not str(tts_provider).startswith("dummy"),
        f"TTS provider: {tts_provider}.",
        "warning",
    )
    liquidsoap = liquidsoap_status(settings)
    checklist["liquidsoap_command"] = _readiness_item(
        (not settings.liquidsoap_enabled) or liquidsoap["command_found"],
        f"Liquidsoap command {settings.liquidsoap_command!r} is {'available' if liquidsoap['command_found'] else 'missing'}.",
        "blocking",
    )
    checklist["liquidsoap_queue"] = _readiness_item(
        (not settings.liquidsoap_enabled) or liquidsoap["queue_exists"],
        f"Queue file: {liquidsoap['queue_path']} ({liquidsoap['queue_length']} items).",
        "blocking",
    )
    checklist["icecast_mount"] = _readiness_item(
        (not settings.liquidsoap_enabled) or liquidsoap["mount_active"],
        f"Icecast mount {liquidsoap['icecast_url']} is {'active' if liquidsoap['mount_active'] else 'not active'}.",
        "warning",
    )
    checklist["public_sync"] = _readiness_item(
        bool(settings.public_sync_url and settings.public_sync_token),
        "Public snapshot sync configured." if settings.public_sync_url and settings.public_sync_token else "Public snapshot sync is not configured.",
        "warning",
    )
    blocking_failures = [name for name, item in checklist.items() if not item["ok"] and item["severity"] == "blocking"]
    if "liquidsoap_command" in blocking_failures:
        return {
            "ready": False,
            "reason": "liquidsoap_not_ready",
            "music_library": library,
            "announcement_buffer": buffer_state,
            "liquidsoap": liquidsoap,
            "stream": {"reason": "liquidsoap_missing", **liquidsoap},
            "readiness": {"checklist": checklist, "blocking_failures": blocking_failures},
        }
    if "liquidsoap_queue" in blocking_failures:
        return {
            "ready": False,
            "reason": "liquidsoap_queue_not_ready",
            "music_library": library,
            "announcement_buffer": buffer_state,
            "liquidsoap": liquidsoap,
            "readiness": {"checklist": checklist, "blocking_failures": blocking_failures},
        }
    if not buffer_state["ready_to_broadcast"]:
        return {
            "ready": False,
            "reason": "announcement_prebuffer_not_ready",
            "music_library": library,
            "announcement_buffer": buffer_state,
            "readiness": {"checklist": checklist, "blocking_failures": blocking_failures},
        }
    return {
        "ready": True,
        "reason": "ready",
        "music_library": library,
        "announcement_buffer": buffer_state,
        "liquidsoap": liquidsoap,
        "readiness": {"checklist": checklist, "blocking_failures": blocking_failures},
    }


def _readiness_item(ok: bool, detail: str, severity: str) -> dict:
    return {"ok": bool(ok), "detail": detail, "severity": severity}


def _model_to_dict(model) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _requires_admin_token(settings: Settings, request: Request) -> bool:
    if not settings.admin_api_token:
        return False
    path = request.url.path
    if path.startswith("/api/public/") or path.startswith("/static/") or path.startswith("/assets/") or path == "/ai":
        return False
    return request.method in {"POST", "PATCH", "PUT", "DELETE"} and path.startswith("/api/")


def music_library_status(settings: Settings) -> dict:
    with connect(settings) as conn:
        total = conn.execute("select count(*) from tracks").fetchone()[0]
        playable = conn.execute("select count(*) from tracks where file_path <> ''").fetchone()[0]
        scan = conn.execute(
            """
            select created_at
            from agent_logs
            where message like 'Music scan complete:%'
            order by created_at desc, id desc
            limit 1
            """
        ).fetchone()
    return {
        "total_indexed_tracks": int(total),
        "playable_track_count": int(playable),
        "last_scan_time": scan["created_at"] if scan else None,
    }


def operator_configuration(settings: Settings) -> dict:
    mount = settings.liquidsoap_mount if settings.liquidsoap_mount.startswith("/") else f"/{settings.liquidsoap_mount}"
    tts_command = settings.qwen_tts_command or settings.piper_tts_command or settings.tts_provider
    return {
        "MUSIC_DIR": settings.music_dir,
        "OLLAMA_MODEL": settings.ollama_model,
        "TTS_COMMAND": tts_command,
        "LIQUIDSOAP_PATH": settings.liquidsoap_command,
        "LIQUIDSOAP_SCRIPT": settings.liquidsoap_script_path,
        "ICECAST_URL": f"http://{settings.liquidsoap_host}:{settings.liquidsoap_port}{mount}",
        "ICECAST_MOUNT": mount,
        "PUBLIC_SYNC_URL": settings.public_sync_url,
        "PUBLIC_STREAM_URL": settings.public_stream_url,
        "ADMIN_AUTH": "enabled" if settings.admin_api_token else "disabled",
        "BUFFER_SIZES": {
            "min": int(settings.min_ready_announcements),
            "max": int(settings.max_ready_announcements),
        },
    }


def test_tts(settings: Settings, agent: RadioAgent, program_id: str) -> dict:
    with connect(settings) as conn:
        row = conn.execute("select * from programs where id=?", (program_id,)).fetchone()
    program = dict(row) if row else current_program(settings)
    voice = program.get("voice") or ""
    host = program.get("host_name") or "RadioTEDU"
    text = f"RadioTEDU TTS test. {host} voice check for {program.get('name', 'current program')}."
    output = settings.tts_path / f"tts_test_{program.get('id', 'program')}.wav"
    try:
        file_path = agent.tts.synthesize(text, str(output), voice=voice)
        ok = Path(file_path).exists()
        provider = getattr(agent.tts, "provider_name", settings.tts_provider)
        health_info = agent.tts.health() if hasattr(agent.tts, "health") else {"active_provider": provider, "status": "unknown"}
        return {
            "ok": ok,
            "provider": provider.split("->")[-1] if provider.startswith("qwen->") else provider,
            "active_provider": provider,
            "voice": voice,
            "program_id": program.get("id"),
            "file_path": file_path,
            "health": health_info,
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": getattr(agent.tts, "provider_name", settings.tts_provider),
            "voice": voice,
            "program_id": program.get("id"),
            "error": str(exc),
        }


def website_sync_health(settings: Settings, public_snapshot_pusher: PublicSnapshotPusher | None = None) -> dict:
    configured = bool(settings.public_sync_url and settings.public_sync_token)
    pusher_status = public_snapshot_pusher.status() if public_snapshot_pusher else None
    return {
        "configured": configured,
        "health": "running" if pusher_status and pusher_status["running"] else ("configured" if configured else "not_configured"),
        "public_sync_url": settings.public_sync_url,
        "public_stream_url": settings.public_stream_url,
        "interval_seconds": int(settings.public_sync_interval_seconds),
        "pusher": pusher_status,
    }


def patch_program(settings: Settings, program_id: str, request: ProgramUpdateRequest) -> dict:
    _validate_program_update(request)
    allowed = {
        "name": request.name,
        "description": request.description,
        "vibe": request.vibe,
        "start_time": request.start_time,
        "end_time": request.end_time,
        "days_of_week": request.days_of_week,
        "host_name": request.host_name,
        "host_gender": request.host_gender,
        "voice": request.voice,
        "personality": request.personality,
        "active": request.active,
    }
    updates = {key: value for key, value in allowed.items() if value is not None}
    if not updates:
        with connect(settings) as conn:
            row = conn.execute("select * from programs where id=? and channel_id='radiotedu'", (program_id,)).fetchone()
            return dict(row) if row else {"updated": False, "reason": "not_found"}
    with connect(settings) as conn:
        previous = conn.execute("select * from programs where id=? and channel_id='radiotedu'", (program_id,)).fetchone()
        if previous is None:
            return {"updated": False, "reason": "not_found"}
        assignments = ", ".join(f"{key}=?" for key in updates)
        values = list(updates.values()) + [now_iso(), program_id]
        conn.execute(f"update programs set {assignments}, updated_at=? where id=? and channel_id='radiotedu'", values)
        current = conn.execute("select * from programs where id=? and channel_id='radiotedu'", (program_id,)).fetchone()
        conn.execute(
            """
            insert into schedule_revisions (
                program_id, old_start_time, old_end_time, old_days_of_week,
                new_start_time, new_end_time, new_days_of_week, reason, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                program_id,
                previous["start_time"],
                previous["end_time"],
                previous["days_of_week"],
                current["start_time"],
                current["end_time"],
                current["days_of_week"],
                "dashboard program edit",
                now_iso(),
            ),
        )
        log_event(conn, "info", "Program edited from dashboard.", {"program_id": program_id, "fields": sorted(updates)})
        conn.commit()
        return dict(current)


VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def _validate_program_update(request: ProgramUpdateRequest) -> None:
    for field_name in ("start_time", "end_time"):
        value = getattr(request, field_name)
        if value is not None:
            _validate_time(value, field_name)
    if request.days_of_week is not None:
        days = [day.strip().lower() for day in request.days_of_week.split(",") if day.strip()]
        if not days or any(day not in VALID_DAYS for day in days):
            raise ValueError("days_of_week must use comma-separated mon,tue,wed,thu,fri,sat,sun values")
    if request.active is not None and request.active not in {0, 1}:
        raise ValueError("active must be 0 or 1")


def _validate_time(value: str, field_name: str) -> None:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, AttributeError):
        raise ValueError(f"{field_name} must be HH:MM") from None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"{field_name} must be HH:MM")


def weekly_schedule(settings: Settings) -> dict:
    day_order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    with connect(settings) as conn:
        programs = rows_to_dicts(conn.execute("select * from programs where channel_id='radiotedu' and active=1 order by start_time").fetchall())
    days = []
    for day in day_order:
        items = [
            {
                "id": program["id"],
                "name": program["name"],
                "start_time": program["start_time"],
                "end_time": program["end_time"],
                "host_name": program.get("host_name"),
                "vibe": program.get("vibe"),
            }
            for program in programs
            if day in {part.strip().lower() for part in str(program["days_of_week"]).split(",")}
        ]
        days.append({"day": day, "programs": items})
    return {"channel_id": "radiotedu", "days": days}


def emergency_fallback_playlist(settings: Settings, limit: int = 8) -> dict:
    fallback = FallbackPlaylistBuilder(settings).rebuild()
    with connect(settings) as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                select id, title, artist, genre, duration_seconds, file_path
                from tracks
                where file_path <> ''
                order by coalesce(last_played_at, ''), play_count asc, title asc
                limit ?
                """,
                (limit,),
            ).fetchall()
        )
    tracks = []
    music_root = settings.music_path.resolve()
    for row in rows:
        file_path = str(row.pop("file_path") or "")
        resolved = Path(file_path).expanduser().resolve()
        if not file_path or not resolved.is_file() or not resolved.is_relative_to(music_root):
            continue
        tracks.append({**row, "file_exists": True})
    return {
        "channel_id": "radiotedu",
        "count": len(tracks),
        "tracks": tracks,
        "playlist_path": str(fallback.playlist_path),
        "coverage_seconds": fallback.coverage_seconds,
        "required_seconds": fallback.required_seconds,
        "air_ready": fallback.air_ready,
    }


def latest_segment_clip(settings: Settings) -> dict:
    with connect(settings) as conn:
        row = conn.execute(
            """
            select clip_type, text, file_path, voice, program_id, created_at
            from generated_clips
            order by created_at desc, id desc
            limit 1
            """
        ).fetchone()
    if row is None:
        return {"available": False, "reason": "no_generated_clips"}
    item = dict(row)
    file_path = Path(item.pop("file_path") or "")
    if not file_path.exists():
        return {"available": False, "reason": "clip_file_missing", **item}
    return {
        "available": True,
        **item,
        "file_name": file_path.name,
        "file_exists": True,
    }


def metrics(settings: Settings) -> dict:
    with connect(settings) as conn:
        listener_count = conn.execute("select count(*) from listener_events").fetchone()[0]
        feedback_count = conn.execute("select count(*) from listener_events where event_type='feedback'").fetchone()[0]
    return {
        "local_listeners": listener_count if listener_count else None,
        "popularity": None,
        "average_session": None,
        "feedback_count": feedback_count,
    }


def recent_listener_messages(settings: Settings) -> list[dict]:
    with connect(settings) as conn:
        rows = conn.execute(
            """
            select created_at, metadata_json
            from listener_events
            where event_type='feedback'
            order by created_at desc, id desc
            limit 25
            """
        ).fetchall()
    messages = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        content = " ".join(str(metadata.get("text", "")).split())
        source = " ".join(str(metadata.get("source", "dashboard")).split()) or "dashboard"
        if content:
            messages.append({"content": content, "source": source, "created_at": row["created_at"]})
    return messages


def recent_incidents(settings: Settings) -> list[dict]:
    with connect(settings) as conn:
        rows = conn.execute(
            """
            select id, component, severity, status, summary, created_at, updated_at
            from incidents
            where status='open'
            order by case severity when 'critical' then 0 when 'warning' then 1 else 2 end, updated_at desc
            limit 6
            """
        ).fetchall()
    return rows_to_dicts(rows)


def recent_autonomous_tasks(settings: Settings) -> list[dict]:
    with connect(settings) as conn:
        rows = conn.execute(
            """
            select id, task_type, component, title, status, priority, attempts, created_at, updated_at
            from autonomous_tasks
            where status in ('queued', 'running')
            order by priority desc, created_at asc
            limit 6
            """
        ).fetchall()
    return rows_to_dicts(rows)


def observability(settings: Settings, agent: RadioAgent) -> dict:
    now = datetime.now(timezone.utc)
    prebuffer = agent.announcement_readiness(current_program(settings)["id"])
    with connect(settings) as conn:
        generated = conn.execute("select count(*) from generated_clips").fetchone()[0]
        errors = rows_to_dicts(
            conn.execute(
                "select message, created_at from agent_logs where level='error' order by created_at desc, id desc limit 5"
            ).fetchall()
        )
        restarts = conn.execute("select value from station_metrics where key='supervisor_restarts'").fetchone()
    return {
        "uptime_seconds": int((now - STARTED_AT).total_seconds()),
        "announcement_prebuffer": {
            "ready": int(prebuffer["ready"]),
            "used": int(prebuffer["used"]),
            "failed": int(prebuffer["failed"]),
            "required": int(prebuffer["required"]),
            "target": int(prebuffer["target"]),
            "ready_to_broadcast": bool(prebuffer["ready_to_broadcast"]),
            "oldest_ready_age_seconds": prebuffer["oldest_ready_age_seconds"],
            "next_announcement_type": prebuffer["next_announcement_type"],
        },
        "rundown_coverage": {
            "planned_seconds": int(prebuffer["planned_seconds"]),
            "rendered_seconds": int(prebuffer["rendered_seconds"]),
            "fallback_seconds": int(prebuffer["fallback_seconds"]),
            "needs_refill": bool(prebuffer["needs_refill"]),
            "air_ready": bool(prebuffer["air_ready"]),
        },
        "generated_clips": int(generated),
        "recent_errors": errors,
        "supervisor_restarts": int(restarts["value"]) if restarts else 0,
        "playback_now": agent.playback.state(),
        "news": {
            "enabled": bool(settings.news_enabled),
            "last_checked_at": agent.last_news_checked_at.isoformat() if agent.last_news_checked_at else None,
            "last_source_at": agent.last_news_source_at.isoformat() if agent.last_news_source_at else None,
            "last_source_title": agent.last_news_source_title,
            "max_age_hours": int(settings.news_max_age_hours),
        },
    }


def top_songs(settings: Settings) -> list[dict]:
    with connect(settings) as conn:
        rows = conn.execute(
            """
            select tracks.id, tracks.title, tracks.artist, count(play_history.id) as plays
            from play_history
            join tracks on tracks.id = play_history.track_id
            where play_history.played_at >= datetime('now', '-14 days')
            group by tracks.id, tracks.title, tracks.artist
            order by plays desc, tracks.title asc
            limit 10
            """
        ).fetchall()
    return rows_to_dicts(rows)


def top_genres(settings: Settings) -> list[dict]:
    with connect(settings) as conn:
        rows = conn.execute(
            """
            select coalesce(tracks.genre, 'unknown') as genre, count(play_history.id) as plays
            from play_history
            join tracks on tracks.id = play_history.track_id
            where play_history.played_at >= datetime('now', '-14 days')
            group by coalesce(tracks.genre, 'unknown')
            order by plays desc, genre asc
            limit 10
            """
        ).fetchall()
    return rows_to_dicts(rows)


def recent_logs(settings: Settings) -> list[dict]:
    with connect(settings) as conn:
        rows = conn.execute("select level, message, metadata_json, created_at from agent_logs order by created_at desc, id desc limit 50").fetchall()
    logs = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except Exception:
            item["metadata"] = {}
        logs.append(item)
    return logs


def health(settings: Settings, agent: RadioAgent) -> dict:
    llm_runtime = ollama_runtime_status(settings)
    llm_setup = check_ollama_setup(settings, runtime=llm_runtime)
    return {
        "database": "ok" if Path(settings.database_path).exists() else "missing",
        "llm": settings.ollama_model,
        "llm_runtime": llm_runtime,
        "llm_setup": llm_setup,
        "tts": getattr(agent.tts, "provider_name", settings.tts_provider),
        "tts_runtime": agent.tts.health() if hasattr(agent.tts, "health") else {
            "provider": settings.tts_provider,
            "active_provider": getattr(agent.tts, "provider_name", settings.tts_provider),
            "status": "unknown",
            "configured": False,
            "last_error": None,
        },
        "search": settings.search_provider,
        "weather": settings.weather_provider if settings.weather_enabled else "disabled",
        "playback": agent.playback.health(),
        "website_sync": website_sync_health(settings)["health"],
    }


app: FastAPI | None = None


if __name__ == "__main__":
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port, reload=False)
