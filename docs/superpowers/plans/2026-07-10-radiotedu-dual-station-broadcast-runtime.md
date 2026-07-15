# RadioTEDU Dual-Station Broadcast Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run English and French RadioTEDU as two independently supervised broadcast processes with station-scoped agents, fair shared-model access, separate Liquidsoap/Icecast boundaries, and Qwen-only music-continuity recovery.

**Architecture:** Each process builds one immutable `StationContext`, one `RadioAgent`, one `AutonomousOrchestrator`, one snapshot pusher, and one stream supervisor inside a `StationRuntime`. The two processes may call shared localhost Ollama and Qwen services through a fair scheduler, but all writable state, queues, stream processes, credentials, health, and restart decisions remain station-scoped.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, dataclasses, threading, Liquidsoap, Icecast, pytest/unittest, Qwen3-TTS.

## Global Constraints

- The only station IDs are `radiotedu-en` and `radiotedu-fr`.
- Consume `StationProfile`, `PublicProfile`, `AudioProfile`, and `RuntimeProfile` from `backend.stations.models` exactly as frozen by the approved design.
- Consume `StationContext` and `build_station_context(settings, profile)` from `backend.stations.context`.
- A resolved profile is immutable after startup; missing or mismatched station context fails before filesystem, database, queue, cache, log, stream, or network side effects.
- Runtime uses real `Settings` for `build_station_context` and keeps `create_app` station_context keyword.
- `create_station_runtime` signature includes stream_supervisor consistently.
- The runtime plan owns and tests `scripts/run_broadcast_computer.py` with exact command:
  `python -m scripts.run_broadcast_computer --profiles-dir config/stations --station radiotedu-en --station radiotedu-fr`
- `radiotedu-en` uses English (`en`, `en-US`), `/radiotedu-en`, `radiotedu-en-voices-v1`, and `RADIOTEDU_EN_SNAPSHOT_SECRET`.
- `radiotedu-fr` uses French (`fr`, `fr-FR`), `/radiotedu-fr`, `radiotedu-fr-voices-v1`, and `RADIOTEDU_FR_SNAPSHOT_SECRET`.
- Both stations use `Europe/Istanbul`, `-16 LUFS`, `-1 dBTP`, and a minimum Qwen announcement buffer of five.
- Qwen is the sole speech engine. SAPI, Piper, cloud TTS, dummy audio, and silence represented as speech are prohibited.
- A Qwen outage moves only speech into degraded mode; already-live stations continue music and restore speech only after real synthesis health succeeds and the five-clip buffer is rebuilt.
- The two station processes have separate SQLite databases, writable roots, announcement queues, caches, logs, Liquidsoap processes, Icecast instances, ports, mounts, source credentials, PID files, and health state.
- One `FairGenerationScheduler` runs inside the persistent shared Qwen/Ollama service boundary; both station processes use clients for that same scheduler. Creating one independent scheduler per station is prohibited because it cannot enforce cross-process fairness.
- `backend.app:app` is the private broadcast-computer API for one selected station. It is never packaged as the public webserver. The runtime plan owns and tests `scripts/run_broadcast_computer.py` with exact command `python -m scripts.run_broadcast_computer --profiles-dir config/stations --station radiotedu-en --station radiotedu-fr`.
- Exactly one snapshot pusher belongs to each `StationRuntime`. `RadioAgent`, `AutonomousOrchestrator`, and FastAPI startup hooks must not construct additional pushers.
- Preserve current English schedules, history, artwork, and operator behavior through the `radiotedu-en` compatibility context.
- Every implementation task is assigned to one worker with exclusive ownership of its listed files. Mini workers receive only one task at a time; strong-review-required tasks receive an independent contract review before merge.

## File Ownership

**Owned by this plan:**

- `backend/stations/runtime.py`
- `backend/shared_generation.py`
- `backend/runtime_supervisor.py`
- `backend/orchestrator.py`
- `backend/radio_agent.py`
- `backend/liquidsoap.py`
- `backend/app.py`
- `scripts/run_station_forever.py`
- `tests/backend/test_dual_station_runtime.py`
- `tests/backend/test_core_behaviour.py`
- `tests/backend/test_full_autonomy_runtime.py`
- `tests/backend/test_desktop_packaging.py`

**Forbidden to runtime workers:**

- `backend/stations/models.py`
- `backend/stations/context.py`
- `backend/stations/loader.py`
- `backend/public_snapshot_v2.py`
- `backend/public_app.py`
- `backend/public_dashboard.py`
- `backend/public_snapshot_pusher.py`
- `backend/tts/**`
- `frontend/**`
- `release/**`

The public-web plan owns snapshot transport and supplies an object satisfying the `SnapshotPusher` protocol defined in Task 3. The voice-system plan owns Qwen synthesis and supplies the `GenerationBackend` protocol defined in Task 2.

---

### Task 1: Make RadioAgent and AutonomousOrchestrator Station-Scoped

**Worker profile:** Strong-review-required because this removes global station assumptions from core control flow.

**Files:**
- Modify: `backend/radio_agent.py`
- Modify: `backend/orchestrator.py`
- Create: `tests/backend/test_dual_station_runtime.py`
- Modify: `tests/backend/test_core_behaviour.py`

