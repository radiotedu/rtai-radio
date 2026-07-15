# RadioTEDU Pop Rundown and Talk-Over Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build bilingual pop-first RadioTEDU playout with natural filler, jazz/classical-only sourced commentary, durable four-hour rundowns, 60-minute rendered readiness, six-hour local fallback, and best-effort ducked talk-over.

**Architecture:** Add small editorial, research, rundown, fallback, and talk-over units around the existing station-local SQLite runtime. The orchestrator maintains durable time coverage in the background; Liquidsoap consumes only ready local artifacts and switches to a validated fallback playlist without depending on AI or the website.

**Tech Stack:** Python 3.14, SQLite, FastAPI, Qwen TTS client, SearXNG, FFmpeg, Liquidsoap, pytest, React/TypeScript/Vitest.

## Global Constraints

- Display/API identity remains `RadioTEDU`; every speech path pronounces it as `Radio TED U` where “TED” rhymes with “bed” and “U” is “you.”
- Music is approximately 70–80 percent pop and compatible R&B, dance, indie-pop, and soft rock; curated jazz and classical remain occasional.
- Pop uses no song web research; pop speech contains only safe localized filler plus catalog title and artist.
- Sourced commentary is limited to catalog tracks explicitly classified as jazz or classical.
- EN and FR retain separate databases, queues, media, caches, fallback playlists, health, and orchestrators.
- Each station targets four hours planned, 60 minutes rendered, refill below two hours, and six hours validated fallback.
- Talk-over prefers curated cues, accepts estimated cues at confidence `>= 0.65`, and may use a default opening window capped at six seconds.
- Talk-over ducks music by 10–12 dB; one-to-two-second lyric overlap is acceptable, and unknown-cue default windows may overlap more.
- No purchasing, downloading, contacting, messaging, voting, social posting, remote control, or production deployment.
- Never commit Icecast source credentials, HMAC secrets, generated evidence containing secrets, or local voice-reference media.
- Preserve `main` and its existing untracked `release/` directory.

## File Structure

- Create `backend/editorial.py`: genre normalization, research gating, localized rotating pop liners.
- Create `backend/editorial_research.py`: exact-identity SearXNG fact-card extraction and persistence interface.
- Create `backend/rundown.py`: duration-based planning, coverage, rendering state, claims, and overnight continuity.
- Create `backend/fallback_playlist.py`: validated six-hour station-local fallback construction.
- Create `backend/audio/talkover_renderer.py`: auditable FFmpeg composite rendering and command generation.
- Modify `backend/database.py`: additive migrations 7 and 8 plus broadened program descriptions and explicit overnight windows.
- Modify `backend/config.py`: exact duration and ducking settings.
- Modify `backend/tts/voice_policy.py`: speech-only brand pronunciation.
- Modify `backend/audio/models.py` and `backend/audio/segue_policy.py`: relaxed cue threshold and default-window decisions.
- Modify `backend/radio_agent.py`, `backend/orchestrator.py`, and `backend/playback.py`: use the durable rundown without blocking live playout.
- Modify `backend/liquidsoap.py` and station templates only where needed to consume ready composites and validated fallback.
- Modify `backend/app.py` status/observability without exposing control through the public app.
- Modify `handoff/broadcast-server/prompt.md`, `handoff/web-server/prompt.md`, and active runbooks.
- Add focused tests under `tests/backend/`; update existing contract tests rather than duplicating them.

---

### Task 1: Speech Branding, Pop Liners, and Program Identity

**Files:**
- Create: `backend/editorial.py`
- Modify: `backend/tts/voice_policy.py:12-18`
- Modify: `backend/database.py:80-137`
- Test: `tests/backend/test_editorial_policy.py`
- Test: `tests/backend/test_tts_voice_policy.py`
- Test: `tests/backend/test_core_behaviour.py`

**Interfaces:**
- Produces: `normalize_genre(value: object) -> str`
- Produces: `research_allowed(genre: object) -> bool`
- Produces: `PopLiner` and `build_pop_liner(language, daypart, title, artist, recent_template_ids=()) -> PopLiner`
- Preserves: `normalize_broadcast_text(text, language, locale) -> str`

- [ ] **Step 1: Write failing editorial and pronunciation tests**

