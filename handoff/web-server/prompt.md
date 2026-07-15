# RadioTEDU Website Server — Codex Prompt

You are Codex on the RadioTEDU website/API server. This computer is not the builder computer and it is not the broadcasting computer. The builder prepared and transferred an approved RadioTEDU revision; all server discovery, protected configuration, staging, and verification happen here.

## Objective

Stage the public-only RadioTEDU platform and bilingual listener pages. Run `backend.public_app`, not the operator/broadcast `backend.app`. This server receives signed public state but has no music library, AI/TTS, Liquidsoap, source credential, autonomous playout orchestrator, or remote playout controls.

This is a status-only service with no control surface. Accept and render only sanitized EN/FR public state; do not receive station rundown rows, local paths, editorial research notes, voice references, HMAC material in payloads, Icecast source credentials, logs, incidents, or operator tasks.

Stop after staging and conformance verification. Do not switch production traffic, change public DNS, replace a running service, or issue production TLS certificates unless the operator explicitly authorizes it in this task.

## Fixed contract

- API origin: `https://api.radiotedu.com`
- Listener routes: `/ai`, `/ai/en`, `/ai/fr`; `/ai` is the English compatibility entry.
- Station IDs: `radiotedu-en`, `radiotedu-fr`
- Public streams: `https://stream.radiotedu.com/en`, `https://stream.radiotedu.com/fr`
- Private Icecast upstream: `10.98.98.75:11154`, mounts `/en` and `/fr`
- Broadcast service identity: `school-radio-pc`
- Allowed scope: `agent:playout`
- Snapshot maximum: 256 KiB
- Timestamp skew: 60 seconds
- Snapshot freshness: `SNAPSHOT_TTL_SECONDS=30`
- Public compatibility flag: `PUBLIC_COMPATIBILITY_ENABLED=false` for new deployments.

The website server does not need and must never receive the Icecast source password.

`RadioTEDU` is the display brand on this server. `Radio TED U` is a speech-only pronunciation instruction owned by the broadcasting computer; do not rewrite the visual brand into spaced words.

## Public product boundary

Each EN/FR page contains only:

- stream player;
- now playing;
- current and next program;
- active website listeners;
- rolling 14-day music/talking percentages;
- curated editorial sound-character tags.

No admin, contact, message, call, purchase, wallet, reward, voting, social posting, sharing, or playout-control capability may appear in the UI or public OpenAPI. Do not imitate Andon FM branding or layout; retain the original RadioTEDU design and only its clear information hierarchy.

The public product may show sanitized coverage summaries supplied by the canonical snapshot, but it must not expose rundown item IDs, queue internals, failure traces, research provenance intended for operators, or any endpoint that can mutate playout.

Listener counts come only from station-scoped session start/heartbeat/end records. Store no IP address, user agent, browser fingerprint, or browser identity.

Music/talking uses actual completed classified airtime over the previous 14 days. Music tracks and instrumental imaging count as music; Qwen speech, live segments, and spoken imaging count as talking; silence and unknown are excluded. Compute music first and talking as `100 - music`. With no classified airtime, show unavailable rather than `0/0`.

Sound tags come only from curated program `vibe` and track `mood` metadata through the bounded allowlist `warm`, `bright`, `calm`, `focused`, `energetic`. Do not run another AI or technical audio analysis.

## Repository and staging procedure

1. Inspect the transferred repository and record `git rev-parse HEAD`, branch/tag, `git status --short`, and known-good rollback SHA. Do not deploy a mutable branch tip or discard server-owned data.
2. Confirm the revision contains `backend/public_app.py`, `backend/platform_api.py`, `frontend/src/components/PublicDashboard.tsx`, and `scripts/smoke_public_server.py`.
3. Create an isolated Python environment and install the approved locked dependencies. Run `npm ci` and build the Vite frontend into `dist/frontend`.
4. Store the distinct EN and FR HMAC verification secrets in the server secret manager or ACL-protected environment. Never print, log, commit, or paste them into Codex. Do not configure a source password on this machine.
5. Configure a durable database path and backup/restore procedure for snapshots, play events, covers, idempotency records, nonce replay records, listener sessions, and the last valid station snapshots.
6. Start the staging service with `python -m backend.public_app` or the equivalent service-manager command. Bind it behind the staging reverse proxy; do not expose `backend.app`.
7. Configure the main website reverse proxy so `/ai`, `/ai/en`, `/ai/fr`, `/assets`, and versioned API/session paths reach the public app as appropriate. Configure `api.radiotedu.com` for the canonical API. Reject oversized requests at the proxy before forwarding: 256 KiB for snapshots and play events, and 5 MiB for cover uploads; keep the application-level bounded streaming checks enabled as defense in depth.
8. Configure `stream.radiotedu.com` to terminate valid HTTPS and proxy `/en` and `/fr` to the corresponding private Icecast mounts. Do not expose the Icecast admin interface or source port publicly.

## Canonical API and security

Expose:

- `POST /v1/radio/stations/{station_id}/snapshot`
- `POST /v1/radio/stations/{station_id}/plays`
- `PUT /v1/radio/stations/{station_id}/covers/{cover_id}`
- `GET /v1/radio/stations/{station_id}/status`
- station-scoped session start, heartbeat, and end endpoints.

Do not expose any control endpoint. Authenticate broadcast writes using `school-radio-pc`, `agent:playout`, and only the two configured station IDs. Require agent ID, timestamp, nonce, HMAC signature, idempotency key, and correlation ID. Bind the HMAC to method, versioned path, identity, station, replay/idempotency fields, and body hash.

Enforce constant-time verification, 60-second skew, nonce replay protection, monotonic station sequence, idempotent play storage, 256 KiB snapshot limit, private-field rejection, stable redacted errors, and returned `correlation_id`. Preserve the last valid snapshot and mark it stale when updates expire.

The deprecated English `/api/public/status` and session adapter may be enabled only for an explicitly approved compatibility window. It must read canonical storage and emit deprecation/sunset headers. The legacy shared-token snapshot write must remain absent.

## Required verification

Run and retain redacted results for:

```bash
python -m pytest -q
npm test
npm run build
python scripts/smoke_public_server.py --base-url http://127.0.0.1:<staging-port> --strict --json
```

Before accepting staging, also prove:

- `/ai`, `/ai/en`, and `/ai/fr` render localized status-only pages with a keyboard-visible language switch;
- the EN player uses `https://stream.radiotedu.com/en` and FR uses `https://stream.radiotedu.com/fr`;
- browser playback works through the public TLS host in staging;
- fresh, stale, and absent snapshots render honestly and the last valid snapshot survives polling failures;
- listener sessions remain station-scoped and store no browser identity;
- rolling cutoff, duration aggregation, rounding, empty history, and sound-tag fallbacks work;
- valid/invalid HMAC, wrong identity/scope/station/path, stale time, replayed nonce, duplicate key, sequence rollback, oversized/private payload, redaction, and correlation tests pass;
- OpenAPI and UI contain none of the forbidden engagement, commerce, social, admin, or playout-control capabilities;
- the deployed application remains status-only with no control surface and accepts no private rundown internals or broadcast secrets;
- no generated file contains the previously shared Icecast source credential.

Report the staged revision, reverse-proxy/TLS assumptions, commands, pass/fail evidence, unresolved blockers, and exact actions still requiring production authorization. Do not perform the production cutover.
