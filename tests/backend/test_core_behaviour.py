import json
import os
import tempfile
import unittest
import wave
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.database import connect, init_db, now_iso
from backend.liquidsoap import render_liquidsoap_config
from backend.llm import build_user_prompt, choose_track_with_llm, ollama_runtime_status
from backend.music_library import iter_audio_files, scan_music
from backend.playback import QueueItem
from backend.public_dashboard import PublicSnapshotPusher, public_snapshot_from_state
from backend.tts.dummy_tts import DummyTTSProvider
from backend.tts.factory import build_tts_provider
from backend.weather.open_meteo import OpenMeteoWeatherProvider


def make_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 800)


def use_test_qwen_synthesis(agent) -> None:
    """Keep non-TTS core tests isolated from model assets and local HTTP."""

    def synthesize(text, output_path, **kwargs) -> str:
        del text, kwargs
        make_wav(Path(output_path))
        return str(output_path)

    agent._synthesize_qwen = synthesize


class RadioTEDUCoreTests(unittest.TestCase):
    def make_settings(self, root: Path) -> Settings:
        return Settings(
            database_path=str(root / "radiotedu.db"),
            music_dir=str(root / "music"),
            static_dir=str(root / "static"),
            rss_feeds_path=str(root / "rss_feeds.json"),
            playback_backend="simulate",
        )

    def test_database_seeds_one_channel_and_no_fake_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            init_db(settings)
            with connect(settings) as conn:
                channels = conn.execute("select id, name from channels").fetchall()
                programs = conn.execute("select name from programs order by name").fetchall()
                self.assertEqual([("radiotedu", "RadioTEDU")], [tuple(row) for row in channels])
                self.assertEqual(
                    [
                        ("Campus Flow",),
                        ("Jazz Lab",),
                        ("TEDU Dawn",),
                        ("Weekend Signal",),
                    ],
                    [tuple(row) for row in programs],
                )
                voices = conn.execute("select name, host_name, host_gender, voice from programs order by name").fetchall()
                self.assertIn(("Jazz Lab", "Selin", "female", "tr_female_cool"), [tuple(row) for row in voices])
                for table in ("tracks", "play_history", "listener_events"):
                    count = conn.execute(f"select count(*) from {table}").fetchone()[0]
                    self.assertEqual(0, count, table)
                financial_table = conn.execute("select name from sqlite_master where type='table' and name='donations'").fetchone()
                self.assertIsNone(financial_table)
                self.assertIsNotNone(conn.execute("select name from sqlite_master where type='table' and name='public_snapshots'").fetchone())
                self.assertIsNotNone(conn.execute("select name from sqlite_master where type='table' and name='public_listener_sessions'").fetchone())

    def test_empty_library_status_is_idle_setup_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            app = create_app(settings)
            response = TestClient(app).get("/api/status")
            self.assertEqual(200, response.status_code)
            payload = response.json()
            self.assertEqual("idle", payload["channel"]["status"])
            self.assertEqual("Idle — waiting for music library.", payload["now_playing"]["title"])
            self.assertEqual(
                "No music library found. Add audio files to data/music and click Rescan.",
                payload["setup"]["message"],
            )
            self.assertEqual([], payload["top_songs"])
            self.assertEqual([], payload["top_genres"])

    def test_public_snapshot_requires_token_and_rejects_private_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            settings.public_sync_token = "secret-token"
            settings.public_stream_url = "https://radiotedu.com/live.mp3"
            client = TestClient(create_app(settings))
            clean_payload = {
                "generated_at": "2026-07-06T00:00:00+00:00",
                "expires_at": "2026-07-06T00:00:30+00:00",
                "channel": {"id": "radiotedu", "name": "RadioTEDU", "status": "live", "cover_path": "/static/generated/covers/radiotedu_station.png"},
                "now_playing": {
                    "type": "track",
                    "title": "Blue Room",
                    "artist": "Alice",
                    "started_at": "2026-07-06T00:00:00+00:00",
                },
                "current_program": None,
                "current_minutes_left": None,
                "next_program": None,
                "next_programs": [],
                "programs": [],
                "top_songs": [],
                "top_genres": [],
                "stream": {"url": "https://radiotedu.com/live.mp3"},
                "content_breakdown": [
                    {"label": "Music", "percent": 84},
                    {"label": "Talking", "percent": 16},
                ],
                "activity": [
                    {"kind": "listener", "actor": "@student", "content": "more piano please", "created_at": "2026-07-06T00:10:00+00:00"},
                ],
                "metrics": {"current_listeners": None, "popularity": None, "average_session": None},
            }
            private_payload = {
                **clean_payload,
                "now_playing": {**clean_payload["now_playing"], "file_path": "F:/Songs/Jazz/Alice - Blue Room.flac"},
                "logs": [{"message": "private"}],
                "incidents": [{"summary": "private"}],
            }

            self.assertEqual(401, client.post("/api/public/snapshot", json=clean_payload).status_code)
            self.assertEqual(
                422,
                client.post("/api/public/snapshot", json=private_payload, headers={"X-RadioTEDU-Sync-Token": "secret-token"}).status_code,
            )
            response = client.post("/api/public/snapshot", json=clean_payload, headers={"X-RadioTEDU-Sync-Token": "secret-token"})
            self.assertEqual(200, response.status_code)
            public = client.get("/api/public/status").json()
            self.assertTrue(public["online"])
            self.assertEqual(1, public["schema_version"])
            self.assertEqual("Blue Room", public["now_playing"]["title"])
            self.assertNotIn("file_path", public["now_playing"])
            self.assertEqual("/static/generated/covers/radiotedu_station.png", public["channel"]["cover_path"])
            self.assertEqual([{"label": "Music", "percent": 84}, {"label": "Talking", "percent": 16}], public["content_breakdown"])
            self.assertEqual("more piano please", public["activity"][0]["content"])
            self.assertEqual(1, len(public["activity"]))
            self.assertNotIn("logs", public)
            self.assertNotIn("incidents", public)
            self.assertNotRegex(json.dumps(public).lower(), r"f:/songs|private|donation|payment|money|support")

    def test_public_status_expires_to_waiting_state_and_sessions_are_real(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            settings.snapshot_ttl_seconds = 1
            client = TestClient(create_app(settings))
            initial = client.get("/api/public/status").json()
            self.assertFalse(initial["online"])
            self.assertEqual(0, initial["metrics"]["current_listeners"])
            self.assertIsNone(initial["metrics"]["average_session"])

            response = client.post("/api/public/session/start", json={"session_id": "listener_123456"})
            self.assertEqual(200, response.status_code)
            active = client.get("/api/public/status").json()
            self.assertEqual(1, active["metrics"]["current_listeners"])
            ended = client.post("/api/public/session/end", json={"session_id": "listener_123456"}).json()
            self.assertEqual(0, ended["metrics"]["current_listeners"])

    def test_ai_route_serves_public_dashboard_shell_when_build_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            client = TestClient(create_app(settings))

            response = client.get("/ai")

            self.assertIn(response.status_code, {200, 404})
            if response.status_code == 200:
                self.assertIn("text/html", response.headers["content-type"])

    def test_station_backend_never_owns_public_sync_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            settings.public_sync_url = "https://radiotedu.example/api/public/snapshot"
            settings.public_sync_token = "secret-token"
            app = create_app(settings)

            self.assertIsNone(app.state.public_snapshot_pusher)
            with TestClient(app):
                self.assertIsNone(app.state.public_snapshot_pusher)

    def test_backend_does_not_start_public_snapshot_pusher_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            app = create_app(settings)

            self.assertIsNone(app.state.public_snapshot_pusher)
            with TestClient(app):
                self.assertIsNone(app.state.public_snapshot_pusher)

    def test_public_snapshot_push_failure_does_not_stop_local_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            settings.public_sync_url = "http://127.0.0.1:9/api/public/snapshot"
            settings.public_sync_token = "secret-token"
            app = create_app(settings)
            pusher = PublicSnapshotPusher(settings, app.state.agent)
            pusher.last_push_at = 0

            result = pusher.maybe_push()

            self.assertFalse(result["pushed"])
            self.assertEqual("push_failed", result["reason"])
            self.assertEqual(1, pusher.status()["consecutive_failures"])
            response = TestClient(app).get("/api/status")
            self.assertEqual(200, response.status_code)
            self.assertEqual("idle", response.json()["channel"]["status"])

    def test_public_snapshot_from_state_includes_dossier_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.public_stream_url = "https://radiotedu.com/ai"
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            app = create_app(settings)
            with connect(settings) as conn:
                track_id = conn.execute("select id from tracks where title='Blue Room'").fetchone()[0]
                conn.execute(
                    "insert into play_history (track_id, program_id, played_at, duration_seconds, source) values (?, ?, ?, ?, ?)",
                    (track_id, "night_lab", "2026-07-06T00:00:00+00:00", 120, "track"),
                )
                conn.execute(
                    "insert into generated_clips (clip_type, text, file_path, voice, program_id, created_at) values (?, ?, ?, ?, ?, ?)",
                    ("dj", "Welcome to Jazz Lab.", str(root / "tts" / "dj.wav"), "tr_female_cool", "night_lab", "2026-07-06T00:01:00+00:00"),
                )
                conn.execute(
                    "insert into autonomy_memory (kind, content, source, weight, created_at) values (?, ?, ?, ?, ?)",
                    ("listener_feedback", "more mellow piano after midnight", "web", 1.0, "2026-07-06T00:02:00+00:00"),
                )
                conn.execute(
                    "insert into agent_logs (level, message, metadata_json, created_at) values (?, ?, ?, ?)",
                    ("info", "Queued Blue Room by Alice.", "{}", "2026-07-06T00:03:00+00:00"),
                )
                conn.execute(
                    "insert into agent_logs (level, message, metadata_json, created_at) values (?, ?, ?, ?)",
                    ("error", "F:/Songs private payment path leaked.", "{}", "2026-07-06T00:04:00+00:00"),
                )
                conn.commit()

            payload = public_snapshot_from_state(settings, app.state.agent)
            self.assertEqual("https://radiotedu.com/ai", payload["stream"]["url"])
            self.assertIn("current_minutes_left", payload)
            self.assertIn("next_program", payload)
            self.assertIn({"label": "Music", "percent": 50}, payload["content_breakdown"])
            self.assertIn({"label": "Talking", "percent": 50}, payload["content_breakdown"])
            self.assertEqual("listener", payload["activity"][0]["kind"])
            self.assertEqual("more mellow piano after midnight", payload["activity"][0]["content"])
            self.assertFalse(any("Queued Blue Room" in item["content"] for item in payload["activity"]))
            self.assertNotRegex(json.dumps(payload).lower(), r"queued|f:/songs|private|payment|file_path")

    def test_news_prebuffer_uses_curated_rss_without_inventing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.news_enabled = True
            settings.min_ready_announcements = 1
            settings.rss_feeds_path = str(root / "rss_feeds.json")
            feed = root / "feed.xml"
            feed.write_text(
                f"""
                <rss><channel><item><title>Campus observatory opens tonight</title>
                <link>https://news.example/observatory</link>
                <pubDate>{format_datetime(datetime.now(timezone.utc))}</pubDate>
                <description>Students will host a public skywatch after sunset.</description></item></channel></rss>
                """,
                encoding="utf-8",
            )
            (root / "rss_feeds.json").write_text(json.dumps({"feeds": [feed.as_uri()]}), encoding="utf-8")
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            from backend.radio_agent import RadioAgent

            agent = RadioAgent(settings)
            use_test_qwen_synthesis(agent)
            readiness = agent.ensure_announcement_prebuffer("night_lab")
            self.assertTrue(readiness["ready_to_broadcast"])
            with connect(settings) as conn:
                row = conn.execute("select text, metadata_json from announcement_queue where status='ready'").fetchone()
            self.assertIn("Campus observatory opens tonight", row["text"])
            metadata = json.loads(row["metadata_json"])
            self.assertEqual("news", metadata["kind"])
            self.assertEqual("https://news.example/observatory", metadata["news_url"])
            self.assertIn("published_at", metadata)
            from backend.app import observability

            news_status = observability(settings, agent)["news"]
            self.assertTrue(news_status["enabled"])
            self.assertIsNotNone(news_status["last_checked_at"])
            self.assertIsNotNone(news_status["last_source_at"])
            self.assertEqual("Campus observatory opens tonight", news_status["last_source_title"])

    def test_news_prebuffer_skips_stale_rss_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.news_enabled = True
            settings.min_ready_announcements = 1
            settings.max_ready_announcements = 1
            settings.rss_feeds_path = str(root / "rss_feeds.json")
            stale_date = format_datetime(datetime.now(timezone.utc) - timedelta(days=3))
            feed = root / "feed.xml"
            feed.write_text(
                f"""
                <rss><channel><item><title>Old campus notice</title>
                <link>https://news.example/old</link>
                <pubDate>{stale_date}</pubDate>
                <description>This should not be read on air.</description></item></channel></rss>
                """,
                encoding="utf-8",
            )
            (root / "rss_feeds.json").write_text(json.dumps({"feeds": [feed.as_uri()]}), encoding="utf-8")
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            from backend.radio_agent import RadioAgent

            agent = RadioAgent(settings)
            use_test_qwen_synthesis(agent)
            readiness = agent.ensure_announcement_prebuffer("night_lab")

            self.assertTrue(readiness["ready_to_broadcast"])
            with connect(settings) as conn:
                row = conn.execute("select text, metadata_json from announcement_queue where status='ready'").fetchone()
            metadata = json.loads(row["metadata_json"])
            self.assertNotEqual("news", metadata.get("kind"))
            self.assertNotIn("Old campus notice", row["text"])

    def test_scanner_indexes_real_audio_without_fake_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            make_wav(root / "music" / "Alice - Blue Room.wav")
            init_db(settings)
            result = scan_music(settings)
            self.assertEqual(1, result.tracks_found)
            self.assertEqual(1, result.tracks_indexed)
            with connect(settings) as conn:
                row = conn.execute("select title, artist, file_path from tracks").fetchone()
                self.assertEqual("Blue Room", row["title"])
                self.assertEqual("Alice", row["artist"])
                self.assertTrue(row["file_path"].endswith("Alice - Blue Room.wav"))
                history_count = conn.execute("select count(*) from play_history").fetchone()[0]
                self.assertEqual(0, history_count)

    def test_status_with_music_is_idle_ready_not_setup_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            payload = TestClient(create_app(settings)).get("/api/status").json()
            self.assertTrue(payload["setup"]["has_music"])
            self.assertEqual("", payload["setup"]["message"])
            self.assertEqual("Idle — ready to start RadioTEDU.", payload["now_playing"]["title"])

    def test_run_air_refuses_to_start_without_real_music(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            settings.min_ready_announcements = 5
            client = TestClient(create_app(settings))

            response = client.post("/api/air/start")
            payload = response.json()
            status = client.get("/api/status").json()

            self.assertEqual(200, response.status_code)
            self.assertFalse(payload["started"])
            self.assertEqual("no_music", payload["reason"])
            self.assertEqual(0, payload["music_library"]["playable_track_count"])

    def test_air_readiness_blocks_real_air_when_liquidsoap_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.liquidsoap_enabled = True
            settings.liquidsoap_command = "definitely-missing-liquidsoap"
            settings.playback_backend = "liquidsoap"
            settings.min_ready_announcements = 0
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            app = create_app(settings)

            response = TestClient(app).post("/api/air/start")
            payload = response.json()

            self.assertFalse(payload["started"])
            self.assertEqual("liquidsoap_not_ready", payload["reason"])
            checklist = payload["readiness"]["checklist"]
            self.assertFalse(checklist["liquidsoap_command"]["ok"])
            self.assertEqual("blocking", checklist["liquidsoap_command"]["severity"])
            self.assertIn("definitely-missing-liquidsoap", checklist["liquidsoap_command"]["detail"])
            self.assertEqual("warning", checklist["tts"]["severity"])

    def test_liquidsoap_status_reports_operator_health_and_queue_length(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.liquidsoap_enabled = True
            settings.liquidsoap_queue_path = str(root / "liquidsoap" / "queue.m3u")
            settings.liquidsoap_script_path = str(root / "liquidsoap" / "radiotedu.liq")
            render_liquidsoap_config(settings)
            Path(settings.liquidsoap_queue_path).write_text("a.wav\nb.wav\n", encoding="utf-8")

            from backend.liquidsoap import liquidsoap_status

            status = liquidsoap_status(settings)

            self.assertIn(status["health"], {"missing", "ready", "running"})
            self.assertEqual(2, status["queue_length"])
            self.assertTrue(status["queue_exists"])

    def test_liquidsoap_status_reports_icecast_mount_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.liquidsoap_enabled = True
            settings.liquidsoap_host = "127.0.0.1"
            settings.liquidsoap_port = 8001
            settings.liquidsoap_mount = "/ai"
            settings.liquidsoap_queue_path = str(root / "liquidsoap" / "queue.m3u")
            settings.liquidsoap_script_path = str(root / "liquidsoap" / "radiotedu.liq")
            render_liquidsoap_config(settings)

            from backend.liquidsoap import liquidsoap_status

            status = liquidsoap_status(
                settings,
                icecast_checker=lambda url, timeout=0.5: {
                    "reachable": True,
                    "mount_active": True,
                    "status": 200,
                    "url": url,
                },
            )

            self.assertTrue(status["icecast_reachable"])
            self.assertTrue(status["mount_active"])
            self.assertEqual("http://127.0.0.1:8001/ai", status["icecast_url"])

    def test_liquidsoap_backend_writes_tts_then_track_to_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.playback_backend = "liquidsoap"
            settings.liquidsoap_queue_path = str(root / "liquidsoap" / "queue.m3u")
            settings.liquidsoap_script_path = str(root / "liquidsoap" / "radiotedu.liq")
            tts = root / "tts" / "intro.wav"
            track = root / "music" / "Alice - Blue Room.wav"
            make_wav(tts)
            make_wav(track)

            from backend.playback import PlaybackController

            playback = PlaybackController(settings)
            playback.add(QueueItem("tts", "RadioTEDU DJ", str(tts), duration_seconds=1.0))
            playback.add(QueueItem("track", "Blue Room", str(track), duration_seconds=1.0, artist="Alice", track_id=1))

            playback.play_next()
            playback.play_next()

            queued = Path(settings.liquidsoap_queue_path).read_text(encoding="utf-8").splitlines()
            self.assertEqual([str(tts.resolve()), str(track.resolve())], queued)

    def test_status_exposes_operator_library_config_and_public_sync_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.music_dir = str(root / "music")
            settings.ollama_model = "qwen3.5:4b"
            settings.qwen_tts_command = "python scripts/qwen_tts_command.py"
            settings.liquidsoap_enabled = True
            settings.liquidsoap_script_path = str(root / "liquidsoap" / "radiotedu.liq")
            settings.liquidsoap_host = "127.0.0.1"
            settings.liquidsoap_port = 8000
            settings.liquidsoap_mount = "/ai"
            settings.public_sync_url = "https://radiotedu.com/api/public/snapshot"
            settings.public_sync_token = "secret-token"
            settings.public_stream_url = "https://radiotedu.com/ai/stream"
            settings.min_ready_announcements = 5
            settings.max_ready_announcements = 8
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)

            app = create_app(settings)
            app.state.agent.playback.now_playing = QueueItem("track", "Blue Room", str(root / "music" / "Alice - Blue Room.wav"), artist="Alice")
            payload = TestClient(app).get("/api/status").json()

            self.assertEqual(1, payload["music_library"]["total_indexed_tracks"])
            self.assertEqual(1, payload["music_library"]["playable_track_count"])
            self.assertIsNotNone(payload["music_library"]["last_scan_time"])
            self.assertEqual(settings.music_dir, payload["configuration"]["MUSIC_DIR"])
            self.assertEqual("qwen3.5:4b", payload["configuration"]["OLLAMA_MODEL"])
            self.assertEqual("python scripts/qwen_tts_command.py", payload["configuration"]["TTS_COMMAND"])
            self.assertEqual(settings.liquidsoap_script_path, payload["configuration"]["LIQUIDSOAP_SCRIPT"])
            self.assertEqual("http://127.0.0.1:8000/ai", payload["configuration"]["ICECAST_URL"])
            self.assertEqual("https://radiotedu.com/api/public/snapshot", payload["configuration"]["PUBLIC_SYNC_URL"])
            self.assertEqual("https://radiotedu.com/ai/stream", payload["configuration"]["PUBLIC_STREAM_URL"])
            self.assertEqual({"min": 5, "max": 8}, payload["configuration"]["BUFFER_SIZES"])
            self.assertEqual("configured", payload["website_sync"]["health"])
            self.assertTrue(payload["website_sync"]["configured"])
            self.assertNotIn("secret-token", json.dumps(payload))
            self.assertNotIn("hackme", json.dumps(payload))

    def test_liquidsoap_config_streams_to_ai_mount_and_status_is_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.liquidsoap_enabled = True
            settings.playback_backend = "liquidsoap"
            settings.liquidsoap_queue_path = str(root / "queue.m3u")
            settings.liquidsoap_script_path = str(root / "radiotedu.liq")
            settings.liquidsoap_host = "icecast.example"
            settings.liquidsoap_port = 8010
            settings.liquidsoap_mount = "/ai"
            settings.liquidsoap_icecast_password = "secret"

            rendered = render_liquidsoap_config(settings)
            script = (root / "radiotedu.liq").read_text(encoding="utf-8")

            self.assertEqual("/ai", rendered["mount"])
            self.assertEqual("http://icecast.example:8010/ai", rendered["icecast_url"])
            self.assertIn('mount="/ai"', script)
            self.assertIn('environment.get("ICECAST_PASSWORD")', script)
            self.assertIn("password=source_password", script)
            self.assertNotIn("secret", script)
            self.assertIn('%fdkaac(bitrate=192, aot="mpeg4_aac_lc", transmux="adts", afterburner=true)', script)
            self.assertIn('user="source"', script)
            self.assertIn("public=true", script)
            self.assertNotIn("radiotedu.mp3", script)

            payload = TestClient(create_app(settings)).get("/api/status").json()
            self.assertEqual("/ai", payload["liquidsoap"]["mount"])
            self.assertEqual("http://icecast.example:8010/ai", payload["liquidsoap"]["icecast_url"])

    def test_run_air_reports_missing_stream_engine_instead_of_pretending_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.liquidsoap_enabled = True
            settings.playback_backend = "liquidsoap"
            settings.liquidsoap_command = "definitely-missing-liquidsoap"
            settings.liquidsoap_mount = "/ai"
            settings.min_ready_announcements = 0
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)

            response = TestClient(create_app(settings)).post("/api/air/start")
            payload = response.json()

            self.assertEqual(200, response.status_code)
            self.assertFalse(payload["started"])
            self.assertEqual("liquidsoap_missing", payload["stream"]["reason"])
            self.assertFalse(payload["stream"]["command_found"])

    def test_status_does_not_show_live_when_playback_is_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            with connect(settings) as conn:
                conn.execute("update channels set status='live'")
                conn.commit()
            payload = TestClient(create_app(settings)).get("/api/status").json()
            self.assertEqual("idle", payload["channel"]["status"])
            self.assertEqual("idle", payload["now_playing"]["type"])

    def test_status_does_not_show_live_when_liquidsoap_stream_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.liquidsoap_enabled = True
            settings.playback_backend = "liquidsoap"
            settings.liquidsoap_command = "definitely-missing-liquidsoap"
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            app = create_app(settings)
            with connect(settings) as conn:
                conn.execute("update channels set status='live'")
                conn.commit()

            app.state.agent.playback.now_playing = QueueItem("track", "Blue Room", str(root / "music" / "Alice - Blue Room.wav"), artist="Alice")
            payload = TestClient(app).get("/api/status").json()

            self.assertEqual("idle", payload["channel"]["status"])
            self.assertFalse(payload["liquidsoap"]["running"])
            self.assertFalse(payload["liquidsoap"]["command_found"])

    def test_autonomy_api_controls_are_local_and_nonfinancial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            client = TestClient(create_app(settings))
            strategy_response = client.post("/api/autonomy/strategy")
            tick_response = client.post("/api/autonomy/tick")
            status = client.get("/api/status").json()
            strategy_payload = strategy_response.json()
            self.assertEqual(200, strategy_response.status_code)
            self.assertEqual(1, strategy_payload["revision"])
            self.assertEqual(0, strategy_payload["policy"]["library_tracks"])
            self.assertTrue(strategy_payload["policy"]["single_channel"])
            self.assertGreaterEqual(len(strategy_payload["policy"]["goals"]), 2)
            self.assertGreaterEqual(len(strategy_payload["policy"]["next_actions"]), 2)
            self.assertEqual(200, tick_response.status_code)
            tick_payload = tick_response.json()
            self.assertEqual(False, tick_payload["played"])
            self.assertEqual("no_music", tick_payload["reason"])
            self.assertIn("recovery", tick_payload)
            self.assertEqual("radiotedu", status["channel"]["id"])
            self.assertIn("self_review", status["orchestrator"])
            self.assertEqual(strategy_payload["policy"], status["orchestrator"]["strategy_policy"])
            self.assertNotRegex(str(status).lower(), r"money|payment|donation|support|revenue|profit|buy|purchase")

    def test_jazz_library_path_can_be_scanned_with_a_limit(self) -> None:
        jazz = Path("F:/Songs/Jazz")
        if not jazz.exists():
            self.skipTest("F:/Songs/Jazz is not available on this machine")
        files = list(iter_audio_files(jazz, limit=3))
        self.assertGreaterEqual(len(files), 1)
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(file.suffix.lower() in {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg"} for file in files))

    def test_invalid_llm_response_uses_deterministic_fallback(self) -> None:
        choice = choose_track_with_llm(
            candidates=[
                {"id": 5, "title": "First", "artist": "Artist A"},
                {"id": 8, "title": "Second", "artist": "Artist B"},
            ],
            program={"name": "TEDU Dawn", "description": "Bright", "vibe": "upbeat"},
            recent_tracks=[],
            web_context=[],
            llm_response_text="not json",
        )
        self.assertEqual(5, choice.song_id)
        self.assertLessEqual(len(choice.dj_line.split()), 24)
        self.assertIn("fallback", choice.reason.lower())

    def test_llm_fallback_uses_real_track_metadata_without_inventing(self) -> None:
        choice = choose_track_with_llm(
            candidates=[
                {
                    "id": 5,
                    "title": "Blue Room",
                    "artist": "Alice",
                    "album": "Midnight Sessions",
                    "genre": "Jazz",
                }
            ],
            program={"name": "TEDU Dawn", "description": "Bright", "vibe": "upbeat"},
            recent_tracks=[],
            web_context=[],
            llm_response_text="not json",
        )
        self.assertEqual(5, choice.song_id)
        self.assertIn("Midnight Sessions", choice.dj_line)
        self.assertIn("Jazz", choice.dj_line)
        self.assertLessEqual(len(choice.dj_line.split()), 24)

    def test_llm_fallback_uses_supplied_search_snippet_when_available(self) -> None:
        choice = choose_track_with_llm(
            candidates=[
                {"id": 5, "title": "Blue Room", "artist": "Alice", "album": "Midnight Sessions", "genre": "Jazz"}
            ],
            program={"name": "TEDU Dawn", "description": "Bright", "vibe": "upbeat"},
            recent_tracks=[],
            web_context=[
                {
                    "title": "Alice Blue Room",
                    "snippet": "hard bop campus session with a late-night trio arrangement",
                    "url": "https://example.test/alice-blue-room",
                }
            ],
            llm_response_text="not json",
        )
        self.assertEqual(5, choice.song_id)
        self.assertIn("hard bop", choice.dj_line)
        self.assertLessEqual(len(choice.dj_line.split()), 24)

    def test_known_unready_llm_runtime_uses_fast_fallback_without_calling_ollama(self) -> None:
        settings = Settings(ollama_url="http://127.0.0.1:11434", ollama_model="qwen3.5:4b")
        from backend import llm as llm_module

        original = llm_module.call_ollama
        llm_module.call_ollama = lambda _settings, _prompt: (_ for _ in ()).throw(AssertionError("Ollama should not be called"))
        try:
            choice = choose_track_with_llm(
                candidates=[
                    {"id": 5, "title": "Blue Room", "artist": "Alice", "album": "Midnight Sessions", "genre": "Jazz"}
                ],
                program={"name": "Jazz Lab", "description": "Late", "vibe": "mellow"},
                recent_tracks=[],
                web_context=[],
                settings=settings,
                runtime_status={
                    "status": "unreachable",
                    "reachable": False,
                    "model_available": False,
                    "configured_model": "qwen3.5:4b",
                },
            )
        finally:
            llm_module.call_ollama = original
        self.assertTrue(choice.used_fallback)
        self.assertEqual(5, choice.song_id)
        self.assertIn("LLM runtime unavailable", choice.reason)

    def test_ready_llm_retries_once_before_falling_back_on_bad_json(self) -> None:
        settings = Settings(ollama_url="http://127.0.0.1:11434", ollama_model="qwen3.5:4b")
        from backend import llm as llm_module

        responses = iter(
            [
                "{not json",
                '{"song_id": 5, "dj_line": "A real AI line for RadioTEDU.", "reason": "Fits the program."}',
            ]
        )
        original = llm_module.call_ollama
        llm_module.call_ollama = lambda _settings, _prompt: next(responses)
        try:
            choice = choose_track_with_llm(
                candidates=[{"id": 5, "title": "Blue Room", "artist": "Alice"}],
                program={"name": "Jazz Lab", "description": "Late", "vibe": "mellow"},
                recent_tracks=[],
                web_context=[],
                settings=settings,
                runtime_status={
                    "status": "ready",
                    "reachable": True,
                    "model_available": True,
                    "configured_model": "qwen3.5:4b",
                },
            )
        finally:
            llm_module.call_ollama = original
        self.assertFalse(choice.used_fallback)
        self.assertEqual("A real AI line for RadioTEDU.", choice.dj_line)

    def test_dummy_tts_writes_wav_and_text_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "clip.wav"
            path = DummyTTSProvider().synthesize("A short RadioTEDU line.", str(output))
            self.assertEqual(str(output), path)
            self.assertTrue(output.exists())
            self.assertEqual("A short RadioTEDU line.", output.with_suffix(".txt").read_text(encoding="utf-8"))

    def test_tts_factory_is_qwen_only_and_production_does_not_route_legacy_speech(self) -> None:
        station_context = SimpleNamespace(
            profile=SimpleNamespace(station_id="radiotedu-en", language="en", locale="en-US")
        )
        provider = build_tts_provider(station_context, "http://127.0.0.1:8090")
        self.assertEqual("qwen", provider.provider_name)
        self.assertFalse(hasattr(provider, "fallback"))

        root = Path(__file__).resolve().parents[2]
        production = "\n".join(
            (root / path).read_text(encoding="utf-8")
            for path in (
                "backend/tts/__init__.py",
                "backend/tts/factory.py",
                "backend/tts/qwen_tts.py",
                "backend/tts/qwen_service.py",
            )
        )
        for forbidden in ("SapiTTSProvider", "PiperTTSProvider", "DummyTTSProvider"):
            self.assertNotIn(forbidden, production)

    def test_maintenance_cleanup_removes_old_generated_clips_and_bounds_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            init_db(settings)
            old_clip = root / "static" / "generated" / "tts" / "old.wav"
            new_clip = root / "static" / "generated" / "tts" / "new.wav"
            make_wav(old_clip)
            old_clip.with_suffix(".txt").write_text("old", encoding="utf-8")
            make_wav(new_clip)
            with connect(settings) as conn:
                conn.execute(
                    "insert into generated_clips (clip_type, text, file_path, voice, program_id, created_at) values (?, ?, ?, ?, ?, ?)",
                    ("prebuffer_announcement", "old", str(old_clip), "dummy", "night_lab", "2026-01-01T00:00:00+00:00"),
                )
                conn.execute(
                    "insert into generated_clips (clip_type, text, file_path, voice, program_id, created_at) values (?, ?, ?, ?, ?, ?)",
                    ("prebuffer_announcement", "new", str(new_clip), "dummy", "night_lab", now_iso()),
                )
                for index in range(15):
                    conn.execute(
                        "insert into agent_logs (level, message, metadata_json, created_at) values (?, ?, '{}', ?)",
                        ("info", f"log {index}", f"2026-01-01T00:00:{index:02d}+00:00"),
                    )
                conn.commit()

            from backend.maintenance import run_maintenance

            result = run_maintenance(settings, clip_retention_days=1, max_agent_logs=5)

            self.assertEqual(1, result["clips_deleted"])
            self.assertFalse(old_clip.exists())
            self.assertFalse(old_clip.with_suffix(".txt").exists())
            self.assertTrue(new_clip.exists())
            self.assertEqual(10, result["logs_deleted"])
            with connect(settings) as conn:
                self.assertEqual(1, conn.execute("select count(*) from generated_clips").fetchone()[0])
                self.assertEqual(5, conn.execute("select count(*) from agent_logs").fetchone()[0])

    def test_status_exposes_watchdog_and_maintenance_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            app = create_app(settings)

            payload = TestClient(app).get("/api/status").json()

            self.assertIn("maintenance", payload)
            self.assertIn("watchdog", payload)
            self.assertIn("generated_clip_count", payload["maintenance"])
            self.assertIn("stale_prebuffer", payload["watchdog"])

    def test_watchdog_reports_stream_failures_and_maintenance_vacuums_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            settings.liquidsoap_enabled = True
            settings.liquidsoap_command = "definitely-missing-liquidsoap"
            init_db(settings)

            from backend.maintenance import run_maintenance, watchdog_summary

            watchdog = watchdog_summary(settings)
            result = run_maintenance(settings)

            self.assertEqual(1, watchdog["liquidsoap_process_down"])
            self.assertEqual(1, watchdog["icecast_mount_down"])
            self.assertTrue(result["database_vacuumed"])

    def test_playback_watchdog_reports_stuck_now_playing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            track = root / "track.wav"
            make_wav(track)
            from backend.playback import PlaybackController

            playback = PlaybackController(settings)
            playback.add(QueueItem("track", "Blue Room", str(track), duration_seconds=0.1, artist="Alice", track_id=1))
            playback.play_next()

            state = playback.state()
            watchdog = playback.watchdog_status(grace_seconds=0)

            self.assertIsNotNone(state["started_at"])
            self.assertEqual(1, watchdog["stuck_playback"])
            self.assertEqual("Blue Room", watchdog["title"])

    def test_weather_context_uses_real_payload_and_is_exposed_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            settings.weather_enabled = True
            settings.weather_location = "Ankara"
            settings.weather_latitude = "39.9208"
            settings.weather_longitude = "32.8541"
            provider = OpenMeteoWeatherProvider(
                settings,
                fetch_json=lambda _url: {
                    "current": {
                        "temperature_2m": 18.4,
                        "relative_humidity_2m": 61,
                        "wind_speed_10m": 8.2,
                        "weather_code": 3,
                    }
                },
            )
            context = provider.current_context()
            self.assertTrue(context.available)
            self.assertEqual("Ankara", context.location)
            self.assertIn("18", context.summary)
            self.assertIn("Overcast", context.summary)

            app = create_app(settings)
            app.state.agent.weather_provider = provider
            payload = TestClient(app).get("/api/status").json()
            self.assertEqual("Ankara", payload["weather"]["location"])
            self.assertTrue(payload["weather"]["available"])
            self.assertIn("Overcast", payload["weather"]["summary"])

    def test_weather_context_failure_does_not_invent_forecast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            settings.weather_enabled = True
            settings.weather_location = "Ankara"
            settings.weather_latitude = "39.9208"
            settings.weather_longitude = "32.8541"
            provider = OpenMeteoWeatherProvider(
                settings,
                fetch_json=lambda _url: (_ for _ in ()).throw(RuntimeError("network down")),
            )
            context = provider.current_context()
            self.assertFalse(context.available)
            self.assertEqual("No weather data.", context.summary)
            self.assertIsNone(context.temperature_c)

    def test_weather_prebuffer_uses_real_available_weather_without_inventing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.weather_enabled = True
            settings.min_ready_announcements = 1
            settings.max_ready_announcements = 1
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            from backend.radio_agent import RadioAgent
            from backend.weather.open_meteo import WeatherContext

            class FixedWeatherProvider:
                def current_context(self):
                    return WeatherContext(True, "Ankara", "Ankara: 18 C, Overcast, wind 8 km/h.", temperature_c=18, wind_kmh=8, condition="Overcast")

            agent = RadioAgent(settings)
            use_test_qwen_synthesis(agent)
            agent.weather_provider = FixedWeatherProvider()
            readiness = agent.ensure_announcement_prebuffer("night_lab")

            self.assertTrue(readiness["ready_to_broadcast"])
            with connect(settings) as conn:
                row = conn.execute("select text, metadata_json from announcement_queue where status='ready'").fetchone()
            metadata = json.loads(row["metadata_json"])
            self.assertEqual("weather", metadata["kind"])
            self.assertIn("Ankara: 18 C, Overcast", row["text"])
            self.assertIn("open-meteo", metadata["source"])

    def test_weather_prebuffer_skips_unavailable_weather(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.weather_enabled = True
            settings.min_ready_announcements = 1
            settings.max_ready_announcements = 1
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            from backend.radio_agent import RadioAgent
            from backend.weather.open_meteo import WeatherContext

            class MissingWeatherProvider:
                def current_context(self):
                    return WeatherContext(False, "Ankara", "No weather data.", source="unconfigured")

            agent = RadioAgent(settings)
            use_test_qwen_synthesis(agent)
            agent.weather_provider = MissingWeatherProvider()
            readiness = agent.ensure_announcement_prebuffer("night_lab")

            self.assertTrue(readiness["ready_to_broadcast"])
            with connect(settings) as conn:
                row = conn.execute("select text, metadata_json from announcement_queue where status='ready'").fetchone()
            metadata = json.loads(row["metadata_json"])
            self.assertNotEqual("weather", metadata.get("kind"))
            self.assertNotIn("No weather data", row["text"])

    def test_song_context_prebuffer_uses_sourced_context_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.min_ready_announcements = 1
            settings.max_ready_announcements = 1
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            from backend.radio_agent import RadioAgent

            agent = RadioAgent(settings)
            use_test_qwen_synthesis(agent)
            agent._web_context = lambda _query: [
                {
                    "title": "Blue Room by Alice source note",
                    "snippet": "Alice recorded Blue Room during a late Ankara session.",
                    "url": "https://music.example/blue-room",
                    "source": "rss",
                }
            ]
            readiness = agent.ensure_announcement_prebuffer("night_lab")

            self.assertTrue(readiness["ready_to_broadcast"])
            with connect(settings) as conn:
                row = conn.execute("select text, metadata_json from announcement_queue where status='ready'").fetchone()
            metadata = json.loads(row["metadata_json"])
            self.assertEqual("song_context", metadata["kind"])
            self.assertEqual("https://music.example/blue-room", metadata["context_url"])
            self.assertIn("Alice recorded Blue Room", row["text"])

    def test_song_context_prebuffer_skips_when_no_sourced_context_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.min_ready_announcements = 1
            settings.max_ready_announcements = 1
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            from backend.radio_agent import RadioAgent

            agent = RadioAgent(settings)
            use_test_qwen_synthesis(agent)
            agent._web_context = lambda _query: []
            readiness = agent.ensure_announcement_prebuffer("night_lab")

            self.assertTrue(readiness["ready_to_broadcast"])
            with connect(settings) as conn:
                row = conn.execute("select text, metadata_json from announcement_queue where status='ready'").fetchone()
            metadata = json.loads(row["metadata_json"])
            self.assertNotEqual("song_context", metadata.get("kind"))

    def test_llm_prompt_includes_weather_context_for_announcements(self) -> None:
        prompt = build_user_prompt(
            program={"name": "TEDU Dawn", "description": "Bright", "vibe": "fresh", "host_name": "Ece", "host_gender": "female", "personality": "warm"},
            candidates=[{"id": 1, "title": "Blue Room", "artist": "Alice", "genre": "Jazz"}],
            recent_tracks=[],
            web_context=[],
            weather_context={"available": True, "summary": "Ankara: 18 C, overcast, wind 8 km/h."},
        )
        self.assertIn("Weather context: Ankara: 18 C, overcast, wind 8 km/h.", prompt)

    def test_ollama_runtime_status_reports_unreachable_without_claiming_model_ready(self) -> None:
        settings = Settings(ollama_url="http://127.0.0.1:11434", ollama_model="qwen3.5:4b")
        status = ollama_runtime_status(
            settings,
            fetch_json=lambda _url: (_ for _ in ()).throw(RuntimeError("connection refused")),
        )
        self.assertEqual("ollama", status["provider"])
        self.assertEqual("qwen3.5:4b", status["configured_model"])
        self.assertFalse(status["reachable"])
        self.assertFalse(status["model_available"])
        self.assertEqual("unreachable", status["status"])
        self.assertIn("connection refused", status["error"])

    def test_ollama_runtime_status_detects_configured_model_in_tags(self) -> None:
        settings = Settings(ollama_url="http://127.0.0.1:11434", ollama_model="qwen3.5:4b")
        status = ollama_runtime_status(
            settings,
            fetch_json=lambda _url: {"models": [{"name": "qwen3.5:4b"}, {"name": "llama3.2:3b"}]},
        )
        self.assertTrue(status["reachable"])
        self.assertTrue(status["model_available"])
        self.assertEqual("ready", status["status"])
        self.assertEqual(["qwen3.5:4b", "llama3.2:3b"], status["installed_models"])

    def test_ollama_generation_uses_configured_timeout_for_cpu_models(self) -> None:
        settings = Settings(ollama_url="http://127.0.0.1:11434", ollama_model="qwen3.5:4b", ollama_timeout_seconds=45)
        from backend import llm as llm_module

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"response": "{\\"song_id\\": 1, \\"dj_line\\": \\"A short line.\\", \\"reason\\": \\"fit\\"}"}'

        original = llm_module.urllib.request.urlopen
        llm_module.urllib.request.urlopen = lambda _request, timeout: captured.setdefault("timeout", timeout) and FakeResponse()
        try:
            response = llm_module.call_ollama(settings, "choose")
        finally:
            llm_module.urllib.request.urlopen = original
        self.assertEqual(45, captured["timeout"])
        self.assertIn("song_id", response)

    def test_ollama_generation_uses_chat_api_with_thinking_disabled(self) -> None:
        settings = Settings(ollama_url="http://127.0.0.1:11434", ollama_model="qwen3.5:4b", ollama_timeout_seconds=45)
        from backend import llm as llm_module

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"message": {"content": "{\\"song_id\\": 1, \\"dj_line\\": \\"A short line.\\", \\"reason\\": \\"fit\\"}"}}'

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        original = llm_module.urllib.request.urlopen
        llm_module.urllib.request.urlopen = fake_urlopen
        try:
            response = llm_module.call_ollama(settings, "choose")
        finally:
            llm_module.urllib.request.urlopen = original
        self.assertTrue(captured["url"].endswith("/api/chat"))
        self.assertFalse(captured["payload"]["think"])
        self.assertEqual("json", captured["payload"]["format"])
        self.assertEqual("system", captured["payload"]["messages"][0]["role"])
        self.assertEqual("user", captured["payload"]["messages"][1]["role"])
        self.assertIn("song_id", response)

    def test_status_exposes_real_llm_runtime_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            app = create_app(settings)
            from backend import app as app_module

            original = app_module.ollama_runtime_status
            app_module.ollama_runtime_status = lambda _settings: {
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
                payload = TestClient(app).get("/api/status").json()
            finally:
                app_module.ollama_runtime_status = original
            self.assertEqual("unreachable", payload["health"]["llm_runtime"]["status"])
            self.assertFalse(payload["health"]["llm_runtime"]["model_available"])

    def test_status_exposes_ollama_setup_guidance_for_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            app = create_app(settings)
            from backend import app as app_module

            original = app_module.check_ollama_setup
            app_module.check_ollama_setup = lambda _settings, **_kwargs: {
                "provider": "ollama",
                "configured_model": "qwen3.5:4b",
                "base_url": "http://127.0.0.1:11434",
                "cli_found": False,
                "cli_path": None,
                "server_reachable": False,
                "reachable": False,
                "model_available": False,
                "installed_models": [],
                "status": "cli_missing",
                "summary": "Ollama CLI was not found.",
                "suggested_commands": ["winget install Ollama.Ollama", "ollama pull qwen3.5:4b"],
                "error": "connection refused",
                "runtime": {},
            }
            try:
                payload = TestClient(app).get("/api/status").json()
            finally:
                app_module.check_ollama_setup = original
            self.assertEqual("cli_missing", payload["health"]["llm_setup"]["status"])
            self.assertIn("ollama pull qwen3.5:4b", payload["health"]["llm_setup"]["suggested_commands"])

    def test_status_exposes_open_incidents_and_autonomous_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            app = create_app(settings)
            with connect(settings) as conn:
                conn.execute(
                    "insert into incidents (component, severity, status, summary, details_json, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?)",
                    ("llm", "warning", "open", "Ollama runtime is unreachable.", "{}", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00"),
                )
                conn.execute(
                    "insert into autonomous_tasks (task_type, component, title, status, priority, details_json, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("restart_llm_runtime", "llm", "Restart Ollama", "queued", 80, "{}", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00"),
                )
                conn.commit()

            payload = TestClient(app).get("/api/status").json()

            self.assertEqual("llm", payload["incidents"][0]["component"])
            self.assertEqual("Ollama runtime is unreachable.", payload["incidents"][0]["summary"])
            self.assertEqual("restart_llm_runtime", payload["autonomous_tasks"][0]["task_type"])
            self.assertEqual(80, payload["autonomous_tasks"][0]["priority"])
            self.assertNotRegex(str(payload).lower(), r"money|payment|donation|support|revenue|profit|buy|purchase")

    def test_ollama_setup_api_returns_real_setup_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            app = create_app(settings)
            from backend import app as app_module

            original = app_module.check_ollama_setup
            app_module.check_ollama_setup = lambda _settings, **_kwargs: {
                "provider": "ollama",
                "configured_model": "qwen3.5:4b",
                "base_url": "http://127.0.0.1:11434",
                "cli_found": False,
                "cli_path": None,
                "server_reachable": False,
                "reachable": False,
                "model_available": False,
                "installed_models": [],
                "status": "cli_missing",
                "summary": "Ollama CLI was not found.",
                "suggested_commands": ["winget install Ollama.Ollama", "ollama pull qwen3.5:4b"],
                "error": "connection refused",
                "runtime": {},
            }
            try:
                payload = TestClient(app).get("/api/setup/ollama").json()
            finally:
                app_module.check_ollama_setup = original
            self.assertEqual("cli_missing", payload["status"])
            self.assertFalse(payload["cli_found"])
            self.assertIn("ollama pull qwen3.5:4b", payload["suggested_commands"])
            self.assertNotRegex(str(payload).lower(), r"money|payment|donation|support|revenue|profit|buy|purchase")

    def test_program_updates_validate_schedule_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            client = TestClient(create_app(settings))

            bad_time = client.patch("/api/programs/night_lab", json={"start_time": "25:99"})
            bad_days = client.patch("/api/programs/night_lab", json={"days_of_week": "mon,funday"})
            valid = client.patch("/api/programs/night_lab", json={"start_time": "19:00", "end_time": "23:30", "days_of_week": "fri,sat"})

            self.assertEqual(400, bad_time.status_code)
            self.assertEqual(400, bad_days.status_code)
            self.assertEqual(200, valid.status_code)
            self.assertEqual("19:00", valid.json()["start_time"])
            self.assertEqual("fri,sat", valid.json()["days_of_week"])

    def test_emergency_fallback_playlist_uses_real_indexed_tracks_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            make_wav(root / "music" / "Alice - Blue Room.wav")
            make_wav(root / "music" / "Ben - Amber Night.wav")
            scan_music(settings)
            client = TestClient(create_app(settings))

            payload = client.get("/api/fallback-playlist").json()

            self.assertEqual(2, payload["count"])
            self.assertTrue(all(item["file_exists"] for item in payload["tracks"]))
            self.assertEqual({"Blue Room", "Amber Night"}, {item["title"] for item in payload["tracks"]})
            self.assertNotRegex(json.dumps(payload).lower(), r"fake|demo|donation|payment|money|support")

    def test_public_status_exposes_sanitized_share_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            settings.public_sync_token = "secret-token"
            settings.public_stream_url = "https://radiotedu.com/ai"
            client = TestClient(create_app(settings))
            response = client.post(
                "/api/public/snapshot",
                headers={"X-RadioTEDU-Sync-Token": "secret-token"},
                json={
                    "channel": {"id": "radiotedu", "name": "RadioTEDU", "status": "live", "cover_path": "/static/generated/covers/radiotedu_station.png"},
                    "now_playing": {"type": "track", "title": "Blue Room", "artist": "Alice"},
                    "stream": {"url": "https://radiotedu.com/ai"},
                },
            )
            self.assertEqual(200, response.status_code)

            payload = client.get("/api/public/status").json()

            self.assertEqual("RadioTEDU: Blue Room", payload["share_card"]["title"])
            self.assertEqual("https://radiotedu.com/ai", payload["share_card"]["url"])
            self.assertNotRegex(json.dumps(payload["share_card"]).lower(), r"f:/|file_path|token|payment|money|support")

    def test_optional_admin_auth_protects_local_control_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            settings.admin_api_token = "local-secret"
            client = TestClient(create_app(settings))

            denied = client.post("/api/air/stop")
            allowed = client.post("/api/air/stop", headers={"X-RadioTEDU-Admin-Token": "local-secret"})
            public = client.get("/api/public/status")

            self.assertEqual(401, denied.status_code)
            self.assertEqual(200, allowed.status_code)
            self.assertEqual(200, public.status_code)

    def test_liquidsoap_verify_reports_queue_and_mount_without_starting_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            settings.liquidsoap_enabled = True
            settings.liquidsoap_command = "definitely-missing-liquidsoap"
            client = TestClient(create_app(settings))

            payload = client.post("/api/liquidsoap/verify").json()

            self.assertTrue(payload["queue_readable"])
            self.assertFalse(payload["command_found"])
            self.assertFalse(payload["mount_active"])
            self.assertIn("/ai", payload["icecast_url"])


if __name__ == "__main__":
    unittest.main()