```python
from backend.editorial import build_pop_liner, research_allowed
from backend.tts.voice_policy import normalize_broadcast_text


def test_speech_brand_is_pronounced_radio_ted_u_in_both_languages():
    assert normalize_broadcast_text("You are listening to RadioTEDU.", "en", "en-US") == (
        "You are listening to Radio TED U."
    )
    assert normalize_broadcast_text("Vous écoutez RadioTEDU.", "fr", "fr-FR") == (
        "Vous écoutez Radio TED U."
    )


def test_pop_liners_are_localized_safe_and_rotate():
    first = build_pop_liner("en", "daytime", "Levitating", "Dua Lipa")
    second = build_pop_liner(
        "en", "daytime", "Levitating", "Dua Lipa", recent_template_ids=(first.template_id,)
    )
    french = build_pop_liner("fr", "daytime", "Levitating", "Dua Lipa")
    assert "Radio TED U" in first.text
    assert "Levitating" in first.text and "Dua Lipa" in first.text
    assert second.template_id != first.template_id
    assert "Vous écoutez Radio TED U" in french.text


def test_only_jazz_and_classical_allow_research():
    assert research_allowed("jazz") is True
    assert research_allowed("Classical") is True
    assert research_allowed("pop") is False
    assert research_allowed("classic rock") is False
    assert research_allowed(None) is False
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python -m pytest -q tests/backend/test_editorial_policy.py tests/backend/test_tts_voice_policy.py
```

Expected: FAIL because `backend.editorial` does not exist and speech normalization still returns `RadioTEDU`.

- [ ] **Step 3: Implement the minimal editorial policy**

```python
# backend/editorial.py
from dataclasses import dataclass

RESEARCH_GENRES = frozenset({"jazz", "classical"})

POP_TEMPLATES = {
    "en": {
        "morning": (
            "Good morning from Radio TED U—here's {title} by {artist}.",
            "Have a bright morning with Radio TED U. Here's {title} by {artist}.",
        ),
        "daytime": (
            "You're with Radio TED U. Have a great afternoon—here's {title} by {artist}.",
            "Stay with Radio TED U for more music. Here's {title} by {artist}.",
        ),
        "night": (
            "You're listening to Radio TED U tonight. Here's {title} by {artist}.",
            "Stay with Radio TED U—here's {title} by {artist}.",
        ),
        "weekend": (
            "Enjoy your weekend with Radio TED U—here's {title} by {artist}.",
            "Radio TED U keeps your weekend moving with {title} by {artist}.",
        ),
    },
    "fr": {
        "morning": (
            "Bonjour, vous écoutez Radio TED U—voici {title} de {artist}.",
            "Passez une belle matinée avec Radio TED U. Voici {title} de {artist}.",
        ),
        "daytime": (
            "Vous écoutez Radio TED U. Passez une excellente journée—voici {title} de {artist}.",
            "Restez avec Radio TED U pour plus de musique. Voici {title} de {artist}.",
        ),
        "night": (
            "Vous passez la soirée avec Radio TED U—voici {title} de {artist}.",
            "Restez avec Radio TED U—voici {title} de {artist}.",
        ),
        "weekend": (
            "Bon week-end avec Radio TED U—voici {title} de {artist}.",
            "Radio TED U accompagne votre week-end avec {title} de {artist}.",
        ),
    },
}


@dataclass(frozen=True, slots=True)
class PopLiner:
    template_id: str
    text: str


def normalize_genre(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def research_allowed(genre: object) -> bool:
    return normalize_genre(genre) in RESEARCH_GENRES


def build_pop_liner(language, daypart, title, artist, recent_template_ids=()):
    templates = POP_TEMPLATES[language][daypart]
    recent = set(recent_template_ids)
    index = next((i for i in range(len(templates)) if f"{language}:{daypart}:{i}" not in recent), 0)
    template_id = f"{language}:{daypart}:{index}"
    return PopLiner(template_id, templates[index].format(title=title, artist=artist))
```

In `normalize_broadcast_text`, replace brand tokens after whitespace normalization:

```python
normalized = re.sub(r"\bRadioTEDU\b", "Radio TED U", normalized, flags=re.IGNORECASE)
```

Broaden the four program descriptions/vibes to pop-first copy and add explicit weekday/weekend overnight program rows with non-overlapping day/time windows.

- [ ] **Step 4: Run focused and seed tests and verify GREEN**

Run:

```bash
python -m pytest -q tests/backend/test_editorial_policy.py tests/backend/test_tts_voice_policy.py tests/backend/test_core_behaviour.py
```

Expected: all selected tests PASS; stored/display identity remains `RadioTEDU` while synthesized text uses `Radio TED U`.

- [ ] **Step 5: Commit Task 1**

```bash
git add backend/editorial.py backend/tts/voice_policy.py backend/database.py tests/backend/test_editorial_policy.py tests/backend/test_tts_voice_policy.py tests/backend/test_core_behaviour.py
git commit -m "feat: add bilingual pop editorial policy"
```

