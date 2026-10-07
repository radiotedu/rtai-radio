# RadioTEDU

RadioTEDU is a local-first AI radio station that runs one channel only: `RadioTEDU`. Programs such as TEDU Dawn, Campus Flow, Jazz Lab, and Weekend Signal are scheduled blocks inside that channel.

There is no demo mode and no invented listening data. Add your own local music before starting playback. If no playable music exists, the backend and dashboard still run, the station stays idle, and the dashboard asks you to add music and rescan.

For the next implementation backlog, see [`docs/NEXT_TODOS.md`](docs/NEXT_TODOS.md).

## Quickstart

```bash
cd RadioTEDU
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install
mkdir -p data/music
python scripts/scan_music.py
python -m backend.app
```

In another terminal:

```bash
npm run dev
```

Open `http://localhost:5173`.

## Current Music Library

The portable default is:

```env
MUSIC_DIR=data/music
```

For this workstation you can point RadioTEDU at the Jazz library:

```env
MUSIC_DIR=F:/Songs/Jazz
```

The scanner walks the directory recursively, stores metadata in SQLite, deduplicates by file path, and keeps selection queries limited so large FLAC libraries stay manageable on an 8 GB CPU-only machine.

## Local AI

The portable default LLM is Ollama with `qwen2.5:0.5b-instruct`:

```bash
ollama pull qwen2.5:0.5b-instruct
```

For this workstation, the live `.env` uses the stronger local 4B model you selected:

```env
OLLAMA_MODEL=qwen3.5:4b
```

Check the local runtime without installing or pulling anything:

```bash
python scripts/check_ollama.py
python scripts/check_ollama.py --json
```

The checker reports whether the Ollama CLI exists, whether the server is reachable, and whether the configured model is installed. It returns suggested commands such as:

```bash
winget install Ollama.Ollama
ollama serve
ollama pull qwen3.5:4b
```

It never installs, starts, or downloads anything unless you explicitly ask it to:

```bash
python scripts/check_ollama.py --pull
```

To explicitly bootstrap the local Windows runtime in one command:

```bash
python scripts/check_ollama.py --install --start --pull
```

The DJ prompt is intentionally small and JSON-only. If Ollama is unavailable or returns invalid JSON, RadioTEDU picks from real candidate tracks deterministically and writes a short deterministic DJ line.

The dashboard separates configured model from runtime health. `health.llm` shows the requested model name, while `health.llm_runtime` checks the Ollama `/api/tags` endpoint and reports whether the server is reachable and whether the configured model is installed. The backend also exposes `GET /api/setup/ollama` for the same setup guidance used by the checker script.

## Autonomous Orchestrator

RadioTEDU can keep the station running continuously while the backend process is alive:

```env
AUTONOMY_ENABLED=true
AUTONOMY_TICK_SECONDS=30
STRATEGY_INTERVAL_MINUTES=240
```

The orchestrator is intentionally local and conservative. It keeps one RadioTEDU channel alive, refills the queue from real local tracks, records real play history, refreshes a long-horizon strategy note, edits the program schedule in SQLite, stores listener feedback as local memory, writes self-reviews, and drafts local segment notes. It also persists a structured strategy policy with goals, next actions, real library signals, and constraints so the dashboard can show what the agent is optimizing. It does not create extra channels, invent analytics, or operate external accounts.

Listener feedback submitted through the dashboard is sanitized for the non-financial station scope, stored as local autonomy memory, and answered with a queued local TTS reply. This works even before music is indexed; it does not invent listener counts or popularity.

Use the dashboard Start and Stop buttons to start or stop the in-process runner. Stop shuts down the runner and leaves the backend available.

For a watchdog that restarts the backend if it exits, run:

```bash
python scripts/run_station_forever.py --root F:/RTAI/RadioTEDU --frontend
```

