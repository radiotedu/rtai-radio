> Repository copy: all six authentic JSON files listed below are in `docs/fixtures/2026-10-07/`. Use these recorded fixtures for validation, not as newly fabricated production decisions.

# RadioTEDU web/API repair — 7 October 2026

Apply these changes to the existing production RadioTEDU web/API server and
`https://radiotedu.com/ai`. This is a real broadcaster integration, not a demo.
Keep working station status, snapshots, completed plays, completed speech,
listener sessions, authentication, and stream URLs backward compatible.
Use the server's existing provisioned credentials; do not request, print, copy,
or commit secrets. This document and the attached public JSON contain no secrets.

## Confirmed production fault

The broadcaster is making independent local Laya decisions while music plays.
The site still displays a newest EN decision from **2026-10-01T09:03:12Z**,
with an obsolete 16-candidate batch policy. This is stale historical evidence,
not the current selection policy.

A genuine current single-choice event was signed and submitted on 7 October.
The server returned HTTP 422:

```json
{
  "error": {
    "code": "invalid_payload",
    "message": "public event payload is invalid: unexpected fields in public evidence object (validate_selection:200)"
  },
  "correlation_id": "142010ca-3bf0-407b-904c-7be57df49871"
}
```

The oldest pending full-catalog batch also fails with the same error at
`validate_selection:233`. Snapshots and completed speech succeed. The existence
of the selection POST route or status array does not prove that selection
events are being accepted. Find and repair the actual deployed validator and
any equivalent storage, serializer, or response allowlist.

## Authentic fixtures to copy to the server project

These four files on the broadcaster's Desktop are original recorded decisions
in the public API representation. Only legacy candidate `id` is normalized to
`choice_id`; model input, raw output, probabilities, policy hashes, event IDs,
and original occurrence times are preserved:

- `radiotedu-en-laya-oldest-pending-20261007.json`
- `radiotedu-fr-laya-oldest-pending-20261007.json`
- `radiotedu-en-laya-latest-real-20261007.json`
- `radiotedu-fr-laya-latest-real-20261007.json`

Two additional genuine captures made after the broadcaster reload include
the new catalog-cycle diagnostics listed below:

- `radiotedu-en-laya-deployed-real-20261007.json`
- `radiotedu-fr-laya-deployed-real-20261007.json`

The captured deployed EN event completed at `2026-10-07T07:19:59Z`, evaluated
537 eligible tracks and 64 finalists, and records a total safe catalog of
1,935 with 1,382 tracks already reserved/selected in the current rotation cycle.
Those are observed counts for that event, not fixed configuration constants.

Do not use these historical fixtures as fabricated fresh production events.
Use them to repair validation and integration tests. The broadcaster retains
the real backlog in a durable SQLite outbox and retries signed requests.
It now attempts the newest genuine event before draining older evidence, so
unsupported historical events cannot prevent a supported live schema from
reaching the panel. It deletes no rejected evidence. Successful acknowledgements
are recorded only after actual API acceptance.

## API changes

Preserve signed `POST /v1/radio/stations/{station_id}/selection-decisions`, its
existing HMAC protocol, timestamp and nonce checks, correlation IDs, station
scope, `Idempotency-Key`, and 2 MiB body limit. Retain the structured errors.
Support all four actual historical decision schema versions:

- `radio-song-choice-v1`
- `radio-song-choice-batch-v1`
- `radio-song-choice-library-shortlist-batch-v1`
- `radio-song-choice-library-shortlist-single-v1`

Use schema-specific typed validation. Do not merely disable validation or remove
unknown fields from the evidence to make a request pass. Read the four fixtures
and reconcile every top-level and nested field with the deployed allowlists.
Current and earlier live policy versions must remain valid under their original
names and hashes; do not rewrite history to the newest policy.

The current envelope includes `protocol`, `schema_version`, `event_id`,
`station_id`, `event_type`, `occurred_at`, `language`, `program_id`,
`program_name`, `selection_mode`, `selector_version`, `selector_policy_version`,
`selector_policy_sha256`, `model_provider`, `model_version`,
`model_package_version`, `decision_schema_version`, `prefilter_rules`,
`live_selection_queue`, `recent_tracks`, `catalog_candidate_count`,
`catalog_ordered_pool_sha256`, `candidate_tracks`, `shortlist_evidence`,
`finalist_choice_ids`, `model_input`, `model_output_raw_json`, `model_output`,
`program_selection`, `selected_track_id`, and `validation`. Some legacy schemas
omit the live queue or catalog fields; validate them according to their schema.