### Task 2: Jazz/Classical Research and Durable Fact Cards

**Files:**
- Create: `backend/editorial_research.py`
- Modify: `backend/database.py:870-932`
- Modify: `backend/radio_agent.py:487-516,608-625`
- Test: `tests/backend/test_editorial_research.py`
- Test: `tests/backend/test_database_migrations.py`
- Test: `tests/backend/test_full_autonomy_runtime.py`

**Interfaces:**
- Consumes: `research_allowed(genre)` from Task 1
- Produces: `FactCard`
- Produces: `EditorialResearchService.research(track: dict, language: str) -> FactCard | None`
- Produces: migration 7 table `editorial_fact_cards`

- [ ] **Step 1: Write failing research-gating tests**

```python
def test_pop_never_calls_search_provider():
    provider = RecordingProvider()
    service = EditorialResearchService(provider)
    assert service.research({"id": 1, "title": "Levitating", "artist": "Dua Lipa", "genre": "pop"}, "en") is None
    assert provider.queries == []


def test_jazz_requires_title_and_artist_identity_and_keeps_provenance():
    provider = RecordingProvider(
        results=[
            SearchResult(
                title="Blue in Green by Miles Davis",
                url="https://music.example/blue-in-green",
                snippet="Miles Davis recorded Blue in Green for the album Kind of Blue.",
                source="searxng",
            )
        ]
    )
    card = EditorialResearchService(provider).research(
        {"id": 2, "title": "Blue in Green", "artist": "Miles Davis", "genre": "jazz"}, "en"
    )
    assert card is not None
    assert card.track_id == 2
    assert card.url == "https://music.example/blue-in-green"
    assert card.source == "searxng"


def test_artist_only_collision_is_rejected():
    provider = RecordingProvider(
        results=[SearchResult("Miles Davis biography", "https://music.example/miles", "Miles Davis was a trumpeter.", "searxng")]
    )
    assert EditorialResearchService(provider).research(
        {"id": 2, "title": "Blue in Green", "artist": "Miles Davis", "genre": "jazz"}, "en"
    ) is None
```

- [ ] **Step 2: Run research tests and verify RED**

Run:

```bash
python -m pytest -q tests/backend/test_editorial_research.py tests/backend/test_database_migrations.py
```

Expected: FAIL because `EditorialResearchService` and migration 7 do not exist.

- [ ] **Step 3: Implement strict fact-card research and migration 7**

```python
@dataclass(frozen=True, slots=True)
class FactCard:
    track_id: int
    language: str
    fact: str
    url: str
    source: str
    retrieved_at: str
    match_evidence: str


class EditorialResearchService:
    def __init__(self, provider):
        self.provider = provider

    def research(self, track: dict, language: str) -> FactCard | None:
        if not research_allowed(track.get("genre")):
            return None
        title = clean_identity(track.get("title"))
        artist = clean_identity(track.get("artist"))
        if not title or not artist:
            return None
        query = f'"{artist}" "{title}" music'
        for result in self.provider.search(query, limit=5):
            if result.url.startswith(("http://", "https://")) and result_matches(result, title, artist):
                fact = sanitize_fact(result.snippet, max_words=28)
                if fact:
                    return FactCard(int(track["id"]), language, fact, result.url, result.source, now_iso(), f"title+artist:{title}|{artist}")
        return None
```

Add `EDITORIAL_FACT_CARD_SCHEMA` and migration 7 with unique `(track_id, language, url, fact_hash)`, source URL, sanitized fact, match evidence, retrieval time, and creation time. Replace `_song_context_announcement` with the service; pop bypasses the provider entirely, and failed research falls through to Task 1 filler/catalog speech.

- [ ] **Step 4: Run research, migration, and autonomy tests and verify GREEN**

Run:

```bash
python -m pytest -q tests/backend/test_editorial_research.py tests/backend/test_database_migrations.py tests/backend/test_full_autonomy_runtime.py
```

Expected: all selected tests PASS; pop issues zero search calls and only jazz/classical persist fact cards.

- [ ] **Step 5: Commit Task 2**

```bash
git add backend/editorial_research.py backend/database.py backend/radio_agent.py tests/backend/test_editorial_research.py tests/backend/test_database_migrations.py tests/backend/test_full_autonomy_runtime.py
git commit -m "feat: gate sourced commentary to curated genres"
```

### Task 3: Durable Four-Hour Rundown and Coverage Accounting

