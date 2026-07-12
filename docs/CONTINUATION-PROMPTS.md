# RadioTEDU continuation prompts

## Current checkpoint

Branch: `feature/dual-station-radiotedu`.

This checkpoint implements the dual-station foundation through T26, including
station isolation, catalog/imaging, announcements, Qwen-only contracts and
client policy, programming clocks/rotation, processing profiles, canonical
runtime ownership, and station-specific Liquidsoap templates.

It is **not production-qualified**. The following remain:

- T27–T44: failover, service installation, public sync and website, release
  packaging, and qualification.
- Approved local Qwen voice-reference assets and program promos. T15 records
  these as blocked; no synthetic fallback voice was introduced.
- Long-duration qualification: 72-hour soak, seven-day canary, and 30-day
  supervised production.

The current checkpoint also includes a rendered Liquidsoap guard file and a
work-in-progress T27 failover test. Continue from this branch; inspect the
working tree before changing either.

## Short broadcast-computer prompt

```text
Open the RadioTEDU repository on branch feature/dual-station-radiotedu. Read
docs/CONTINUATION-PROMPTS.md and docs/superpowers/plans/2026-07-11-radiotedu-terra-execution-pack.md. Resume at T27, preserve EN/FR isolation and Qwen-only speech, then finish service packaging and install only after the focused tests pass. Do not claim production qualification until the documented soak/canary gates are complete.
```

## Stream mounts

- English uses the fixed Icecast mount `/spark`.
- French must try these mounts in order and persist the first usable one:
  `/event`, `/etkinlik`, `/ai`, then `/experimental`.
- Never place both stations on the same mount. The mount-probe/fallback choice
  must not block local music playout.

## Short web-server prompt

```text
Open the RadioTEDU repository on branch feature/dual-station-radiotedu. Read
docs/CONTINUATION-PROMPTS.md and the Terra execution pack. Implement the remaining signed public-sync and /ai website work (T31 onward) before deploying radiotedu.com/ai. The website must be public-state-only and must never control or block broadcasting.
```