**Interfaces:**
- Consumes: `StationContext.profile.station_id`, `StationContext.profile.language`, `StationContext.profile.runtime`, and `StationContext.settings`.
- Produces: `RadioAgent(context: StationContext, generation_client: SharedGenerationClient | None = None)`.
- Produces: `AutonomousOrchestrator(context: StationContext, agent: RadioAgent)` with no `public_pusher` attribute.
- Produces: every station metric, channel update, listener event, task, schedule lookup, and log write uses `context.profile.station_id` instead of the literal `radiotedu`.

- [ ] **Step 1: Write the station-context regression tests**

Add these tests to `tests/backend/test_dual_station_runtime.py`, using the shared profile test factory from the Station Profile plan:

```python
import tempfile
import unittest
from pathlib import Path

from backend.config import Settings
from backend.database import connect, init_db
from backend.orchestrator import AutonomousOrchestrator
from backend.radio_agent import RadioAgent
from backend.stations.context import build_station_context
from tests.backend.station_profile_fixtures import make_station_profile


class DualStationRuntimeTests(unittest.TestCase):
    def test_agents_write_only_to_their_station_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            en_profile = make_station_profile(root, "radiotedu-en", "en", "en-US")
            fr_profile = make_station_profile(root, "radiotedu-fr", "fr", "fr-FR")
            settings = Settings(static_dir=str(root / "public"))
            en_context = build_station_context(settings, en_profile)
            fr_context = build_station_context(settings, fr_profile)
            init_db(en_context.settings)
            init_db(fr_context.settings)

            en_agent = RadioAgent(en_context)
            fr_agent = RadioAgent(fr_context)
            en_orchestrator = AutonomousOrchestrator(en_context, en_agent)
            fr_orchestrator = AutonomousOrchestrator(fr_context, fr_agent)
            en_orchestrator._set_metric("runtime_probe", "english")
            fr_orchestrator._set_metric("runtime_probe", "francais")

            with connect(en_context.settings) as conn:
                en_value = conn.execute(
                    "select value from station_metrics where channel_id=? and key=?",
                    ("radiotedu-en", "runtime_probe"),
                ).fetchone()["value"]
                cross_en = conn.execute(
                    "select count(*) from station_metrics where channel_id='radiotedu-fr'"
                ).fetchone()[0]
            with connect(fr_context.settings) as conn:
                fr_value = conn.execute(
                    "select value from station_metrics where channel_id=? and key=?",
                    ("radiotedu-fr", "runtime_probe"),
                ).fetchone()["value"]
                cross_fr = conn.execute(
                    "select count(*) from station_metrics where channel_id='radiotedu-en'"
                ).fetchone()[0]

            self.assertEqual("english", en_value)
            self.assertEqual("francais", fr_value)
            self.assertEqual(0, cross_en)
            self.assertEqual(0, cross_fr)

    def test_orchestrator_does_not_own_a_snapshot_pusher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = make_station_profile(root, "radiotedu-en", "en", "en-US")
            context = build_station_context(Settings(static_dir=str(root / "public")), profile)
            orchestrator = AutonomousOrchestrator(context, RadioAgent(context))
            self.assertFalse(hasattr(orchestrator, "public_pusher"))
```

- [ ] **Step 2: Run the focused tests and verify the legacy constructors fail**

Run: `python -m pytest tests/backend/test_dual_station_runtime.py -v`

Expected: FAIL because `RadioAgent` and `AutonomousOrchestrator` still accept global `Settings`, the `tests.backend.station_profile_fixtures` dependency comes from the Station Profile plan, and the orchestrator still constructs `PublicSnapshotPusher`.

- [ ] **Step 3: Replace settings ownership with explicit station context**

Implement these constructor contracts in `backend/radio_agent.py` and `backend/orchestrator.py`, then replace all SQL literals and thread names with `self.station_id`:

```python
# backend/radio_agent.py
from backend.stations.context import StationContext


class RadioAgent:
    def __init__(
        self,
        context: StationContext,
        generation_client: "SharedGenerationClient | None" = None,
    ) -> None:
        self.context = context
        self.profile = context.profile
        self.settings = context.settings
        self.station_id = context.profile.station_id
        self.language = context.profile.language
        self.generation_client = generation_client
        self.playback = PlaybackController(self.settings)
        self._initialize_existing_providers()
```

```python
# backend/orchestrator.py
from backend.stations.context import StationContext


class AutonomousOrchestrator:
    def __init__(self, context: StationContext, agent: RadioAgent) -> None:
        if agent.context.profile.station_id != context.profile.station_id:
            raise ValueError("agent station context does not match orchestrator context")
        self.context = context
        self.profile = context.profile
        self.settings = context.settings
        self.station_id = context.profile.station_id
        self.agent = agent
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick_at: datetime | None = None
        self.last_strategy_at: datetime | None = None
        self.last_error: str | None = None

    def _channel_where(self) -> tuple[str]:
        return (self.station_id,)
```

Use parameterized SQL for every former `channel_id='radiotedu'` or `where id='radiotedu'` expression. Name the thread `radiotedu-orchestrator-{self.station_id}`. Remove the `PublicSnapshotPusher` import, constructor call, and `maybe_push()` call from `tick()`; the returned tick dictionary must no longer contain `public_sync`.

- [ ] **Step 4: Update English compatibility tests to construct a context**

In existing tests that instantiate `RadioAgent(settings)` or `AutonomousOrchestrator(settings, agent)`, use:

```python
profile = make_station_profile(root, "radiotedu-en", "en", "en-US")
context = build_station_context(settings, profile)
agent = RadioAgent(context)
orchestrator = AutonomousOrchestrator(context, agent)
```