**Files:**
- Create: `backend/rundown.py`
- Modify: `backend/database.py:870-932`
- Modify: `backend/config.py:60-100,140-170`
- Test: `tests/backend/test_rundown.py`
- Test: `tests/backend/test_database_migrations.py`
- Test: `tests/backend/test_station_isolation.py`

**Interfaces:**
- Produces: `CoveragePolicy(planned_seconds=14400, rendered_seconds=3600, refill_seconds=7200, fallback_seconds=21600)`
- Produces: `CoverageStatus`
- Produces: `RundownPlanner.maintain(now: datetime) -> CoverageStatus`
- Produces: `RundownPlanner.claim_next_ready(now: datetime) -> RundownItem | None`
- Produces: migration 8 tables `rundown_items`, `rundown_transitions`, and `liner_template_usage`

- [ ] **Step 1: Write failing coverage and overnight tests**

```python
def test_coverage_policy_uses_exact_approved_windows():
    policy = CoveragePolicy()
    assert policy.planned_seconds == 4 * 60 * 60
    assert policy.rendered_seconds == 60 * 60
    assert policy.refill_seconds == 2 * 60 * 60
    assert policy.fallback_seconds == 6 * 60 * 60


def test_planner_builds_four_hours_and_refills_below_two(tmp_path):
    planner = seeded_planner(tmp_path, track_durations=[1800] * 10)
    first = planner.maintain(NOW)
    assert first.planned_seconds >= 14400
    assert first.needs_refill is False
    consume_until(planner, remaining_seconds=7199)
    assert planner.coverage(NOW).needs_refill is True


def test_every_week_minute_has_a_named_or_overnight_program(tmp_path):
    settings = seeded_settings(tmp_path)
    for day_offset in range(7):
        for hour in range(24):
            program = current_program(settings, MONDAY + timedelta(days=day_offset, hours=hour))
            assert program["id"]
            assert program["name"]
```

- [ ] **Step 2: Run rundown tests and verify RED**

Run:

```bash
python -m pytest -q tests/backend/test_rundown.py tests/backend/test_database_migrations.py tests/backend/test_station_isolation.py
```

Expected: FAIL because migration 8 and `RundownPlanner` do not exist and overnight gaps fall through to the first program.

- [ ] **Step 3: Implement migration 8 and the planner**

Core models:

```python
@dataclass(frozen=True, slots=True)
class CoveragePolicy:
    planned_seconds: int = 14_400
    rendered_seconds: int = 3_600
    refill_seconds: int = 7_200
    fallback_seconds: int = 21_600


@dataclass(frozen=True, slots=True)
class CoverageStatus:
    planned_seconds: int
    rendered_seconds: int
    fallback_seconds: int
    needs_refill: bool
    air_ready: bool


class RundownPlanner:
    def coverage(self, now):
        planned = self._sum_remaining(("planned", "researching", "rendering", "ready", "queued"), now)
        rendered = self._sum_remaining(("ready", "queued"), now)
        return CoverageStatus(planned, rendered, self._fallback_seconds(), planned < self.policy.refill_seconds, planned >= self.policy.planned_seconds and rendered >= self.policy.rendered_seconds and self._fallback_seconds() >= self.policy.fallback_seconds)

    def maintain(self, now):
        status = self.coverage(now)
        if status.planned_seconds < self.policy.planned_seconds:
            self._append_valid_tracks_until(now, self.policy.planned_seconds)
        return self.coverage(now)
```

Migration 8 stores planned/actual times, measured duration, item type, track/program IDs, state, source path, rendered path, editorial kind, fact-card ID, template ID, transition ID, attempts, error code, and timestamps. Use a partial unique constraint preventing two active rows for the same station-local track and planned start. Settings expose exact seconds with environment names `RUNDOWN_PLANNED_SECONDS`, `RUNDOWN_RENDERED_SECONDS`, `RUNDOWN_REFILL_SECONDS`, and `FALLBACK_COVERAGE_SECONDS`.

- [ ] **Step 4: Run rundown, migration, and isolation tests and verify GREEN**

Run:

```bash
python -m pytest -q tests/backend/test_rundown.py tests/backend/test_database_migrations.py tests/backend/test_station_isolation.py
```

Expected: all selected tests PASS; EN/FR planners write only their injected station databases.

- [ ] **Step 5: Commit Task 3**

```bash
git add backend/rundown.py backend/database.py backend/config.py tests/backend/test_rundown.py tests/backend/test_database_migrations.py tests/backend/test_station_isolation.py
git commit -m "feat: add durable time-based station rundowns"
```