On Windows, you can register that watchdog at login:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_windows_task.ps1 -ProjectRoot F:\RTAI\RadioTEDU -WithFrontend
```

### Announcement prebuffer

For weaker machines, RadioTEDU can build a spoken-announcement buffer before it allows broadcast startup:

```env
MIN_READY_ANNOUNCEMENTS=5
MAX_READY_ANNOUNCEMENTS=8
```

The agent fills `announcement_queue` with ready TTS clips, starts playback only when the ready count reaches the minimum, then consumes one prepared announcement before each real track. When playable tracks exist, each prepared announcement stores the planned real `track_id`, title, artist, genre, and decision reason in `metadata_json`, so the spoken intro and the song stay paired. If Ollama is unavailable, fallback intros still use real search/RSS snippets when supplied, then local metadata such as album, genre, mood, or duration; they do not invent song facts. Legacy generic agent prebuffer rows are retired as `stale` once real track-bound announcements can be prepared. This avoids generating every DJ line at the last second.

When sourced search/RSS context explicitly matches an upcoming real track, the prebuffer can queue a short `song_context` note with the source URL in metadata. If no matching sourced context exists, RadioTEDU skips that segment and uses only local track metadata.

Autonomous ticks also maintain the prebuffer even when another item, such as a listener reply, is already queued. The tick response includes the current prebuffer snapshot so operators can see whether the station is ready to broadcast.

## TTS

Production speech uses only the loopback Kokoro 82M service. English and
French contextual song links are prepared three to five songs ahead with Qwen3
0.6B text generation. Music starts immediately even while the first speech clips
are being generated; an unfinished or invalid spoken link is skipped without
interrupting the music timeline.

English and French host links are generated ahead into durable queues. Contextual
song and artist links receive priority over ordinary buffer refills. Night delivery
uses a lower-register neutral Kokoro voice with measured phrasing. The local Kokoro
service listens on `http://127.0.0.1:8090`; English and French TTS output are
validated before they are admitted to the broadcast queue.
The broadcast text spells the station name as `Radio Ted You`, with Ted rhyming
with “bed” and U pronounced as “you”. The selected Kokoro voices use neutral
delivery without expressive prompts; audio quality checks reject invalid,
clipped, truncated, or overly long output before it enters the queue.

There is no SAPI, Piper, Edge, cloud, dummy, or silent-clip TTS fallback. If Kokoro is
unavailable or returns an invalid WAV, the spoken link is skipped and music continues.

## Search

RSS is the default lightweight search provider:

```env
SEARCH_PROVIDER=rss
RSS_FEEDS_PATH=data/rss_feeds.json
```

To use SearXNG:

```env
SEARCH_PROVIDER=searxng
SEARXNG_URL=http://localhost:8080
```

Search is throttled by `WEB_SEARCH_INTERVAL_MINUTES` and never blocks playback.

## Weather

Weather context is optional and real-only. When enabled, RadioTEDU fetches current conditions from Open-Meteo, can queue a short weather note into the same prepared announcement buffer, and passes a short summary into the DJ decision prompt; if the provider is disabled, unconfigured, or unreachable, the status payload and dashboard show `No weather data.` and the agent skips weather announcements.

Portable default:

```env
WEATHER_ENABLED=false
WEATHER_PROVIDER=open_meteo
WEATHER_LOCATION=Ankara
WEATHER_LATITUDE=
WEATHER_LONGITUDE=
WEATHER_INTERVAL_MINUTES=30
```

This workstation can enable Ankara weather with:

```env
WEATHER_ENABLED=true
WEATHER_LOCATION=Ankara
WEATHER_LATITUDE=39.9208
WEATHER_LONGITUDE=32.8541
```

## Public Dashboard And Website Sync

The operator dashboard is local. The public website should use the `/ai` route and the public API only. The broadcast computer pushes sanitized snapshots outward to the website server; the website server does not call into the broadcast computer.

