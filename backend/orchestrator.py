from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .announcements.models import AnnouncementJob
from .config import Settings
from .database import connect, init_db, log_event, now_iso
from .llm import ollama_runtime_status
from .music_library import scan_music
from .ollama_setup import repair_ollama_runtime
from .radio_agent import RadioAgent
from .stations.context import StationContext, coerce_station_context


NONFINANCIAL_GUARD = re.compile(
    r"\b(money|payment|donation|donations|support|revenue|profit|buy|purchase|sponsor|sponsorship|"
    r"pay|paid|price|pricing|balance|funding|funds|cash|earn|income|sales)\b",
    re.IGNORECASE,
)


def _normalized_runtime_identity(context: StationContext) -> tuple[Path, ...]:
    return (
        context.database_file,
        context.data_root,
        context.music_root,
        context.announcement_root,
        context.cache_root,
        context.log_root,
    )


def _validate_agent_runtime_pair(
    runtime: Settings | StationContext,
    context: StationContext,
    agent: RadioAgent,
) -> None:
    agent_context = getattr(agent, "context", None)
    if not isinstance(agent_context, StationContext):
        raise ValueError("station runtime and agent context mismatch")
    if isinstance(runtime, StationContext):
        if agent_context is not runtime:
            raise ValueError("station runtime and agent context mismatch")
        return
    if (
        type(agent_context) is not type(context)
        or context.profile.station_id != "radiotedu-en"
        or agent_context.profile.station_id != "radiotedu-en"
        or _normalized_runtime_identity(agent_context) != _normalized_runtime_identity(context)
    ):
        raise ValueError("station runtime and agent context mismatch")