In particular, accept and preserve these actual nested structures:

1. `candidate_tracks`: the full eligible ordered catalog pool, stable opaque
   track IDs, title, artist, genre, choice IDs, and actual encoder similarity
   and rank fields when present. Examine exact field names in the fixtures.
2. `shortlist_evidence`: `method`, `score_type`, `candidate_count`,
   `ordered_pool_sha256`, `shortlist_size`, `choice_ids_by_similarity_rank`,
   `query_text`, and `candidate_option_texts`.
3. `prefilter_rules`: actual required genre and advisory genre policy,
   recent-artist exclusion fields, stable candidate order, false random
   sampling flag, queued-track exclusions/count/IDs, and nullable current ID.
4. `live_selection_queue`: `mode`, `selection_started_at`, nullable
   `current_track_id`, `pipeline_current_track_id`, `current_track_clock`,
   `queued_track_ids`, `queued_track_count`, `queue_target_depth`,
   `candidate_pool_excludes_queued_tracks`, and `selection_timing`.
   New additive broadcaster diagnostics include integer `catalog_track_count`,
   integer `rotation_cycle_reserved_track_count`, and string
   `rotation_cycle_policy` with value
   `exclude_tracks_selected_in_current_catalog_cycle`. They are optional for
   earlier events and reflect the genuine local no-repeat catalog cycle.
5. `model_input`: preserve the original `state` and typed `questions`, including
   compact v4 state and queue/upcoming-song context. Do not reconstruct or
   expand the prompt and claim it was the model's original input.
6. `model_output_raw_json`: preserve the exact string. Validate a parsed copy
   against the derived output/probability map without rewriting the raw string.
7. `model_output`: retain real choice, choice key, confidence, answer confidence,
   candidate probabilities, action/routing where present, and actual token usage
   including truncation fields. These are genuine model outputs.
8. `program_selection`: active single-choice events must have exactly one
   `typed_model_choice`, matching `selected_track_id`, the typed answer, and the
   actual finalist probability. Preserve historical batch selection bases.
9. `validation`: real boolean completeness/membership/fallback checks, including
   full catalog pool and shortlist membership checks when present.

Check numeric finiteness, probability range/completeness using the broadcaster's
actual rounded distribution tolerance, ID consistency, timestamps, station and
language scope, membership, declared counts, and original ordered-pool/policy
hashes. Never require encoder similarities to sum to one: they are **scores**,
not probabilities. Never invent zero probabilities for non-finalists.
Keep private local paths and credentials out of public responses.

Persist full accepted evidence append-only, uniquely keyed by station/event ID.
Retries are idempotent; conflicting content under an existing ID is an error.
Store original `occurred_at` separately from server receive time. Events may
arrive out of order as the backlog drains. Return acknowledgement only after
commit. Sort the public latest view by original occurrence time, not insertion
time, so a later-arriving historical record cannot replace a newer decision.

Keep `GET /v1/radio/stations/{station_id}/status` and extend its existing
`metrics.recent_song_selection_decisions` read model with the accepted evidence.
Keep `metrics.recent_spoken_segments` for completed speech. Preserve and update
the existing `/selection-decisions/stream` SSE read path with committed genuine
events. Deduplicate event IDs and reconnect cleanly; refresh status after an SSE
reconnect. Document actual accepted schemas in OpenAPI.

## Website presentation

Show separate sections for **On air now**, **Upcoming music**, **Latest Laya
decision**, **Decision evidence**, and **Recent host links**.

- On-air music comes from the current source-clock snapshot. Selecting or
  preparing a future track does not mean it has started playing. A talking
  snapshot displays the actual host transcript/host segment, not a future song.
- Upcoming music displays actual ordered queued track IDs and associated facts
  from accepted queue evidence, with a visible evidence timestamp. Do not call
  stale queued data current. Do not infer the entire queue from one selected ID.
- The latest decision shows original selection start/completion times, program,
  actual song playing when the model acquired its context, the selected song,
  and its real finalist probability. Show source receive time separately.
- Show total safe library size separately from **eligible candidates evaluated**
  and **typed-head finalists**. The safe library had 1,935 tracks on 7 October.
  In the captured EN decision, 549 remained eligible and 64 were finalists;
  earlier tracks in the no-repeat rotation cycle and real filters reduce the
  pool. The FR fixture has 546 eligible candidates. Never label a 64-finalist
  distribution as probabilities for all 1,935 library tracks.