### Task 4: Six-Hour Validated Fallback

**Files:**
- Create: `backend/fallback_playlist.py`
- Modify: `backend/liquidsoap.py:165-205,382-410`
- Modify: `backend/app.py:800-860`
- Test: `tests/backend/test_fallback_playlist.py`
- Test: `tests/backend/test_dual_station_runtime.py`
- Test: `tests/backend/test_core_behaviour.py`

**Interfaces:**
- Consumes: `CoveragePolicy.fallback_seconds`
- Produces: `FallbackStatus`
- Produces: `FallbackPlaylistBuilder.rebuild() -> FallbackStatus`
- Produces: atomic station-local `fallback.m3u`

- [ ] **Step 1: Write failing fallback-duration tests**

```python
def test_fallback_requires_six_hours_of_valid_local_audio(tmp_path):
    builder = fallback_builder(tmp_path, durations=[3600] * 5)
    short = builder.rebuild()
    assert short.coverage_seconds == 5 * 3600
    assert short.air_ready is False
    add_track(builder, duration_seconds=3600)
    ready = builder.rebuild()
    assert ready.coverage_seconds >= 6 * 3600
    assert ready.air_ready is True


def test_fallback_contains_only_existing_station_local_files(tmp_path):
    builder = fallback_builder(tmp_path, durations=[3600] * 6, include_missing=True)
    status = builder.rebuild()
    lines = status.playlist_path.read_text(encoding="utf-8").splitlines()
    assert lines
    assert all(Path(line).is_file() for line in lines)
    assert not any("radiotedu-fr" in line for line in lines if builder.station_id == "radiotedu-en")
```

- [ ] **Step 2: Run fallback tests and verify RED**

Run:

```bash
python -m pytest -q tests/backend/test_fallback_playlist.py tests/backend/test_dual_station_runtime.py
```

Expected: FAIL because the builder does not exist and Liquidsoap currently accepts an empty fallback file.

- [ ] **Step 3: Implement atomic fallback construction and readiness**

```python
@dataclass(frozen=True, slots=True)
class FallbackStatus:
    playlist_path: Path
    track_count: int
    coverage_seconds: int
    air_ready: bool


class FallbackPlaylistBuilder:
    def rebuild(self) -> FallbackStatus:
        selected, seconds = [], 0
        for track in self._eligible_tracks():
            path = Path(track["file_path"]).resolve()
            if not path.is_file() or float(track["duration_seconds"] or 0) <= 0:
                continue
            selected.append(path)
            seconds += int(float(track["duration_seconds"]))
            if seconds >= self.required_seconds:
                break
        temporary = self.playlist_path.with_suffix(".m3u.tmp")
        temporary.write_text("".join(f"{path.as_posix()}\n" for path in selected), encoding="utf-8")
        temporary.replace(self.playlist_path)
        return FallbackStatus(self.playlist_path, len(selected), seconds, seconds >= self.required_seconds)
```

Expose fallback duration and readiness in operator-only observability. `render_liquidsoap_config` may create the file but cannot call it ready until validated coverage reaches 21,600 seconds.

- [ ] **Step 4: Run fallback, Liquidsoap, and core tests and verify GREEN**

Run:

```bash
python -m pytest -q tests/backend/test_fallback_playlist.py tests/backend/test_dual_station_runtime.py tests/backend/test_core_behaviour.py
```

Expected: all selected tests PASS; empty and five-hour fallback playlists are explicitly unready.

- [ ] **Step 5: Commit Task 4**

```bash
git add backend/fallback_playlist.py backend/liquidsoap.py backend/app.py tests/backend/test_fallback_playlist.py tests/backend/test_dual_station_runtime.py tests/backend/test_core_behaviour.py
git commit -m "feat: require six-hour local fallback coverage"
```

### Task 5: Relaxed Talk-Over Decisions and Offline Composite Rendering

**Files:**
- Create: `backend/audio/talkover_renderer.py`
- Modify: `backend/audio/models.py`
- Modify: `backend/audio/segue_policy.py:47-201`
- Modify: `backend/playback.py:14-73`
- Test: `tests/backend/test_segue_policy.py`
- Test: `tests/backend/test_talkover_renderer.py`

**Interfaces:**
- Produces: `CueSource = curated | estimated | default | none`
- Produces: extended `SegueDecision` with `cue_source`, `duck_db`, and `estimated_lyric_overlap_seconds`
- Produces: `TalkOverRenderer.render(speech_path, track_path, output_path, decision) -> Path`
- Preserves: sequential speech+music fallback when rendering fails