The AIStreams broadcaster also uses the signed platform API documented in the
broadcasting connection bundle. It publishes snapshots and play events, and it
now keeps completed host transcripts in a separate durable outbox for
`POST /v1/radio/stations/{station_id}/spoken-segments`. That endpoint and the
`metrics.recent_spoken_segments` status field require the web/API server update
described in the connection bundle's `SERVER-UPDATE-PROMPT.md`. Until the server
deploys that contract, speech events stay queued locally and audio playback
continues normally.

Broadcast computer example:

```env
PUBLIC_SYNC_URL=https://radiotedu.com/api/public/snapshot
PUBLIC_SYNC_TOKEN=change-this-shared-secret
PUBLIC_STREAM_URL=https://radiotedu.com/live.mp3
PUBLIC_SYNC_INTERVAL_SECONDS=10
```

Website server example:

```env
PUBLIC_DASHBOARD_ENABLED=true
PUBLIC_DASHBOARD_ROUTE=/ai
PUBLIC_SYNC_TOKEN=change-this-shared-secret
PUBLIC_STREAM_URL=https://radiotedu.com/live.mp3
SNAPSHOT_TTL_SECONDS=30
AUTONOMY_ENABLED=false
PLAYBACK_BACKEND=simulate
```

Public endpoints:

```text
POST /api/public/snapshot
GET  /api/public/status
POST /api/public/session/start
POST /api/public/session/heartbeat
POST /api/public/session/end
```

Snapshot POSTs require `X-RadioTEDU-Sync-Token`. Public status intentionally excludes local file paths, logs, incidents, autonomous tasks, secrets, and operator controls. Listener counts and average session values are derived only from real browser session events on the website server; empty data is shown as `No data` or `0`, never invented.

The two Codex handoff prompts are stored in:

```text
docs/BROADCAST_COMPUTER_CODEX_PROMPT.md
docs/WEBSITE_SERVER_CODEX_PROMPT.md
```

## Curated RSS News

RadioTEDU can read short news notes from configured RSS feeds:

```env
NEWS_ENABLED=true
NEWS_INTERVAL_MINUTES=60
NEWS_MAX_AGE_HOURS=24
RSS_FEEDS_PATH=data/rss_feeds.json
```

News is retrieval-first. The agent uses titles/snippets from configured RSS feeds, requires a fresh RSS `pubDate`/Dublin Core date, queues a short announcement in the same prebuffer as DJ/song/weather/listener lines, and skips news if the feed is empty, unreachable, undated, or older than `NEWS_MAX_AGE_HOURS`. The model must not invent headlines.

## Playback

The portable default playback backend is safe simulation:

```env
PLAYBACK_BACKEND=simulate
```

To use local players:

```env
PLAYBACK_BACKEND=auto
MPV_PATH=mpv
FFPLAY_PATH=ffplay
```

`auto` tries `mpv`, then `ffplay`, and falls back to simulation only if neither player is available. You can also force a player:

```env
PLAYBACK_BACKEND=mpv
```

## Liquidsoap And Icecast

RadioTEDU can run the local admin dashboard as the control app while Liquidsoap streams the actual audio to Icecast at the `/ai` mount:

```env
PLAYBACK_BACKEND=liquidsoap
LIQUIDSOAP_ENABLED=true
LIQUIDSOAP_QUEUE_PATH=data/liquidsoap/queue.m3u
LIQUIDSOAP_SCRIPT_PATH=data/liquidsoap/radiotedu.liq
LIQUIDSOAP_COMMAND=liquidsoap
LIQUIDSOAP_HOST=127.0.0.1
LIQUIDSOAP_PORT=8001
LIQUIDSOAP_MOUNT=/ai
LIQUIDSOAP_ICECAST_PASSWORD=hackme
PUBLIC_STREAM_URL=http://127.0.0.1:8001/ai
```

Generate the Liquidsoap files from the API or from the admin dashboard `Air Output` panel:

```bash
python - <<'PY'
from backend.config import Settings
from backend.liquidsoap import render_liquidsoap_config
print(render_liquidsoap_config(Settings.from_env()))
PY
```

