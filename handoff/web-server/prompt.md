# RadioTEDU website server — one-way Codex execution prompt

You are Codex running on the authorized RadioTEDU Windows/IIS website server. This is a one-way execution handoff: do not ask the builder computer questions and do not wait for a reply. Use safe local discovery and best judgment. Never print or copy secrets into chat, logs, source files, or reports. Complete every safe step you can, write the redacted deployment report described below, and leave production unchanged only when a required security or infrastructure dependency is genuinely unavailable.

## Immutable baseline and one approved delta

Clone the public repository and check out the exact release tag below. Do not deploy another branch, a moving default branch, or an unverified local copy.

- Repository: `https://github.com/Radio-TEDU/radiotedu-ai.git`
- Web release tag: `v1.0.4`
- Release page: `https://github.com/Radio-TEDU/radiotedu-ai/releases/tag/v1.0.4`

The release contains the website source, supplied brand assets, six program covers, the website-only API, broadcast-connection contract, verification scripts and locked dependencies. It contains no music library, jingles, voice assets or credentials. Apply exactly one product delta after checkout: remove listener counting and listener-session tracking as specified below. Record that server-side patch in the deployment report. Make no other redesign.

## Outcome

Create and deploy the transferred RadioTEDU web package as the live public AI-radio experience at exactly:

- Listener page: `https://radiotedu.com/ai`
- English audio, temporary mount: `https://stream.radiotedu.com/ai`
- French audio, temporary mount: `https://stream.radiotedu.com/event`
- Icecast-only audio origin: `https://stream.radiotedu.com`; it exposes the public audio mounts and never hosts the listener UI or metadata API.
- Protocol-canonical station identities remain `https://stream.radiotedu.com/en` and `https://stream.radiotedu.com/fr` for signed-status compatibility only; the browser release remains pinned to `/ai` and `/event`.

The Icecast-only origin must not serve HTML, API, or application routes.
- Canonical status/write API: `https://api.radiotedu.com/v1/radio/...`

There is a single listener page with an in-page EN/FR station selector. EN and FR are selected inside `/ai`. Do not create `/ai/en` or `/ai/fr`, and do not redirect to `/Radio`, `/radio`, `/rock`, or any other listener route. `https://radiotedu.com/ai` is the HTML listener page, while `https://stream.radiotedu.com/ai` is the temporary English Icecast audio mount; they are different hostnames and must never be conflated. `/event` on the stream hostname is the temporary French audio mount.

The AI application is not expected to exist on this server yet. At handoff time, `https://radiotedu.com/ai` may still be a WordPress redirect to `/ai-music/`; that is the exact mapping this deployment replaces. Preserve all unrelated WordPress/IIS routes and content. Back up the current `/ai` redirect/article and relevant IIS configuration before changing only the exact `/ai` mapping. Do not alter the existing legacy `/radio` or `/rock` audio services.

## Product already built — do not redesign it

Use the transferred source and assets exactly as the approved build. The visual direction is an original RadioTEDU editorial radio profile inspired by the broad information hierarchy of Andon FM, not a replica. It includes:

- a prominent but viewport-balanced RadioTEDU AI editorial introduction; preserve the words `RadioTEDU AI`, but do not let the masthead consume the full first screen;
- an above-the-fold live panel showing station language, current programme, now-playing title and artist, play/pause, codec/bitrate and live state without scrolling;
- a restrained AI-capabilities strip;
- an EN/FR station selector inside `/ai`;
- a large station profile with current-program artwork, now playing, live player, RTAI state, current/next program, 14-day music/talking split, and bounded sound tags;
- a `Recently played` list containing the latest 10 completed music plays with title, artist, programme, timestamp and cover when available;
- `Top songs · last 14 days`, showing the five most-played songs and exact play counts;
- `Top genres · last 14 days`, showing up to six genres ranked by classified music airtime percentage;
- a complete six-program schedule with artwork based on the real Europe/Istanbul dayparts;
- an explanation of the continuous AI-led broadcast model.

