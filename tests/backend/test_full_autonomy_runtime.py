import importlib.util
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.database import connect, init_db
from backend.editorial_research import EditorialResearchService
from backend.llm import choose_track_with_llm
from backend.liquidsoap import render_liquidsoap_config
from backend.music_library import scan_music
from backend.ollama_setup import check_ollama_setup, repair_ollama_runtime
from backend.orchestrator import AutonomousOrchestrator
from backend.playback import PlaybackController, QueueItem
from backend.radio_agent import RadioAgent
from backend.rundown import CoverageStatus
from backend.search.base import SearchResult
from backend.stations.context import coerce_station_context
from backend.tts.contracts import QwenUnavailableError
from backend.tts.qwen_tts import QwenTTSProvider
from backend.tts.sapi_tts import SapiTTSProvider


class RecordingSapiTTSProvider(SapiTTSProvider):
    def _invoke_sapi(self, text: str, output_path: Path, voice: str | None = None) -> None:
        with wave.open(str(output_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 1600)


def make_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 800)


def fake_qwen_synthesis(_agent, _text, output_path, **_kwargs) -> str:
    output = Path(output_path)
    make_wav(output)
    return str(output)


def make_settings(root: Path) -> Settings:
    return Settings(
        database_path=str(root / "radiotedu.db"),
        music_dir=str(root / "music"),
        static_dir=str(root / "static"),
        rss_feeds_path=str(root / "rss_feeds.json"),
        playback_backend="simulate",
        min_ready_announcements=0,
        ollama_url="http://127.0.0.1:9",
        ollama_timeout_seconds=1,
    )


def force_night_lab(settings: Settings) -> None:
    with connect(settings) as conn:
        conn.execute("update programs set active=0 where id <> 'night_lab'")
        conn.execute(
            "update programs set active=1, start_time='00:00', end_time='23:59', days_of_week='mon,tue,wed,thu,fri,sat,sun' where id='night_lab'"
        )
        conn.commit()


class FullAutonomyRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(RadioAgent, "_synthesize_qwen", fake_qwen_synthesis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sapi_tts_provider_writes_real_wav_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "voice.wav"
            provider = RecordingSapiTTSProvider(command_path="powershell")
            result = provider.synthesize("RadioTEDU is live from the local library.", str(output))
            self.assertEqual(str(output), result)
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 44)
            self.assertEqual(
                "RadioTEDU is live from the local library.",
                output.with_suffix(".txt").read_text(encoding="utf-8"),
            )

    def test_radio_agent_never_uses_configured_sapi_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.tts_provider = "qwen"
            settings.qwen_tts_command = ""
            settings.fallback_tts_provider = "sapi"
            agent = RadioAgent(settings)
            self.assertEqual("qwen", agent.tts.provider_name)
            self.assertFalse(hasattr(agent.tts, "fallback"))

    def test_pop_editorial_uses_localized_filler_without_search(self) -> None:
        class RecordingProvider:
            queries = []

            def search(self, query, limit=5):
                self.queries.append((query, limit))
                return []

        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            agent = RadioAgent(settings)
            provider = RecordingProvider()
            agent.editorial_research = EditorialResearchService(provider)

            announcement = agent._editorial_announcement(
                {"name": "Campus Flow"},
                {"id": 1, "title": "Levitating", "artist": "Dua Lipa", "genre": "pop"},
            )

            self.assertEqual([], provider.queries)
            self.assertEqual("pop_liner", announcement["metadata"]["kind"])
            self.assertIn("Radio TED U", announcement["text"])
            self.assertIn("Levitating", announcement["text"])

    def test_curated_editorial_persists_fact_card_provenance(self) -> None:
        class RecordingProvider:
            def search(self, query, limit=5):
                del query, limit
                return [
                    SearchResult(
                        "Blue Room by Alice",
                        "https://music.example/blue-room",
                        "Alice recorded Blue Room during a late-night jazz session.",
                        "searxng",
                    )
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            agent = RadioAgent(settings)
            agent.editorial_research = EditorialResearchService(RecordingProvider())
            with connect(settings) as conn:
                conn.execute("update tracks set genre='jazz' where title='Blue Room'")
                track = dict(conn.execute("select * from tracks where title='Blue Room'").fetchone())
                conn.commit()

            announcement = agent._editorial_announcement({"name": "Jazz Lab"}, track)

            self.assertEqual("sourced_fact", announcement["metadata"]["kind"])
            self.assertEqual("https://music.example/blue-room", announcement["metadata"]["context_url"])
            with connect(settings) as conn:
                card = conn.execute(
                    "select track_id, language, url, source, match_evidence from editorial_fact_cards"
                ).fetchone()
            self.assertEqual(track["id"], card["track_id"])
            self.assertEqual("en", card["language"])
            self.assertEqual("https://music.example/blue-room", card["url"])
            self.assertEqual("searxng", card["source"])
            self.assertIn("title+artist", card["match_evidence"])

    def test_listener_messages_api_returns_sanitized_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = TestClient(create_app(settings))
            response = client.post(
                "/api/listener/feedback",
                json={"text": "please play mellow piano and pay for late night jazz", "source": "dashboard"},
            )
            self.assertEqual(200, response.status_code)
            inbox = client.get("/api/listener/messages").json()
            status = client.get("/api/status").json()
            self.assertEqual(1, len(inbox))
            self.assertEqual(1, len(status["listener_messages"]))
            self.assertEqual("dashboard", inbox[0]["source"])
            self.assertIn("mellow piano", inbox[0]["content"])
            self.assertIn("mellow piano", status["listener_messages"][0]["content"])
            self.assertNotRegex(inbox[0]["content"].lower(), r"money|payment|donation|support|revenue|profit|buy|purchase|pay")

    def test_listener_feedback_queues_real_tts_reply_without_music(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            app = create_app(settings)
            client = TestClient(app)

            response = client.post(
                "/api/listener/feedback",
                json={"text": "please play mellow piano and pay for late night jazz", "source": "dashboard"},
            )

            self.assertEqual(200, response.status_code)
            payload = response.json()
            self.assertTrue(payload["stored"])
            self.assertTrue(payload["reply_queued"])
            self.assertEqual(1, len(app.state.agent.playback.queue))
            self.assertEqual("tts", app.state.agent.playback.queue[0].item_type)
            with connect(settings) as conn:
                clip = conn.execute(
                    "select clip_type, text, file_path from generated_clips where clip_type='listener_reply'"
                ).fetchone()
            self.assertIsNotNone(clip)
            self.assertTrue(Path(clip["file_path"]).exists())
            self.assertIn("mellow piano", clip["text"])
            self.assertIn("ask for late night jazz", clip["text"])
            self.assertRegex(clip["text"], r"RadioTEDU|Jazz Lab|TEDU Dawn|Campus Flow|Weekend Signal")
            self.assertNotRegex(clip["text"].lower(), r"money|payment|donation|support|revenue|profit|buy|purchase|pay")

    def test_forever_runner_exposes_backend_and_frontend_specs(self) -> None:
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "run_station_forever.py"
        spec = importlib.util.spec_from_file_location("run_station_forever", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        specs = module.build_process_specs(Path("F:/RTAI/RadioTEDU"), start_frontend=True)
        self.assertEqual(
            ["backend-radiotedu-en", "backend-radiotedu-fr", "frontend-development-only"],
            [item.name for item in specs],
        )
        self.assertTrue(specs[0].args[0].lower().endswith("python.exe"))
        self.assertEqual(
            ["-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8765"],
            specs[0].args[1:],
        )
        self.assertIn("npm.cmd", specs[2].args[0])
        inherited = {
            "PATH": "safe",
            "RADIOTEDU_EN_SOURCE_CREDENTIALS": "en-secret",
            "RADIOTEDU_FR_SOURCE_CREDENTIALS": "fr-secret",
            "RADIOTEDU_EN_SNAPSHOT_SECRET": "en-hmac",
            "RADIOTEDU_FR_SNAPSHOT_SECRET": "fr-hmac",
        }
        en_environment = module.process_environment(specs[0], inherited)
        fr_environment = module.process_environment(specs[1], inherited)
        self.assertEqual("en-secret", en_environment["RADIOTEDU_EN_SOURCE_CREDENTIALS"])
        self.assertNotIn("RADIOTEDU_FR_SOURCE_CREDENTIALS", en_environment)
        self.assertEqual("fr-secret", fr_environment["RADIOTEDU_FR_SOURCE_CREDENTIALS"])
        self.assertNotIn("RADIOTEDU_EN_SOURCE_CREDENTIALS", fr_environment)
        self.assertNotIn("RADIOTEDU_EN_SNAPSHOT_SECRET", en_environment)
        self.assertNotIn("RADIOTEDU_FR_SNAPSHOT_SECRET", fr_environment)
        self.assertFalse(module.backend_health_due(started_at=100.0, now=110.0, grace_seconds=30))
        self.assertTrue(module.backend_health_due(started_at=100.0, now=131.0, grace_seconds=30))

    def test_windows_startup_task_script_installs_forever_runner(self) -> None:
        script = Path(__file__).resolve().parents[2] / "scripts" / "install_windows_task.ps1"
        content = script.read_text(encoding="utf-8")
        self.assertIn("Register-ScheduledTask", content)
        self.assertIn("run_station_forever.py", content)
        self.assertNotRegex(content.lower(), r"money|payment|donation|support|revenue|profit|buy|purchase")

    def test_ollama_setup_check_reports_missing_cli_without_installing(self) -> None:
        settings = Settings(ollama_url="http://127.0.0.1:11434", ollama_model="qwen3.5:4b")
        result = check_ollama_setup(
            settings,
            which=lambda _name: None,
            fetch_json=lambda _url: (_ for _ in ()).throw(RuntimeError("connection refused")),
        )
        self.assertEqual("cli_missing", result["status"])
        self.assertFalse(result["cli_found"])
        self.assertFalse(result["server_reachable"])
        self.assertFalse(result["model_available"])
        self.assertIn("winget install Ollama.Ollama", result["suggested_commands"])
        self.assertIn("ollama pull qwen3.5:4b", result["suggested_commands"])
        self.assertNotIn("installed", result["summary"].lower())

    def test_ollama_setup_check_reports_missing_model_pull_command(self) -> None:
        settings = Settings(ollama_url="http://127.0.0.1:11434", ollama_model="qwen3.5:4b")
        result = check_ollama_setup(
            settings,
            which=lambda _name: "C:/Program Files/Ollama/ollama.exe",
            fetch_json=lambda _url: {"models": [{"name": "llama3.2:3b"}]},
        )
        self.assertEqual("model_missing", result["status"])
        self.assertTrue(result["cli_found"])
        self.assertTrue(result["server_reachable"])
        self.assertFalse(result["model_available"])
        self.assertEqual(["llama3.2:3b"], result["installed_models"])
        self.assertIn("ollama pull qwen3.5:4b", result["suggested_commands"])
        self.assertNotIn("winget install Ollama.Ollama", result["suggested_commands"])

    def test_ollama_check_script_renders_operator_guidance(self) -> None:
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "check_ollama.py"
        spec = importlib.util.spec_from_file_location("check_ollama", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        report = module.render_report(
            {
                "status": "model_missing",
                "provider": "ollama",
                "configured_model": "qwen3.5:4b",
                "base_url": "http://127.0.0.1:11434",
                "cli_found": True,
                "cli_path": "C:/Program Files/Ollama/ollama.exe",
                "server_reachable": True,
                "model_available": False,
                "installed_models": ["llama3.2:3b"],
                "suggested_commands": ["ollama pull qwen3.5:4b"],
                "summary": "Ollama is reachable, but qwen3.5:4b is not installed.",
                "error": "Model qwen3.5:4b is not installed.",
            }
        )
        self.assertIn("qwen3.5:4b", report)
        self.assertIn("ollama pull qwen3.5:4b", report)
        self.assertNotRegex(report.lower(), r"money|payment|donation|support|revenue|profit|buy|purchase")

    def test_ollama_check_script_exposes_explicit_bootstrap_actions(self) -> None:
        script = Path(__file__).resolve().parents[2] / "scripts" / "check_ollama.py"
        content = script.read_text(encoding="utf-8")
        self.assertIn("--install", content)
        self.assertIn("--start", content)
        self.assertIn("--pull", content)
        self.assertIn("winget", content)
        self.assertIn("Start-Process", content)

    def test_real_playback_waits_for_external_player(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.playback_backend = "mpv"
            settings.mpv_path = "mpv"

            class FakeProcess:
                waited = False

                def wait(self):
                    FakeProcess.waited = True

            with patch("backend.playback.shutil.which", return_value="mpv"), patch("backend.playback.subprocess.Popen", return_value=FakeProcess()):
                controller = PlaybackController(settings)
                controller.add(QueueItem("track", "Blue Room", str(Path(tmp) / "song.wav")))
                played = controller.play_next()
                self.assertEqual("Blue Room", played.title)
                self.assertTrue(FakeProcess.waited)

    def test_prebuffer_blocks_broadcast_until_minimum_announcements_are_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.min_ready_announcements = 5
            init_db(settings)
            agent = RadioAgent(settings)
            with connect(settings) as conn:
                conn.execute(
                    "insert into announcement_queue (text, file_path, status, program_id, source, created_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?)",
                    ("ready line", str(Path(tmp) / "ready.wav"), "ready", "night_lab", "test", "2026-07-05T00:00:00+00:00", "{}"),
                )
                conn.commit()
            readiness = agent.announcement_readiness("night_lab")
            self.assertEqual(1, readiness["ready"])
            self.assertFalse(readiness["ready_to_broadcast"])

            result = agent.ensure_announcement_prebuffer("night_lab")
            self.assertGreaterEqual(result["ready"], 5)
            self.assertFalse(result["ready_to_broadcast"])
            self.assertFalse(result["air_ready"])

    def test_prebuffer_readiness_reports_age_failures_and_next_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.min_ready_announcements = 2
            init_db(settings)
            agent = RadioAgent(settings)
            with connect(settings) as conn:
                conn.execute(
                    "insert into announcement_queue (text, file_path, status, program_id, source, created_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "news line",
                        str(Path(tmp) / "news.wav"),
                        "ready",
                        "night_lab",
                        "agent_prebuffer",
                        "2026-07-05T00:00:00+00:00",
                        json.dumps({"kind": "news", "prebuffer": True}, ensure_ascii=True),
                    ),
                )
                conn.execute(
                    "insert into announcement_queue (text, file_path, status, program_id, source, created_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?)",
                    ("failed line", "", "failed", "night_lab", "agent_prebuffer", "2026-07-05T00:01:00+00:00", "{}"),
                )
                conn.execute(
                    "insert into announcement_queue (text, file_path, status, program_id, source, created_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?)",
                    ("used line", "", "used", "night_lab", "agent_prebuffer", "2026-07-05T00:02:00+00:00", "{}"),
                )
                conn.commit()

            readiness = agent.announcement_readiness("night_lab")

            self.assertEqual(1, readiness["ready"])
            self.assertEqual(1, readiness["failed"])
            self.assertEqual(1, readiness["used"])
            self.assertEqual("news", readiness["next_announcement_type"])
            self.assertIsNotNone(readiness["oldest_ready_age_seconds"])
            self.assertFalse(readiness["ready_to_broadcast"])

    def test_full_prebuffer_refill_targets_maximum_ready_announcements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 2
            settings.max_ready_announcements = 4
            for index in range(6):
                make_wav(root / "music" / f"Artist {index} - Track {index}.wav")
            scan_music(settings)
            force_night_lab(settings)
            agent = RadioAgent(settings)

            readiness = agent.ensure_announcement_prebuffer("night_lab")

            self.assertFalse(readiness["ready_to_broadcast"])
            self.assertFalse(readiness["air_ready"])
            self.assertEqual(4, readiness["ready"])
            self.assertEqual(4, readiness["target"])

    def test_autonomous_tick_refills_prebuffer_even_when_playback_queue_is_not_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 2
            make_wav(root / "music" / "Alice - Blue Room.wav")
            make_wav(root / "music" / "Ben - Amber Night.wav")
            scan_music(settings)
            with connect(settings) as conn:
                conn.execute(
                    "insert into announcement_queue (text, file_path, status, program_id, source, created_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?)",
                    ("ready line", str(root / "ready.wav"), "ready", "night_lab", "test", "2026-07-05T00:00:00+00:00", "{}"),
                )
                conn.commit()
            agent = RadioAgent(settings)
            force_night_lab(settings)
            agent.playback.add(QueueItem("tts", "Pending listener reply", str(root / "listener.wav")))
            orchestrator = AutonomousOrchestrator(settings, agent)

            result = orchestrator.tick()

            self.assertFalse(result["played"])
            self.assertIn("prebuffer", result)
            self.assertFalse(result["prebuffer"]["ready_to_broadcast"])
            self.assertFalse(result["prebuffer"]["air_ready"])
            self.assertEqual(1, len(agent.playback.queue))
            with connect(settings) as conn:
                ready = conn.execute("select count(*) from announcement_queue where status='ready'").fetchone()[0]
                plays = conn.execute("select count(*) from play_history").fetchone()[0]
            self.assertEqual(2, ready)
            self.assertEqual(0, plays)

    def test_orchestrator_backfills_only_one_announcement_per_tick_to_avoid_dead_air(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 5
            settings.max_ready_announcements = 8
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)

            class CountingAgent(RadioAgent):
                calls = []

                def maintain_rundown(self, max_render_items=1):
                    self.calls.append(max_render_items)
                    return CoverageStatus(0, 0, 0, True, False)

            agent = CountingAgent(settings)
            agent.playback.add(QueueItem("tts", "Already queued", str(root / "queued.wav")))
            orchestrator = AutonomousOrchestrator(settings, agent)

            result = orchestrator.tick()

            self.assertFalse(result["played"])
            self.assertEqual([1], agent.calls)

    def test_orchestrator_records_llm_incident_and_recovery_task_when_runtime_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            agent = RadioAgent(settings)
            orchestrator = AutonomousOrchestrator(settings, agent)
            from backend import orchestrator as orchestrator_module

            original = orchestrator_module.ollama_runtime_status
            orchestrator_module.ollama_runtime_status = lambda _settings: {
                "provider": "ollama",
                "configured_model": "qwen3.5:4b",
                "base_url": "http://127.0.0.1:11434",
                "reachable": False,
                "model_available": False,
                "installed_models": [],
                "status": "unreachable",
                "error": "connection refused",
            }
            try:
                result = orchestrator.tick()
            finally:
                orchestrator_module.ollama_runtime_status = original

            self.assertIn("recovery", result)
            with connect(settings) as conn:
                incident = conn.execute("select component, severity, status from incidents where component='llm'").fetchone()
                task = conn.execute("select task_type, status, priority from autonomous_tasks where task_type='restart_llm_runtime'").fetchone()
            self.assertIsNotNone(incident)
            self.assertEqual(("llm", "warning", "open"), tuple(incident))
            self.assertIsNotNone(task)
            self.assertEqual(("restart_llm_runtime", "queued", 80), tuple(task))

    def test_orchestrator_does_not_duplicate_open_recovery_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            agent = RadioAgent(settings)
            orchestrator = AutonomousOrchestrator(settings, agent)
            from backend import orchestrator as orchestrator_module

            original = orchestrator_module.ollama_runtime_status
            orchestrator_module.ollama_runtime_status = lambda _settings: {
                "provider": "ollama",
                "configured_model": "qwen3.5:4b",
                "base_url": "http://127.0.0.1:11434",
                "reachable": False,
                "model_available": False,
                "installed_models": [],
                "status": "unreachable",
                "error": "connection refused",
            }
            try:
                orchestrator.tick()
                orchestrator.tick()
            finally:
                orchestrator_module.ollama_runtime_status = original

            with connect(settings) as conn:
                task_count = conn.execute("select count(*) from autonomous_tasks where task_type='restart_llm_runtime' and status='queued'").fetchone()[0]
            self.assertEqual(1, task_count)

    def test_orchestrator_restart_llm_task_starts_ollama_and_pulls_missing_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.ollama_model = "qwen3.5:4b"
            agent = RadioAgent(settings)
            orchestrator = AutonomousOrchestrator(settings, agent)
            calls = []

            def fake_repair(repair_settings):
                calls.append(repair_settings.ollama_model)
                return {
                    "status": "ready",
                    "configured_model": "qwen3.5:4b",
                    "start_attempted": True,
                    "pull_attempted": True,
                    "actions": ["start", "pull"],
                }

            from backend import orchestrator as orchestrator_module

            original = orchestrator_module.repair_ollama_runtime
            orchestrator_module.repair_ollama_runtime = fake_repair
            try:
                details = orchestrator._run_task({"task_type": "restart_llm_runtime"})
            finally:
                orchestrator_module.repair_ollama_runtime = original

            self.assertEqual(["qwen3.5:4b"], calls)
            self.assertEqual("ready", details["status"])
            self.assertTrue(details["start_attempted"])
            self.assertTrue(details["pull_attempted"])

    def test_ollama_repair_starts_server_and_pulls_missing_model_before_rechecking(self) -> None:
        settings = Settings(ollama_url="http://127.0.0.1:11434", ollama_model="qwen3.5:4b")
        calls = []
        states = [
            {
                "provider": "ollama",
                "configured_model": "qwen3.5:4b",
                "base_url": "http://127.0.0.1:11434",
                "reachable": False,
                "model_available": False,
                "installed_models": [],
                "status": "unreachable",
                "error": "connection refused",
            },
            {
                "provider": "ollama",
                "configured_model": "qwen3.5:4b",
                "base_url": "http://127.0.0.1:11434",
                "reachable": True,
                "model_available": False,
                "installed_models": [],
                "status": "model_missing",
                "error": None,
            },
            {
                "provider": "ollama",
                "configured_model": "qwen3.5:4b",
                "base_url": "http://127.0.0.1:11434",
                "reachable": True,
                "model_available": True,
                "installed_models": ["qwen3.5:4b"],
                "status": "ready",
                "error": None,
            },
        ]

        def fake_runtime(_settings):
            return states.pop(0)

        def fake_runner(command):
            calls.append(command)
            return 0

        result = repair_ollama_runtime(
            settings,
            runtime_status=fake_runtime,
            runner=fake_runner,
            sleeper=lambda _seconds: None,
            which=lambda _name: "C:/Program Files/Ollama/ollama.exe",
        )

        self.assertEqual([["ollama", "serve"], ["ollama", "pull", "qwen3.5:4b"]], calls)
        self.assertEqual("ready", result["status"])
        self.assertTrue(result["start_attempted"])
        self.assertTrue(result["pull_attempted"])

    def test_disabled_llm_provider_uses_deterministic_fallback_without_network(self) -> None:
        settings = Settings(llm_provider="disabled", ollama_timeout_seconds=30)
        with patch("backend.llm.call_ollama", side_effect=AssertionError("network should not be called")):
            choice = choose_track_with_llm(
                [{"id": 7, "title": "Blue Room", "artist": "Alice"}],
                {"name": "Night Lab"},
                [],
                [],
                settings,
            )

        self.assertEqual(7, choice.song_id)
        self.assertTrue(choice.used_fallback)
        self.assertIn("disabled", choice.reason)

    def test_orchestrator_records_prebuffer_incident_when_announcements_cannot_be_prepared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 2
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)

            class BrokenPrebufferAgent(RadioAgent):
                def ensure_announcement_prebuffer(self, program_id=None, max_to_prepare=None):
                    return {"ready": 0, "used": 0, "failed": 2, "required": 2, "ready_to_broadcast": False}

            orchestrator = AutonomousOrchestrator(settings, BrokenPrebufferAgent(settings))
            result = orchestrator.tick()

            self.assertFalse(result["prebuffer"]["ready_to_broadcast"])
            with connect(settings) as conn:
                incident = conn.execute("select component, severity, status from incidents where component='prebuffer'").fetchone()
                task = conn.execute("select task_type, status from autonomous_tasks where task_type='repair_announcement_prebuffer'").fetchone()
            self.assertIsNotNone(incident)
            self.assertEqual(("prebuffer", "critical", "open"), tuple(incident))
            self.assertIsNotNone(task)
            self.assertEqual(("repair_announcement_prebuffer", "queued"), tuple(task))

    def test_orchestrator_executes_music_rescan_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            init_db(settings)
            make_wav(root / "music" / "Alice - Blue Room.wav")
            agent = RadioAgent(settings)
            orchestrator = AutonomousOrchestrator(settings, agent)
            with connect(settings) as conn:
                conn.execute(
                    "insert into autonomous_tasks (task_type, component, title, status, priority, details_json, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("rescan_music_library", "music_library", "Rescan music", "queued", 60, "{}", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00"),
                )
                conn.commit()

            result = orchestrator.execute_next_task()

            self.assertEqual("completed", result["status"])
            with connect(settings) as conn:
                tracks = conn.execute("select count(*) from tracks").fetchone()[0]
                task = conn.execute("select status, attempts from autonomous_tasks where task_type='rescan_music_library'").fetchone()
            self.assertEqual(1, tracks)
            self.assertEqual(("completed", 1), tuple(task))

    def test_orchestrator_executes_prebuffer_repair_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 1
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            agent = RadioAgent(settings)
            orchestrator = AutonomousOrchestrator(settings, agent)
            with connect(settings) as conn:
                conn.execute(
                    "insert into autonomous_tasks (task_type, component, title, status, priority, details_json, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("repair_announcement_prebuffer", "prebuffer", "Repair prebuffer", "queued", 90, "{}", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00"),
                )
                conn.commit()

            result = orchestrator.execute_next_task()

            self.assertEqual("completed", result["status"])
            with connect(settings) as conn:
                ready = conn.execute("select count(*) from announcement_queue where status='ready'").fetchone()[0]
            self.assertGreaterEqual(ready, 1)

    def test_orchestrator_marks_failed_task_without_infinite_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            agent = RadioAgent(settings)
            orchestrator = AutonomousOrchestrator(settings, agent)
            with connect(settings) as conn:
                conn.execute(
                    "insert into autonomous_tasks (task_type, component, title, status, priority, details_json, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("unknown_task", "test", "Unknown", "queued", 10, "{}", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00"),
                )
                conn.commit()

            result = orchestrator.execute_next_task()

            self.assertEqual("failed", result["status"])
            with connect(settings) as conn:
                task = conn.execute("select status, attempts from autonomous_tasks where task_type='unknown_task'").fetchone()
            self.assertEqual(("failed", 1), tuple(task))

    def test_prebuffer_announcements_are_bound_to_real_upcoming_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 2
            make_wav(root / "music" / "Alice - Blue Room.wav")
            make_wav(root / "music" / "Ben - Amber Night.wav")
            scan_music(settings)
            agent = RadioAgent(settings)

            result = agent.ensure_announcement_prebuffer("night_lab")

            self.assertFalse(result["ready_to_broadcast"])
            self.assertFalse(result["air_ready"])
            with connect(settings) as conn:
                rows = conn.execute(
                    """
                    select text, metadata_json
                    from announcement_queue
                    where status='ready' and program_id='night_lab'
                    order by created_at asc, id asc
                    limit 2
                    """
                ).fetchall()
                real_track_ids = {
                    row["id"] for row in conn.execute("select id from tracks order by id").fetchall()
                }
            self.assertEqual(2, len(rows))
            planned_ids = []
            for row in rows:
                metadata = json.loads(row["metadata_json"])
                planned_ids.append(metadata["track_id"])
                self.assertIn(metadata["track_id"], real_track_ids)
                self.assertIn(metadata["track_title"], row["text"])
                self.assertIn(metadata["track_artist"], row["text"])
                self.assertLessEqual(len(row["text"].split()), 24)
            self.assertEqual(len(planned_ids), len(set(planned_ids)))

    def test_prebuffer_announcement_uses_strict_curated_context_when_llm_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 1
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)

            class SearchProvider:
                def search(self, query: str, limit: int = 5):
                    del query, limit
                    return [
                        SearchResult(
                            "Alice Blue Room jazz session",
                            "https://example.test/alice-blue-room",
                            "Alice recorded Blue Room as a hard bop campus session with a late-night trio arrangement.",
                            "searxng",
                        )
                    ]

            agent = RadioAgent(settings)
            agent.editorial_research = EditorialResearchService(SearchProvider())
            with connect(settings) as conn:
                conn.execute("update tracks set genre='jazz' where title='Blue Room'")
                conn.commit()
            result = agent.ensure_announcement_prebuffer("night_lab")

            self.assertFalse(result["ready_to_broadcast"])
            self.assertFalse(result["air_ready"])
            with connect(settings) as conn:
                row = conn.execute(
                    "select text, metadata_json from announcement_queue where status='ready' and program_id='night_lab'"
                ).fetchone()
            metadata = json.loads(row["metadata_json"])
            self.assertEqual("Blue Room", metadata["track_title"])
            self.assertTrue(metadata["used_fallback"])
            self.assertEqual("dead_air_prevention", metadata["fallback_role"])
            self.assertEqual("sourced_fact", metadata["kind"])
            self.assertEqual("searxng", metadata["source"])
            self.assertIn("hard bop", row["text"])
            self.assertLessEqual(len(row["text"].split()), 40)

    def test_prebuffer_replaces_legacy_generic_agent_rows_when_tracks_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 2
            make_wav(root / "music" / "Alice - Blue Room.wav")
            make_wav(root / "music" / "Ben - Amber Night.wav")
            scan_music(settings)
            agent = RadioAgent(settings)
            with connect(settings) as conn:
                for index in range(2):
                    conn.execute(
                        "insert into announcement_queue (text, file_path, status, program_id, source, created_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"legacy generic {index}",
                            str(settings.tts_path / f"legacy_{index}.wav"),
                            "ready",
                            "night_lab",
                            "agent_prebuffer",
                            f"2026-07-05T00:00:0{index}+00:00",
                            json.dumps({"program": "Jazz Lab", "prebuffer": True}, ensure_ascii=True),
                        ),
                    )
                conn.commit()

            result = agent.ensure_announcement_prebuffer("night_lab")

            self.assertFalse(result["ready_to_broadcast"])
            self.assertFalse(result["air_ready"])
            with connect(settings) as conn:
                legacy = conn.execute("select status from announcement_queue where text like 'legacy generic%' order by id").fetchall()
                ready = conn.execute(
                    "select metadata_json from announcement_queue where status='ready' and program_id='night_lab' order by id"
                ).fetchall()
            self.assertEqual(["stale", "stale"], [row["status"] for row in legacy])
            self.assertEqual(2, len(ready))
            self.assertTrue(all(json.loads(row["metadata_json"]).get("track_id") for row in ready))

    def test_prebuffer_avoids_duplicate_track_plan_if_another_runner_inserts_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 2
            make_wav(root / "music" / "Alice - Blue Room.wav")
            make_wav(root / "music" / "Ben - Amber Night.wav")
            make_wav(root / "music" / "Cara - Dawn Steps.wav")
            scan_music(settings)

            class RacingAgent(RadioAgent):
                injected = False

                def _prepare_announcement(self, program, index, required, planned_track_ids):
                    prepared = super()._prepare_announcement(program, index, required, planned_track_ids)
                    if not self.injected and prepared["metadata"].get("track_id") is not None:
                        self.injected = True
                        metadata = prepared["metadata"]
                        with connect(settings) as conn:
                            conn.execute(
                                "insert into announcement_queue (text, file_path, status, program_id, source, created_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?)",
                                (
                                    "concurrent prepared intro",
                                    str(settings.tts_path / "concurrent.wav"),
                                    "ready",
                                    "night_lab",
                                    "agent_prebuffer",
                                    "2026-07-05T00:00:00+00:00",
                                    json.dumps(metadata, ensure_ascii=True),
                                ),
                            )
                            conn.commit()
                    return prepared

            agent = RacingAgent(settings)
            result = agent.ensure_announcement_prebuffer("night_lab")

            self.assertFalse(result["ready_to_broadcast"])
            self.assertFalse(result["air_ready"])
            with connect(settings) as conn:
                ready = conn.execute(
                    "select metadata_json from announcement_queue where status='ready' and program_id='night_lab' order by id"
                ).fetchall()
            track_ids = [json.loads(row["metadata_json"])["track_id"] for row in ready]
            self.assertEqual(3, len(track_ids))
            self.assertEqual(len(track_ids), len(set(track_ids)))

    def test_prebuffer_retires_existing_duplicate_track_bound_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 2
            make_wav(root / "music" / "Alice - Blue Room.wav")
            make_wav(root / "music" / "Ben - Amber Night.wav")
            make_wav(root / "music" / "Cara - Dawn Steps.wav")
            scan_music(settings)
            agent = RadioAgent(settings)
            with connect(settings) as conn:
                alice = conn.execute("select id, title, artist, genre from tracks where artist='Alice'").fetchone()
                metadata = {
                    "program": "Jazz Lab",
                    "prebuffer": True,
                    "track_id": alice["id"],
                    "track_title": alice["title"],
                    "track_artist": alice["artist"],
                    "track_genre": alice["genre"],
                }
                for index in range(2):
                    conn.execute(
                        "insert into announcement_queue (text, file_path, status, program_id, source, created_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"duplicate alice {index}",
                            str(settings.tts_path / f"duplicate_{index}.wav"),
                            "ready",
                            "night_lab",
                            "agent_prebuffer",
                            f"2026-07-05T00:00:0{index}+00:00",
                            json.dumps(metadata, ensure_ascii=True),
                        ),
                    )
                conn.commit()

            result = agent.ensure_announcement_prebuffer("night_lab")

            self.assertFalse(result["ready_to_broadcast"])
            self.assertFalse(result["air_ready"])
            with connect(settings) as conn:
                duplicates = conn.execute("select status from announcement_queue where text like 'duplicate alice%' order by id").fetchall()
                ready = conn.execute(
                    "select metadata_json from announcement_queue where status='ready' and program_id='night_lab' order by id"
                ).fetchall()
            track_ids = [json.loads(row["metadata_json"])["track_id"] for row in ready]
            self.assertEqual(["ready", "stale"], [row["status"] for row in duplicates])
            self.assertEqual(3, len(track_ids))
            self.assertEqual(len(track_ids), len(set(track_ids)))

    def test_queue_next_track_uses_ready_announcement_before_generating_live_speech(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 1
            wav = root / "music" / "Alice - Blue Room.wav"
            make_wav(wav)
            scan_music(settings)
            agent = RadioAgent(settings)
            force_night_lab(settings)
            ready_clip = settings.tts_path / "ready.wav"
            make_wav(ready_clip)
            with connect(settings) as conn:
                conn.execute(
                    "insert into announcement_queue (text, file_path, status, program_id, source, created_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?)",
                    ("prebuilt intro", str(ready_clip), "ready", "night_lab", "test", "2026-07-05T00:00:00+00:00", "{}"),
                )
                conn.commit()

            result = agent.queue_next_track()
            self.assertTrue(result["started"])
            with connect(settings) as conn:
                used = conn.execute("select status from announcement_queue where text='prebuilt intro'").fetchone()
            self.assertEqual("used", used["status"])

    def test_queue_next_track_does_not_prepare_announcements_on_live_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 5
            settings.max_ready_announcements = 5
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)

            class LivePathAgent(RadioAgent):
                def _prepare_announcement(self, program, index, required, planned_track_ids):
                    raise AssertionError("live playback path must not wait on announcement generation")

            agent = LivePathAgent(settings)
            force_night_lab(settings)

            result = agent.queue_next_track()

            self.assertFalse(result["started"])
            self.assertEqual("announcement_prebuffer_not_ready", result["reason"])

    def test_queue_next_track_uses_track_bound_ready_announcement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 1
            make_wav(root / "music" / "Alice - Blue Room.wav")
            make_wav(root / "music" / "Ben - Amber Night.wav")
            scan_music(settings)
            agent = RadioAgent(settings)
            force_night_lab(settings)
            ready_clip = settings.tts_path / "ben_ready.wav"
            make_wav(ready_clip)
            with connect(settings) as conn:
                ben = conn.execute("select id, title, artist from tracks where artist='Ben'").fetchone()
                metadata = {
                    "prebuffer": True,
                    "track_id": ben["id"],
                    "track_title": ben["title"],
                    "track_artist": ben["artist"],
                }
                conn.execute(
                    "insert into announcement_queue (text, file_path, status, program_id, source, created_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "Amber Night by Ben is ready next on RadioTEDU.",
                        str(ready_clip),
                        "ready",
                        "night_lab",
                        "test",
                        "2026-07-05T00:00:00+00:00",
                        json.dumps(metadata, ensure_ascii=True),
                    ),
                )
                conn.commit()

            result = agent.queue_next_track()

            self.assertTrue(result["started"])
            self.assertEqual(ben["id"], result["track_id"])
            with connect(settings) as conn:
                history = conn.execute("select track_id from play_history order by id desc limit 1").fetchone()
                used = conn.execute("select status from announcement_queue where text like 'Amber Night%'").fetchone()
            self.assertEqual(ben["id"], history["track_id"])
            self.assertEqual("used", used["status"])

    def test_liquidsoap_config_and_queue_are_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.liquidsoap_queue_path = str(Path(tmp) / "liquidsoap" / "queue.m3u")
            settings.liquidsoap_script_path = str(Path(tmp) / "liquidsoap" / "radiotedu.liq")
            result = render_liquidsoap_config(settings)
            self.assertTrue(Path(result["queue_path"]).exists())
            script = Path(result["script_path"]).read_text(encoding="utf-8")
            self.assertIn("playlist", script)
            self.assertIn("RadioTEDU", script)

    def test_liquidsoap_config_renders_measured_playout_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.liquidsoap_queue_path = str(Path(tmp) / "liquidsoap" / "queue.m3u")
            settings.liquidsoap_script_path = str(Path(tmp) / "liquidsoap" / "radiotedu.liq")

            result = render_liquidsoap_config(settings)

            self.assertEqual(
                {
                    "silence_threshold_dbfs": -60.0,
                    "degraded_primary_seconds": 1.0,
                    "fallback_seconds": 1.5,
                    "talk_over_minimum_intro_confidence": 0.65,
                    "talk_over_minimum_instrumental_intro_seconds": 3.0,
                    "speech_end_before_intro_seconds": 0.5,
                    "speech_end_before_intro_range_seconds": (0.3, 0.7),
                    "time_stretch_ratio": 1.0,
                    "speaks_over_vocals": False,
                },
                result["playout"],
            )
            script = Path(result["script_path"]).read_text(encoding="utf-8")
            self.assertIn("assume_autocue=true", script)
            self.assertIn("conservative=true", script)
            self.assertIn("blank.detect(", script)
            self.assertIn("max_blank=1.0", script)
            self.assertIn("blank.skip(", script)
            self.assertIn("max_blank=1.5", script)
            self.assertIn("threshold=-60.0", script)

    def test_program_patch_updates_schedule_and_records_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            client = TestClient(create_app(settings))
            response = client.patch(
                "/api/programs/night_lab",
                json={"start_time": "18:00", "end_time": "23:00", "days_of_week": "mon,tue", "vibe": "focused late jazz"},
            )
            self.assertEqual(200, response.status_code)
            payload = response.json()
            self.assertEqual("18:00", payload["start_time"])
            self.assertEqual("focused late jazz", payload["vibe"])
            with connect(settings) as conn:
                revision = conn.execute("select reason from schedule_revisions where program_id='night_lab'").fetchone()
            self.assertIn("dashboard program edit", revision["reason"])

    def test_observability_status_reports_prebuffer_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.min_ready_announcements = 5
            client = TestClient(create_app(settings))
            payload = client.get("/api/status").json()
            self.assertIn("observability", payload)
            self.assertEqual(5, payload["observability"]["announcement_prebuffer"]["required"])
            self.assertIn("uptime_seconds", payload["observability"])
            self.assertIn("recent_errors", payload["observability"])

    def test_env_binds_local_qwen_service_without_fallback(self) -> None:
        env_text = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
        self.assertIn("QWEN_TTS_SERVICE_URL=http://127.0.0.1:8090", env_text)
        self.assertNotIn("FALLBACK_TTS_PROVIDER", env_text)

    def test_qwen_provider_rejects_legacy_shell_command_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = coerce_station_context(make_settings(root))
            with self.assertRaisesRegex(ValueError, "loopback HTTP"):
                QwenTTSProvider(context, "python scripts/qwen_tts_command.py")

    def test_qwen_failure_marks_prebuffer_failed_without_crashing_playout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.min_ready_announcements = 1
            settings.max_ready_announcements = 1
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            force_night_lab(settings)
            agent = RadioAgent(settings)

            with patch.object(
                RadioAgent,
                "_synthesize_qwen",
                side_effect=QwenUnavailableError("voice pack unavailable"),
            ):
                readiness = agent.ensure_announcement_prebuffer("night_lab")

            self.assertEqual(0, readiness["ready"])
            self.assertEqual(1, readiness["failed"])
            self.assertFalse(readiness["ready_to_broadcast"])


if __name__ == "__main__":
    unittest.main()