When `PLAYBACK_BACKEND=liquidsoap`, queued TTS announcements and real track paths are appended to the Liquidsoap playlist for the Liquidsoap process to stream. Install and run Icecast separately with a matching source password and port, then use `Start Icecast Air` from the admin dashboard. If Liquidsoap is not installed, the admin panel shows it as missing instead of pretending the stream is live.

For `radiotedu.com/ai`, the broadcast computer should push public snapshots to the website server:

```env
PUBLIC_SYNC_URL=https://radiotedu.com/api/public/snapshot
PUBLIC_SYNC_TOKEN=change-this-shared-secret
PUBLIC_STREAM_URL=https://stream.radiotedu.com/en
```

The website server renders those snapshots at `https://radiotedu.com/ai` without exposing the broadcast computer, local file paths, logs, or admin controls. `PUBLIC_STREAM_URL` should point to the public Icecast stream URL, which can use the Icecast `/ai` mount on a stream subdomain or port.

The admin `Air Output` panel also has `Verify Icecast Air`, which renders the Liquidsoap config, confirms the queue file is readable, checks that the script references the queue, and probes the configured Icecast mount. It reports the real mount state; it does not claim the stream is live when Icecast/Liquidsoap are missing.

## Cover Art

RadioTEDU generates original deterministic square cover art with Python. Refresh assets with:

```bash
python - <<'PY'
from backend.art.cover_generator import generate_covers
from backend.config import Settings
generate_covers(Settings.from_env())
PY
```

The prompts in `backend/art/prompts.py` can also be pasted into an external image generator later.

## Programs

Programs remain schedule blocks inside the one RadioTEDU channel. You can edit start time, end time, days, and vibe from the dashboard. The API is:

```http
PATCH /api/programs/{program_id}
```

Edits are recorded in `schedule_revisions`.

Schedule edits validate `HH:MM` times and comma-separated `mon,tue,wed,thu,fri,sat,sun` day values. The admin dashboard also shows a weekly one-channel strategy view and an emergency fallback playlist built only from real indexed tracks.

For trusted remote operators, set `ADMIN_API_TOKEN` on the broadcast server and store the same value in the browser local storage key `radiotedu_admin_token`. Public dashboard/session APIs remain available without that admin token.

## Observability

The dashboard includes Runtime Watch: announcement prebuffer readiness, uptime, generated clips, recent errors, restart count, and current playback state. These values come from real SQLite/runtime state, not invented analytics.


## Live Laya next-song selection (2026-10-01)

Normal dynamic playback uses `laya-live-next-track`: every queued song requires a
separate local typed-choice call. The producer targets four upcoming songs
while the PCM timeline plays. It scans the available safe catalog across genres;
scheduled genres are preferences. Queue reservations and recent artists are
excluded explicitly. The pinned Laya encoder scores the entire eligible pool,
and its 64 finalists receive actual typed-head probabilities. Cosine similarity
is not a probability for the remaining catalog.

Opaque stable catalog choice IDs allow exact encoder inputs to be cached. The
SQLite encoder cache is keyed by the pinned model/revision/checksum, pooling
implementation and encoder token limit. It does not cache or invent new typed
song decisions. Short option text retains title, artist and genre, and the long
query is embedded separately to avoid padding short options to 512 tokens.

Selection evidence is persisted before a song enters the queue. Restart recovery
can seed the buffer from real archived single-choice events for the same program;
status labels these `restored_real_laya_decisions`, preserving their original
IDs and timestamps. New decisions then refill the queue. The model lock is taken
before reading the current song and queue, so the second station does not use a
context captured before a long wait. Old announcement readiness is invalidated
on restart and known song pairs are checked again during playback.