Keep every existing English assertion unchanged except assertions that explicitly expected channel ID `radiotedu`; update those to `radiotedu-en`.

- [ ] **Step 5: Run core behavior and station isolation tests**

Run: `python -m pytest tests/backend/test_dual_station_runtime.py tests/backend/test_core_behaviour.py -v`

Expected: PASS, including the new cross-database negative assertions and the existing English scheduling, playback, metrics, and sanitization assertions.

- [ ] **Step 6: Commit the station-context change**

```bash
git add backend/radio_agent.py backend/orchestrator.py tests/backend/test_dual_station_runtime.py tests/backend/test_core_behaviour.py
git commit -m "refactor: scope radio control to station context"
```

---

### Task 2: Add Fair Shared-Model Scheduling

**Worker profile:** Mini-friendly implementation with strong concurrency review.

**Files:**
- Create: `backend/shared_generation.py`
- Modify: `backend/radio_agent.py`
- Modify: `tests/backend/test_dual_station_runtime.py`

**Interfaces:**
- Consumes: `GenerationBackend.generate(request: GenerationRequest) -> GenerationResult` supplied by the Qwen/Ollama integration plan.
- Produces: immutable `GenerationRequest(station_id, request_id, kind, payload)` and `GenerationResult(request_id, station_id, value)`.
- Produces: `FairGenerationScheduler(backend, station_ids=("radiotedu-en", "radiotedu-fr"), max_concurrency=1)`.
- Produces: `SharedGenerationClient.submit(request) -> Future[GenerationResult]`; both station processes connect to the same scheduler hosted by the persistent model service.
- Produces: `submit(request) -> Future[GenerationResult]`, `depth(station_id) -> int`, and `metrics(station_id) -> dict[str, int | float | None]`.

- [ ] **Step 1: Write deterministic fairness and rejection tests**

Append to `tests/backend/test_dual_station_runtime.py`:

```python
from backend.shared_generation import FairGenerationScheduler, GenerationRequest, GenerationResult


class RecordingGenerationBackend:
    def __init__(self) -> None:
        self.order: list[str] = []

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.order.append(request.station_id)
        return GenerationResult(request.request_id, request.station_id, request.payload)


def test_generation_scheduler_round_robins_reserved_station_queues() -> None:
    backend = RecordingGenerationBackend()
    scheduler = FairGenerationScheduler(backend, autostart=False)
    scheduler.submit(GenerationRequest("radiotedu-en", "en-1", "tts", "one"))
    scheduler.submit(GenerationRequest("radiotedu-en", "en-2", "tts", "two"))
    scheduler.submit(GenerationRequest("radiotedu-fr", "fr-1", "tts", "un"))
    scheduler.submit(GenerationRequest("radiotedu-fr", "fr-2", "tts", "deux"))

    scheduler.drain_for_test()

    assert backend.order == ["radiotedu-en", "radiotedu-fr", "radiotedu-en", "radiotedu-fr"]
    assert scheduler.depth("radiotedu-en") == 0
    assert scheduler.depth("radiotedu-fr") == 0


def test_generation_scheduler_rejects_unknown_station() -> None:
    scheduler = FairGenerationScheduler(RecordingGenerationBackend(), autostart=False)
    with pytest.raises(ValueError, match="unknown station_id"):
        scheduler.submit(GenerationRequest("radiotedu", "bad-1", "tts", "text"))
```

- [ ] **Step 2: Run the scheduler tests and verify the module is absent**

Run: `python -m pytest tests/backend/test_dual_station_runtime.py -k generation_scheduler -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'backend.shared_generation'`.

- [ ] **Step 3: Implement a reserved round-robin scheduler**

Create `backend/shared_generation.py` with these public types and behaviors:

```python
from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Condition, Thread
from time import monotonic
from typing import Any, Protocol


@dataclass(frozen=True)
class GenerationRequest:
    station_id: str
    request_id: str
    kind: str
    payload: Any


@dataclass(frozen=True)
class GenerationResult:
    request_id: str
    station_id: str
    value: Any


class GenerationBackend(Protocol):
    def generate(self, request: GenerationRequest) -> GenerationResult:
        raise NotImplementedError


class SharedGenerationClient(Protocol):
    def submit(self, request: GenerationRequest) -> Future[GenerationResult]:
        raise NotImplementedError


class FairGenerationScheduler:
    def __init__(
        self,
        backend: GenerationBackend,
        station_ids: tuple[str, ...] = ("radiotedu-en", "radiotedu-fr"),
        max_concurrency: int = 1,
        autostart: bool = True,
    ) -> None:
        if max_concurrency != 1:
            raise ValueError("high-quality generation concurrency must be one")
        self.backend = backend
        self.station_ids = station_ids
        self._queues = {station_id: deque() for station_id in station_ids}
        self._condition = Condition()
        self._cursor = 0
        self._stopping = False
        self._metrics = {
            station_id: {"submitted": 0, "completed": 0, "failed": 0, "oldest_wait_seconds": None}
            for station_id in station_ids
        }
        self._thread: Thread | None = None
        if autostart:
            self.start()

    def submit(self, request: GenerationRequest) -> Future[GenerationResult]:
        if request.station_id not in self._queues:
            raise ValueError(f"unknown station_id: {request.station_id}")
        future: Future[GenerationResult] = Future()
        with self._condition:
            self._queues[request.station_id].append((monotonic(), request, future))
            self._metrics[request.station_id]["submitted"] += 1
            self._condition.notify()
        return future
```

