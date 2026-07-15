import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from backend.config import Settings
from backend.database import connect, init_db
from backend.music_library import scan_music
from backend.orchestrator import AutonomousOrchestrator
from backend.radio_agent import RadioAgent
from backend.scheduler import current_program


def make_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 800)


def fake_qwen_synthesis(_agent, _text, output_path, **_kwargs) -> str:
    output = Path(output_path)
    make_wav(output)
    return str(output)


class AutonomousOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(RadioAgent, "_synthesize_qwen", fake_qwen_synthesis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_settings(self, root: Path) -> Settings:
        return Settings(
            database_path=str(root / "radiotedu.db"),
            music_dir=str(root / "music"),
            static_dir=str(root / "static"),
            rss_feeds_path=str(root / "rss_feeds.json"),
            playback_backend="simulate",
            autonomy_tick_seconds=1,
            strategy_interval_minutes=0,
        )

    def test_current_program_uses_database_schedule_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            init_db(settings)
            with connect(settings) as conn:
                conn.execute(
                    "update programs set start_time='00:00', end_time='23:59', days_of_week='mon,tue,wed,thu,fri,sat,sun' where id='weekend_transmission'"
                )
                conn.commit()
            program = current_program(settings)
            self.assertEqual("weekend_transmission", program["id"])

    def test_tick_runs_one_real_cycle_and_persists_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.min_ready_announcements = 1
            settings.max_ready_announcements = 1
            make_wav(root / "music" / "Alice - Blue Room.wav")
            scan_music(settings)
            orchestrator = AutonomousOrchestrator(settings, RadioAgent(settings))
            result = orchestrator.tick()
            self.assertTrue(result["played"])
            with connect(settings) as conn:
                history_count = conn.execute("select count(*) from play_history").fetchone()[0]
                strategy = conn.execute("select value from station_metrics where key='long_horizon_strategy'").fetchone()
                revision = conn.execute("select value from station_metrics where key='strategy_revision'").fetchone()
                night_lab = conn.execute("select start_time from programs where id='night_lab'").fetchone()
            self.assertEqual(1, history_count)
            self.assertIsNotNone(strategy)
            self.assertIn("RadioTEDU", strategy["value"])
            self.assertEqual("1", revision["value"])
            self.assertEqual("17:30", night_lab["start_time"])

    def test_listener_feedback_becomes_nonfinancial_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            init_db(settings)
            orchestrator = AutonomousOrchestrator(settings, RadioAgent(settings))
            saved = orchestrator.record_listener_feedback("more mellow piano at night", source="dashboard")
            sanitized = orchestrator.record_listener_feedback("pay money for jazz", source="dashboard")
            self.assertTrue(saved["stored"])
            self.assertTrue(sanitized["stored"])
            orchestrator.maintain_long_horizon_strategy(track_count=0)
            with connect(settings) as conn:
                memory = conn.execute("select kind, content, source from autonomy_memory").fetchall()
                strategy = conn.execute("select value from station_metrics where key='long_horizon_strategy'").fetchone()
                review = conn.execute("select value from station_metrics where key='last_self_review'").fetchone()
            self.assertIn(("listener_feedback", "more mellow piano at night", "dashboard"), [tuple(row) for row in memory])
            self.assertNotRegex(" ".join(row["content"] for row in memory).lower(), r"money|payment|donation|support|revenue|profit|buy|purchase")
            self.assertIn("mellow piano", strategy["value"])
            self.assertNotRegex(strategy["value"].lower(), r"money|payment|donation|support|revenue|profit|buy|purchase")
            self.assertIsNotNone(review)
            self.assertNotRegex(review["value"].lower(), r"money|payment|donation|support|revenue|profit|buy|purchase")

    def test_strategy_keeps_one_channel_and_records_schedule_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            init_db(settings)
            orchestrator = AutonomousOrchestrator(settings, RadioAgent(settings))
            orchestrator.maintain_long_horizon_strategy(track_count=42)
            with connect(settings) as conn:
                channels = conn.execute("select id from channels").fetchall()
                revisions = conn.execute("select program_id, reason from schedule_revisions").fetchall()
                drafts = conn.execute("select draft_type, content from outbound_drafts").fetchall()
                self_reviews = conn.execute("select content from autonomy_memory where kind='self_review'").fetchall()
            self.assertEqual([("radiotedu",)], [tuple(row) for row in channels])
            self.assertGreaterEqual(len(revisions), 1)
            self.assertTrue(any("long-horizon" in row["reason"] for row in revisions))
            self.assertGreaterEqual(len(drafts), 1)
            self.assertNotRegex(" ".join(row["content"] for row in drafts).lower(), r"money|payment|donation|support|revenue|profit|buy|purchase")
            self.assertGreaterEqual(len(self_reviews), 1)
            self.assertNotRegex(" ".join(row["content"] for row in self_reviews).lower(), r"money|payment|donation|support|revenue|profit|buy|purchase")

    def test_status_metrics_have_no_financial_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            init_db(settings)
            from backend.app import build_status

            agent = RadioAgent(settings)
            orchestrator = AutonomousOrchestrator(settings, agent)
            payload = build_status(settings, agent, orchestrator)
            self.assertNotIn("support_balance", payload["metrics"])
            self.assertNotIn("support_balance", payload["orchestrator"])
            self.assertNotIn("donations", str(payload).lower())
            self.assertNotIn("money", str(payload).lower())

    def test_background_runner_start_stop_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.make_settings(Path(tmp))
            init_db(settings)
            orchestrator = AutonomousOrchestrator(settings, RadioAgent(settings))
            started = orchestrator.start_background()
            self.assertTrue(started["running"])
            self.assertTrue(orchestrator.status()["running"])
            stopped = orchestrator.stop_background()
            self.assertFalse(stopped["running"])


if __name__ == "__main__":
    unittest.main()