Pending selection events are written individually to
`public-sync-selection-decisions.sqlite3` with SQLite WAL and durable commits.
The legacy JSON backlog is migrated without dropping events and archived after
successful migration. Acknowledged records retain the original payload. Audio
threads no longer wait for the entire pending backlog to be serialized for each
new choice.

Keep both workspace and installed watchdogs compatible with this selection mode
and the configured announcement lead interval. An old mode allowlist otherwise
restarts a healthy stream and interrupts catalog warmup. Selection API HTTP 422
is a server contract failure: retain the signed delivery outbox and update its
validator using Desktop `apipr.md` and the captured real public event files.


Opaque catalog IDs are precomputed before the rolling queue starts. No catalog
filesystem traversal runs under the queue condition used by PCM playout. Encoder
workers persist at most one status heartbeat per second, rather than rewriting
JSON for every PCM chunk. Reconnect diagnostics retain the actual last failure.


The mixed AI supervisor process runs at normal Windows priority; its audio
FFmpeg children run above normal. Raising the supervisor also raises CPU-heavy
Laya workers and competes with continuous encoders. Input backpressure remains
bounded, and recovery checks actual encoded-audio progress rather than a full
input queue alone. Keep `RADIOTEDU_PROCESS_PRIORITY=normal` for AIStreams.

Announcement copy uses `varied-full-script-v1`. Qwen can write complete short
links, with exact catalog names verified before synthesis. Recent phrasing is
persisted in each station's `announcement-history.json`; repeated openings,
similar scripts and instruction echoes are rejected. A varied grounded template
is used when a model draft fails, with its actual `copy_source` recorded locally
and no Qwen authorship claimed in the spoken-event `text_model` field. Wishes
appear every third link. Cache keys include the durable announcement sequence,
so a repeated song pair does not reuse last week's speech. Before airing, clip
metadata must match both actual adjacent songs. Existing Kokoro audio gates apply.

Catalog metadata is normalized by `scripts/metadata_cleanup.py` before selection,
playout and API publication. Video labels and YouTube IDs are removed; meaningful
live, acoustic, remix and remaster versions are retained. The metadata audit can
write this cleanup with `--write-clean-labels`, saving original title/artist
values under the report directory before changes. Historical model evidence
retains the exact metadata used in its original call.

For live Laya playout, `track_count` and `catalog_track_count` report the complete
eligible catalog. `program_items` and `prepared_buffer_track_count` describe the
playlist buffer written at that point; `laya_song_queue.queue_depth` is the live
buffer depth, with a target of four. This buffer is continuously replenished
using separate decisions from the remaining catalog.

Startup recovery seeds the first item plus four future items using actual
archived single-choice decisions. `laya_song_queue.queue_depth` counts future
items; the initial playlist contains the first item as well. Archived choices
from a prior program may seed an uninterrupted restart when the current program
has insufficient history; status labels that source explicitly and never calls
those decisions fresh inference. Fresh choices use the current program.

Live mode refreshes its validated catalog every 120 seconds on a background
thread. New imports receive stable choice IDs without restarting the encoders;
removed files are dropped from the future queue while current audio continues.
No full catalog scan runs at a live song boundary.

`scripts/audio_prefetch.py` decodes the actual selected horizon and validated
host clips ahead to bounded disk-backed 48 kHz stereo PCM. The same FFmpeg
normalization chain is used for prepared and direct decoding. File leases keep
current audio readable during horizon changes; obsolete PCM is evicted.
`pcm_prefetch` reports actual ready counts, cache hits, preparation errors and
boundary misses. Explicit silence padding between items is disabled.

`scripts/playout_clock.py` parses the MP3 frames sent to the origin and tracks
their sample positions against offered PCM item boundaries. `now_playing`,
play-event timestamps, spoken-event timestamps and complete PCM durations now
follow that output clock. `preparing_playout` identifies producer work still
waiting in the audio buffers. Publishing metadata happens outside the sender;
publication retries cannot restart audio. A source item change wakes snapshot
sync instead of waiting for the regular ten-second interval. Downstream player
buffering adds its own delay; this clock describes source output, not an inferred
timestamp on every listener's device.