Implement `_next_locked()` by scanning `station_ids` once from `_cursor`, selecting the first nonempty queue, and advancing the cursor to the following station. `drain_for_test()` repeatedly calls the same selection and execution path until both queues are empty. Record completion, failure, current depth, and oldest wait independently per station.

- [ ] **Step 4: Route Qwen and Ollama generation through the scheduler**

In `RadioAgent`, add one internal adapter that preserves synchronous callers while using station-scoped requests:

```python
def _run_shared_generation(self, kind: str, request_id: str, payload: object) -> object:
    if self.generation_client is None:
        raise RuntimeError("shared generation client is required")
    request = GenerationRequest(self.station_id, request_id, kind, payload)
    result = self.generation_client.submit(request).result(timeout=self.settings.llm_timeout_seconds)
    if result.station_id != self.station_id:
        raise RuntimeError("generation result station mismatch")
    return result.value
```

Use this path for Qwen speech and Ollama work. Cache and queue writes remain inside `RadioAgent` after the station identity check.

- [ ] **Step 5: Run scheduler and agent tests**

Run: `python -m pytest tests/backend/test_dual_station_runtime.py tests/backend/test_full_autonomy_runtime.py -v`

Expected: PASS; queue order alternates under simultaneous backlog, unknown station IDs fail, and existing generation behavior still passes through an injected test backend.

- [ ] **Step 6: Commit the fair scheduler**

```bash
git add backend/shared_generation.py backend/radio_agent.py tests/backend/test_dual_station_runtime.py
git commit -m "feat: reserve fair generation capacity per station"
```

---

### Task 3: Build One StationRuntime and One Pusher per Process

**Worker profile:** Strong-review-required because it defines lifecycle ownership.

**Files:**
- Create: `backend/stations/runtime.py`
- Modify: `backend/app.py`
- Modify: `backend/orchestrator.py`
- Modify: `tests/backend/test_dual_station_runtime.py`
- Modify: `tests/backend/test_core_behaviour.py`

**Interfaces:**
- Consumes: `StationContext`, `RadioAgent`, `AutonomousOrchestrator`, and the injected `SnapshotPusher` protocol.
- Produces: `SnapshotPusher.start_background()`, `stop_background()`, `maybe_push()`, and `status()`.
- Produces: `StationRuntime(context, agent, orchestrator, snapshot_pusher, stream_supervisor)`.
- Produces: `create_station_runtime(context: StationContext, snapshot_pusher: SnapshotPusher | None = None, generation_client: SharedGenerationClient | None = None, stream_supervisor: StreamSupervisor | None = None) -> StationRuntime`.
- Produces: `create_app(settings: Settings | None = None, station_context: StationContext | None = None, snapshot_pusher: SnapshotPusher | None = None) -> FastAPI` for exactly one private broadcast station.

- [ ] **Step 1: Write lifecycle ownership tests**

Append to `tests/backend/test_dual_station_runtime.py`:

```python
class RecordingPusher:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start_background(self) -> dict:
        self.starts += 1
        return {"running": True}

    def stop_background(self) -> dict:
        self.stops += 1
        return {"running": False}

    def maybe_push(self) -> dict:
        return {"pushed": False, "reason": "interval"}

    def status(self) -> dict:
        return {"running": self.starts > self.stops}


class RecordingStreamSupervisor:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self) -> dict:
        self.starts += 1
        return {"running": True}

    def stop(self) -> dict:
        self.stops += 1
        return {"running": False}

    def status(self) -> dict:
        return {"running": self.starts > self.stops}


def test_station_runtime_starts_and_stops_exactly_one_pusher(en_context) -> None:
    from backend.stations.runtime import create_station_runtime

    pusher = RecordingPusher()
    supervisor = RecordingStreamSupervisor()
    runtime = create_station_runtime(
        en_context,
        snapshot_pusher=pusher,
        stream_supervisor=supervisor,
    )
    runtime.start()
    runtime.stop()

    assert pusher.starts == 1
    assert pusher.stops == 1
    assert supervisor.starts == 1
    assert supervisor.stops == 1
    assert not hasattr(runtime.orchestrator, "public_pusher")
```

- [ ] **Step 2: Run the lifecycle test and verify StationRuntime is absent**

Run: `python -m pytest tests/backend/test_dual_station_runtime.py -k station_runtime_starts -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.stations.runtime'`.

- [ ] **Step 3: Implement StationRuntime as the lifecycle owner**