- [ ] **Step 1: Write failing relaxed-policy and renderer-command tests**

```python
def test_estimated_cue_at_point_sixty_five_allows_talk_over():
    incoming = music(Genre.POP, intro_end_seconds=4.0, intro_confidence=0.65, cue_source="estimated")
    decision = SeguePolicy().choose(None, speech(4.5), incoming)
    assert decision.kind is SegueKind.TALK_OVER
    assert decision.cue_source == "estimated"
    assert decision.duck_db in {-10.0, -11.0, -12.0}
    assert decision.speaks_over_vocals is True


def test_unknown_cue_uses_six_second_default_window():
    incoming = music(Genre.POP, intro_end_seconds=None, intro_confidence=None, cue_source="none")
    decision = SeguePolicy().choose(None, speech(5.0), incoming)
    assert decision.kind is SegueKind.TALK_OVER
    assert decision.cue_source == "default"
    assert decision.speech_end_seconds <= 6.0


def test_ffmpeg_command_ducks_music_and_preserves_track_duration(tmp_path):
    command = build_talkover_command(
        speech_path=tmp_path / "speech.wav",
        track_path=tmp_path / "track.wav",
        output_path=tmp_path / "mixed.wav",
        speech_start_seconds=0.25,
        speech_end_seconds=5.25,
        duck_db=-11.0,
    )
    rendered = " ".join(command)
    assert "volume" in rendered and "-11.0dB" in rendered
    assert "adelay=250" in rendered
    assert "amix=inputs=2:duration=first" in rendered
```

- [ ] **Step 2: Run talk-over tests and verify RED**

Run:

```bash
python -m pytest -q tests/backend/test_segue_policy.py tests/backend/test_talkover_renderer.py
```

Expected: FAIL because confidence 0.65 is below the current threshold, unknown cues are sequential, and no renderer exists.

- [ ] **Step 3: Implement relaxed decisions and FFmpeg rendering**

Update the audio policy to `talk_over_minimum_intro_confidence=0.65`. Add cue-source and ducking fields without changing music-to-music transition presets. Curated and estimated decisions position speech near the estimated boundary and permit a bounded lyric overlap; absent cues use `speech_start_seconds=0.25` and `speech_end_seconds=min(6.0, 0.25 + speech.duration_seconds)` unless immediate-loud-vocal evidence is true.

```python
def build_talkover_command(*, speech_path, track_path, output_path, speech_start_seconds, speech_end_seconds, duck_db):
    delay_ms = round(speech_start_seconds * 1000)
    gain = f"{duck_db:.1f}dB"
    filter_graph = (
        f"[0:a]volume='{gain}':enable='between(t,{speech_start_seconds:.3f},{speech_end_seconds:.3f})'[music];"
        f"[1:a]adelay={delay_ms}|{delay_ms}[voice];"
        "[music][voice]amix=inputs=2:duration=first:dropout_transition=0[mix]"
    )
    return ["ffmpeg", "-y", "-hide_banner", "-nostats", "-i", str(track_path), "-i", str(speech_path), "-filter_complex", filter_graph, "-map", "[mix]", str(output_path)]
```

`TalkOverRenderer.render` runs the command without a shell, rejects nonzero exit, verifies the output with the existing audio analyzer, and returns the validated composite. The composite becomes one ready playout item, preserving the full track duration. Any error returns control to sequential speech+track playout.

- [ ] **Step 4: Run segue, renderer, and playback tests and verify GREEN**

Run:

```bash
python -m pytest -q tests/backend/test_segue_policy.py tests/backend/test_talkover_renderer.py tests/backend/test_core_behaviour.py
```

Expected: all selected tests PASS; talk-over decisions are auditable and renderer failure remains sequential.

- [ ] **Step 5: Commit Task 5**

```bash
git add backend/audio/models.py backend/audio/segue_policy.py backend/audio/talkover_renderer.py backend/playback.py tests/backend/test_segue_policy.py tests/backend/test_talkover_renderer.py tests/backend/test_core_behaviour.py
git commit -m "feat: render best-effort radio talk-over"
```

### Task 6: Integrate Rundown Maintenance with the Station Orchestrator

**Files:**
- Modify: `backend/radio_agent.py:23-225,282-362`
- Modify: `backend/orchestrator.py:154-197,520-530`
- Modify: `backend/station_runtime.py`
- Modify: `backend/app.py:350-430,800-860`
- Test: `tests/backend/test_rundown_runtime.py`
- Test: `tests/backend/test_full_autonomy_runtime.py`
- Test: `tests/backend/test_dual_station_runtime.py`
- Test: `tests/backend/test_station_isolation.py`

