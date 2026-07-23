# RadioTEDU web ↔ broadcast connection contract

The public website and the broadcast computer exchange status and audio through two deliberately separate paths. There is no remote playout-control path.

## 1. Broadcast computer → website mutual handshake

The broadcast computer always initiates the network connection over outbound HTTPS. The website server must never open an inbound management connection to the broadcast computer.

Before sending status, each station performs:

`POST /v1/radio/stations/{station_id}/handshake`

The JSON body is:

```json
{
  "protocol": "radiotedu-platform/v1",
  "schema_version": 1,
  "station_id": "radiotedu-en",
  "agent_id": "school-radio-pc",
  "client_nonce": "<same fresh nonce used in the signed headers>"
}
```

The request uses the normal `radiotedu-platform/v1` signed headers described below. The server rejects stale timestamps, invalid station/agent binding and replayed client nonces. A successful response includes a fresh `server_nonce`, `server_timestamp`, the echoed station/agent/client nonce, a 60-second expiry and `server_signature`.

The broadcast computer must recompute `server_signature` with the station secret over these UTF-8 newline-delimited fields and compare it in constant time:

```text
radiotedu-platform/v1
handshake-response
{station_id}
{agent_id}
{client_nonce}
{server_nonce}
{server_timestamp}
{correlation_id}
```

Only after that proof passes is the website authenticated. This handshake is a readiness/authenticity check, not a long-lived bearer session: every later snapshot, play and cover request remains independently signed, timestamp-bounded and replay-protected.

The EN and FR shared secrets must first be provisioned on both machines through an approved secret manager or ACL-protected out-of-band transfer. A public Git repository or prompt is not a safe bootstrap channel for those secrets. Until matching secrets exist on both sides, deployment may be healthy but the mutual handshake must be reported as `awaiting_secret_provisioning`, never falsely reported as complete.

## 2. Broadcast computer → website status API

The broadcast supervisor pushes sanitized, station-scoped snapshots, play events and cover images to:

- `https://api.radiotedu.com/v1/radio/stations/radiotedu-en/...`
- `https://api.radiotedu.com/v1/radio/stations/radiotedu-fr/...`

Each request uses the `radiotedu-platform/v1` HMAC protocol implemented by `backend/public_sync.py` and verified by `backend/platform_api.py`. The website service must have distinct protected values for:

- `RADIOTEDU_EN_SNAPSHOT_SECRET`
- `RADIOTEDU_FR_SNAPSHOT_SECRET`

The broadcast computer must use matching station-specific values. Obtain them from the approved secret manager or ACL-protected configuration; never put them in Git, IIS rewrite rules, logs or chat. Required identity is `school-radio-pc`, required scope is `agent:playout`, maximum timestamp skew is 60 seconds, and request bodies are bounded to 256 KiB except covers, which are bounded to 5 MiB.

The website accepts public state only. It exposes no broadcast start/stop, playlist, Liquidsoap, TTS, request or admin command endpoint.

## 3. Browser → existing public streams

The listener browser plays the two existing public stream services directly:

- English (temporary public mount): `https://stream.radiotedu.com/ai`
- French (temporary public mount): `https://stream.radiotedu.com/event`

The website server does not host, proxy, configure or administer these streams. Do not add an IIS binding, ARR rule or private Icecast upstream on the website server. Stream availability is owned by the existing stream service and does not block deployment of an otherwise healthy `/ai` website.

No private Icecast host, port or source credential may appear in browser HTML, the public API, website configuration or deployment reports.

## 4. Browser → public website

The browser loads one listener page at `https://radiotedu.com/ai`. Its in-page selector chooses between the exact temporary public stream URLs `https://stream.radiotedu.com/ai` for English and `https://stream.radiotedu.com/event` for French. It polls only sanitized status endpoints on the website origin/API. `/ai/en` and `/ai/fr` do not exist. The website route and the Icecast `/ai` mount are on different hostnames and must not be conflated.

Run `packaging/web/verify-website-runtime.ps1` on the website server after configuring the loopback public app. It verifies `/ai`, both public status endpoints, the two exact browser stream URLs, and the absence of private Icecast or operator-control references.