Create `backend/stations/runtime.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from backend.orchestrator import AutonomousOrchestrator
from backend.radio_agent import RadioAgent
from backend.shared_generation import SharedGenerationClient
from backend.stations.context import StationContext


class SnapshotPusher(Protocol):
    def start_background(self) -> dict:
        raise NotImplementedError
    def stop_background(self) -> dict:
        raise NotImplementedError
    def maybe_push(self) -> dict:
        raise NotImplementedError
    def status(self) -> dict:
        raise NotImplementedError


class StreamSupervisor(Protocol):
    def start(self) -> dict:
        raise NotImplementedError
    def stop(self) -> dict:
        raise NotImplementedError
    def status(self) -> dict:
        raise NotImplementedError


@dataclass
class StationRuntime:
    context: StationContext
    agent: RadioAgent
    orchestrator: AutonomousOrchestrator
    snapshot_pusher: SnapshotPusher
    stream_supervisor: StreamSupervisor

    def start(self) -> dict:
        stream = self.stream_supervisor.start()
        pusher = self.snapshot_pusher.start_background()
        autonomy = self.orchestrator.start_background()
        return {"station_id": self.context.profile.station_id, "stream": stream, "pusher": pusher, "autonomy": autonomy}

    def stop(self) -> dict:
        autonomy = self.orchestrator.stop_background()
        pusher = self.snapshot_pusher.stop_background()
        stream = self.stream_supervisor.stop()
        return {"station_id": self.context.profile.station_id, "stream": stream, "pusher": pusher, "autonomy": autonomy}


def create_station_runtime(
    context: StationContext,
    snapshot_pusher: SnapshotPusher | None = None,
    generation_client: SharedGenerationClient | None = None,
    stream_supervisor: StreamSupervisor | None = None,
) -> StationRuntime:
    client = generation_client or build_shared_generation_client(context)
    agent = RadioAgent(context, client)
    orchestrator = AutonomousOrchestrator(context, agent)
    pusher = snapshot_pusher or build_station_snapshot_pusher(context, agent)
    supervisor = stream_supervisor or build_station_stream_supervisor(context, agent)
    return StationRuntime(context, agent, orchestrator, pusher, supervisor)
```

Define `build_shared_generation_client` as a localhost-only client for the single shared scheduler, `build_station_snapshot_pusher` as the public-web plan's v2 pusher factory, and `build_station_stream_supervisor` as the Task 5 station-local supervisor. None may load a second station context into the process.

- [ ] **Step 4: Make FastAPI delegate startup and shutdown once**

In `backend/app.py`, construct or accept exactly one context and runtime, expose them on application state, and replace separate orchestrator/pusher hooks:

```python
def select_station_context(settings: Settings) -> StationContext:
    profiles = load_station_profiles(settings.station_profiles_path)
    station_id = os.environ.get("RADIOTEDU_STATION_ID", "radiotedu-en")
    profile = profiles.get(station_id)
    if profile is None:
        raise RuntimeError(f"unknown RADIOTEDU_STATION_ID: {station_id}")
    return build_station_context(settings, profile)


def create_app(
    settings: Settings | None = None,
    station_context: StationContext | None = None,
    snapshot_pusher: SnapshotPusher | None = None,
) -> FastAPI:
    selected_context = station_context or select_station_context(settings or Settings.from_env())
    runtime = create_station_runtime(selected_context, snapshot_pusher=snapshot_pusher)
    app = FastAPI(title=f"{selected_context.profile.display_name} Broadcast Control")
    app.state.context = selected_context
    app.state.runtime = runtime
    app.state.settings = selected_context.settings
    app.state.agent = runtime.agent
    app.state.orchestrator = runtime.orchestrator
    app.state.public_snapshot_pusher = runtime.snapshot_pusher

    @app.on_event("startup")
    def startup() -> None:
        runtime.start()

    @app.on_event("shutdown")
    def shutdown() -> None:
        runtime.stop()

    return app
```

Keep the current private route registrations between lifecycle setup and `return app`; replace their captured `settings`, `agent`, and `orchestrator` values with `selected_context.settings`, `runtime.agent`, and `runtime.orchestrator`. The English default is permitted only when tests or current local development invoke `create_app(settings)`; the production launcher always supplies `RADIOTEDU_STATION_ID` and rejects a missing value before invoking Uvicorn.

- [ ] **Step 5: Run lifecycle and existing API tests**

Run: `python -m pytest tests/backend/test_dual_station_runtime.py tests/backend/test_core_behaviour.py -v`

Expected: PASS; each TestClient lifespan records one pusher start and stop, and current English private controls continue to operate against `radiotedu-en`.

- [ ] **Step 6: Commit runtime ownership**

```bash
git add backend/stations/runtime.py backend/app.py backend/orchestrator.py tests/backend/test_dual_station_runtime.py tests/backend/test_core_behaviour.py
git commit -m "refactor: centralize station runtime lifecycle"
```

---

### Task 4: Render Independent Liquidsoap and Icecast Boundaries

**Worker profile:** Mini-friendly with an independent isolation test review.

**Files:**
- Modify: `backend/liquidsoap.py`
- Modify: `backend/stations/runtime.py`
- Modify: `tests/backend/test_full_autonomy_runtime.py`
- Modify: `tests/backend/test_dual_station_runtime.py`

**Interfaces:**
- Consumes: `StationContext.profile.audio.stream_mount` and station-scoped deployment values for Icecast host, port, source password, queue path, script path, and PID path.
- Produces: `StationStreamConfig.from_context(context) -> StationStreamConfig`.
- Produces: `render_liquidsoap_config(context: StationContext) -> dict`.
- Produces: `liquidsoap_status(context: StationContext) -> dict` with station ID, PID health, queue readability, configured mount, and real Icecast mount state.

- [ ] **Step 1: Write separate-stream tests**

Add to `tests/backend/test_dual_station_runtime.py`:

```python
from backend.liquidsoap import StationStreamConfig, render_liquidsoap_config


def test_stream_configs_cannot_share_runtime_identity(en_context, fr_context) -> None:
    en = StationStreamConfig.from_context(en_context)
    fr = StationStreamConfig.from_context(fr_context)

    assert en.mount == "/radiotedu-en"
    assert fr.mount == "/radiotedu-fr"
    assert en.port != fr.port
    assert en.queue_path != fr.queue_path
    assert en.script_path != fr.script_path
    assert en.pid_path != fr.pid_path
    assert en.source_password != fr.source_password


def test_liquidsoap_config_uses_only_selected_station(en_context, fr_context) -> None:
    en_result = render_liquidsoap_config(en_context)
    fr_result = render_liquidsoap_config(fr_context)
    en_text = Path(en_result["script_path"]).read_text(encoding="utf-8")
    fr_text = Path(fr_result["script_path"]).read_text(encoding="utf-8")

    assert 'mount="/en"' in en_text
    assert "radiotedu-fr" not in en_text
    assert 'mount="/fr"' in fr_text
    assert "radiotedu-en" not in fr_text
```