class AutonomousOrchestrator:
    def __init__(self, runtime: Settings | StationContext, agent: RadioAgent) -> None:
        context = coerce_station_context(runtime)
        _validate_agent_runtime_pair(runtime, context, agent)
        self.context = context
        self.settings = self.context.settings
        self._database_runtime: Settings | StationContext = (
            self.context if isinstance(runtime, StationContext) else self.settings
        )
        self.agent = agent
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_name = f"radiotedu-orchestrator-{self.context.profile.station_id}"
        self.last_tick_at: datetime | None = None
        self.last_strategy_at: datetime | None = None
        self.last_error: str | None = None

    def start_background(self) -> dict:
        if self._thread and self._thread.is_alive():
            return {"running": True, "already_running": True}
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever, name=self._thread_name, daemon=True)
        self._thread.start()
        with connect(self._database_runtime) as conn:
            conn.execute("update channels set status='live', updated_at=? where id='radiotedu'", (now_iso(),))
            log_event(conn, "info", "Autonomous orchestrator started.")
            conn.commit()
        return {"running": True, "already_running": False}

    def stop_background(self) -> dict:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        with connect(self._database_runtime) as conn:
            conn.execute("update channels set status='stopped', updated_at=? where id='radiotedu'", (now_iso(),))
            log_event(conn, "info", "Autonomous orchestrator stopped.")
            conn.commit()
        return {"running": False}

    def status(self) -> dict:
        strategy = self._metric("long_horizon_strategy")
        revision = self._metric("strategy_revision")
        self_review = self._metric("last_self_review")
        strategy_policy = self._json_metric("strategy_policy_json")
        with connect(self._database_runtime) as conn:
            memory_count = conn.execute("select count(*) from autonomy_memory").fetchone()[0]
            draft_count = conn.execute("select count(*) from outbound_drafts").fetchone()[0]
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "last_strategy_at": self.last_strategy_at.isoformat() if self.last_strategy_at else None,
            "last_error": self.last_error,
            "strategy": strategy,
            "strategy_policy": strategy_policy,
            "strategy_revision": int(revision or 0),
            "memory_count": memory_count,
            "draft_count": draft_count,
            "self_review": self_review,
        }

    def record_listener_feedback(self, text: str, source: str = "dashboard") -> dict:
        content = self._nonfinancial_text(" ".join(text.strip().split()))[:500]
        clean_source = " ".join(source.strip().split())[:80] or "dashboard"
        if not content:
            return {"stored": False, "reason": "empty_text"}
        with connect(self._database_runtime) as conn:
            conn.execute(
                "insert into listener_events (channel_id, event_type, created_at, metadata_json) values ('radiotedu', ?, ?, ?)",
                ("feedback", now_iso(), json.dumps({"text": content, "source": clean_source}, ensure_ascii=True)),
            )
            conn.execute(
                "insert into autonomy_memory (kind, content, source, weight, created_at) values (?, ?, ?, ?, ?)",
                ("listener_feedback", content, clean_source, 1.5, now_iso()),
            )
            log_event(conn, "info", "Listener feedback stored for autonomy memory.", {"source": clean_source})
            conn.commit()
        reply = self.agent.queue_listener_reply(content, clean_source)
        return {"stored": True, "reply_queued": bool(reply.get("queued")), "reply": reply}

    def enqueue_announcement_job(
        self,
        job: AnnouncementJob,
        *,
        dispatch_rule: str,
        dispatch_inputs: dict[str, object],
    ) -> dict:
        """Accept only this station's queued dispatch intent without invoking AI."""

        if job.station_id != self.context.profile.station_id or job.language.casefold() != self.context.profile.language.casefold():
            raise ValueError("announcement job does not belong to this orchestrator")
        return self.agent.enqueue_announcement_job(
            job,
            dispatch_rule=dispatch_rule,
            dispatch_inputs=dispatch_inputs,
        )

    def tick(self) -> dict:
        init_db(self._database_runtime)
        self.last_tick_at = datetime.now(timezone.utc)
        executed_task = self.execute_next_task()
        with connect(self._database_runtime) as conn:
            track_count = conn.execute("select count(*) from tracks").fetchone()[0]
            if track_count == 0:
                conn.execute("update channels set status='idle', updated_at=? where id='radiotedu'", (now_iso(),))
                log_event(conn, "info", "Autonomous tick skipped because no playable tracks exist.")
                self._record_incident(
                    conn,
                    component="music_library",
                    severity="warning",
                    summary="No playable tracks are indexed.",
                    details={"action": "rescan_or_add_music"},
                    task_type="rescan_music_library",
                    task_title="Rescan or add local music",
                    priority=60,
                )
                conn.commit()
                return {"played": False, "reason": "no_music", "recovery": self._recovery_snapshot(conn), "executed_task": executed_task}
            conn.execute("update channels set status='live', updated_at=? where id='radiotedu'", (now_iso(),))
            recovery = self._evaluate_runtime_health(conn)
            conn.commit()

        strategy_updated = self._maybe_update_strategy(track_count)
        self.agent.reconcile_playing_rundown()
        coverage = self.agent.maintain_rundown(max_render_items=1)
        prebuffer = self.agent.announcement_readiness()
        with connect(self._database_runtime) as conn:
            self._evaluate_prebuffer_health(conn, prebuffer)
            recovery = self._recovery_snapshot(conn)
            conn.commit()
        played = False
        if not self.agent.playback.queue and self.agent.playback.now_playing is None:
            result = self.agent.queue_next_ready_rundown_item()
            played = bool(result.get("started"))
        return {
            "played": played,
            "strategy_updated": strategy_updated,
            "prebuffer": prebuffer,
            "coverage": asdict(coverage),
            "recovery": recovery,
            "executed_task": executed_task,
        }

    def execute_next_task(self) -> dict:
        init_db(self._database_runtime)
        with connect(self._database_runtime) as conn:
            task = conn.execute(
                """
                select *
                from autonomous_tasks
                where status='queued'
                order by priority desc, created_at asc
                limit 1
                """
            ).fetchone()
            if not task:
                return {"executed": False, "reason": "no_queued_tasks"}
            task_id = int(task["id"])
            attempts = int(task["attempts"]) + 1
            conn.execute(
                "update autonomous_tasks set status='running', attempts=?, updated_at=? where id=?",
                (attempts, now_iso(), task_id),
            )
            conn.commit()
        try:
            details = self._run_task(dict(task))
            with connect(self._database_runtime) as conn:
                conn.execute(
                    "update autonomous_tasks set status='completed', details_json=?, completed_at=?, updated_at=? where id=?",
                    (json.dumps(details, ensure_ascii=True), now_iso(), now_iso(), task_id),
                )
                log_event(conn, "info", f"Autonomous task completed: {task['task_type']}", {"task_id": task_id})
                conn.commit()
            return {"executed": True, "task_type": task["task_type"], "status": "completed", "details": details}
        except Exception as exc:
            with connect(self._database_runtime) as conn:
                conn.execute(
                    "update autonomous_tasks set status='failed', details_json=?, updated_at=? where id=?",
                    (json.dumps({"error": str(exc)}, ensure_ascii=True), now_iso(), task_id),
                )
                log_event(conn, "error", f"Autonomous task failed: {task['task_type']}", {"task_id": task_id, "error": str(exc)})
                conn.commit()
            return {"executed": True, "task_type": task["task_type"], "status": "failed", "error": str(exc)}

    def _run_task(self, task: dict) -> dict:
        task_type = str(task["task_type"])
        if task_type == "rescan_music_library":
            result = scan_music(self.settings)
            return {"tracks_found": result.tracks_found, "tracks_indexed": result.tracks_indexed}
        if task_type == "repair_announcement_prebuffer":
            with connect(self._database_runtime) as conn:
                conn.execute(
                    "update announcement_queue set status='stale', used_at=? where status='failed'",
                    (now_iso(),),
                )
                conn.commit()
            readiness = self.agent.ensure_announcement_prebuffer()
            if int(readiness.get("ready") or 0) < int(readiness.get("required") or 0):
                raise RuntimeError("announcement prebuffer is still not ready")
            return readiness
        if task_type == "restart_llm_runtime":
            status = repair_ollama_runtime(self.settings)
            if status.get("status") != "ready":
                raise RuntimeError(f"LLM runtime is still {status.get('status')}")
            return {
                "status": status.get("status"),
                "model": status.get("configured_model"),
                "start_attempted": status.get("start_attempted"),
                "pull_attempted": status.get("pull_attempted"),
                "actions": status.get("actions", []),
            }
        raise RuntimeError(f"unknown autonomous task: {task_type}")

    def maintain_long_horizon_strategy(self, track_count: int | None = None) -> dict:
        with connect(self._database_runtime) as conn:
            if track_count is None:
                track_count = conn.execute("select count(*) from tracks").fetchone()[0]
            top_genres = conn.execute(
                """
                select coalesce(genre, 'unknown') as genre, count(*) as total
                from tracks
                group by coalesce(genre, 'unknown')
                order by total desc, genre asc
                limit 3
                """
            ).fetchall()
            revision_raw = conn.execute("select value from station_metrics where channel_id='radiotedu' and key='strategy_revision'").fetchone()
            revision = int(revision_raw["value"]) + 1 if revision_raw else 1
            genres = ", ".join(f"{row['genre']} ({row['total']})" for row in top_genres) or "metadata still sparse"
            memories = conn.execute(
                "select content from autonomy_memory where kind <> 'self_review' order by weight desc, created_at desc limit 4"
            ).fetchall()
            memory_text = "; ".join(row["content"] for row in memories) or "no listener notes yet"
            policy = self._strategy_policy(track_count, genres, memory_text)
            strategy = (
                f"RadioTEDU long-horizon strategy rev {revision}: keep one local pop-first channel with occasional curated jazz and classical, "
                f"use {track_count} indexed tracks, rotate dayparts by program vibe, and avoid repeating artists too often. "
                f"Current library signals: {genres}. Listener memory: {memory_text}."
            )
            self._set_metric(conn, "long_horizon_strategy", strategy)
            self._set_metric(conn, "strategy_revision", str(revision))
            self._set_metric(conn, "strategy_policy_json", json.dumps(policy, ensure_ascii=True))
            self._apply_schedule_strategy(conn)
            self._write_self_review(conn, track_count, memory_text)
            self._write_outbound_draft(conn, revision, strategy)
            log_event(conn, "info", "Long-horizon strategy refreshed.", {"revision": revision})
            conn.commit()
        self.last_strategy_at = datetime.now(timezone.utc)
        return {"revision": revision, "strategy": strategy, "policy": policy}

    def _strategy_policy(self, track_count: int, genres: str, memory_text: str) -> dict:
        next_actions = ["Keep prepared announcements ready"]
        if track_count == 0:
            next_actions.insert(0, "Add music or rescan the library")
        else:
            next_actions.insert(0, "Review real play history before changing rotation")
        if memory_text != "no listener notes yet":
            next_actions.append("Use listener memory in upcoming program choices")
        return {
            "single_channel": True,
            "library_tracks": int(track_count),
            "library_signals": genres,
            "listener_memory": memory_text,
            "goals": [
                "Keep RadioTEDU as one channel",
                "Choose songs from the real local library",
                "Grow useful listener memory",
            ],
            "next_actions": next_actions,
            "constraints": [
                "Use real tracks only",
                "Avoid invented analytics",
                "Keep prepared announcements ready before broadcast",
            ],
        }

    def _maybe_update_strategy(self, track_count: int) -> bool:
        if self.last_strategy_at is None:
            self.maintain_long_horizon_strategy(track_count)
            return True
        elapsed = datetime.now(timezone.utc) - self.last_strategy_at
        if elapsed >= timedelta(minutes=self.settings.strategy_interval_minutes):
            self.maintain_long_horizon_strategy(track_count)
            return True
        return False

    def _evaluate_runtime_health(self, conn) -> dict:
        if self.settings.llm_provider.lower() == "ollama":
            status = ollama_runtime_status(self.settings)
            if not status.get("reachable") or not status.get("model_available"):
                self._record_incident(
                    conn,
                    component="llm",
                    severity="warning",
                    summary=f"Ollama runtime is {status.get('status', 'unavailable')}.",
                    details=status,
                    task_type="restart_llm_runtime",
                    task_title="Restart Ollama and verify the configured model",
                    priority=80,
                )
            else:
                self._resolve_component_incidents(conn, "llm")
                self._complete_component_tasks(conn, "llm")
        return self._recovery_snapshot(conn)

    def _evaluate_prebuffer_health(self, conn, prebuffer: dict) -> None:
        required = int(prebuffer.get("required") or 0)
        ready = int(prebuffer.get("ready") or 0)
        failed = int(prebuffer.get("failed") or 0)
        if required > 0 and not prebuffer.get("ready_to_broadcast") and (failed > 0 or ready < required):
            self._record_incident(
                conn,
                component="prebuffer",
                severity="critical",
                summary="Announcement prebuffer is not ready for broadcast.",
                details=prebuffer,
                task_type="repair_announcement_prebuffer",
                task_title="Repair announcement prebuffer",
                priority=90,
            )
        elif required == 0 or prebuffer.get("ready_to_broadcast"):
            self._resolve_component_incidents(conn, "prebuffer")
            self._complete_component_tasks(conn, "prebuffer")

    def _record_incident(
        self,
        conn,
        *,
        component: str,
        severity: str,
        summary: str,
        details: dict,
        task_type: str,
        task_title: str,
        priority: int,
    ) -> None:
        now = now_iso()
        row = conn.execute(
            "select id from incidents where component=? and status='open' order by id desc limit 1",
            (component,),
        ).fetchone()
        payload = json.dumps(details, ensure_ascii=True)
        if row:
            conn.execute(
                "update incidents set severity=?, summary=?, details_json=?, updated_at=? where id=?",
                (severity, summary, payload, now, row["id"]),
            )
        else:
            conn.execute(
                "insert into incidents (component, severity, status, summary, details_json, created_at, updated_at) values (?, ?, 'open', ?, ?, ?, ?)",
                (component, severity, summary, payload, now, now),
            )
            log_event(conn, "warning" if severity == "warning" else "error", f"Autonomous incident opened: {summary}", {"component": component})
        existing_task = conn.execute(
            "select id from autonomous_tasks where task_type=? and status in ('queued', 'running') order by id desc limit 1",
            (task_type,),
        ).fetchone()
        if existing_task:
            conn.execute(
                "update autonomous_tasks set priority=?, details_json=?, updated_at=? where id=?",
                (priority, payload, now, existing_task["id"]),
            )
        else:
            conn.execute(
                """
                insert into autonomous_tasks (task_type, component, title, status, priority, details_json, created_at, updated_at)
                values (?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (task_type, component, task_title, priority, payload, now, now),
            )

    def _resolve_component_incidents(self, conn, component: str) -> None:
        conn.execute(
            "update incidents set status='resolved', resolved_at=?, updated_at=? where component=? and status='open'",
            (now_iso(), now_iso(), component),
        )

    def _complete_component_tasks(self, conn, component: str) -> None:
        conn.execute(
            "update autonomous_tasks set status='completed', completed_at=?, updated_at=? where component=? and status in ('queued', 'running')",
            (now_iso(), now_iso(), component),
        )

    def _recovery_snapshot(self, conn) -> dict:
        open_incidents = conn.execute("select count(*) from incidents where status='open'").fetchone()[0]
        queued_tasks = conn.execute("select count(*) from autonomous_tasks where status='queued'").fetchone()[0]
        top_task = conn.execute(
            "select task_type, component, priority from autonomous_tasks where status='queued' order by priority desc, created_at asc limit 1"
        ).fetchone()
        return {
            "open_incidents": int(open_incidents),
            "queued_tasks": int(queued_tasks),
            "top_task": dict(top_task) if top_task else None,
        }

    def _apply_schedule_strategy(self, conn) -> None:
        updates = [
            ("morning_signal", "06:00", "10:00", "mon,tue,wed,thu,fri"),
            ("campus_frequencies", "10:00", "17:30", "mon,tue,wed,thu,fri"),
            ("night_lab", "17:30", "23:59", "mon,tue,wed,thu,fri,sat,sun"),
            ("weekend_transmission", "08:00", "18:00", "sat,sun"),
        ]
        for program_id, start, end, days in updates:
            previous = conn.execute(
                "select start_time, end_time, days_of_week from programs where id=? and channel_id='radiotedu'",
                (program_id,),
            ).fetchone()
            if previous and (
                previous["start_time"] != start
                or previous["end_time"] != end
                or previous["days_of_week"] != days
            ):
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
                        start,
                        end,
                        days,
                        "long-horizon strategy schedule adaptation",
                        now_iso(),
                    ),
                )
            conn.execute(
                "update programs set start_time=?, end_time=?, days_of_week=?, updated_at=? where id=? and channel_id='radiotedu'",
                (start, end, days, now_iso(), program_id),
            )

    def _write_outbound_draft(self, conn, revision: int, strategy: str) -> None:
        content = (
            f"RadioTEDU segment draft rev {revision}: share a concise station update, invite local listener feedback, "
            "and preview the current program mood with a calm local-radio tone."
        )
        conn.execute(
            "insert into outbound_drafts (draft_type, content, status, created_at) values (?, ?, ?, ?)",
            ("station_segment", content, "draft", now_iso()),
        )

    def _write_self_review(self, conn, track_count: int, memory_text: str) -> None:
        plays = conn.execute("select count(*) from play_history where played_at >= datetime('now', '-14 days')").fetchone()[0]
        review = (
            f"Self-review: {plays} recent real plays, {track_count} indexed tracks, listener memory: {memory_text}. "
            "Next action: keep schedule aligned to program moods and ask for local feedback."
        )
        self._set_metric(conn, "last_self_review", review)
        conn.execute(
            "insert into autonomy_memory (kind, content, source, weight, created_at) values (?, ?, ?, ?, ?)",
            ("self_review", review, "orchestrator", 0.7, now_iso()),
        )

    def _nonfinancial_text(self, text: str) -> str:
        cleaned = re.sub(r"\bpay\s+for\b", "ask for", text, flags=re.IGNORECASE)
        cleaned = NONFINANCIAL_GUARD.sub("local", cleaned)
        return " ".join(cleaned.split())

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                with connect(self._database_runtime) as conn:
                    log_event(conn, "error", "Autonomous orchestrator tick failed.", {"error": self.last_error})
                    conn.commit()
            self._stop.wait(max(1, int(self.settings.autonomy_tick_seconds)))

    def _metric(self, key: str) -> str | None:
        with connect(self._database_runtime) as conn:
            row = conn.execute("select value from station_metrics where channel_id='radiotedu' and key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def _json_metric(self, key: str) -> dict | None:
        raw = self._metric(key)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def _set_metric(self, conn, key: str, value: str) -> None:
        conn.execute(
            """
            insert into station_metrics (channel_id, key, value, updated_at)
            values ('radiotedu', ?, ?, ?)
            on conflict(channel_id, key) do update set value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, now_iso()),
        )