Required brand and program assets are built into `dist/frontend`:

- `/brand/radiotedu-logo-white.png`
- `/brand/rtai-logo.png`
- `/programs/overnight_signal.png` — weekdays 00:00–05:59
- `/programs/morning_signal.png` — weekdays 06:00–09:59
- `/programs/campus_frequencies.png` — weekdays 10:00–17:59
- `/programs/night_lab.png` — every day 18:00–23:59
- `/programs/weekend_overnight.png` — weekends 00:00–07:59
- `/programs/weekend_transmission.png` — weekends 08:00–17:59

Keep the supplied RadioTEDU and RTAI marks exact. Do not substitute, redraw, distort, or AI-regenerate them. Do not copy Andon Labs logos, text, source assets, illustrations, or distinctive branded artwork.

Use an Andon FM-inspired information hierarchy with original RadioTEDU branding. This is a status-only listener product with no control surface. No playout controls are permitted. The browser-local play/pause control affects only the visitor's audio element and never issues broadcast, playlist, or Liquidsoap commands.

The listener page has no store, song buying, donations, requests, messages, phone calls, voting, social feed, sharing flow, or admin UI.

The listener page must have no listener count. Remove the visible EN/FR listener metric, all browser session start/heartbeat/end requests, public listener-session endpoints, listener-session storage and `active_website_listeners` from the public response schema. Do not replace it with Icecast listeners, analytics, a hidden counter or a placeholder.

Now-playing and historical information are required and are not listener analytics. The browser must show the signed snapshot's current title and artist immediately when available. The website must store completed signed `play.completed` events and expose bounded station-scoped aggregates in the public status response:

- `recent_plays`: latest 10 completed `music` events, newest first;
- `top_songs_14d`: top five songs by completed play count, grouped by `track_id` when present and otherwise by normalized title plus artist;
- `top_genres_14d`: up to six genres by music airtime percentage over the last 14 days;
- `airtime`: existing music/talking split over the last 14 days.

Extend the sanitized play-event schema with an optional bounded `genre` field for top-genre aggregation. Accept only a normalized public genre label of at most 64 characters; never infer a missing genre with an LLM. Exclude missing/unknown genre from the genre denominator and show an honest unavailable state when no classified genre data exists. All history queries must be station-scoped and bounded. Do not expose event IDs, local paths, queue state or internal track IDs to the browser.

## Architecture boundary

Run `backend.public_app`, never `backend.app`, on the website server. This machine is a public status/API/UI server only. It must not contain or run the music library, jingles, Qwen/TTS, Liquidsoap, the autonomous playout orchestrator, operator logs, Icecast source credentials, or any remote broadcast-control endpoint.

Accepted public state is limited to the strict sanitized snapshot/play/cover protocol implemented in the transferred revision after applying the approved no-listener-count delta. Reject local paths, source credentials, private research/rundown internals, arbitrary tags, logs, incidents, tasks and extra fields.

Fixed signed-status contract:

- station IDs: `radiotedu-en`, `radiotedu-fr`
- display languages: `en`, `fr`
- signed station stream identifiers remain `/en`, `/fr` with AAC-LC 192 kbps for compatibility with the broadcast status protocol already implemented in this release;
- temporary browser audio mapping for this website release is English `/ai` and French `/event`, both currently MP3 192 kbps;
- broadcast identity: `school-radio-pc`
- allowed scope: `agent:playout`
- timestamp skew: 60 seconds
- snapshot freshness: `SNAPSHOT_TTL_SECONDS=30`
- maximum snapshot/play request: 256 KiB
- maximum cover upload: 5 MiB
- new deployment compatibility flag: `PUBLIC_COMPATIBILITY_ENABLED=false`

The website server verifies distinct EN and FR HMAC secrets. Obtain them only from the server's approved secret manager or ACL-protected environment. Never request or store an Icecast source password here.

Before accepting operational status, expose the implemented mutual-authentication endpoint:

- `POST /v1/radio/stations/{station_id}/handshake`
- the broadcast computer initiates the outbound HTTPS request;
- the server verifies the normal station-specific signed headers, fresh timestamp and one-time client nonce;
- the server returns a fresh server nonce and station-secret HMAC proof;
- the broadcast computer verifies that proof before considering the web server trusted;
- every subsequent snapshot/play/cover request remains independently signed.

This is not remote control and not an inbound connection to the broadcast computer. Matching station secrets require an approved out-of-band secret bootstrap. Never put them in this prompt, Git, deployment reports or browser code. If matching secrets are not yet present on both machines, report `awaiting_secret_provisioning`; do not claim a successful live handshake.

## Execute

1. Clone `https://github.com/Radio-TEDU/radiotedu-ai.git` into a new versioned server directory, fetch tags, check out detached tag `v1.0.4`, and record the resolved commit SHA. Verify every file listed in `MANIFEST.json` before continuing. Do not deploy the default branch or discard server-owned data.
2. Back up the current IIS configuration and current `/ai` WordPress mapping/content in a recoverable, timestamped server-only location. Record exact rollback commands without exposing secrets.
3. Confirm these required files exist: `backend/public_app.py`, `backend/platform_api.py`, `frontend/src/components/PublicDashboard.tsx`, `frontend/public/brand`, `frontend/public/programs`, `scripts/smoke_public_server.py`.
4. Create an isolated Python environment, install `packaging/web/requirements-web.lock.txt`, run `npm ci`, and run `npm run build`. Confirm `dist/frontend/brand` and all six `dist/frontend/programs/*.png` files exist.
5. Configure an ACL-protected durable database and backup/restore path for snapshots, play events, covers and HMAC replay/idempotency records. Do not create or retain listener-session storage.
6. Configure distinct EN/FR HMAC verification secrets through protected environment/service configuration. Do not output their values.
7. Install `python -m backend.public_app` as a durable least-privilege Windows service bound to loopback. Enable automatic restart and health logging with secret redaction. Do not install the operator app.
8. Remove listener counting from the checked-out web release: delete the visible listener metric, browser session lifecycle calls, listener-session API routes/storage, related public schema fields and listener-count tests. Add negative tests proving that no listener count text, metric, session request or session endpoint remains.
9. Implement the bounded public history aggregates described above from signed completed play events. Add the optional sanitized `genre` play-event field, database migration/indexes, recent/top-song/top-genre queries, public response types, EN/FR UI sections, empty states and tests. Do not fabricate seed data.
9. Configure IIS/ARR so only these application paths reach the public app: `/ai`, `/assets/*`, `/brand/*`, `/programs/*`, `/v1/radio/*`, and `/openapi.json` if intentionally public. Ensure `/ai/en` and `/ai/fr` return 404. Preserve all unrelated WordPress routes.
9. Configure `api.radiotedu.com` HTTPS binding/reverse proxy to the same public app's versioned API. If public DNS for this hostname is absent, configure IIS and certificate readiness but do not silently substitute another canonical origin; record the DNS dependency.
10. Build only the signed status connection specified in `packaging/web/BROADCAST-CONNECTION.md`. Verify the implemented mutual handshake for both stations when matching secrets are already available: the broadcast computer initiates signed requests and verifies the server's signed nonce response. Then accept signed EN/FR status and play events at `https://api.radiotedu.com/v1/radio/...`. If matching secrets are not available on both machines, finish the safe website deployment, record `awaiting_secret_provisioning`, and provide an ACL-protected server-local secret provisioning checklist in the report. The website server never initiates a connection to the broadcast computer, connects to Icecast or sends playout commands.
11. Do not create, modify, proxy, bind or administer `stream.radiotedu.com` on this website server. The website's browser audio element must map EN directly to `https://stream.radiotedu.com/ai` and FR directly to `https://stream.radiotedu.com/event`. These two release-pinned URLs override any older stream URL carried in a delayed signed status snapshot. Treat their availability as an external stream-service state, not as a website-server deployment responsibility.
12. Run `packaging/web/verify-website-runtime.ps1` from the website server. Require healthy loopback `/ai` and EN/FR status endpoints, require both exact public stream URLs in the built browser bundle, and reject any private Icecast address or operator-control path in that bundle.
13. Apply API proxy limits before forwarding: 256 KiB for snapshots and play events, 5 MiB for covers. Keep application-side bounded reads as defense in depth.
14. Run the full verification gate below. If it passes, switch only the exact approved website/API/asset routes to the new service, verify from the public hostnames, and retain the rollback backup. If a required gate fails, roll back the changed route/binding and keep the verified staging service available only on loopback.

