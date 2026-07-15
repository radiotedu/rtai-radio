from __future__ import annotations

import json
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from uuid import uuid4

from .config import Settings
from .announcements.models import AnnouncementJob
from .database import connect, init_db, log_event, now_iso, rows_to_dicts
from .llm import choose_track_with_llm, ollama_runtime_status
from .playback import PlaybackController, QueueItem
from .scheduler import current_program
from .search.rss import RSSSearchProvider
from .search.searxng import SearXNGSearchProvider
from .stations.context import StationContext, coerce_station_context
from .tts.contracts import AnnouncementLabel, QwenUnavailableError, SynthesisRequest
from .tts.factory import build_tts_provider
from .tts.voice_policy import VoicePolicy
from .weather.open_meteo import OpenMeteoWeatherProvider


class RadioAgent:
    def __init__(self, runtime: Settings | StationContext) -> None:
        self.context = coerce_station_context(runtime)
        self.settings = self.context.settings
        self._database_runtime: Settings | StationContext = (
            self.context if isinstance(runtime, StationContext) else self.settings
        )
        init_db(self._database_runtime)
        self.playback = PlaybackController(self.settings)
        self.tts = build_tts_provider(self.context)
        self.last_search_at: datetime | None = None
        self.weather_provider = OpenMeteoWeatherProvider(self.settings)
        self.last_weather_at: datetime | None = None
        self.last_weather_context: dict | None = None
        self.last_llm_runtime_at: datetime | None = None
        self.last_llm_runtime_status: dict | None = None
        self.last_news_at: datetime | None = None
        self.last_news_checked_at: datetime | None = None
        self.last_news_source_at: datetime | None = None
        self.last_news_source_title: str | None = None
        self.last_weather_announcement_at: datetime | None = None

    def start(self) -> dict:
        with connect(self._database_runtime) as conn:
            count = conn.execute("select count(*) from tracks").fetchone()[0]
            if count == 0:
                conn.execute("update channels set status='idle', updated_at=? where id='radiotedu'", (now_iso(),))
                log_event(conn, "info", "Radio loop not started because no playable tracks exist.")
                conn.commit()
                return {"started": False, "reason": "no_music"}
            conn.execute("update channels set status='live', updated_at=? where id='radiotedu'", (now_iso(),))
            conn.commit()
        return self.queue_next_track()

    def stop(self) -> dict:
        self.playback.running = False
        with connect(self._database_runtime) as conn:
            conn.execute("update channels set status='stopped', updated_at=? where id='radiotedu'", (now_iso(),))
            log_event(conn, "info", "RadioTEDU stopped.")
            conn.commit()
        return {"stopped": True}

    def skip(self) -> dict:
        self.playback.skip()
        with connect(self._database_runtime) as conn:
            log_event(conn, "info", "Skip requested.")
            conn.commit()
        return self.queue_next_track()

    def say(self, text: str) -> dict:
        safe_text = text.strip()[:240]
        if not safe_text:
            return {"queued": False, "reason": "empty_text"}
        filename = f"say_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.wav"
        output = self.settings.tts_path / filename
        clip_path = self._synthesize_qwen(
            safe_text,
            output,
            announcement_label="listener_reply",
            program_id="manual",
        )
        item = self._queue_speech("User message", clip_path)
        with connect(self._database_runtime) as conn:
            conn.execute(
                "insert into generated_clips (clip_type, text, file_path, voice, program_id, created_at) values (?, ?, ?, ?, ?, ?)",
                ("user_message", safe_text, clip_path, None, None, now_iso()),
            )
            log_event(conn, "info", "User message queued.")
            conn.commit()
        return {"queued": True, "file_path": clip_path}

    def enqueue_announcement_job(
        self,
        job: AnnouncementJob,
        *,
        dispatch_rule: str,
        dispatch_inputs: dict[str, object],
    ) -> dict:
        """Record station-local dispatch intent without starting model work inline."""

        if job.station_id != self.context.profile.station_id or job.language.casefold() != self.context.profile.language.casefold():
            raise ValueError("announcement job does not belong to this radio agent")
        metadata_json = json.dumps(dispatch_inputs, sort_keys=True, separators=(",", ":"))
        with connect(self._database_runtime) as conn:
            row = conn.execute(
                "select state from announcement_jobs where station_id = ? and job_id = ?",
                (job.station_id, job.job_id),
            ).fetchone()
            event_recorded = row is not None
            if event_recorded:
                conn.execute(
                    """
                    insert into announcement_job_events (
                        event_id, job_id, from_state, to_state, actor, reason, metadata_json, occurred_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        job.job_id,
                        row["state"],
                        row["state"],
                        "bilingual-dispatcher",
                        dispatch_rule,
                        metadata_json,
                        now_iso(),
                    ),
                )
                conn.commit()
        return {
            "queued": True,
            "job_id": job.job_id,
            "station_id": job.station_id,
            "event_recorded": event_recorded,
        }

    def queue_listener_reply(self, feedback: str, source: str = "dashboard") -> dict:
        safe_feedback = " ".join(feedback.strip().split())[:180]
        if not safe_feedback:
            return {"queued": False, "reason": "empty_text"}
        program = current_program(self._database_runtime)
        reply = self._listener_reply_text(safe_feedback, program)
        filename = f"listener_reply_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}.wav"
        output = self.settings.tts_path / filename
        voice = self.tts.provider_name
        clip_path = self._synthesize_qwen(
            reply,
            output,
            announcement_label="listener_reply",
            program_id=program["id"],
        )
        self._queue_speech("RadioTEDU listener reply", clip_path)
        with connect(self._database_runtime) as conn:
            conn.execute(
                "insert into generated_clips (clip_type, text, file_path, voice, program_id, created_at) values (?, ?, ?, ?, ?, ?)",
                ("listener_reply", reply, clip_path, voice or getattr(self.tts, "provider_name", "tts"), program["id"], now_iso()),
            )
            log_event(conn, "info", "Listener reply queued.", {"source": source, "program": program["name"]})
            conn.commit()
        return {"queued": True, "file_path": clip_path, "text": reply}

    def queue_next_track(self) -> dict:
        program = current_program(self._database_runtime)
        prebuffer = self.announcement_readiness(program["id"])
        if not prebuffer["ready_to_broadcast"]:
            with connect(self._database_runtime) as conn:
                log_event(conn, "info", "Waiting for announcement prebuffer before broadcast.", prebuffer)
                conn.commit()
            return {"started": False, "reason": "announcement_prebuffer_not_ready", **prebuffer}
        announcement = self._consume_ready_announcement(program["id"])
        selected = self._track_from_announcement(announcement) if announcement else None
        choice = None
        candidates = self._candidates(program)
        if selected is None and not candidates:
            return {"started": False, "reason": "no_candidates"}
        if selected is None and announcement is None and int(prebuffer.get("required") or 0) > 0:
            return {"started": False, "reason": "ready_announcement_missing", **prebuffer}
        if selected is None:
            context = self._web_context(self._search_query_for_candidates(candidates))
            weather_context = self._weather_context()
            recent = self._recent_tracks()
            choice = choose_track_with_llm(
                candidates,
                program,
                recent,
                context,
                self.settings,
                weather_context=weather_context,
                runtime_status=self._llm_runtime_status(),
            )
            selected = next(item for item in candidates if int(item["id"]) == choice.song_id)
        if announcement is None:
            dj_line = choice.dj_line if choice else self._line_for_track(selected)
            clip_path = self._narrate(dj_line, program["id"])
            self._queue_speech("RadioTEDU DJ", clip_path)
        else:
            self._queue_speech("RadioTEDU DJ", announcement["file_path"])
        self.playback.add(
            QueueItem(
                "track",
                selected["title"],
                selected["file_path"],
                duration_seconds=selected.get("duration_seconds"),
                artist=selected.get("artist"),
                track_id=int(selected["id"]),
            )
        )
        with connect(self._database_runtime) as conn:
            log_event(conn, "info", f"Queued {selected['title']} by {selected['artist']}.", {"program": program["name"]})
            if choice and choice.used_fallback:
                log_event(conn, "warning", "LLM fallback used for track decision.", {"reason": choice.reason})
            conn.commit()
        speech_item = self.playback.play_next()
        if speech_item is not None:
            self._record_public_airtime(speech_item, program["id"])
        track_item = self.playback.play_next()
        if track_item and track_item.track_id:
            self._record_play(track_item.track_id, program["id"], track_item.duration_seconds)
            if self.playback.backend == "simulate":
                self.playback.now_playing = None
        dj_line = announcement["text"] if announcement else choice.dj_line if choice else self._line_for_track(selected)
        return {"started": True, "track_id": int(selected["id"]), "dj_line": dj_line}

    def announcement_readiness(self, program_id: str | None = None) -> dict:
        required = max(0, int(self.settings.min_ready_announcements))
        with connect(self._database_runtime) as conn:
            if program_id:
                ready = conn.execute(
                    "select count(*) from announcement_queue where status='ready' and (program_id=? or program_id is null)",
                    (program_id,),
                ).fetchone()[0]
                used = conn.execute(
                    "select count(*) from announcement_queue where status='used' and (program_id=? or program_id is null)",
                    (program_id,),
                ).fetchone()[0]
                failed = conn.execute(
                    "select count(*) from announcement_queue where status='failed' and (program_id=? or program_id is null)",
                    (program_id,),
                ).fetchone()[0]
                oldest_ready = conn.execute(
                    """
                    select created_at from announcement_queue
                    where status='ready' and (program_id=? or program_id is null)
                    order by created_at asc, id asc
                    limit 1
                    """,
                    (program_id,),
                ).fetchone()
                next_ready = conn.execute(
                    """
                    select metadata_json from announcement_queue
                    where status='ready' and (program_id=? or program_id is null)
                    order by created_at asc, id asc
                    limit 1
                    """,
                    (program_id,),
                ).fetchone()
            else:
                ready = conn.execute("select count(*) from announcement_queue where status='ready'").fetchone()[0]
                used = conn.execute("select count(*) from announcement_queue where status='used'").fetchone()[0]
                failed = conn.execute("select count(*) from announcement_queue where status='failed'").fetchone()[0]
                oldest_ready = conn.execute(
                    "select created_at from announcement_queue where status='ready' order by created_at asc, id asc limit 1"
                ).fetchone()
                next_ready = conn.execute(
                    "select metadata_json from announcement_queue where status='ready' order by created_at asc, id asc limit 1"
                ).fetchone()
        return {
            "ready": int(ready),
            "used": int(used),
            "failed": int(failed),
            "required": required,
            "target": max(required, int(self.settings.max_ready_announcements)),
            "ready_to_broadcast": int(ready) >= required,
            "oldest_ready_age_seconds": self._age_seconds(oldest_ready["created_at"] if oldest_ready else None),
            "next_announcement_type": self._announcement_type(next_ready["metadata_json"] if next_ready else None),
        }

    def ensure_announcement_prebuffer(self, program_id: str | None = None, max_to_prepare: int | None = None) -> dict:
        required = max(0, int(self.settings.min_ready_announcements))
        maximum = max(required, int(self.settings.max_ready_announcements))
        program = current_program(self._database_runtime)
        target_program_id = program_id or program["id"]
        if self._has_tracks():
            self._retire_legacy_generic_prebuffer(target_program_id)
            self._retire_duplicate_track_prebuffer(target_program_id)
        readiness = self.announcement_readiness(program_id)
        if required == 0:
            return readiness
        planned_track_ids = self._ready_announcement_track_ids(target_program_id)
        prepared_count = 0
        target_ready = required if max_to_prepare is not None else maximum
        while readiness["ready"] < target_ready and readiness["ready"] < maximum:
            if max_to_prepare is not None and prepared_count >= max(0, int(max_to_prepare)):
                break
            prepared = self._prepare_announcement(program, readiness["ready"] + 1, required, planned_track_ids)
            if prepared is None:
                break
            text = prepared["text"]
            metadata = prepared["metadata"]
            track_id = metadata.get("track_id")
            if track_id is not None:
                planned_track_ids.add(int(track_id))
            filename = f"prebuffer_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}.wav"
            output = self.settings.tts_path / filename
            voice = self.tts.provider_name
            try:
                clip_path = self._synthesize_qwen(
                    text,
                    output,
                    announcement_label="track_intro",
                    program_id=target_program_id,
                )
            except (QwenUnavailableError, ValueError, OSError) as exc:
                failure_metadata = dict(metadata)
                failure_metadata["failure_code"] = type(exc).__name__
                with connect(self._database_runtime) as conn:
                    conn.execute(
                        """
                        insert into announcement_queue (
                            text, file_path, status, program_id, source, created_at, metadata_json
                        ) values (?, '', 'failed', ?, 'agent_prebuffer', ?, ?)
                        """,
                        (
                            text,
                            target_program_id,
                            now_iso(),
                            json.dumps(failure_metadata, ensure_ascii=True),
                        ),
                    )
                    conn.commit()
                readiness = self.announcement_readiness(program_id)
                break
            with connect(self._database_runtime) as conn:
                if track_id is not None and self._ready_track_exists(conn, program_id or program["id"], int(track_id)):
                    conn.commit()
                    readiness = self.announcement_readiness(program_id)
                    continue
                conn.execute(
                    """
                    insert into announcement_queue (text, file_path, status, program_id, source, created_at, metadata_json)
                    values (?, ?, 'ready', ?, 'agent_prebuffer', ?, ?)
                    """,
                    (
                        text,
                        clip_path,
                        program_id or program["id"],
                        now_iso(),
                        json.dumps(metadata, ensure_ascii=True),
                    ),
                )
                conn.execute(
                    "insert into generated_clips (clip_type, text, file_path, voice, program_id, created_at) values (?, ?, ?, ?, ?, ?)",
                    ("prebuffer_announcement", text, clip_path, voice or getattr(self.tts, "provider_name", "tts"), program_id or program["id"], now_iso()),
                )
                conn.commit()
            readiness = self.announcement_readiness(program_id)
            prepared_count += 1
        return readiness

    def _age_seconds(self, created_at: str | None) -> int | None:
        if not created_at:
            return None
        try:
            created = datetime.fromisoformat(created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
        except Exception:
            return None

    def _announcement_type(self, metadata_json: str | None) -> str | None:
        if not metadata_json:
            return None
        try:
            metadata = json.loads(metadata_json or "{}")
        except Exception:
            return "unknown"
        if metadata.get("kind"):
            return str(metadata["kind"])[:40]
        if metadata.get("track_id") is not None:
            return "song"
        if metadata.get("prebuffer"):
            return "program"
        return "unknown"

    def _consume_ready_announcement(self, program_id: str) -> dict | None:
        with connect(self._database_runtime) as conn:
            row = conn.execute(
                """
                select id, text, file_path, metadata_json from announcement_queue
                where status='ready' and (program_id=? or program_id is null)
                order by created_at asc, id asc
                limit 1
                """,
                (program_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute("update announcement_queue set status='used', used_at=? where id=?", (now_iso(), row["id"]))
            conn.commit()
            return dict(row)

    def _prebuffer_announcement_text(self, program: dict, index: int, required: int) -> str:
        return (
            f"RadioTEDU hazır anons {index} / {required}: "
            f"{program.get('name', 'RadioTEDU')} için kısa, sakin ve yerel bir geçiş."
        )

    def _prepare_announcement(self, program: dict, index: int, required: int, planned_track_ids: set[int]) -> dict | None:
        news = self._news_announcement(program)
        if news:
            return news
        weather = self._weather_announcement(program)
        if weather:
            return weather
        candidates = self._candidates(program, exclude_track_ids=planned_track_ids)
        if not candidates:
            if self._has_tracks():
                return None
            return {
                "text": self._prebuffer_announcement_text(program, index, required),
                "metadata": {"program": program.get("name"), "prebuffer": True},
            }
        context = self._web_context(self._search_query_for_candidates(candidates))
        song_context = self._song_context_announcement(program, candidates, context)
        if song_context:
            return song_context
        weather_context = self._weather_context()
        recent = self._recent_tracks()
        choice = choose_track_with_llm(
            candidates,
            program,
            recent,
            context,
            self.settings,
            weather_context=weather_context,
            runtime_status=self._llm_runtime_status(),
        )
        selected = next(item for item in candidates if int(item["id"]) == choice.song_id)
        return {
            "text": choice.dj_line,
            "metadata": {
                "program": program.get("name"),
                "prebuffer": True,
                "track_id": int(selected["id"]),
                "track_title": selected.get("title"),
                "track_artist": selected.get("artist"),
                "track_genre": selected.get("genre"),
                "decision_reason": choice.reason,
                "used_fallback": choice.used_fallback,
                "fallback_role": "dead_air_prevention" if choice.used_fallback else None,
            },
        }

    def _weather_announcement(self, program: dict) -> dict | None:
        if not self.settings.weather_enabled:
            return None
        now = datetime.now(timezone.utc)
        if (
            self.last_weather_announcement_at
            and now - self.last_weather_announcement_at < timedelta(minutes=self.settings.weather_interval_minutes)
        ):
            return None
        context = self._weather_context()
        if not context.get("available"):
            return None
        summary = " ".join(str(context.get("summary") or "").split())
        if not summary or summary == "No weather data.":
            return None
        self.last_weather_announcement_at = now
        line = f"RadioTEDU weather note: {summary}"
        return {
            "text": " ".join(line.split()[:28]),
            "metadata": {
                "program": program.get("name"),
                "prebuffer": True,
                "kind": "weather",
                "location": context.get("location"),
                "source": context.get("source") or self.settings.weather_provider,
            },
        }

    def _song_context_announcement(self, program: dict, candidates: list[dict], context: list[dict]) -> dict | None:
        for candidate in candidates:
            title = " ".join(str(candidate.get("title") or "").split())
            artist = " ".join(str(candidate.get("artist") or "").split())
            if not title and not artist:
                continue
            for item in context[:5]:
                snippet = " ".join(str(item.get("snippet") or "").split())
                url = " ".join(str(item.get("url") or "").split())
                source = " ".join(str(item.get("source") or "").split())
                haystack = f"{item.get('title') or ''} {snippet}".lower()
                if not snippet or not source or not url:
                    continue
                if title.lower() not in haystack and artist.lower() not in haystack:
                    continue
                line = f"RadioTEDU source note for {title or artist}: {snippet}"
                return {
                    "text": " ".join(line.split()[:30]),
                    "metadata": {
                        "program": program.get("name"),
                        "prebuffer": True,
                        "kind": "song_context",
                        "track_id": int(candidate["id"]),
                        "track_title": title,
                        "track_artist": artist,
                        "context_url": url,
                        "source": source,
                    },
                }
        return None

    def _news_announcement(self, program: dict) -> dict | None:
        if not self.settings.news_enabled:
            return None
        now = datetime.now(timezone.utc)
        if self.last_news_at and now - self.last_news_at < timedelta(minutes=self.settings.news_interval_minutes):
            return None
        self.last_news_checked_at = now
        provider = RSSSearchProvider(self.settings.rss_feeds_path)
        try:
            item = next(iter(provider.search("", limit=1)), None)
        except Exception:
            return None
        if item is None or not item.title.strip():
            return None
        published_at = self._fresh_news_timestamp(getattr(item, "published_at", None), now)
        if not published_at:
            return None
        self.last_news_at = now
        title = " ".join(item.title.split())
        self.last_news_source_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        self.last_news_source_title = title[:180]
        snippet = " ".join(item.snippet.split())
        context = snippet[:90] if snippet else title
        line = f"RadioTEDU news note: {title}. {context}"
        return {
            "text": " ".join(line.split()[:32]),
            "metadata": {
                "program": program.get("name"),
                "prebuffer": True,
                "kind": "news",
                "news_title": title[:180],
                "news_url": item.url,
                "source": item.source,
                "published_at": published_at,
            },
        }

    def _fresh_news_timestamp(self, published_at: str | None, now: datetime) -> str | None:
        if not published_at:
            return None
        try:
            parsed = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        max_age = timedelta(hours=max(1, int(self.settings.news_max_age_hours)))
        if now - parsed.astimezone(timezone.utc) > max_age:
            return None
        return parsed.astimezone(timezone.utc).isoformat()

    def _candidates(self, program: dict, exclude_track_ids: set[int] | None = None) -> list[dict]:
        cutoff_song = (datetime.now(timezone.utc) - timedelta(hours=self.settings.song_repeat_hours)).isoformat()
        cutoff_artist = (datetime.now(timezone.utc) - timedelta(minutes=self.settings.artist_repeat_minutes)).isoformat()
        excluded = sorted(exclude_track_ids or set())
        exclude_clause = ""
        params: list[object] = [cutoff_song, cutoff_artist]
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            exclude_clause = f"and id not in ({placeholders})"
            params.extend(excluded)
        with connect(self._database_runtime) as conn:
            rows = conn.execute(
                f"""
                select * from tracks
                where (last_played_at is null or last_played_at < ?)
                  and artist not in (
                    select artist from tracks
                    where last_played_at is not null and last_played_at >= ?
                  )
                  {exclude_clause}
                order by play_count asc, coalesce(last_played_at, '') asc, id asc
                limit 10
                """,
                params,
            ).fetchall()
        return rows_to_dicts(rows)

    def _recent_tracks(self) -> list[dict]:
        with connect(self._database_runtime) as conn:
            rows = conn.execute(
                """
                select tracks.title, tracks.artist from play_history
                join tracks on tracks.id = play_history.track_id
                order by play_history.played_at desc
                limit 5
                """
            ).fetchall()
        return rows_to_dicts(rows)

    def _web_context(self, query: str = "music culture") -> list[dict]:
        now = datetime.now(timezone.utc)
        if self.last_search_at and now - self.last_search_at < timedelta(minutes=self.settings.web_search_interval_minutes):
            return []
        self.last_search_at = now
        provider = SearXNGSearchProvider(self.settings.searxng_url) if self.settings.search_provider == "searxng" else RSSSearchProvider(self.settings.rss_feeds_path)
        try:
            return [item.__dict__ for item in provider.search(query, limit=3)]
        except Exception:
            return []

    def _search_query_for_candidates(self, candidates: list[dict]) -> str:
        terms = []
        for item in candidates[:3]:
            title = item.get("title") or ""
            artist = item.get("artist") or ""
            terms.append(f"{artist} {title}".strip())
        return " music ".join(term for term in terms if term) or "music culture"

    def _ready_announcement_track_ids(self, program_id: str) -> set[int]:
        planned: set[int] = set()
        with connect(self._database_runtime) as conn:
            rows = conn.execute(
                """
                select metadata_json from announcement_queue
                where status='ready' and (program_id=? or program_id is null)
                """,
                (program_id,),
            ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
                if metadata.get("track_id") is not None:
                    planned.add(int(metadata["track_id"]))
            except Exception:
                continue
        return planned

    def _track_from_announcement(self, announcement: dict | None) -> dict | None:
        if not announcement:
            return None
        try:
            metadata = json.loads(announcement.get("metadata_json") or "{}")
            track_id = int(metadata["track_id"])
        except Exception:
            return None
        with connect(self._database_runtime) as conn:
            row = conn.execute("select * from tracks where id=?", (track_id,)).fetchone()
        return dict(row) if row else None

    def _line_for_track(self, track: dict) -> str:
        title = track.get("title") or "this track"
        artist = track.get("artist") or "a local artist"
        line = f"RadioTEDU keeps it local with {title} by {artist}."
        words = line.split()
        return " ".join(words[:24])

    def _listener_reply_text(self, feedback: str, program: dict) -> str:
        program_name = program.get("name") or "RadioTEDU"
        line = f"RadioTEDU hears you: {feedback}. Noted for {program_name}."
        words = line.split()
        return " ".join(words[:32])

    def _has_tracks(self) -> bool:
        with connect(self._database_runtime) as conn:
            count = conn.execute("select count(*) from tracks").fetchone()[0]
        return int(count) > 0

    def _retire_legacy_generic_prebuffer(self, program_id: str) -> None:
        stale_ids: list[int] = []
        with connect(self._database_runtime) as conn:
            rows = conn.execute(
                """
                select id, metadata_json from announcement_queue
                where status='ready'
                  and source='agent_prebuffer'
                  and (program_id=? or program_id is null)
                """,
                (program_id,),
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except Exception:
                    metadata = {}
                if metadata.get("prebuffer") and metadata.get("track_id") is None and metadata.get("kind") != "news":
                    stale_ids.append(int(row["id"]))
            for row_id in stale_ids:
                conn.execute("update announcement_queue set status='stale', used_at=? where id=?", (now_iso(), row_id))
            if stale_ids:
                log_event(conn, "info", "Retired legacy generic prebuffer announcements.", {"count": len(stale_ids)})
            conn.commit()

    def _retire_duplicate_track_prebuffer(self, program_id: str) -> None:
        seen_track_ids: set[int] = set()
        stale_ids: list[int] = []
        with connect(self._database_runtime) as conn:
            rows = conn.execute(
                """
                select id, metadata_json from announcement_queue
                where status='ready'
                  and source='agent_prebuffer'
                  and (program_id=? or program_id is null)
                order by created_at asc, id asc
                """,
                (program_id,),
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                    track_id = int(metadata["track_id"])
                except Exception:
                    continue
                if track_id in seen_track_ids:
                    stale_ids.append(int(row["id"]))
                else:
                    seen_track_ids.add(track_id)
            for row_id in stale_ids:
                conn.execute("update announcement_queue set status='stale', used_at=? where id=?", (now_iso(), row_id))
            if stale_ids:
                log_event(conn, "info", "Retired duplicate track-bound prebuffer announcements.", {"count": len(stale_ids)})
            conn.commit()

    def _ready_track_exists(self, conn, program_id: str, track_id: int) -> bool:
        rows = conn.execute(
            """
            select metadata_json from announcement_queue
            where status='ready' and (program_id=? or program_id is null)
            """,
            (program_id,),
        ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
                if int(metadata.get("track_id")) == track_id:
                    return True
            except Exception:
                continue
        return False

    def _weather_context(self) -> dict:
        now = datetime.now(timezone.utc)
        if (
            self.last_weather_context is not None
            and self.last_weather_at
            and now - self.last_weather_at < timedelta(minutes=self.settings.weather_interval_minutes)
        ):
            return self.last_weather_context
        self.last_weather_at = now
        self.last_weather_context = self.weather_provider.current_context().to_dict()
        return self.last_weather_context

    def _llm_runtime_status(self) -> dict | None:
        if self.settings.llm_provider.lower() != "ollama":
            return None
        now = datetime.now(timezone.utc)
        if (
            self.last_llm_runtime_status is not None
            and self.last_llm_runtime_at is not None
            and now - self.last_llm_runtime_at < timedelta(seconds=60)
        ):
            return self.last_llm_runtime_status
        self.last_llm_runtime_at = now
        self.last_llm_runtime_status = ollama_runtime_status(self.settings)
        return self.last_llm_runtime_status

    def _narrate(self, text: str, program_id: str) -> str:
        filename = f"dj_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.wav"
        output = self.settings.tts_path / filename
        program = current_program(self._database_runtime)
        voice = self.tts.provider_name
        clip_path = self._synthesize_qwen(
            text,
            output,
            announcement_label="track_intro",
            program_id=program_id,
        )
        with connect(self._database_runtime) as conn:
            conn.execute(
                "insert into generated_clips (clip_type, text, file_path, voice, program_id, created_at) values (?, ?, ?, ?, ?, ?)",
                ("dj_line", text, clip_path, voice, program_id, now_iso()),
            )
            conn.commit()
        return clip_path

    def _synthesize_qwen(
        self,
        text: str,
        output_path,
        *,
        announcement_label: AnnouncementLabel,
        program_id: str,
    ) -> str:
        """Build a station-bound request; generated text cannot select a voice."""
        policy = VoicePolicy.from_context(self.context)
        normalized_text, voice = policy.select(
            program_id=program_id,
            daypart=self._voice_daypart(),
            announcement_label=announcement_label,
            text=text,
        )
        request = SynthesisRequest(
            request_id=str(uuid4()),
            station_id=self.context.profile.station_id,
            language=self.context.profile.language,
            locale=self.context.profile.locale,
            normalized_text=normalized_text,
            announcement_label=announcement_label,
            voice=voice,
        )
        return self.tts.synthesize_request(request, str(output_path)).output_path

    def _voice_daypart(self) -> str:
        now = datetime.now(ZoneInfo(self.context.profile.timezone))
        if now.weekday() >= 5:
            return "weekend"
        if 5 <= now.hour < 12:
            return "morning"
        if 12 <= now.hour < 20:
            return "daytime"
        return "night"

    def _program_voice(self, program: dict | None) -> str | None:
        if not program:
            return None
        voice = " ".join(str(program.get("voice") or "").split())
        return voice or None

    def _record_play(self, track_id: int, program_id: str, duration_seconds: float | None) -> None:
        with connect(self._database_runtime) as conn:
            conn.execute(
                "insert into play_history (track_id, program_id, played_at, duration_seconds, source) values (?, ?, ?, ?, ?)",
                (track_id, program_id, now_iso(), duration_seconds, "local_file"),
            )
            conn.execute(
                "update tracks set last_played_at=?, play_count=play_count+1, updated_at=? where id=?",
                (now_iso(), now_iso(), track_id),
            )
            conn.commit()

    @staticmethod
    def _speech_duration_seconds(file_path: str) -> float:
        try:
            with wave.open(str(Path(file_path)), "rb") as clip:
                frames = clip.getnframes()
                frame_rate = clip.getframerate()
        except (OSError, EOFError, wave.Error) as exc:
            raise RuntimeError("prepared speech clip is not a readable WAV file") from exc
        if frames <= 0 or frame_rate <= 0:
            raise RuntimeError("prepared speech clip has no measurable airtime")
        return frames / float(frame_rate)

    def _queue_speech(self, title: str, file_path: str) -> QueueItem:
        item = QueueItem(
            "tts",
            title,
            file_path,
            duration_seconds=self._speech_duration_seconds(file_path),
        )
        self.playback.add(item)
        return item

    def _record_public_airtime(self, item: QueueItem, program_id: str | None) -> None:
        if item.item_type in {"tts", "speech", "announcement", "live"}:
            classification = "talking"
        elif item.item_type in {"track", "music", "imaging_instrumental"}:
            classification = "music"
        elif item.item_type == "silence":
            classification = "silence"
        else:
            classification = "unknown"
        with connect(self._database_runtime) as conn:
            conn.execute(
                """
                insert into station_public_events(
                    event_type, occurred_at, classification, duration_seconds,
                    program_id, title, metadata_json
                ) values ('play.completed', ?, ?, ?, ?, ?, '{}')
                """,
                (
                    now_iso(),
                    classification,
                    max(0.0, float(item.duration_seconds or 0.0)),
                    program_id,
                    item.title,
                ),
            )
            conn.commit()
