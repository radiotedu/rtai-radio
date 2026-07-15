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

Qwen TTS is configured through a command template:

```env
QWEN_TTS_COMMAND=python scripts/qwen_tts_command.py --text {text} --out {output_path} --voice {voice}
```

The wrapper uses `QWEN_TTS_HTTP_URL` when you have a Qwen TTS HTTP endpoint that returns WAV bytes. If no endpoint is configured, it exits quickly and the configured fallback provider handles the clip.

The local admin app shows TTS runtime health and includes a `Test TTS` button. The test uses the current program host voice, such as `tr_female_cool` for Jazz Lab, and writes the generated WAV plus its `.txt` sidecar locally.

On Windows, set the fallback to SAPI for real local speech when the Qwen command is empty:

```env
FALLBACK_TTS_PROVIDER=sapi
```

The dummy provider still exists as the final reliability fallback. It writes a short silent WAV and a `.txt` sidecar containing the narration text.

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

The operator dashboard is local. The website server runs `backend.public_app`, which exposes only the bilingual listener pages and versioned public platform API. The broadcast supervisor pushes sanitized state outward; website failure never blocks playout.

Broadcast computer example:

```env
PUBLIC_SYNC_URL=https://api.radiotedu.com
PUBLIC_SYNC_INTERVAL_SECONDS=10
RADIOTEDU_AGENT_ID=school-radio-pc
RADIOTEDU_AGENT_SCOPE=agent:playout
RADIOTEDU_EN_SNAPSHOT_SECRET=<protected-secret-reference>
RADIOTEDU_FR_SNAPSHOT_SECRET=<protected-secret-reference>
```

Website server example:

```env
SNAPSHOT_TTL_SECONDS=30
PUBLIC_COMPATIBILITY_ENABLED=false
RADIOTEDU_AGENT_ID=school-radio-pc
RADIOTEDU_AGENT_SCOPE=agent:playout
RADIOTEDU_EN_SNAPSHOT_SECRET=<protected-secret-reference>
RADIOTEDU_FR_SNAPSHOT_SECRET=<protected-secret-reference>
```

Public endpoints:

```text
POST /v1/radio/stations/{station_id}/snapshot
POST /v1/radio/stations/{station_id}/plays
PUT  /v1/radio/stations/{station_id}/covers/{cover_id}
GET  /v1/radio/stations/{station_id}/status
POST /v1/radio/stations/{station_id}/sessions/{start|heartbeat|end}
```

Writes require the `school-radio-pc` identity, `agent:playout` scope, per-station HMAC, nonce, timestamp, idempotency key, and correlation ID. Public state excludes paths, logs, incidents, autonomous tasks, secrets, and operator controls. Listener counts come only from station-scoped browser sessions with no IP or browser identity.

The two Codex handoff prompts are stored in:

```text
handoff/broadcast-server/prompt.md
handoff/web-server/prompt.md
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

RadioTEDU streams two isolated AAC-LC stations to one acknowledged shared Icecast host:

```env
PLAYBACK_BACKEND=liquidsoap
LIQUIDSOAP_ENABLED=true
LIQUIDSOAP_COMMAND=liquidsoap
ICECAST_HOST=10.98.98.75
ICECAST_PORT=11154
ICECAST_USER=source
ICECAST_ENCODER_PROFILE=aac_192
ICECAST_PUBLIC=true
RADIOTEDU_EN_SOURCE_CREDENTIALS=<protected-secret-reference>
RADIOTEDU_FR_SOURCE_CREDENTIALS=<protected-secret-reference>
```

English uses `/en` and `https://stream.radiotedu.com/en`; French uses `/fr` and `https://stream.radiotedu.com/fr`. The source credential shared during development must be rotated before production and must never enter the repository or logs.

Generate station-local Liquidsoap files from the station profiles:

```bash
python - <<'PY'
from backend.stations.loader import load_station_profiles
from backend.liquidsoap import render_liquidsoap_config
print("Render each approved station profile on the target machine.")
PY
```

The installed Liquidsoap build must support FDK-AAC. Missing FDK-AAC is a hard preflight failure; do not fall back to MP3. The rendered encoder is `%fdkaac(bitrate=192, aot="mpeg4_aac_lc", transmux="adts", afterburner=true)` with `public=true` and source username `source`.

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