**Interfaces:**
- Consumes: `RundownPlanner`, `EditorialResearchService`, `FallbackPlaylistBuilder`, `TalkOverRenderer`
- Produces: `RadioAgent.maintain_rundown(max_render_items: int = 1) -> CoverageStatus`
- Produces: compatibility `announcement_readiness()` fields derived from duration coverage
- Produces: operator-only coverage observability

- [ ] **Step 1: Write failing runtime and outage tests**

```python
def test_orchestrator_maintains_one_render_item_per_tick_without_blocking_ready_playout(runtime):
    runtime.planner.seed_ready_minutes(60)
    runtime.planner.seed_planned_hours(4)
    result = runtime.orchestrator.tick()
    assert result["played"] is True
    assert runtime.renderer.calls <= 1
    assert result["coverage"]["rendered_seconds"] >= 3600


def test_search_llm_and_qwen_failure_continue_music(runtime):
    runtime.search.fail = True
    runtime.llm.fail = True
    runtime.tts.fail = True
    runtime.planner.seed_ready_music_only(hours=4)
    result = runtime.orchestrator.tick()
    assert result["played"] is True
    assert runtime.playback.played[-1].item_type == "track"


def test_one_station_rundown_failure_does_not_stop_other_station(dual_runtime):
    dual_runtime.en.planner.fail_next = True
    dual_runtime.supervisor.tick_once()
    assert dual_runtime.fr.playback.running is True
    assert dual_runtime.en.restart_requested is True
```

- [ ] **Step 2: Run runtime tests and verify RED**

Run:

```bash
python -m pytest -q tests/backend/test_rundown_runtime.py tests/backend/test_full_autonomy_runtime.py tests/backend/test_dual_station_runtime.py tests/backend/test_station_isolation.py
```

Expected: FAIL because the runtime still uses count-based announcement readiness and has no coverage integration.

- [ ] **Step 3: Wire duration coverage into the runtime**

Construct all services from the injected `StationContext`. `AutonomousOrchestrator.tick` performs bounded work in this order:

```python
def tick(self):
    coverage = self.agent.maintain_rundown(max_render_items=1)
    if not self.agent.playback.queue and self.agent.playback.now_playing is None:
        result = self.agent.queue_next_ready_rundown_item()
    else:
        result = {"started": False, "reason": "playout_busy"}
    return {"played": bool(result.get("started")), "coverage": asdict(coverage)}
```

`queue_next_ready_rundown_item` claims one ready row transactionally. It queues a talk-over composite when available, otherwise sequential speech+track, otherwise music-only. It records the selected editorial mode, transition decision, and real airtime. The existing `ensure_announcement_prebuffer` remains as a narrow compatibility adapter but delegates to `maintain_rundown`; readiness fields add `planned_seconds`, `rendered_seconds`, `fallback_seconds`, and `air_ready`.

- [ ] **Step 4: Run all station runtime tests and verify GREEN**

Run:

```bash
python -m pytest -q tests/backend/test_rundown_runtime.py tests/backend/test_full_autonomy_runtime.py tests/backend/test_dual_station_runtime.py tests/backend/test_station_isolation.py tests/backend/test_public_sync_service.py
```

Expected: all selected tests PASS; each station owns its services, and public sync never controls playout.

- [ ] **Step 5: Commit Task 6**

```bash
git add backend/radio_agent.py backend/orchestrator.py backend/station_runtime.py backend/app.py tests/backend/test_rundown_runtime.py tests/backend/test_full_autonomy_runtime.py tests/backend/test_dual_station_runtime.py tests/backend/test_station_isolation.py
git commit -m "feat: drive playout from durable coverage"
```

### Task 7: Update Handoff Prompts, Runbooks, and Contract Tests

**Files:**
- Modify: `handoff/broadcast-server/prompt.md`
- Modify: `handoff/web-server/prompt.md`
- Modify: `docs/BROADCAST_COMPUTER_RUNBOOK.md`
- Modify: `docs/WEBSITE_SERVER_RUNBOOK.md`
- Modify: `README.md`
- Test: `tests/backend/test_desktop_packaging.py`
- Test: `tests/backend/test_deployment_contracts.py`

**Interfaces:**
- Preserves exactly two canonical `handoff/*/prompt.md` files
- Preserves status-only web API and no public controls
- Documents builder-only GitHub handoff and staging stop condition

- [ ] **Step 1: Write failing prompt-contract tests**