- [ ] **Step 2: Run the stream tests and verify global Settings still collide**

Run: `python -m pytest tests/backend/test_dual_station_runtime.py -k 'stream_configs or liquidsoap_config' -v`

Expected: FAIL because `StationStreamConfig` is absent and `render_liquidsoap_config` still consumes global `Settings`.

- [ ] **Step 3: Implement immutable station stream configuration**

In `backend/liquidsoap.py`, define:

```python
@dataclass(frozen=True)
class StationStreamConfig:
    station_id: str
    display_name: str
    host: str
    port: int
    mount: str
    source_password: str
    queue_path: Path
    script_path: Path
    pid_path: Path

    @classmethod
    def from_context(cls, context: StationContext) -> "StationStreamConfig":
        station_id = context.profile.station_id
        settings = context.settings
        return cls(
            station_id=station_id,
            display_name=context.profile.display_name,
            host=settings.liquidsoap_host,
            port=settings.liquidsoap_port,
            mount=context.profile.audio.stream_mount,
            source_password=settings.liquidsoap_icecast_password,
            queue_path=Path(context.profile.runtime.data_root) / "liquidsoap" / "queue.m3u",
            script_path=Path(context.profile.runtime.data_root) / "liquidsoap" / f"{station_id}.liq",
            pid_path=Path(context.profile.runtime.data_root) / "liquidsoap" / f"{station_id}.pid",
        )
```

Validation must reject equal English/French Icecast ports or source-secret references when profiles are loaded together. Render one playlist and one `output.icecast` stanza per process. The queue, mount, name, description, credentials, and PID paths come only from the selected context.

- [ ] **Step 4: Update existing Liquidsoap tests to use English context**

Replace `render_liquidsoap_config(settings)` and `liquidsoap_status(settings)` calls in `tests/backend/test_full_autonomy_runtime.py` with an English context. Preserve assertions that config generation is honest when Liquidsoap or Icecast is missing; update the expected mount from `/ai` to `/radiotedu-en` and the test URL to the English station port.

- [ ] **Step 5: Run all stream tests**

Run: `python -m pytest tests/backend/test_dual_station_runtime.py tests/backend/test_full_autonomy_runtime.py -k 'liquidsoap or icecast or stream' -v`

Expected: PASS; both configs render to distinct files, mounts and credentials do not cross, and an inactive mount remains reported as inactive.

- [ ] **Step 6: Commit stream isolation**

```bash
git add backend/liquidsoap.py backend/stations/runtime.py tests/backend/test_dual_station_runtime.py tests/backend/test_full_autonomy_runtime.py
git commit -m "feat: isolate station stream runtimes"
```

---

### Task 5: Supervise Silence, Qwen Degradation, and Music-Only Recovery

**Worker profile:** Strong-review-required because failure handling protects against dead air.

**Files:**
- Create: `backend/runtime_supervisor.py`
- Modify: `backend/stations/runtime.py`
- Modify: `backend/liquidsoap.py`
- Modify: `tests/backend/test_dual_station_runtime.py`

**Interfaces:**
- Consumes: real `QwenHealth.probe_synthesis()`, `RadioAgent.ready_qwen_count()`, `RadioAgent.set_speech_enabled(enabled)`, `RadioAgent.ensure_music_queued()`, and station-scoped Liquidsoap/Icecast probes.
- Produces: `RuntimeMode` values `STARTING`, `LIVE`, `MUSIC_ONLY`, `RECOVERING`, and `STOPPED`.
- Produces: `StationRuntimeSupervisor.evaluate(now) -> SupervisorStatus` and independent `start()`, `stop()`, and `status()`.
- Produces: `SupervisorStatus(station_id, mode, qwen_healthy, qwen_buffer, silence_seconds, liquidsoap_healthy, icecast_healthy, last_error)`.

- [ ] **Step 1: Write failure-injection state-machine tests**

Add to `tests/backend/test_dual_station_runtime.py`:

```python
from backend.runtime_supervisor import RuntimeMode, StationRuntimeSupervisor


def test_qwen_failure_keeps_music_and_disables_only_speech(en_supervisor_harness) -> None:
    harness = en_supervisor_harness
    harness.qwen.healthy = False
    status = harness.supervisor.evaluate(harness.clock.now())

    assert status.mode is RuntimeMode.MUSIC_ONLY
    assert harness.agent.speech_enabled is False
    assert harness.agent.music_queue_calls == 1
    assert harness.agent.fallback_tts_calls == 0


def test_speech_returns_only_after_real_probe_and_full_buffer(en_supervisor_harness) -> None:
    harness = en_supervisor_harness
    harness.qwen.healthy = False
    harness.supervisor.evaluate(harness.clock.now())
    harness.qwen.healthy = True
    harness.agent.qwen_buffer = 4
    recovering = harness.supervisor.evaluate(harness.clock.now())
    harness.agent.qwen_buffer = 5
    live = harness.supervisor.evaluate(harness.clock.now())

    assert recovering.mode is RuntimeMode.RECOVERING
    assert harness.agent.speech_enabled is False
    assert live.mode is RuntimeMode.LIVE
    assert harness.agent.speech_enabled is True


def test_one_silent_stream_restart_does_not_touch_other_station(dual_supervisor_harness) -> None:
    harness = dual_supervisor_harness
    harness.en_audio.silence_seconds = 11
    harness.fr_audio.silence_seconds = 0
    harness.en_supervisor.evaluate(harness.clock.now())
    harness.fr_supervisor.evaluate(harness.clock.now())

    assert harness.en_process.restarts == 1
    assert harness.fr_process.restarts == 0
```

