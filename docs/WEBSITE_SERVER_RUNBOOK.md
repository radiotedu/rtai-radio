# RadioTEDU Website Server Runbook

This runbook applies only to the website/API server. It runs the public-only `backend.public_app`; it must not run the broadcast/operator `backend.app`.

## Public routes

- `/ai` — English compatibility entry
- `/ai/en` — English listener page
- `/ai/fr` — French listener page
- `https://stream.radiotedu.com/en` and `https://stream.radiotedu.com/fr` — public TLS players
- `https://api.radiotedu.com` — canonical platform API

The stream proxy forwards `/en` and `/fr` to the corresponding private mounts at `10.98.98.75:11154`. It must not expose Icecast admin/source interfaces.

## Public-only API

Canonical endpoints are:

- `POST /v1/radio/stations/{station_id}/snapshot`
- `POST /v1/radio/stations/{station_id}/plays`
- `PUT /v1/radio/stations/{station_id}/covers/{cover_id}`
- `GET /v1/radio/stations/{station_id}/status`
- station-scoped `/sessions/start`, `/sessions/heartbeat`, and `/sessions/end`

Broadcast writes authenticate as `school-radio-pc` with `agent:playout` and distinct per-station HMAC secrets. Enforce 256 KiB snapshots, `SNAPSHOT_TTL_SECONDS=30`, 60-second skew, nonce replay protection, monotonic sequence, idempotency, constant-time verification, private-field rejection, redacted errors, and correlation IDs.

New deployments set `PUBLIC_COMPATIBILITY_ENABLED=false`. If an approved compatibility window enables the English `/api/public/status` and session adapter, it must read canonical storage and emit deprecation/sunset headers. The legacy shared-token snapshot write is not part of the public app.

## Listener page boundary

Pages contain only player, now playing, current/next program, active website listeners, rolling 14-day music/talking percentages, and curated sound-character tags. No playout controls, admin, contact, messaging, calls, purchasing, wallet, rewards, voting, social posting, or sharing is allowed.

Session storage is station-scoped and stores no IP, user agent, fingerprint, or browser identity. The airtime split excludes silence/unknown and shows unavailable when no classified duration exists. Sound labels use only the curated `warm`, `bright`, `calm`, `focused`, and `energetic` allowlist.

## Staging

```bash
python -m pytest -q
npm test
npm run build
python -m backend.public_app
python scripts/smoke_public_server.py --base-url http://127.0.0.1:<staging-port> --strict --json
```

Verify `/ai`, `/ai/en`, `/ai/fr`, localized labels, keyboard focus, responsive layout, fresh/stale/no-data behavior, last-valid-snapshot preservation, station session isolation, and HTTPS AAC browser playback. Inspect public OpenAPI for forbidden capabilities.

Store HMAC verification secrets only in the website secret manager. This server must never receive the Icecast source password. Record the staged SHA, proxy/TLS config, rollback SHA, and redacted conformance results. Do not switch production traffic without explicit authorization.