```python
def test_broadcast_prompt_contains_time_coverage_editorial_and_talkover_contracts():
    prompt = (ROOT / "handoff/broadcast-server/prompt.md").read_text(encoding="utf-8")
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


def test_web_prompt_remains_status_only_and_two_prompts_are_canonical():
    prompts = sorted(ROOT.glob("handoff/*/prompt.md"))
    assert len(prompts) == 2
    rendered = " ".join(path.read_text(encoding="utf-8") for path in prompts).casefold()
    for forbidden in ("post /v1/radio/control", "buy now", "send message", "cast vote"):
        assert forbidden not in rendered
```

- [ ] **Step 2: Run handoff tests and verify RED**

Run:

```bash
python -m pytest -q tests/backend/test_desktop_packaging.py tests/backend/test_deployment_contracts.py
```

Expected: FAIL because the prompts do not contain the new coverage, editorial, pronunciation, or talk-over requirements.

- [ ] **Step 3: Update both prompts and active documentation**

The broadcasting prompt must instruct target Codex to provision only supplied local media, commission approved local EN/FR voice references, validate four/60/two/six-hour thresholds, verify pop research is disabled, qualify jazz/classical sources, exercise talk-over and sequential fallback, run an outage soak, and stop before production. The web prompt must retain the canonical API, status-only UI, no control surface, and sanitized EN/FR state; it must not receive rundown internals or secrets.

Document that `RadioTEDU` is the display brand and `Radio TED U` is speech-only. Document that authentic French speech remains blocked until approved local references and Qwen service health pass on the broadcasting computer.

- [ ] **Step 4: Run handoff and public-surface tests and verify GREEN**

Run:

```bash
python -m pytest -q tests/backend/test_desktop_packaging.py tests/backend/test_deployment_contracts.py tests/backend/test_public_app.py tests/backend/test_platform_api.py
```

Expected: all selected tests PASS; exactly two prompts remain and no public control capability appears.

- [ ] **Step 5: Commit Task 7**

```bash
git add handoff/broadcast-server/prompt.md handoff/web-server/prompt.md docs/BROADCAST_COMPUTER_RUNBOOK.md docs/WEBSITE_SERVER_RUNBOOK.md README.md tests/backend/test_desktop_packaging.py tests/backend/test_deployment_contracts.py
git commit -m "docs: update bilingual rundown machine handoff"
```

### Task 8: Full Verification, Review, and GitHub Publication

**Files:**
- Modify only files required by concrete verification findings
- Verify: entire repository

**Interfaces:**
- Produces a clean `feature/dual-station-radiotedu` branch
- Pushes the verified feature branch to configured GitHub `origin`
- Does not merge, deploy, or touch main `release/`

- [ ] **Step 1: Run the complete backend suite**

Run:

```bash
python -m pytest -q
```

Expected: all tests PASS. Deprecation warnings may remain only when already present in the baseline; no new warning category is accepted.

- [ ] **Step 2: Run frontend tests and production build**

Run:

```bash
cd frontend
npm test
npm run build
cd ..
```

Expected: all Vitest tests PASS and Vite production build succeeds.

- [ ] **Step 3: Run compile and static contract scans**

Run:

```bash
python -m compileall -q backend scripts
git diff --check
git status --short
```

Also programmatically verify: exactly two canonical prompt files; exact supplied credentials occur zero times in tracked content; no `/spark`, `/radiotedu-*` mount assignment, `%mp3`, `hackme`, public playout control, pop research call path, or generated Liquidsoap mutation remains.

Expected: every scan passes and only intended tracked changes exist before the final commit.

- [ ] **Step 4: Request focused independent review and fix only concrete blockers**

Review base `bf8318f` through the implementation head for durability, station isolation, factual grounding, audio continuity, secret handling, and prompt accuracy. For every Critical or Important finding, first add a failing regression test, verify RED, implement the minimal fix, verify GREEN, and commit.

- [ ] **Step 5: Confirm remote and push the feature branch**

Run:

```bash
git remote get-url origin
git status --short
git push -u origin feature/dual-station-radiotedu
```

Expected: `origin` resolves to the authorized GitHub repository, the worktree is clean, and GitHub reports the feature branch updated. If authentication or remote ownership cannot be verified, stop without changing another remote and report the exact blocker.

- [ ] **Step 6: Final handoff**

Report the pushed branch and commit, full verification results, no-deployment status, credential rotation requirement, and clickable absolute paths to:

- `handoff/broadcast-server/prompt.md`
- `handoff/web-server/prompt.md`

Do not merge to `main`, open production traffic, or deploy either target computer.