Fresh Laya context reads the actual on-air music from that output clock. The
producer's decoded current item is separately labelled
`pipeline_current_track_id`; it remains an upcoming item while older buffered
audio is on air. Recent played history advances only after sent audio completion.

Laya initialization starts in the queue worker after playout begins, while the
restored unconsumed choices remain ready. `model_warmup_state` reports the real
initialization state; this loads the checkpoint without inventing a new song
decision. The live announcement lead interval is capped at the four-song music
horizon, so both adjacent titles are known before preparing each link.

Host snapshots persist `prepared_boundary_count`, `missed_boundary_count` and
`stale_boundary_count`. A ready count of zero just after a link is consumed does
not mean a missed announcement; these counters record the actual boundary.
Recent-song model context retains metadata captured from aired segments instead
of reopening earlier library files. Removing or retagging an earlier file cannot
rewrite the actual listening history or stop the next music decision.

`pcm_edges.py` removes only leading/trailing PCM below -55 dB lasting at least
0.5 seconds, retaining 50 ms at each edge. Interior music samples and pauses are
preserved byte for byte. Prepared files are trimmed before becoming ready; the
direct decoder fallback holds quiet runs until their position is known, with
disk spill for long interior pauses. Actual playout durations follow the PCM
sent after trimming, while original library files, tags and model evidence stay
intact. Prefetch diagnostics report the real trim count and removed seconds.

For the already running older process, `maintain_running_pcm_edges.py` applies
the same policy to unopened generated cache files without restarting audio.
Windows sharing checks exclude leased playout files; atomic replacement also
fails if a lease opens during preparation. The helper exits when the specified
broadcast PID and creation time stop matching. After the next normal broadcast
restart, edge preparation is integrated in `PcmPrefetch` and the helper is no
longer needed.

Windows sender and encoder-input threads run one priority level above the
broadcaster process via `audio_thread_priority.py`. On 3 October, the live
broadcaster's process priority was raised to AboveNormal to match existing video
and announcement-text encoders. The model loader now also applies this setting
on subsequent starts. Audio I/O remains above model work; video upload delay is
checked alongside radio output. Kokoro's independent process priority is unchanged.

The five-item startup playlist is not the candidate catalog. Live status now
refreshes the actual producer horizon every five seconds and includes its last
queued song in `queue_preview`. `program_items`, `prepared_program_items` and
`prepared_buffer_track_count` describe that horizon; `catalog_track_count` and
`track_count` describe the safe library. PCM readiness is reported separately by
`pcm_prefetch`. This reporting correction is integrated in native supervisor startup.

Selector policy versions live-v4/batch-v3 shorten operational prose in model
state, supply the editorial `vibe` when `description` is absent, and correct the
French instruction encoding. The full candidate pool and 64 finalists are
retained. Opaque recent-track IDs and scheduling timestamps remain in the event
evidence; model state now contains musical recent/current/upcoming facts. The
pool hash remains in model state and is checked against the full recorded pool.
Genuine new live decisions use about 2,000 tokens, versus about 2,600 previously.
Warm calls after the priority adjustment were about 60 seconds; cold calls and
competing workloads take longer. These are measured calls, not a latency guarantee.

`*-live-song-queue.json` checkpoints preserve unconsumed queue order, actual
reservations and source-completed listening history. Restarts validate those
unconsumed choices against their original single-choice Laya events and resume
them instead of replaying the five most recent archived songs. The first upgrade
used `seed_running_laya_queue_checkpoint.py` to reconcile genuine latest queue
evidence with the actual producer position and exact ready depth. A small initial
queue still needs time to refill; restoration alone does not prove a 3–4 song
horizon or continuous audio.

Integrated in native startup: completed PCM with a matching source
size/mtime and decoder command is retained and reused after applying the current
edge policy. Obsolete output and incomplete decoder files are discarded. An
offline fixture verifies byte-exact reuse without launching a decoder.

