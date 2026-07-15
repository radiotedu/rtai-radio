# RadioTEDU Broadcast Computer — Codex Prompt

You are Codex on the RadioTEDU broadcasting computer. This computer is not the builder computer. The builder prepared and transferred an approved RadioTEDU revision; all runtime discovery, installation, protected configuration, staging, and verification happen here.

## Objective

Prepare the real dual-station broadcast runtime without deploying or switching production traffic. English and French are isolated station child processes under one `RadioTEDU.BroadcastSupervisor`; each child owns its own orchestrator, database, queues, fallback playlist, Liquidsoap process, health, metadata, and logs. The supervisor owns exactly one process-level `PublicSyncService` for both stations.

Stop after staging and conformance verification. Do not start live air, replace a currently running service, or make a production cutover unless the operator explicitly authorizes it in this task.

## Fixed contract

- Station IDs: `radiotedu-en`, `radiotedu-fr`
- Languages: English (`en`), French (`fr`)
- Icecast source: `10.98.98.75:11154`
- Source username: `source`
- Mounts: `/en`, `/fr`
- Encoder: `aac_192`, AAC-LC 192 kbps
- Liquidsoap encoder: `%fdkaac(bitrate=192, aot="mpeg4_aac_lc", transmux="adts", afterburner=true)`
- Directory listing: `public=true`
- Public players: `https://stream.radiotedu.com/en`, `https://stream.radiotedu.com/fr`
- Platform API: `https://api.radiotedu.com`
- Service identity: `school-radio-pc`
- Scope: `agent:playout`
- Snapshot heartbeat: 10 seconds
- Public UI routes: `/ai`, `/ai/en`, `/ai/fr`; the broadcast computer does not host them.

The Icecast host is intentionally shared and is one acknowledged failure domain. Do not merge station runtime state merely because the source host is shared.

## Secret handling

- The source credential previously supplied during development is compromised because it was shared in a task transcript. Require it to be rotated before any production use.
- Obtain the rotated source credential and the two per-station HMAC secrets only from the target machine's protected secret store or ACL-restricted service environment.
- Bind them through `RADIOTEDU_EN_SOURCE_CREDENTIALS`, `RADIOTEDU_FR_SOURCE_CREDENTIALS`, `RADIOTEDU_EN_SNAPSHOT_SECRET`, and `RADIOTEDU_FR_SNAPSHOT_SECRET`.
- Never print secret values, put them in shell history, paste them into Codex, write them to the repository, include them in evidence, or log them.
- Give each station child only its own Icecast source credential. HMAC secrets remain with the supervisor's `PublicSyncService` and must not enter station child environments.

## Repository and staging procedure

1. Inspect the transferred repository and record `git rev-parse HEAD`, branch/tag, `git status --short`, and the known-good rollback SHA. Do not deploy a mutable branch tip. Do not discard local operator data.
2. Confirm this revision contains:
   - `scripts/run_station_forever.py`
   - `backend/public_sync.py`
   - `config/deployment/dual-station.json`
   - `config/stations/radiotedu-en.json`
   - `config/stations/radiotedu-fr.json`
   - `packaging/broadcast/`
3. Create an isolated Python environment and install the approved locked dependencies. Use `npm ci` for the checked-in frontend dependencies if frontend verification is performed. Do not weaken endpoint protection, antivirus, or signing policies.
4. Copy the service environment examples into `C:\ProgramData\RadioTEDU\config`, apply ACLs for the service identity and administrators, then inject secrets without revealing them.
5. Configure the real station media roots. Preserve separate EN/FR databases, queues, announcement caches, fallback playlists, and log roots. Do not invent tracks, artists, play events, listener counts, or program data.
6. Verify Qwen/Ollama and TTS remain loopback-only and that each station has at least `MIN_READY_ANNOUNCEMENTS=5` prepared items before any live-air authorization.
7. Verify the installed Liquidsoap build advertises FDK-AAC. Treat missing FDK-AAC as a hard preflight failure; do not fall back to MP3 or another AAC encoder.
8. Render both Liquidsoap configs and inspect redacted output for the exact host, port, source username, mount, AAC-LC encoder, 192 kbps, metadata, and public listing. No password may appear in evidence or logs.
9. Run the two Windows services from `packaging/broadcast`: `RadioTEDU.SharedAI` and `RadioTEDU.BroadcastSupervisor`. Do not recreate separate EN, FR, or PublicSync Windows services.

## Public synchronization contract

The one `PublicSyncService` consumes station events but never selects music, calls Liquidsoap, exposes a control endpoint, or blocks playout. It must:

- push immediately on track, program, speech-status, or stream-state changes;
- send a 10-second heartbeat;
- persist play events and cover uploads in a durable SQLite outbox;
- coalesce unsent snapshots to the newest station state;
- retry with full-jitter exponential backoff from 1 to 60 seconds;
- preserve station ordering and independent station sequence numbers.

Use only these versioned endpoints:

- `POST /v1/radio/stations/{station_id}/snapshot`
- `POST /v1/radio/stations/{station_id}/plays`
- `PUT /v1/radio/stations/{station_id}/covers/{cover_id}`
- `GET /v1/radio/stations/{station_id}/status`

Every write includes `X-RadioTEDU-Agent-ID`, timestamp, nonce, signature, `Idempotency-Key`, and `X-Correlation-ID`. The HMAC binds method, versioned path, agent ID, station ID, timestamp, nonce, idempotency key, correlation ID, and body hash. Never use the old shared-token snapshot endpoint.

## Required verification

Run and retain redacted results for:

```powershell
python -m pytest -q
npm test
npm run build
python scripts/smoke_broadcast.py --strict --json
python scripts/check_icecast.py
```

Before accepting staging, also prove:

- exact `/en` and `/fr` configs use AAC-LC 192 kbps, `source`, and `public=true`;
- EN and FR child environments do not contain the other station's source credential and contain no HMAC secrets;
- station-local orchestrators start independently and one child can be restarted without stopping the other;
- one and only one `PublicSyncService` owns the outbox;
- a simulated website outage does not interrupt playout and queued events recover in order;
- snapshot coalescing, play replay, and 1–60 second jittered retry work;
- public payloads contain no paths, secrets, logs, incidents, browser identity, operator tasks, or private metadata;
- no generated file contains the previously shared source credential.

Report the staged revision, commands, pass/fail evidence, unresolved blockers, and exact actions still requiring production authorization. Never claim production readiness from unit tests alone.
