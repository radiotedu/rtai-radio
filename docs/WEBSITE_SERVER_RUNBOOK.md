# RadioTEDU Website Server Runbook

This runbook applies only to the website/API server. It runs the public-only `backend.public_app`; it must not run the broadcast/operator `backend.app`.

The website is status-only with no control surface. It accepts and renders sanitized station state, not broadcast rundown rows, local paths, research notes, voice references, logs, incidents, operator tasks, or source credentials.

## Public routes

- `https://radiotedu.com/ai` — the single listener page, with a visible and keyboard-accessible in-page EN/FR station selector
- Do not create `/ai/en` or `/ai/fr` listener pages.
- `https://stream.radiotedu.com/ai` and `https://stream.radiotedu.com/event` — Icecast audio mounts only
- `https://api.radiotedu.com` — canonical platform API

`https://stream.radiotedu.com` is an Icecast-only origin. It forwards `/ai` and `/event` to the corresponding private mounts at `10.98.98.75:11154`; it must not serve HTML, API, application routes, or Icecast admin/source interfaces.

Use `RadioTEDU` as the visual brand. `Radio TED U` is a speech-only instruction for the broadcasting computer and is not the website wordmark.

Use an Andon FM-inspired dark, minimal, atmospheric information hierarchy with a prominent central listening control, strong now-playing focus, restrained typography, generous spacing, and calm supporting information. Keep original RadioTEDU branding, copy, colors, and assets; do not copy Andon FM branded artwork or source assets.

## Public-only API

Canonical endpoints are:

- `POST /v1/radio/stations/{station_id}/snapshot`
- `POST /v1/radio/stations/{station_id}/plays`
- `PUT /v1/radio/stations/{station_id}/covers/{cover_id}`
- `GET /v1/radio/stations/{station_id}/status`
- station-scoped `/sessions/start`, `/sessions/heartbeat`, and `/sessions/end`

Broadcast writes authenticate as `school-radio-pc` with `agent:playout` and distinct per-station HMAC secrets. Enforce 256 KiB snapshots and play events plus 5 MiB covers both at the reverse proxy and in the application's bounded streaming reader. Also enforce `SNAPSHOT_TTL_SECONDS=30`, 60-second skew, nonce replay protection, monotonic sequence, idempotency, constant-time verification, private-field rejection, redacted errors, and correlation IDs.

New deployments set `PUBLIC_COMPATIBILITY_ENABLED=false`. If an approved compatibility window enables the English `/api/public/status` and session adapter, it must read canonical storage and emit deprecation/sunset headers. The legacy shared-token snapshot write is not part of the public app.

## Listener page boundary

The single `/ai` page contains only a stream player with browser-local play/pause, now playing, current/next program, active website listeners, rolling 14-day music/talking percentages, and curated sound-character tags. Browser-local play/pause starts or pauses only the visitor's audio element; it never issues broadcast, playlist, or Liquidsoap commands and never mutates station state. No playout controls, admin, contact, messaging, calls, purchasing, wallet, rewards, voting, social posting, or sharing is allowed.

Sanitized coverage may appear only as approved public snapshot fields. Never expose rundown IDs, queue state, retry traces, or a route that mutates station playout.

Session storage is station-scoped and stores no IP, user agent, fingerprint, or browser identity. The airtime split excludes silence/unknown and shows unavailable when no classified duration exists. Sound labels use only the curated `warm`, `bright`, `calm`, `focused`, and `energetic` allowlist.

## Staging

```bash
python -m pytest -q
npm test
npm run build
python -m backend.public_app
python scripts/smoke_public_server.py --base-url http://127.0.0.1:<staging-port> --strict --json
```

Verify `https://radiotedu.com/ai`, its in-page EN/FR station selector, localized labels, keyboard focus, responsive layout, fresh/stale/no-data behavior, last-valid-snapshot preservation, station session isolation, and HTTPS AAC browser playback from the `/ai` and `/event` Icecast mounts. Confirm `/ai/en` and `/ai/fr` are not listener pages. Inspect public OpenAPI for forbidden capabilities.

Store HMAC verification secrets only in the website secret manager. This server must never receive the Icecast source password. Record the staged SHA from the builder-published `feature/dual-station-radiotedu` revision, proxy/TLS config, rollback SHA, and redacted conformance results. Stop after staging; do not switch production traffic without explicit authorization.