Host countdown advancement and producer music
horizon advancement share the queue condition. Production-completed music is
removed from that horizon before preparing the next boundary; aired listening
history still waits for source completion. This prevents the completed song from
shifting a future host context while its speech is being fed. A mismatched host
boundary retains the actual expected and recorded song pairs in local diagnostics.
Reconnect diagnostics retain a total count as well as the latest reason.


### Source reconnect recovery (2026-10-03)

`pcm_replay_buffer.py` retains PCM consumed from the timeline until its MP3
frames have been accepted by the source socket. Each encoder cycle gets an
immutable source-clock base and generation. A late sender from an earlier
cycle cannot advance the new clock. After a reconnect, only unconfirmed PCM
is replayed before consuming new timeline chunks. The encoder writer also
captures its own process, input queue and stop signal, preventing a delayed
old writer from using the next cycle's pipe. `pcm_replay` reports actual
retained duration and the confirmation position. Socket acceptance is the
source clock basis; it does not prove receipt or playback on a listener device.

The autonomous watchdog now checks the actual supervisor child and recent
source progress or retry timestamps independently of upstream/API readiness.
A recoverable mount failure must not restart the shared EN/FR service. Missing
children, stale audio without active recovery, and stale recovery still fail
supervision. Public health remains degraded when the origin listener is absent.

The live 3 October capture found a sender waiting in socket `sendall`, with
PCM input backpressure and a nonempty music queue. Direct listener observation
also found a 404 EN mount and a 200 FR response with no body. The origin reports
`RADIOTEDU Stream Audio Server/1.1.0 (Icecast 2 Compatible)`. Those observations
require investigation of that server's ingestion/listener handling; local queue
readiness and successful socket writes cannot certify uninterrupted public audio.
The duration-aware observer records actual decoded duration and demuxing errors,
so early EOF with exit code zero is not a passing continuity check.


Public current-track snapshots are sent before play/speech history, and
full-catalog selection evidence retries run on a separate delivery thread.
A source change during snapshot delivery keeps its wake signal. An offline
blocking-request fixture checks that selection latency cannot hold up the next
snapshot. Both delivery threads were verified in PID 19256 after the controlled
3 October 01:14:52 UTC deployment, seeded from four actual pending choices per
station. This maintenance restart interrupted the prior source connections;
subsequent public continuity checks still failed and do not certify no dead air.

### English links and selection delivery (2026-10-07)

English links now run after every song (`host_lead_songs_min = 1`,
`host_lead_songs_max = 1`). This is the announcement interval, separate from
the four-track Laya music queue and five-slot preparation horizon. Varied wishes
remain due every third link. The installer and operator settings accept the
same interval. French retains its spaced links.

Host startup restores durable slots and counters before invalidating their
old song contexts. Invalidation before restoration overwrote the checkpoint
with an empty constructor queue. Restored clips must still match the actual
adjacent songs before use.

Selection delivery tries the newest genuine event before older pending events.
It retains every rejected event and acknowledges only actual successful API
requests. Enqueuing new evidence or seeing a status route does not clear the
last delivery error. Local diagnostics include the last accepted event and time.
New decision evidence includes the safe catalog count, current rotation-cycle
reservation count, and the actual no-repeat cycle policy. The eligible pool can
therefore shrink during a cycle; the 64 choice-head probabilities describe
finalists only. They are distinct from full-pool encoder similarities.

The first post-deployment EN source-completed speech event occurred at
2026-10-07 07:19:28 UTC and introduced Cat Stevens after Simon & Garfunkel.
Speech delivery succeeded, but selection delivery still returned HTTP 422 and
origin listener observations ended early with read errors. These external
dependencies remain unresolved; this change does not certify continuous
listener reception. See the focused web/API repair handoff for the real schema
and current evidence. No additional tests were run for the repository publish.
