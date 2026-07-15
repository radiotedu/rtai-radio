# RadioTEDU Broadcast Computer Runbook

This runbook applies only to the broadcasting computer. The builder machine does not install or run these services.

## Runtime shape

- `RadioTEDU.SharedAI`: loopback-only Ollama/Qwen TTS.
- `RadioTEDU.BroadcastSupervisor`: independently supervises the `radiotedu-en` and `radiotedu-fr` station child processes and owns exactly one process-level `PublicSyncService`.
- Every station child has its own orchestrator, database, music/announcement queues, fallback playlist, Liquidsoap process, cache, health, metadata, and logs.
- Both mounts share Icecast at `10.98.98.75:11154`, an acknowledged host-level failure domain.
- `RadioTEDU` is the display brand. Spoken IDs use `Radio TED U`, with `TED` pronounced like “bed” and `U` like “you.”

## Fixed audio contract

| Station | Mount | Public player |
| --- | --- | --- |
| `radiotedu-en` | `/en` | `https://stream.radiotedu.com/en` |
| `radiotedu-fr` | `/fr` | `https://stream.radiotedu.com/fr` |

Both use source username `source`, profile `aac_192`, AAC-LC 192 kbps, and `public=true`. Liquidsoap must support FDK-AAC and render `%fdkaac(bitrate=192, aot="mpeg4_aac_lc", transmux="adts", afterburner=true)`. Missing FDK-AAC is a hard preflight failure.

The source credential shared during development must be rotated before production. Store the rotated value and HMAC secrets only in the protected service environment. Never include them in the repository, prompt, command history, logs, or evidence.

## Rundown, editorial, and voice contract

- Maintain at least four hours of planned station-local rundown, including a one-track cushion when a duration lands exactly on the threshold.
- Keep at least 60 minutes rendered and ready. Refill only when planned coverage falls below two hours.
- Build a validated station-local fallback playlist with at least six hours of actual music duration. Announcement counts are diagnostic and do not establish air readiness.
- Use operator-supplied, rights-cleared local music only. Include pop alongside jazz and classical; do not contact anyone, purchase music, or fetch replacement tracks.
- Pop uses unsourced radio filler and greetings. Do not research pop songs. Research is limited to jazz and classical, with exact title/artist matching, HTTP(S) provenance, and no lyrics or transcripts.
- Validate approved local male/female English and French Qwen reference clips. Authentic French speech stays disabled until the French reference pack and Qwen health pass on this computer.
- Talk-over prefers curated cues. Estimated cues require `0.65` confidence. Duck by 10–12 dB; allow only bounded best-effort overlap, and use sequential speech/track or music-only when the cue, vocal opening, speech duration, or FFmpeg render is unsafe.

## Protected configuration

Start from `packaging/broadcast/service-env/RadioTEDU.BroadcastSupervisor.env.example`. Required non-secret settings include:

```env
RUNDOWN_PLANNED_SECONDS=14400
RUNDOWN_RENDERED_SECONDS=3600
RUNDOWN_REFILL_SECONDS=7200
FALLBACK_COVERAGE_SECONDS=21600
PUBLIC_SYNC_URL=https://api.radiotedu.com
PUBLIC_SYNC_INTERVAL_SECONDS=10
RADIOTEDU_AGENT_ID=school-radio-pc
RADIOTEDU_AGENT_SCOPE=agent:playout
PUBLIC_COMPATIBILITY_ENABLED=false
```

Inject these through the target secret store without revealing values:

- `RADIOTEDU_EN_SOURCE_CREDENTIALS`
- `RADIOTEDU_FR_SOURCE_CREDENTIALS`
- `RADIOTEDU_EN_SNAPSHOT_SECRET`
- `RADIOTEDU_FR_SNAPSHOT_SECRET`

The supervisor passes only the matching source credential to each station child and no HMAC secret. HMAC secrets stay with PublicSync.

## Staging preflight

```powershell
python -m pytest -q
npm test
npm run build
python scripts/check_ollama.py --install --start --pull
python scripts/scan_music.py
python scripts/smoke_broadcast.py --strict --json
python scripts/check_icecast.py
```

Before starting live air, confirm both station music libraries contain playable real files; each station reports four hours planned, 60 minutes rendered, and six hours fallback; and the two rendered configs have the exact mount/encoder/public settings. Run local male/female EN/FR Qwen tests, including `Radio TED U`, without sending them to air. Exercise pop filler without research, sourced jazz/classical fallback, talk-over, sequential fallback, and music-only recovery.

Install the two services with `packaging/broadcast/install-services.ps1`. Omit `-Start` during staging. Do not create separate EN, FR, or PublicSync Windows services.

## PublicSync behavior

PublicSync sends immediate snapshots on track/program/speech/stream changes and a 10-second heartbeat. Plays and covers use a durable outbox; unsent snapshots coalesce; retry uses full jitter from 1 to 60 seconds. Website failure never blocks playout.

Writes use the versioned station endpoints on `https://api.radiotedu.com` with `school-radio-pc`, `agent:playout`, per-station HMAC, nonce, timestamp, idempotency key, and correlation ID. Never use the deprecated shared-token snapshot write.

## Verification and rollback

Verify one child restart leaves the other running, both mounts are independently healthy, payloads contain no private fields, outage recovery replays durable events, and no credential appears in output. Soak search, LLM, Qwen, FFmpeg, website, and station-child outages while confirming music continuity. Record the staged SHA and rollback SHA from the builder-published `feature/dual-station-radiotedu` revision. Stop after staging unless production startup is explicitly authorized.
