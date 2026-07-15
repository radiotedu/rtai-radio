# RadioTEDU `/ai` Listener Experience Design

**Date:** 2026-07-15  
**Status:** Approved direction; implementation pending

## Objective

Present both RadioTEDU stations through one public listener experience at `https://radiotedu.com/ai`. The page should use an Andon FM-inspired dark, minimal, atmospheric information hierarchy while retaining original RadioTEDU branding, copy, colors, and assets.

The streaming hostname is not a website host. `stream.radiotedu.com` serves Icecast audio only.

## Canonical routes and responsibilities

| URL | Responsibility |
| --- | --- |
| `https://radiotedu.com/ai` | The only listener UI; contains an accessible English/French station selector. |
| `https://stream.radiotedu.com/en` | English Icecast audio mount. |
| `https://stream.radiotedu.com/fr` | French Icecast audio mount. |
| `https://api.radiotedu.com` | Signed public-state ingestion and read-only listener status/session API. |

Do not create listener pages at `/ai/en` or `/ai/fr`. Do not publish HTML, application routes, or the RadioTEDU API from `stream.radiotedu.com`.

## Listener interaction

The single `/ai` page offers a visible, keyboard-accessible EN/FR station selector. Switching stations changes the localized public metadata and selects the corresponding `/en` or `/fr` audio source without navigating to a language-specific route.

The page contains only:

- browser-local play/pause;
- now playing;
- current and next program;
- active website listeners;
- rolling 14-day music/talking percentages; and
- curated sound-character tags.

Play/pause affects only the visitor's audio element. The website cannot start, stop, schedule, select, or otherwise control broadcast playout.

## Visual direction

Use an Andon FM-inspired presentation: dark and immersive, restrained typography, a prominent central listening control, clear now-playing emphasis, generous spacing, and supporting station information arranged in a calm hierarchy.

This is an inspiration reference, not a visual clone. Do not copy Andon FM logos, text, illustrations, source assets, or distinctive branded artwork. RadioTEDU remains the visible brand.

The interface must remain responsive and keyboard accessible. It must not add contact, messaging, purchasing, wallet, rewards, voting, social posting, sharing, admin, or remote playout controls.

## Data flow

1. Each isolated broadcast runtime streams AAC-LC 192 kbps to its private Icecast mount and sends signed public state to `api.radiotedu.com`.
2. The website reads sanitized, station-scoped public state from the API.
3. The browser selects either `https://stream.radiotedu.com/en` or `https://stream.radiotedu.com/fr` for audio playback.
4. The website never participates in broadcast scheduling or playout, and the Icecast hostname never hosts the listener application.

## Prompt and documentation changes

Update both machine handoff prompts and the website/broadcast runbooks so they consistently state:

- one listener page at `radiotedu.com/ai`;
- an in-page EN/FR station selector;
- no `/ai/en` or `/ai/fr` listener routes;
- Icecast-only `stream.radiotedu.com` with mounts `/en` and `/fr`; and
- Andon FM-inspired, original RadioTEDU listener styling.

Add packaging-contract tests that fail if either handoff prompt confuses listener routes with streaming mounts or assigns UI/API hosting to the Icecast hostname.

## Out of scope

- Production deployment or traffic switching.
- Changes to Icecast mount names, codec, bitrate, or source authentication.
- Remote broadcast controls of any kind.
- Replicating Andon FM branding or proprietary assets.
