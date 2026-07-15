# RadioTEDU Pop Rundown, Editorial Speech, and Talk-Over Design

**Status:** Approved conversational design; pending written-spec review  
**Date:** 2026-07-15  
**Branch:** `feature/dual-station-radiotedu`

## Purpose

Extend the isolated English and French RadioTEDU stations with a pop-first music identity, natural bilingual presenter links, genre-gated web research, and time-based continuity protection. The design replaces count-only announcement readiness with measurable prepared-audio coverage while preserving the existing dual-station supervisor, station-local orchestrators, AAC Icecast outputs, outbound-only public synchronization, and status-only website.

The finished builder revision will be pushed to GitHub on the feature branch and will retain exactly two target-machine Codex prompts: one for the broadcasting computer and one for the web server. It will not deploy or switch production traffic.

## Goals

- Pronounce the displayed brand `RadioTEDU` as **“Radio TED U”**: “TED” rhymes with “bed,” and “U” is pronounced “you.”
- Make the station pop-first rather than jazz-only, while retaining occasional curated jazz and classical selections.
- Use women and men as presenters in both English and French according to daypart.
- Give pop announcements natural radio filler without web-derived claims about the song.
- Permit sourced editorial commentary only for curated jazz and classical tracks.
- Prepare four hours of station-local programming, keep the next 60 minutes fully rendered, refill below two hours, and maintain six hours of AI-independent fallback audio.
- Implement best-effort radio talk-over with music ducking and a preference, but not a guarantee, to finish before or shortly after the first lyric.
- Ensure web search, the LLM, Qwen TTS, the website, and the other station are never required for music continuity.

## Non-goals

- The system will not buy, license, download, or solicit music, voices, or reference material.
- Web search will not retrieve lyrics or provide pop-song facts.
- The website will not gain contact, messaging, purchasing, voting, social, or playout-control features.
- Public sound-character tags remain curated metadata; they are not produced by technical audio analysis.
- The builder will not perform staging or production deployment.

## Station and Program Identity

English and French remain isolated stations with separate databases, media roots, queues, caches, fallback playlists, health, and orchestrators. The English station uses `en-US`; the French station uses `fr-FR`. Both use the Europe/Istanbul clock.

The overall music mix targets approximately 70–80 percent pop and compatible R&B, dance, indie-pop, and soft rock. Curated jazz and classical tracks may fill the remaining portion. The existing public program names remain, but their music descriptions are broadened:

- **TEDU Dawn:** bright morning pop, lighter R&B, and occasional gentle curated selections.
- **Campus Flow:** focused daytime pop, indie-pop, soft rock, and rhythmic work/study music.
- **Jazz Lab:** evening pop-led listening with intentionally curated jazz features rather than an all-jazz block.
- **Weekend Signal:** relaxed pop, discoveries, familiar songs, and occasional curated jazz or classical pieces.

An explicit station-local overnight continuity window covers every minute not matched by the named programs. It uses pop-first music and short neutral liners; it does not falsely report TEDU Dawn as the current program.

Track genre comes from validated local catalog metadata. Ambiguous or missing genre is treated as ordinary music and does not qualify for researched commentary.

## Presenter and Pronunciation Policy

Display text, API identity, paths, and metadata continue to use `RadioTEDU`. Only text sent to speech synthesis is normalized to `Radio TED U`. Normalization occurs after an announcement is selected and before voice selection, caching, hashing, or synthesis so every speech path receives the same pronunciation form.

Both stations retain four daypart presenters:

- Morning: woman, energetic and clear.
- Daytime: man, conversational and clear.
- Evening/night: woman, calm and intimate.
- Weekend: man, relaxed and friendly.

The French station produces French announcements, filler, and sourced jazz/classical commentary. The English station produces English. The system must never assume that a French voice will translate English text.

## Pop Announcement Policy

Pop tracks do not trigger web search and do not receive researched claims. A pop link may contain only:

- a time-safe greeting based on the known daypart;
- station identity;
- a short listener-friendly phrase such as “have a great afternoon” or “stay with us”;
- a neutral transition such as “more music is coming up”;
- the catalog title and artist.

Templates rotate by language, daypart, and recent usage to avoid repetitive output. A generated variation must pass the same allowlist and word limit. It cannot mention chart history, release dates, collaborators, awards, lyrics, popularity, weather, campus events, or audience behavior unless that information comes from a separately authorized non-song feature.

Examples:

- English: “You’re with Radio TED U. Have a great afternoon—here’s *Title* by *Artist*.”
- French: “Vous écoutez Radio TED U. Passez une excellente journée—voici *Title* de *Artist*.”

If the LLM is unavailable or its result violates policy, a deterministic localized template is used.

## Jazz and Classical Editorial Research

Only catalog tracks explicitly classified as jazz or classical can request song research. Research runs ahead of playout and uses the configured SearXNG provider. The query binds the catalog artist or ensemble and title or work name. A result is eligible only when:

- it has an HTTP or HTTPS source URL;
- the normalized result identifies both the expected artist/ensemble/composer and title/work strongly enough to avoid artist-only collisions;
- the extracted statement is short, factual, and contains no instructions or markup;
- the source, URL, retrieval timestamp, station, track ID, and match evidence are stored with the fact card.

Accepted context may cover musicians, recording or composition context, album or work, movement, period, style, orchestra, conductor, or other concise editorial facts. It may not quote lyrics or long copyrighted text. English and French speech paraphrase the same stored fact card in the station language.

If no eligible result exists, the presenter uses verified catalog title, artist/composer, album/work, and genre only. Unsupported facts are omitted; research failure never blocks the track.

## Durable Time-Based Rundown

Each station gains a station-local `RundownPlanner` backed by its own SQLite database. It stores durable rundown items with planned start, measured duration, program, track, item type, rendering state, source metadata, transition decision, and terminal result. Item states are `planned`, `researching`, `rendering`, `ready`, `queued`, `played`, `failed`, or `superseded`.

Coverage is calculated from measured, unplayed durations rather than item count:

- **Planning target:** at least four hours of valid upcoming local music and imaging.
- **Rendered target:** the next 60 minutes have all required speech and transition artifacts ready.
- **Refill threshold:** background planning starts when valid planned coverage falls below two hours; rendering continuously restores the 60-minute ready window.
- **Fallback target:** at least six hours of validated local music in a station-specific fallback playlist.

The plan respects existing song-repeat and artist-repeat rules and excludes missing or invalid files. EN and FR make scheduling decisions independently; neither station coordinates or blocks the other station's selection.

At startup, a station may declare itself air-ready only when it has four hours of valid planned coverage, 60 minutes of fully rendered coverage, and six hours of validated fallback audio. The target-computer prompt may permit a staging-only music-continuity test, but no production override is added.

## Playout Independence and Continuity

Planning, research, and synthesis are background responsibilities. Liquidsoap consumes only ready items. The live path never waits for web search, the LLM, or Qwen.

Failure behavior is deterministic:

- Web search failure: omit editorial facts.
- Invalid or ambiguous source: reject the fact card and use catalog-only speech.
- LLM failure: use a localized deterministic liner.
- Qwen failure: use an already rendered compatible liner or a music-only segue.
- Rundown worker failure: consume remaining ready primary coverage while the supervisor restarts the station worker.
- Primary exhaustion or invalid media: switch to the validated six-hour fallback playlist.
- Public API or website outage: persist outbound events and continue playout.
- One station failure: recover that station independently without stopping the other.

Liquidsoap retains silence detection, blank skipping, `mksafe`, and primary/fallback switching. The six-hour fallback must contain real validated local audio; an empty `fallback.m3u` is not air-ready. The shared Icecast host and infrastructure-wide power or network loss remain acknowledged failure domains, so the design reduces but cannot mathematically eliminate every form of dead air.

