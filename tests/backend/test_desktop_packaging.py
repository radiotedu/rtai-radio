import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_electron_desktop_admin_shell_is_declared() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

    assert "desktop:dev" in package["scripts"]
    assert "desktop:build" in package["scripts"]
    assert "electron" in package["devDependencies"]
    assert "electron-builder" in package["devDependencies"]

    main = (ROOT / "desktop" / "main.cjs").read_text(encoding="utf-8")
    assert "BrowserWindow" in main
    assert "RadioTEDU Admin Panel" in main
    assert "127.0.0.1" in main
    assert "child_process" in main
    assert "spawn(" in main
    assert "python" in main
    assert "backend.app" in main
    assert "killBackend" in main
    assert "radiotedu.com" not in main


def test_electron_desktop_admin_manages_local_frontend_and_setup_screen() -> None:
    main = (ROOT / "desktop" / "main.cjs").read_text(encoding="utf-8")

    assert "frontendProcess" in main
    assert "RADIOTEDU_MANAGE_FRONTEND" in main
    assert "npm" in main
    assert "run', 'dev" in main
    assert "waitForUrl" in main
    assert "loadSetupScreen" in main
    assert "killFrontend" in main
    assert "did-fail-load" in main
    assert "Backend startup failed" in main


def test_single_broadcast_computer_runner_exists() -> None:
    runner_path = ROOT / "scripts" / "run_station_forever.py"
    assert runner_path.exists()
    runner = runner_path.read_text(encoding="utf-8")

    assert '"radiotedu-en": 8765' in runner
    assert '"radiotedu-fr": 8766' in runner
    assert "build_public_sync_service" in runner
    assert "PublicSyncService" in runner
    assert "station_environment" in runner
    assert "uvicorn" in runner


def test_broadcast_runner_declares_backend_orchestrator_and_backoff_contract() -> None:
    runner = (ROOT / "scripts" / "run_station_forever.py").read_text(encoding="utf-8")

    assert "RESTART_DELAYS_SECONDS" in runner
    assert "MAX_STARTS_PER_WINDOW" in runner
    assert "backend_is_healthy" in runner
    assert "supervise" in runner
    assert "public_sync.start_background()" in runner
    assert "public_sync.stop_background()" in runner


def test_two_machine_runbooks_and_smoke_scripts_exist() -> None:
    broadcast_runbook = ROOT / "docs" / "BROADCAST_COMPUTER_RUNBOOK.md"
    website_runbook = ROOT / "docs" / "WEBSITE_SERVER_RUNBOOK.md"
    broadcast_smoke = ROOT / "scripts" / "smoke_broadcast.py"
    public_smoke = ROOT / "scripts" / "smoke_public_server.py"

    assert broadcast_runbook.exists()
    assert website_runbook.exists()
    assert broadcast_smoke.exists()
    assert public_smoke.exists()

    broadcast_text = broadcast_runbook.read_text(encoding="utf-8")
    website_text = website_runbook.read_text(encoding="utf-8")
    broadcast_script = broadcast_smoke.read_text(encoding="utf-8")
    public_script = public_smoke.read_text(encoding="utf-8")

    for required in [
        "MUSIC_DIR=F:/Songs/Jazz",
        "MIN_READY_ANNOUNCEMENTS=5",
        "https://api.radiotedu.com",
        "10.98.98.75:11154",
        "`/en`",
        "`/fr`",
        "aac_192",
        "RADIOTEDU_EN_SNAPSHOT_SECRET",
        "RADIOTEDU_FR_SNAPSHOT_SECRET",
    ]:
        assert required in broadcast_text
    for required in [
        "/ai/en",
        "/ai/fr",
        "backend.public_app",
        "/v1/radio/stations/{station_id}/snapshot",
        "school-radio-pc",
        "SNAPSHOT_TTL_SECONDS",
        "No playout controls",
    ]:
        assert required in website_text
    for required in ["check_ollama_setup", "liquidsoap_status", "platform_hmac_secret_en", "music_library"]:
        assert required in broadcast_script
    for required in ["/ai/en", "/ai/fr", "/v1/radio/stations/", "/status", "/sessions/start", "openapi"]:
        assert required in public_script


def test_public_smoke_checks_ai_route_and_strict_snapshot_payload() -> None:
    public_smoke = (ROOT / "scripts" / "smoke_public_server.py").read_text(encoding="utf-8")

    assert '"/ai/en"' in public_smoke
    assert '"/ai/fr"' in public_smoke
    assert "request_text" in public_smoke
    assert "radiotedu-en" in public_smoke
    assert "radiotedu-fr" in public_smoke
    assert "active_website_listeners" in public_smoke
    assert "forbidden_paths" in public_smoke
    assert "/api/public/snapshot" not in public_smoke


def test_required_local_streaming_and_sync_helpers_exist() -> None:
    liquidsoap_runner = (ROOT / "scripts" / "run_liquidsoap.ps1").read_text(encoding="utf-8")
    icecast_checker = (ROOT / "scripts" / "check_icecast.py").read_text(encoding="utf-8")
    public_sync = (ROOT / "backend" / "public_sync.py").read_text(encoding="utf-8")
    liq_template = (ROOT / "liquidsoap" / "radiotedu.liq").read_text(encoding="utf-8")

    assert "LIQUIDSOAP_SCRIPT" in liquidsoap_runner
    assert "liquidsoap" in liquidsoap_runner.lower()
    assert "10.98.98.75" in icecast_checker
    assert "11154" in icecast_checker
    assert '"/en", "/fr"' in icecast_checker
    assert "urllib.request" in icecast_checker
    assert "sign_platform_headers" in public_sync
    assert "idempotency_key" in public_sync
    assert "station_public_events" in public_sync
    assert "%fdkaac(bitrate=192" in liq_template
    assert 'mount=mount' in liq_template
    assert 'user="source"' in liq_template
    assert "public=true" in liq_template
    assert "%mp3" not in liq_template
    assert "hackme" not in liq_template
    assert "playlist" in liq_template


def test_windows_package_uses_one_supervisor_and_one_shared_ai_service() -> None:
    installer = (ROOT / "packaging" / "broadcast" / "install-services.ps1").read_text(encoding="utf-8")
    runner = (ROOT / "packaging" / "broadcast" / "run-service.ps1").read_text(encoding="utf-8")
    env_files = sorted(path.name for path in (ROOT / "packaging" / "broadcast" / "service-env").glob("*.example"))

    assert '"RadioTEDU.SharedAI", "RadioTEDU.BroadcastSupervisor"' in installer
    assert "scripts.run_station_forever" in runner
    assert "RadioTEDU.Station.EN" not in installer + runner
    assert "RadioTEDU.Station.FR" not in installer + runner
    assert "RadioTEDU.PublicSync" not in installer + runner
    assert env_files == ["RadioTEDU.BroadcastSupervisor.env.example", "RadioTEDU.SharedAI.env.example"]


def test_exactly_two_target_machine_codex_prompts_are_packaged() -> None:
    prompts = sorted((ROOT / "handoff").glob("*/prompt.md"))
    assert [path.parent.name for path in prompts] == ["broadcast-server", "web-server"]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in prompts)

    for required in (
        "This computer is not the builder computer",
        "10.98.98.75:11154",
        "https://stream.radiotedu.com/en",
        "https://stream.radiotedu.com/fr",
        "https://api.radiotedu.com",
        "staging",
    ):
        assert required in combined
    assert not (ROOT / "packaging" / "streaming").exists()