- A searchable, virtualized full-pool table shows title, artist, genre, encoder
  score/rank, finalist status, actual finalist probability where available, and
  chosen status. Non-finalist probability cells say **Not evaluated by the
  choice head**. Include all recorded candidates, not an arbitrary sample of 16.
- A collapsible evidence panel shows the original model input, exact raw output,
  policy/version/hash, filter facts, shortlist method and actual outputs.
  Name it **Decision evidence**, not a fabricated internal-thinking transcript.
  Laya's observable inputs, scores, probabilities and chosen action are real;
  do not invent first-person thoughts or a narrative reasoning chain.
- Host links show the actual completed on-air transcripts with Ankara display
  times while retaining UTC API times, adjacent songs, well-wish flag, TTS and
  authorship metadata. A null text model means the grounded local fallback wrote
  the script; do not attribute those scripts to Qwen. The broadcaster's EN
  configuration is being changed to a contextual link after every song; music
  remains queued ahead and a varied well-wish occurs every third link.
- Display a visible **Selection evidence stale** state when status is fresh
  but selection evidence is stale or absent. Do not imply Laya is inactive from
  an API validation failure. Keep historical events inspectable and label their
  original policy/age. Do not present old 16-candidate batch data as live.

## Acceptance and deployment

1. Validate all four authentic fixtures and both languages without truncating
   the evidence or changing their original IDs, hashes, times or model output.
2. Verify tampered membership, wrong station/language, malformed probabilities,
   private paths and oversized bodies still fail. Check old clients still read
   the unchanged legacy status shape.
3. Deploy the API validator/storage/read model, then the website. Allow the
   broadcaster's existing retries to submit actual pending events; do not
   impersonate it with fabricated events or manually clear its local backlog.
4. Confirm a new real single-choice event receives 2xx and is visible in both
   status and SSE, with its original timestamp, full eligible pool, 64 finalist
   probabilities, and exactly one selected track. Confirm historical batch
   events also receive 2xx, repeated delivery creates no duplicates, and older
   arrivals cannot push the newest decision backwards.
5. Verify a real completed English speech event appears under host links with
   the source-clock time. Preparation alone is not proof of listener reception.
6. Report the deployment revision, genuine accepted event IDs and timestamps,
   and remaining failures. Do not declare success from a snapshot HTTP 200.

## Separate stream-server issue to investigate if announcements still drop

The broadcaster's source connections had accumulated 911 EN and 888 FR
reconnects across approximately four days, frequently reporting
`encoder input stalled without audio progress`. The source server is
`10.98.98.75:11154`, with header
`RADIOTEDU Stream Audio Server/1.1.0 (Icecast 2 Compatible)`; public listener
mounts are `https://stream.radiotedu.com/en` and `/fr`.

Investigate source-body consumption, listener lifecycle and proxy streaming on
that server with its real logs. Preserve long-lived, concurrently flowing source
and listener streams, independent listener queues, and correct HTTP framing.
An initial burst followed by EOF is not continuous streaming. A listener probe
closing its own connection must not close the source or other listeners.
Correlate actual packet gaps with source reconnects before assigning a cause.
Do not hide outages by reporting HTTP 200 or by marking prepared speech aired.

Post-reload observation on 7 October requested 240 seconds of origin audio.
EN decoded only 110.976 seconds before a demuxing read error; FR decoded only
101.928 seconds before an I/O error. Neither completed the requested duration.
EN source connections reconnected twice during that observation despite a
3–4-song ready queue, prepared contextual speech, no PCM cache misses, and
no model error. Do not report this as an end-to-end continuity pass.

The first newly scheduled English link subsequently completed at the source
and was accepted by the speech API:

- Event: `speech-radiotedu-en-ddf1fe3c44394bafaaa7f1dfe07559a3`
- Start: `2026-10-07T07:19:28.183520+00:00`
- End: `2026-10-07T07:19:37.056976+00:00`
- Transcript: “The Sound of Silence by Simon & Garfunkel just finished. This
  is Radio Ted You; next, Here Comes My Baby by Cat Stevens.”

This confirms source completion and speech-event acceptance. It does not
prove uninterrupted reception by every listener. Repair and observe the
listener/source server before claiming the announcement-loss issue completely
resolved end to end.