## Verification gate

Run and retain redacted results:

```powershell
python -m pytest -q
npm test
npm run build
python scripts/smoke_public_server.py --base-url http://127.0.0.1:<staging-port> --strict --json
```

Then verify from the public side:

- `/ai` returns the built listener page; `/ai/en`, `/ai/fr`, `/Radio`, `/radio`, and `/rock` are not application routes;
- the visible EN/FR buttons switch station data without changing `window.location.pathname` away from `/ai`;
- EN audio source is exactly `https://stream.radiotedu.com/ai`, FR exactly `https://stream.radiotedu.com/event`, even when an older status snapshot contains `/en` or `/fr`;
- `/brand/radiotedu-logo-white.png`, `/brand/rtai-logo.png`, and all six `/programs/*.png` assets return 200 with correct MIME types;
- desktop and narrow-mobile layouts render without overflow, obscured controls or unreadable logo treatment;
- the first viewport preserves the RadioTEDU AI masthead and shows the live player and now-playing identity without scrolling at 1440×900 and 390×844;
- fresh, stale, absent and interrupted snapshot states are honest, and the last valid snapshot remains visible;
- no listener count, listener-session request, listener-session endpoint, listener-session table or `active_website_listeners` field exists;
- 14-day airtime aggregation, rounding, empty history and curated tag fallback are correct;
- the signed snapshot's now-playing title and artist render without waiting for play-history aggregation;
- recent plays are newest-first, station-isolated, music-only and limited to 10;
- top songs use completed music plays from the last 14 days, group deterministically and show exact counts;
- top genres use classified music airtime from the last 14 days, exclude missing genres, are station-isolated and have deterministic rounding;
- stale or absent history shows an honest unavailable/empty state and never fabricated demo data;
- HMAC identity/scope/station/path binding, stale timestamps, nonce replay, idempotency, sequence rollback, private/oversized payload rejection, stable redaction and correlation IDs pass;
- EN and FR handshake requests reject wrong secrets/replayed nonces, and successful replies have fresh server nonces with independently recomputable station-specific server proofs;
- public OpenAPI and rendered UI expose no commerce, engagement, operator or playout-control surface;
- the built website points directly to `https://stream.radiotedu.com/ai` and `https://stream.radiotedu.com/event`; no IIS/ARR stream proxy or private Icecast address exists on this server;
- no generated or transferred file contains an Icecast source credential.

## Required local report; no conversational handoff

Write a redacted machine-readable result to:

`C:\ProgramData\RadioTEDU\reports\web-deployment.json`

Include: timestamp, hostname, deployed revision, backup/rollback location, build/test results, IIS bindings, loopback service status, public route status matrix, asset checks, API/DNS/TLS state, EN/FR handshake state (`verified` or `awaiting_secret_provisioning`), stream checks, production cutover result, unresolved dependencies and exact rollback commands. Never include secret values, tokens, credentials, private payloads, listener identifiers or local music paths.

Do not wait for the builder computer and do not start a discussion. After writing the report, your final output must be one line only: `RADIO TEDU WEB COMPLETE — report: C:\ProgramData\RadioTEDU\reports\web-deployment.json` or `RADIO TEDU WEB SAFE STOP — report: C:\ProgramData\RadioTEDU\reports\web-deployment.json`.
