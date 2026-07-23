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


def test_windows_services_use_native_pywin32_hosts_and_fail_closed_start() -> None:
    service_host = (ROOT / "packaging" / "broadcast" / "windows_service_host.py").read_text(encoding="utf-8")
    installer = (ROOT / "packaging" / "broadcast" / "install-services.ps1").read_text(encoding="utf-8")

    assert "RadioTEDUSharedAIService" in service_host
    assert "RadioTEDUBroadcastSupervisorService" in service_host
    assert "win32serviceutil.ServiceFramework" in service_host
    assert "taskkill.exe" in service_host
    assert "windows_service_host.py" in installer
    assert "InitializeConfig" in installer
    assert "smoke_broadcast.py" in installer
    assert "Strict dual-station smoke failed" in installer
    assert '"--startup", "manual"' in installer
    assert "[switch]$Automatic" in installer
    assert "sc.exe config $service start= delayed-auto" in installer
    assert "CHILD_RESTART_DELAYS_SECONDS" in service_host
    assert "sc.exe create" not in installer


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
        "RUNDOWN_PLANNED_SECONDS=14400",
        "RUNDOWN_RENDERED_SECONDS=3600",
        "RUNDOWN_REFILL_SECONDS=7200",
        "FALLBACK_COVERAGE_SECONDS=21600",
        "Do not research pop songs",
        "Research is limited to jazz and classical",
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
    assert '%ffmpeg(format="adts", %audio(codec="aac", b="192k"' in liq_template
    assert 'mount=mount' in liq_template
    assert 'user="source"' in liq_template
    assert "public=true" in liq_template
    assert "%mp3" not in liq_template
    assert "hackme" not in liq_template
    assert "playlist" in liq_template


def test_windows_package_uses_one_supervisor_and_one_shared_ai_service() -> None:
    installer = (ROOT / "packaging" / "broadcast" / "install-services.ps1").read_text(encoding="utf-8")
    runner = (ROOT / "packaging" / "broadcast" / "run-service.ps1").read_text(encoding="utf-8")
    windows_installer = (ROOT / "packaging" / "broadcast" / "install-liquidsoap-windows.ps1").read_text(
        encoding="utf-8"
    )
    env_files = sorted(path.name for path in (ROOT / "packaging" / "broadcast" / "service-env").glob("*.example"))

    assert '"RadioTEDU.SharedAI", "RadioTEDU.BroadcastSupervisor"' in installer
    assert "scripts.run_station_forever" in runner
    assert "Test-ProtectedValue" in runner
    assert "approved production voice pack" in runner
    assert "warmed loopback Qwen service" in runner
    assert "service-visible FFmpeg encoding" in runner
    assert '[string]$Version = "2.4.5"' in windows_installer
    assert '$archiveName = "liquidsoap-$Version-win64.zip"' in windows_installer
    assert "17C29C9F662DB11CED6B85E807F6038E15E32B76F30F9695506905879A43F4B6" in windows_installer
    assert "FFmpeg" in windows_installer
    assert "LIQUIDSOAP_COMMAND" in windows_installer
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


def test_broadcast_prompt_contains_time_coverage_editorial_and_talkover_contracts() -> None:
    prompt = (ROOT / "handoff" / "broadcast-server" / "prompt.md").read_text(encoding="utf-8")
    for required in (
        "Radio TED U",
        "four hours",
        "60 minutes",
        "below two hours",
        "six hours",
        "pop",
        "jazz",
        "classical",
        "10–12 dB",
        "0.65",
    ):
        assert required in prompt
    lowered = prompt.casefold()
    assert "do not research pop songs" in lowered
    assert "research is limited to jazz and classical" in lowered
    assert "stop before production" in lowered


def test_web_prompt_remains_status_only_and_two_prompts_are_canonical() -> None:
    prompts = sorted((ROOT / "handoff").glob("*/prompt.md"))
    assert len(prompts) == 2
    rendered = " ".join(path.read_text(encoding="utf-8") for path in prompts).casefold()
    for forbidden in ("post /v1/radio/control", "buy now", "send message", "cast vote"):
        assert forbidden not in rendered

    web_prompt = (ROOT / "handoff" / "web-server" / "prompt.md").read_text(encoding="utf-8").casefold()
    assert "status-only" in web_prompt
    assert "no control surface" in web_prompt
    assert "sanitized" in web_prompt
    assert "browser-local play/pause" in web_prompt
    assert "visitor's audio element" in web_prompt
    assert "never issues broadcast, playlist, or liquidsoap commands" in web_prompt

    broadcast_prompt = (ROOT / "handoff" / "broadcast-server" / "prompt.md").read_text(encoding="utf-8").casefold()
    for prompt in (web_prompt, broadcast_prompt):
        assert "https://radiotedu.com/ai" in prompt
        assert "icecast-only" in prompt
        assert "https://stream.radiotedu.com/en" in prompt
        assert "https://stream.radiotedu.com/fr" in prompt
        assert "do not create `/ai/en` or `/ai/fr`" in prompt

    assert "single listener page" in web_prompt
    assert "in-page en/fr station selector" in web_prompt
    assert "andon fm-inspired" in web_prompt
    assert "original radiotedu" in web_prompt
    assert "must not serve html, api, or application routes" in web_prompt