- [ ] **Step 2: Run failure-injection tests and verify the supervisor is absent**

Run: `python -m pytest tests/backend/test_dual_station_runtime.py -k 'qwen_failure or speech_returns or silent_stream' -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'backend.runtime_supervisor'`.

- [ ] **Step 3: Implement the explicit recovery state machine**

Create `backend/runtime_supervisor.py` with frozen status types and this decision order:

```python
class RuntimeMode(str, Enum):
    STARTING = "starting"
    LIVE = "live"
    MUSIC_ONLY = "music_only"
    RECOVERING = "recovering"
    STOPPED = "stopped"


@dataclass(frozen=True)
class SupervisorStatus:
    station_id: str
    mode: RuntimeMode
    qwen_healthy: bool
    qwen_buffer: int
    silence_seconds: float
    liquidsoap_healthy: bool
    icecast_healthy: bool
    last_error: str | None


def evaluate(self, now: datetime) -> SupervisorStatus:
    qwen_healthy = self.qwen.probe_synthesis().valid_audio
    buffer_depth = self.agent.ready_qwen_count()
    audio = self.audio_probe.measure(now)
    if audio.silence_seconds >= 10 or not audio.liquidsoap_healthy:
        self.process.restart()
    if not qwen_healthy:
        self.agent.set_speech_enabled(False)
        self.agent.ensure_music_queued()
        self.mode = RuntimeMode.MUSIC_ONLY
    elif buffer_depth < self.context.profile.audio.minimum_qwen_buffer:
        self.agent.set_speech_enabled(False)
        self.agent.ensure_announcement_prebuffer()
        self.mode = RuntimeMode.RECOVERING
    else:
        self.agent.set_speech_enabled(True)
        self.mode = RuntimeMode.LIVE
    return self._status(qwen_healthy, buffer_depth, audio)
```

The retry policy is one Qwen retry under the same idempotency key. `RadioAgent.set_speech_enabled(False)` must skip unrendered speech items rather than insert silence. Stream restart counters, last errors, queue depth, and alerts remain station-scoped.

- [ ] **Step 4: Wire the supervisor into StationRuntime**

`build_station_stream_supervisor(context, agent)` must build probes, process control, and the state machine solely from `context`. `StationRuntime.start()` must refuse an attended start if the initial real Qwen probe fails or the Qwen buffer is below five. A runtime already marked live must stay running in `MUSIC_ONLY` when Qwen later fails.

- [ ] **Step 5: Run resilience tests**

Run: `python -m pytest tests/backend/test_dual_station_runtime.py -v`

Expected: PASS; Qwen failure never invokes another speech provider, music is queued, speech returns only at buffer depth five, and one station restart leaves the other process untouched.

- [ ] **Step 6: Commit resilience behavior**

```bash
git add backend/runtime_supervisor.py backend/stations/runtime.py backend/liquidsoap.py tests/backend/test_dual_station_runtime.py
git commit -m "feat: preserve music through station failures"
```

---

### Task 6: Launch Two Independent Station Processes

**Worker profile:** Mini-friendly packaging task with strong process-isolation review.

**Files:**
- Modify: `scripts/run_station_forever.py`
- Modify: `tests/backend/test_desktop_packaging.py`
- Modify: `tests/backend/test_full_autonomy_runtime.py`

**Interfaces:**
- Consumes: installed profiles and deployment configuration for both station IDs.
- Produces: `build_process_specs(project_root, start_frontend=False) -> list[ProcessSpec]` containing `backend-radiotedu-en` and `backend-radiotedu-fr`.
- Produces: each process receives `RADIOTEDU_STATION_ID`, a unique private API port, and no other station's writable paths or secrets.
- Produces: independent restart counters and exponential backoff per process.

- [ ] **Step 1: Replace the single-backend packaging expectation**

In `tests/backend/test_desktop_packaging.py`, assert the two-process contract:

```python
def test_forever_runner_builds_two_isolated_station_processes() -> None:
    module = load_forever_runner()
    specs = module.build_process_specs(Path("F:/RTAI/RadioTEDU"), start_frontend=False)

    assert [item.name for item in specs] == ["backend-radiotedu-en", "backend-radiotedu-fr"]
    assert specs[0].environment["RADIOTEDU_STATION_ID"] == "radiotedu-en"
    assert specs[1].environment["RADIOTEDU_STATION_ID"] == "radiotedu-fr"
    assert specs[0].port != specs[1].port
    assert "RADIOTEDU_FR_SNAPSHOT_SECRET" not in specs[0].environment
    assert "RADIOTEDU_EN_SNAPSHOT_SECRET" not in specs[1].environment
```

- [ ] **Step 2: Run packaging tests and verify the single backend fails the contract**

Run: `python -m pytest tests/backend/test_desktop_packaging.py -v`

