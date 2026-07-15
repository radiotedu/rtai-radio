# RadioTEDU Broadcast Computer Runbook

This runbook applies only to the broadcasting computer. The builder machine does not install or run these services.

## Runtime shape

- `RadioTEDU.SharedAI`: loopback-only Ollama/Qwen TTS.
- `RadioTEDU.BroadcastSupervisor`: independently supervises the `radiotedu-en` and `radiotedu-fr` station child processes and owns exactly one process-level `PublicSyncService`.
- Every station child has its own orchestrator, database, music/announcement queues, fallback playlist, Liquidsoap process, cache, health, metadata, and logs.
- Both mounts share Icecast at `10.98.98.75:11154`, an acknowledged host-level failure domain.

## Fixed audio contract

| Station | Mount | Public player |
| --- | --- | --- |
| `radiotedu-en` | `/en` | `https://stream.radiotedu.com/en` |
| `radiotedu-fr` | `/fr` | `https://stream.radiotedu.com/fr` |

Both use source username `source`, profile `aac_192`, AAC-LC 192 kbps, and `public=true`. Liquidsoap must support FDK-AAC and render `%fdkaac(bitrate=192, aot="mpeg4_aac_lc", transmux="adts", afterburner=true)`. Missing FDK-AAC is a hard preflight failure.

The source credential shared during development must be rotated before production. Store the rotated value and HMAC secrets only in the protected service environment. Never include them in the repository, prompt, command history, logs, or evidence.

## Protected configuration

Start from `packaging/broadcast/service-env/RadioTEDU.BroadcastSupervisor.env.example`. Required non-secret settings include:

```env
MUSIC_DIR=F:/Songs/Jazz
MIN_READY_ANNOUNCEMENTS=5
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

Before starting live air, confirm both station music libraries contain playable real files, each prebuffer has at least five ready announcements, and the two rendered configs have the exact mount/encoder/public settings. Run a target-machine TTS test without sending it to air.

Install the two services with `packaging/broadcast/install-services.ps1`. Omit `-Start` during staging. Do not create separate EN, FR, or PublicSync Windows services.

## PublicSync behavior

PublicSync sends immediate snapshots on track/program/speech/stream changes and a 10-second heartbeat. Plays and covers use a durable outbox; unsent snapshots coalesce; retry uses full jitter from 1 to 60 seconds. Website failure never blocks playout.

Writes use the versioned station endpoints on `https://api.radiotedu.com` with `school-radio-pc`, `agent:playout`, per-station HMAC, nonce, timestamp, idempotency key, and correlation ID. Never use the deprecated shared-token snapshot write.

## Verification and rollback

Verify one child restart leaves the other running, both mounts are independently healthy, payloads contain no private fields, outage recovery replays durable events, and no credential appears in output. Record the staged SHA and rollback SHA. Stop after staging unless production startup is explicitly authorized.