## Talk-Over and Music Ducking

The existing `SeguePolicy` remains the auditable decision owner and is wired into actual Liquidsoap execution. Cue precedence is:

1. Curated vocal-start or intro-end cue.
2. Offline estimated cue with confidence at or above `0.65`.
3. A default opening window capped at six seconds when no reliable cue exists. This best-effort mode is skipped when catalog or prior transition evidence marks the track as an immediate, loud vocal start.

Announcements are normally 5–10 seconds. During talk-over, the incoming track is reduced approximately 10–12 dB with smooth attack and release. With a curated or estimated cue, the presenter should usually finish before the first lyric but may continue roughly one or two seconds into the vocal. With the approved six-second default window, some lyric overlap is accepted when the vocal boundary is unknown. Music returns smoothly to program level when speech ends.

Talk-over is a preference, not a hard vocal-avoidance guarantee. A track that starts with loud vocals, has an unsuitable opening, or cannot accommodate intelligible speech uses a sequential transition. Each decision records cue source, confidence, estimated vocal boundary, ducking level, speech duration, overlap window, and reason.

The mix must never clip, exceed the station loudness policy, or hide the presenter. Tests verify gain envelopes and timing without requiring the public website to expose technical audio details.

## Public Website and Synchronization

The existing `/ai`, `/ai/en`, and `/ai/fr` listener pages remain status-only. They continue to show the player, now playing, current and next program, website listeners, rolling Music/Talking percentages, and curated sound-character tags.

Rundown and research internals are not exposed. Public snapshots use sanitized program and track identity only. The outbound-only `PublicSyncService` remains unable to select music or control Liquidsoap. The web-server prompt is updated only as necessary to accept compatible sanitized state and preserve EN/FR isolation; it must not add control endpoints.

## Data Migration and Compatibility

New rundown, fact-card, transition-decision, and template-rotation tables are introduced through additive migrations. Existing tracks, programs, play history, public events, and outbox records are preserved. Existing count-based prebuffer rows may be consumed during migration only when their media files remain valid; new readiness decisions use duration coverage.

The compatibility public adapter remains governed by its existing deprecation flag. No new legacy endpoint is introduced.

## Verification

Automated tests are written before production changes and cover:

- `RadioTEDU` display preservation and `Radio TED U` speech normalization in EN and FR;
- pop research prohibition and localized rotating filler;
- jazz/classical exact-identity research, source provenance, rejection, and catalog-only fallback;
- male/female daypart voice selection in both stations;
- explicit overnight coverage;
- four-hour planned coverage, 60-minute rendered coverage, two-hour refill, and six-hour validated fallback;
- restart persistence, invalid media removal, independent EN/FR recovery, and website outage isolation;
- curated, estimated, and default-window talk-over decisions;
- moderate confidence threshold, optional one-to-two-second lyric overlap, ducking gain, smooth restoration, and sequential fallback;
- music-only continuity when search, LLM, or Qwen is unavailable;
- exact Icecast `/en` and `/fr` AAC contracts and absence of secrets in tracked files;
- exactly two canonical machine prompts and no public control or engagement capabilities.

Final verification includes the full Python suite, frontend tests and production build, prompt/static contract scans, secret scans, and a clean feature worktree.

## Handoff and GitHub Publication

The broadcasting-computer prompt will describe media provisioning, four-hour planning, 60-minute rendering, six-hour fallback validation, bilingual voice commissioning, talk-over verification, and staging-only conformance. The web-server prompt will describe the unchanged status-only security boundary and any compatible schema migration required for sanitized state.

After all verification passes, the builder commits the implementation to `feature/dual-station-radiotedu` and pushes that branch to the configured GitHub remote. It does not merge to `main`, create a production deployment, expose credentials, or modify the main worktree’s existing untracked `release/` directory.
