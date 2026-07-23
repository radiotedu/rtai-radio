from __future__ import annotations

from datetime import date
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .platform_api import (
    PublicSession,
    apply_session_operation,
    install_platform_routes,
    station_status_payload,
)


COMPATIBILITY_STATION_ID = "radiotedu-en"
COMPATIBILITY_SUNSET = date(2026, 10, 15).strftime("%a, %d %b %Y 00:00:00 GMT")


def create_public_app(
    settings: Settings | None = None,
    *,
    frontend_dist: Path | None = None,
) -> FastAPI:
    """Create the website-only API/UI process with no operator runtime attached."""
    settings = settings or Settings.from_env()
    frontend_dist = frontend_dist or Path(__file__).resolve().parents[1] / "dist" / "frontend"

    app = FastAPI(
        title="RadioTEDU Public Platform",
        version="1.0",
        description="Status ingestion and bilingual listener pages. This service has no playout controls.",
    )
    app.state.settings = settings
    install_platform_routes(app, settings)

    assets = frontend_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="frontend_assets")
    brand_assets = frontend_dist / "brand"
    if brand_assets.exists():
        app.mount("/brand", StaticFiles(directory=str(brand_assets)), name="frontend_brand_assets")
    program_assets = frontend_dist / "programs"
    if program_assets.exists():
        app.mount("/programs", StaticFiles(directory=str(program_assets)), name="frontend_program_assets")

    @app.middleware("http")
    async def public_security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; media-src 'self' https://stream.radiotedu.com; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'none'"
        )
        return response

    def listener_page() -> FileResponse:
        index_path = frontend_dist / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="frontend build is not available")
        return FileResponse(str(index_path), media_type="text/html")

    app.add_api_route("/ai", listener_page, methods=["GET"], include_in_schema=False)

    if settings.public_compatibility_enabled:
        compatibility_headers = {
            "Deprecation": "true",
            "Sunset": COMPATIBILITY_SUNSET,
            "Link": '</v1/radio/stations/radiotedu-en/status>; rel="successor-version"',
        }

        @app.get("/api/public/status", deprecated=True)
        def compatibility_status() -> JSONResponse:
            payload = station_status_payload(settings, COMPATIBILITY_STATION_ID)
            return JSONResponse(payload, headers=compatibility_headers)

        def compatibility_session(session: PublicSession, operation: str) -> JSONResponse:
            payload = apply_session_operation(settings, COMPATIBILITY_STATION_ID, session.session_id, operation)
            if isinstance(payload, Response):
                return payload
            return JSONResponse(payload, headers=compatibility_headers)

        @app.post("/api/public/session/start", deprecated=True)
        def compatibility_session_start(session: PublicSession):
            return compatibility_session(session, "start")

        @app.post("/api/public/session/heartbeat", deprecated=True)
        def compatibility_session_heartbeat(session: PublicSession):
            return compatibility_session(session, "heartbeat")

        @app.post("/api/public/session/end", deprecated=True)
        def compatibility_session_end(session: PublicSession):
            return compatibility_session(session, "end")

    return app


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(create_public_app(settings), host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
    main()