Expected: FAIL because the current runner returns `backend` and optionally `frontend`, not two station-specific backend processes.

- [ ] **Step 3: Build one process specification per station**

Update `scripts/run_station_forever.py` so the default broadcast launcher produces exactly:

```python
def build_process_specs(project_root: Path, start_frontend: bool = False) -> list[ProcessSpec]:
    profiles = load_station_profiles(project_root)
    specs: list[ProcessSpec] = []
    for offset, profile in enumerate(profiles):
        environment = station_environment(profile)
        specs.append(
            ProcessSpec(
                name=f"backend-{profile.station_id}",
                command=[
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "backend.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(8765 + offset),
                ],
                cwd=project_root,
                environment=environment,
                port=8765 + offset,
            )
        )
    return specs
```

`station_environment(profile)` includes only the selected `RADIOTEDU_STATION_ID` and that station's deployment references. The existing frontend dev server is excluded from production broadcast startup. Restart backoff and counters remain fields on each `ProcessSpec` supervisor state, not module globals.

- [ ] **Step 4: Add a process-death isolation test**

In `tests/backend/test_full_autonomy_runtime.py`, add a fake process runner test that terminates `backend-radiotedu-en`, advances the clock through one restart, and asserts `backend-radiotedu-fr` retains its PID and zero restarts.

- [ ] **Step 5: Run packaging and autonomy tests**

Run: `python -m pytest tests/backend/test_desktop_packaging.py tests/backend/test_full_autonomy_runtime.py -v`

Expected: PASS; both process specs are present, secrets are scoped, and restarting English does not restart French.

- [ ] **Step 6: Commit dual-process launch**

```bash
git add scripts/run_station_forever.py tests/backend/test_desktop_packaging.py tests/backend/test_full_autonomy_runtime.py
git commit -m "feat: launch isolated English and French runtimes"
```

---

### Task 7: Run Runtime Qualification Gates

**Worker profile:** Strong read-only reviewer; remediation returns to the owning task worker.

**Files:**
- Verify: `backend/stations/runtime.py`
- Verify: `backend/shared_generation.py`
- Verify: `backend/runtime_supervisor.py`
- Verify: `backend/orchestrator.py`
- Verify: `backend/radio_agent.py`
- Verify: `backend/liquidsoap.py`
- Verify: `backend/app.py`
- Verify: `scripts/run_station_forever.py`
- Verify: `tests/backend/test_dual_station_runtime.py`
- Verify: `tests/backend/test_core_behaviour.py`
- Verify: `tests/backend/test_full_autonomy_runtime.py`
- Verify: `tests/backend/test_desktop_packaging.py`

**Interfaces:**
- Consumes: all runtime contracts from Tasks 1–6 and real Qwen/stream test doubles with explicit station identity.
- Produces: evidence that runtime construction, fairness, failure recovery, and process isolation satisfy the approved design.

- [ ] **Step 1: Run the focused runtime suite**

Run: `python -m pytest tests/backend/test_dual_station_runtime.py tests/backend/test_core_behaviour.py tests/backend/test_full_autonomy_runtime.py tests/backend/test_desktop_packaging.py -v`

Expected: PASS with zero failures and zero errors.

- [ ] **Step 2: Run the full backend suite**

Run: `python -m pytest tests/backend -q`

Expected: PASS with zero failures and zero errors.

- [ ] **Step 3: Run forbidden-engine and global-station scans**

Run: `rg -n "SapiTTSProvider|Piper|dummy_tts|channel_id='radiotedu'|where id='radiotedu'|radiotedu-orchestrator\"" backend scripts tests/backend`

Expected: no production runtime matches. Historical migration assertions may match only in explicitly named compatibility tests.

- [ ] **Step 4: Run a deterministic dual-runtime fault drill**

Run: `python -m pytest tests/backend/test_dual_station_runtime.py -k 'round_robins or qwen_failure or silent_stream or process' -v`

Expected: PASS; fairness alternates under backlog, Qwen failure yields music-only, and English/French process failures remain independent.

- [ ] **Step 5: Record qualification evidence in the implementation commit**

```bash
git status --short
git log --oneline -7
```

Expected: only implementation-plan-owned files are modified, `release/` is untouched, and every preceding task has one focused commit.

- [ ] **Step 6: Commit any test-only qualification fixture adjustments**

If Step 1 required test fixture changes within owned test files, commit only those changes:

```bash
git add tests/backend/test_dual_station_runtime.py tests/backend/test_core_behaviour.py tests/backend/test_full_autonomy_runtime.py tests/backend/test_desktop_packaging.py
git commit -m "test: qualify dual station runtime isolation"
```

Expected: `git status --short` shows no runtime-plan changes. If no fixture adjustment was required, do not create an empty commit.

## Runtime Plan Completion Criteria

- Two OS processes each contain exactly one `StationRuntime` and one snapshot pusher.
- No orchestrator or application hook constructs a duplicate pusher.
- RadioAgent and AutonomousOrchestrator require validated station context.
- Shared generation alternates reserved English/French queues and exposes per-station telemetry.
- Liquidsoap/Icecast paths, ports, mounts, credentials, PIDs, health, and restart state are independent.
- A Qwen failure produces music-only continuity with no substitute voice.
- Speech resumes only after valid Qwen synthesis and a rebuilt five-clip buffer.
- Killing or silencing one station does not restart or interrupt the other.
- Current English operator behavior passes through the `radiotedu-en` compatibility context.
