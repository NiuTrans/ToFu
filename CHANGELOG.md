# Changelog

All notable changes to tofu-open are documented in this file.

## [Unreleased]

### Changed

- Upgraded slide and topic-video production with an auditable `director` mode:
  two contrasting, fact-gated plans compete under one independent critic, with
  deterministic fallback and mode-specific dedup/checkpoints; `standard`
  remains the single-plan A/B control. Slide plans now carry visual modality,
  anchors, and page handoffs; PPTD adds semantic metric/quote/comparison/
  timeline/process/code components plus editable area/doughnut/radar charts.
  Motion beats preserve real-media queries and renderer candidates, optionally
  materialise bounded Pexels photo/video assets when configured, require those
  assets to be used, and publish a credential-free attribution ledger.
  Background slide jobs now mint one bounded owner-authorized text route for
  outline, authoring, image, edit, and visual-QA calls, including worker-thread
  pin propagation and deterministic disposal. An invalid first Connection is
  recorded as route degradation and skipped so a later authorized candidate
  can run instead of failing the whole deck. Metric and comparison components
  also reserve explicit label/support/source regions to prevent dense content
  from colliding with headline values. Portable slide export canonicalizes
  audited CJK family aliases, reports every used/embedded/missing family, and
  production fails closed instead of shipping a partially embedded deck.
  Two-point arrows export as native PowerPoint connectors with shared marker
  direction, while process chevrons pin the same geometry adjustment in HTML
  and OOXML. Outline gates now enforce four layout and visual modalities for
  decks long enough to support them.

- Project Brain now derives one immutable-conversation work item from runtime
  todo/file/isolation signals and projects it from the owner-scoped
  `storage_events` authority. Board and Feed are read-only, the blocked/lease/
  handoff/dispatch model and model-facing Project Brain tools are removed,
  narrative context is acknowledged through a final user-role delta, and
  executable Charter decisions require immutable argv-based Checker versions.
  The backup-backed one-time cutover preserves Watch plus legacy intent as
  non-prompt Attention and removes the former Board/Feed/Status authorities.

- Unified queued Turn identity and durable provider diagnostics around the
  owner-scoped Turn/Attempt model. Queue acceptance now creates the real Turn
  pair and pending Attempt, activation preserves their presentation identity,
  cancellation deletes the unstarted pair atomically, and Steer commits its
  stable injection block before waking the worker. Conversation snapshots now
  expose a shared thread scope and linked queue IDs. Request Inspector can
  lazily read secret-scrubbed raw provider request/response archives under a
  16 MiB-per-Attempt and launch-probed global disk budget, with explicit quota
  truncation and no TTL or silent eviction. The conversation Surface preserves
  keyed DOM nodes across provisional acceptance, queue activation, reconnect,
  and authoritative convergence; concurrent sends choose Steer/Queue/Cancel
  before composer mutation.

- Generalized long-agent cost control around the actual billed prompt shape.
  Automatic working sets now derive from provider/model context-price tiers
  (90% of the last cheaper boundary, 128K fallback), fixed-policy compaction
  payback follows bounded observed rewrite cadence instead of total task age,
  and L1 keeps at most 40/48K tokens of cold tool results while protecting the
  newest complete batch and warm cache prefix. Proactive economics price the
  before/after tiers separately and count only observed warm-prefix replay.
  First-dispatch admission scales with the resolved working set under a 256K
  host ceiling. Turn totals now sum each API round under its own
  model/provider/tier, and the collapsed footer exposes total, uncached,
  cache-read, and output tokens together.
- Added a backend-neutral private field codec for heavy
  `storage_records/task_results` documents. Runtime checkpoint and generic
  record writes now compress each controlled string (`segments`, `metadata`,
  `tool_rounds`, `content`, `thinking`, or `error`) independently at 32 KiB,
  using a versioned zlib level-1 envelope only when the complete stored field
  is smaller. Owner, lifecycle, clocks, experiment ID, and other outer fields
  remain directly queryable. Public `record.get`/`record.list` reads hydrate the
  unchanged value; compact replay hydrates metadata/error and terminal
  content/thinking only when requested, never the segment or tool-round history.
  Summary, abort, and restart-recovery paths can inspect/update outer facts
  without expanding heavy envelopes, while cost-experiment scans decode only
  metadata and retain their plaintext top-level experiment prefilter. Reserved
  key injection and malformed/future/base64/zlib/UTF-8/size-invalid envelopes
  fail closed under the existing protocol/integrity errors, with one shared
  64 MiB decoded and stored-payload budget across the selected fields. Physical
  offline deep-clean now backfills
  historical task results in metadata-first 64-row/64-MiB pages, proves a
  canonical decode/encode/decode round trip, writes only strictly smaller
  documents behind namespace/key/version/source-length CAS, preserves public
  versions and timestamps, checkpoints each write page, and skips under
  `--no-vacuum`. `--analyze` derives threshold candidates inside its existing
  `storage_records` table scan and labels savings as requiring offline semantic
  validation. A read-only shadow copy of all 1,906 live task results preserved
  every public digest, version, and timestamp; 696 rows changed from
  177,187,806 to 78,057,342 bytes, saving 99,130,464 bytes (56.0%) in 3.48
  seconds. These are logical bytes on a disposable copy; no live row, database
  page, deployment, or process was changed.
- Consolidated compaction transcript storage behind the existing owner-scoped
  `storage_compaction_archives` authority. New `archive.create` writes apply the
  same backend-neutral per-message codec already used by frozen conversations;
  reads and idempotency conflicts hydrate the unchanged public message shape,
  while `payloadSize` remains an honest count of private stored message bytes.
  Physical offline deep-clean now backfills current documents of at least
  64 KiB only when canonical round-trip succeeds and bytes strictly decrease.
  It also migrates only the exact retired
  `storage_records/transcript_archive` shape: ownership must resolve uniquely
  through an active or recoverable-trash header, and an existing target must
  match every public transcript and metadata fact. Insert plus version/length-
  fenced source retirement is one transaction. Missing/ambiguous owners,
  malformed rows, duplicate identities, conflicting/oversize targets, and
  over-64-MiB sources stay recoverable; metadata-first 64-row/64-MiB pages
  checkpoint independently, and `--no-vacuum` skips the work. A read-only live
  inventory found 22 current message documents (40,514,151 bytes) and 44 retired
  documents (56,246,348 bytes), with no invalid codec input, owner ambiguity, or
  archive-ID collision. Running the exact production path on a disposable copy
  preserved all 66 public-message digests and all metadata: 6 current rows saved
  1,046,816 bytes and the 44 migrated message documents saved 5,668,873 bytes.
  These are logical-byte results on a temporary copy; no live row, database page,
  deployment, or process was changed.
- Extended physical offline deep-clean to backfill the already-shipped,
  backend-neutral Turn projection codec on inactive inline rows of at least
  64 KiB. This introduces no new stored format: it decodes with the production
  reader, re-encodes with the production writer, proves canonical public
  equality, and performs a revision/owner/conversation/length-fenced update
  only when bytes strictly decrease. Every installed checkpoint/materialized
  head counter must be inactive; malformed and over-64-MiB rows remain
  byte-identical. Selection is metadata-first and bounded to 64 rows / 64 MiB
  of source projection per WAL-checkpointed write page; `--no-vacuum` skips it.
  `--analyze` derives the threshold candidate rows/source bytes inside its
  existing Turn-table aggregate scan and labels actual savings as requiring
  offline semantic validation, avoiding both duplicate I/O and false promises.
  A read-only production-codec sweep validated all 2,948 current rows with
  zero errors. The 379 rows at or above the threshold contain 469,166,433 of
  488,183,469 projection bytes; 300 become strictly smaller, saving exactly
  65,234,859 bytes (to 403,931,574 bytes). Scanning the other 2,569 rows would
  save only 2,164,919 more bytes, so the threshold captures 96.79% of possible
  savings while avoiding 87.14% of row hydrations. The largest improvement is
  11,377,719 to 8,755,011 bytes. These are read-only logical-byte results; no
  live Turn, database page, deployment, or process was changed.
- Removed the frozen header `search_text` copy from every runtime
  `conversation.get`/`conversation.list` SQL projection. The public metadata
  shape keeps an empty compatibility placeholder, while search continues to
  read only the independently rebuilt, owner-scoped
  `storage_search_conversations`/`storage_search_turns` projection. A read-only
  live inventory found 4,548 non-empty legacy header copies totaling
  255,688,300 encoded bytes (largest 11,730,584). On that largest header, a
  25-run warm local fetch/serialization proxy changed from 12.490 ms median,
  35,194,962 traced peak bytes and 11,846,724 result bytes to 0.108 ms,
  3,443 peak bytes and 1,063 result bytes. The physical offline deep-clean now
  validates each frozen transcript and clears its rebuildable header copy in
  the same CAS-fenced write as archive compaction; search-only rows retain
  `messages_json` byte-for-byte, while malformed or over-budget transcripts
  retain both recovery witnesses. `--analyze` reports exact candidate rows and
  bytes plus a `rebuildable_conversation_search_text` reason, deriving both the
  archive total and search subtotal in one table scan. `--no-vacuum` still
  skips the rewrite. These are local read/reclaim proxies; no live row was
  rewritten, no disk space was claimed as already returned, and nothing was
  deployed or restarted.
- Added bounded per-message compression to the frozen pre-Turn transcript
  maintenance path. Explicit physical offline deep-clean now interns exact
  projection copies, gives each message of at least 64 KiB one zlib level-1
  attempt, and stores a versioned JSON envelope only when it is smaller. The
  top-level array and message boundaries remain visible, so 128 KiB-budgeted
  head/tail scans can decode only selected envelopes; SQLite JSONDOC and
  PostgreSQL JSONB share the same representation. Envelope base64 and declared
  decoded sizes are capped at 64 MiB; malformed/future/truncated/trailing/nested envelopes fail
  closed, every update passes a canonical semantic round-trip, and a repeat is
  write-free. A read-only production-encoder sweep validated all 4,544 current
  rows with zero invalid/oversize documents: 3,429 become strictly smaller and
  5,697,009,096 source bytes encode to at most 2,526,642,300 bytes, saving at
  least 3,170,366,796 bytes (-55.65%). Exact projection interning contributes
  453,554,222 bytes and compression another 2,716,399,403 bytes. The largest
  row changes from 62,997,304 to 18,400,552 bytes (-70.79%); its full decode
  trades 0.154 for 0.353 seconds. A 1,163-message sample changes from
  12,122,003 to 10,079,123 bytes (-16.85%) while its exact two-message tail
  remains on the fast path (1.042 versus 1.008 ms). These are stopped-server
  storage/CPU proxies; no live row was rewritten, deployed, or restarted.
- Made deep-clean recovery accounting include the known operator-owned
  directories it previously skipped. `--analyze` now performs independent
  256-entry, non-recursive, non-symlink scans of retired `db_snapshots`,
  `pg_backups`, and `retired_migration_artifacts-*`, reports per-file logical,
  allocated, mtime, and hard-link facts plus per-owner lifecycle, and includes
  their bytes in the recovery total without generating a deletion command.
  On the current volume this reveals 19 files / 1,653,562,933,308 allocated
  bytes that the shallow report omitted, changing total attributed recovery
  material from 546,554,521,088 to 2,200,117,454,396 bytes. No file content was
  read and no artifact was mutated or retired.
- Restored the retired SQLite backup owner's interrupted-copy lifecycle at the
  canonical Sidecar backup boundary. Before capacity admission, online,
  fastpath, and offline backup paths now scan at most 256 `db_snapshots`
  entries and reclaim only unpublished names with the exact historical
  timestamp/PID/UUID grammar after the shared temporary TTL and a dead-owner
  check. Published snapshots, near matches, live/fresh files, malformed job
  manifests, symlinks, and non-regular companions all fail closed; companion
  removal precedes the large primary and the directory is fsynced. The current
  two proven-dead partial copies plus journals occupy 343,090,825,216 bytes
  (about 319.53 GiB). This change schedules safe reclamation on the next backup;
  it did not delete those files, deploy, restart, or claim a latency/API saving.
- Closed the last durable `round_usage` bypass at the Sidecar authority and
  added bounded historical repair to verified-copy/low-space deep clean. New
  generic or atomic event producers now reuse the same copy-on-change projector
  as the manager; retained typed or recovered legacy rows remove only private
  `_wire_*` usage graphs, preserve unknown public fields, and report exact
  decoded input/output savings. Malformed typed history remains byte-identical,
  and a repeat pass is write-free. A read-only projection of the current 3,812
  retained rows changed 3,788 without decode errors and reduced encoded payload
  from 197,109,232 to 5,220,451 bytes (-97.35%, 191,888,781 bytes). This is a
  local storage projection, not an already reclaimed live file or deployed
  latency/RSS/API-billing measurement.
- Reduced the marginal storage cost of permanent exactly-once command receipts
  without adding a TTL or weakening replay. Schema 52 creates an empty v2 table;
  new rows replace arbitrary command IDs with a domain-separated 32-byte key and
  hexadecimal request digests with 32 binary bytes, while retaining operation
  attribution and the existing bounded response. A single indexed lookup reads
  both formats, legacy rows remain byte-for-byte replayable, cross-format
  duplicates fail closed, and upgrade performs no receipt scan or backfill.
  SQLite stores the v2 primary key `WITHOUT ROWID`; PostgreSQL renders the same
  logical schema with `BYTEA`. Reprojecting 380,131 live read-only rows into two
  temporary SQLite tables reduced table-plus-primary-key pages from 126,812,160
  to 78,000,128 bytes (-38.49%, 48,812,032 bytes). This is a local physical
  projection of future row shape, not an immediate live-file shrink, deployed
  PostgreSQL measurement, end-to-end latency, RSS, or API-billing result.
- Removed duplicate durable AttemptEvent envelopes from new Conversation Sync
  replay rows. Schema 51 adds a nullable attempt sequence and a compact partial
  index without backfill; one fenced JOIN reconstructs the identical public
  change while old inline rows remain readable. Sync references protect their
  source from online/offline retention, and turn deletion advances the replay
  floor before removing events; expired sync keys delete in 256-row batches.
  Offline recovery probes for the discriminator so a schema-50 authority can
  still be inspected or cleaned safely before its startup migration.
  In one real read-only 15,424,589-byte change,
  a temporary-SQLite proxy reduced encode peak from 30,809,982 to 1,225 bytes,
  median transaction time from 20.243 to 0.263 ms (77.05x), and WAL from
  15,540,672 to 12,392 bytes (-99.92%); median hydrated read time was
  33.765 vs 32.479 ms. These are local storage proxies, not deployed RSS,
  end-to-end latency, or API-billing savings.
- Fixed an external Turn-checkpoint revision split that made the first
  byte-identical follow-up event advance the Turn while leaving its checkpoint
  one revision behind. The writer now retains that event as an explicit empty
  patch head; schema 50 repairs only the exact one-revision/no-head cohort by
  changing fenced metadata without decoding or rewriting checkpoint JSON.
  Conversation-authority `database_integrity` failures now cooperatively abort
  the worker at the next provider/tool gate instead of withholding thousands of
  frames while invisible model work continues.
- Isolated large live Turn projections from their frequently updated metadata
  rows. One owner/attempt/revision-fenced checkpoint plus the existing exact
  attempt-event patches now reconstructs a live projection, with hard limits of
  64 patches and 1 MiB before checkpoint rollover. Inline live values above
  64 KiB externalize lazily; terminal, recovery, direct mutation, trash, and
  clone boundaries remain fully materialized and event retention protects every
  referenced chain. Schema 49 adds only the nullable checkpoint discriminator
  and an empty checkpoint table, without projection backfill. On a local
  temporary-SQLite proxy of the read-only 955-tool Turn, one structural
  transaction changed from 33.300 to 0.284 ms and WAL from 5,294,232 to 12,392
  bytes; these are storage proxies, not deployed end-to-end or billing savings.
- Added a backend-owned, revision-exact live Turn projection cache so
  consecutive structural events no longer select/decode the same multi-MiB
  projection or rebuild the replay diff after storage has validated the
  incoming patch. Keys include backend, owner, conversation, Turn, attempt,
  and revision; stale/terminal/closed entries disappear and every miss safely
  reloads durable authority. Stable-segment reuse requires private evidence
  from the canonical producer; older/unattested patches safely normalize once
  on their next structural event. The cache is launch-probed (16 MiB lean,
  32 MiB on the 8 GiB reference host, 256 MiB distributed), hard-capped by
  bytes and 256 entries, idle-expires after ten minutes, and charges three
  times stored bytes. On the read-only 955-tool Turn from `mtdx825fjmhmx5`,
  a hit avoids a
  5,235,567-byte BLOB read; with the remaining full encode held constant,
  local median processing changed from 51.985 to 26.407 ms (-49.20%, 1.97x)
  and incremental traced peak from 70.269 to 8.635 MiB (-87.71%). This is a
  local proxy; the 5,235,914-byte full encode and changed-row WAL write remain.
- Replaced cumulative Turn round-trips on structural task events with strict
  revision patches. The Sidecar now validates and copy-on-write applies an
  exact `baseRevision -> base+1` patch against its locked, stable-segment
  projection; malformed/stale patches fail closed, one stale base may refresh
  and rebuild once, and the canonical replay patch plus carried raw task event
  remain atomic. A task-local lock retains one last-applied baseline so only
  the first event reads the full Turn; coalesced progress still checks the
  lightweight attempt fence, and terminal cleanup releases the baseline. The
  redundant full projection was also removed from `event_payload`. On the
  read-only 955-tool Turn from `mtdx825fjmhmx5`, an appended `tool_start`
  command changed from 13,687,938 to 695 serialized bytes (-99.99492%); its
  301-byte patch built in 0.338 ms median. This is a serialization/transport
  proxy, not deployed WAL, RSS, API-billing, or end-to-end latency savings;
  changed Sidecar rows are still fully encoded.
- Prevented explicit tool-result compaction from surviving beside a stale full
  segment mirror. Turn normalization now copies only the compacted
  `toolContent`/status into the uniquely matched, execution-compatible
  `tool_use.result`; blank/reused IDs and tool/input/attempt/task/LLM-round
  mismatches fail closed, ordinary results remain untouched, and extra segment
  metadata/order/identity survive. The completed browser view can then replace
  that segment with its existing `roundRef`. On the largest 955-tool assistant
  Turn in failed conversation `mtdx825fjmhmx5`, 592 explicit L1 mirrors changed:
  serialized segments fell from 15,284,001 to 1,981,839 bytes (-87.03%) and the
  normalized projection from 20,145,745 to 6,843,583 bytes (-66.03%). This is a
  read-only serialization/storage-frame proxy, not observed RSS, API-billing,
  disk-backfill, or end-to-end latency savings.
- Released reconstructible terminal structure from turn-native task carriers.
  After the authoritative Turn and task-result metadata settle, conversation
  attempts now drop `toolRounds`, `segments`, `programRuns`, and checkpoint
  rounds instead of retaining duplicate graphs through the remaining hot-task
  TTL. Inline/headless tasks keep their sole copy for synchronous Chat, Agent,
  trajectory, and compatibility responses. The async commit daemon captures
  its opaque-writer verdict before release, while preference consolidation now
  patches provenance directly rather than refolding (and potentially erasing)
  the full settled Turn. In failed conversation `mtdx825fjmhmx5`, the largest
  assistant Turn held 955 tool rounds plus 956 segments whose serialized
  projections totalled 20,069,740 bytes; this is a durable-payload/local
  retention measurement, not observed RSS or API-billing savings.
- Removed long-task result bodies from settlement and call-ID bookkeeping.
  Settlement is now invocation-local, including nested `execute_tools`
  pipelines; the task-level call-ID ledger retains only signature/name/status,
  shares the launch-probed 64..1,024 receipt ceiling, repairs legacy body rows,
  and is released at terminal settlement. Collision safety no longer depends on
  that evictable ledger: each pipeline indexes historical/enclosing IDs once,
  excludes its current assistant carrier and round rows, and remints every
  recycled or duplicate occurrence. Failed conversation `mtdx825fjmhmx5` had
  955 unique call IDs and 1,048,944 model-visible tool-result characters kept
  alive by the old private ledgers, including at least 310,633 characters L1 had
  already removed from hot context; those references could survive the
  600-second personal terminal TTL. In a 955-message + 955-round synthetic
  replay with 100 conflicts, median remint time changed from 17.831 to 0.459 ms
  (-97.42%, 38.83x). This is local CPU/residency evidence, not an API-billing
  claim.
- Separated retry-safe tools from fresh dynamic observations. Conversation
  references, Project Brain, memory search, schedule listing, swarm artifact
  listing, and motion-video checks no longer reuse a task-lifetime stale cache
  entry; they execute on every call. A separate task-local idempotency resolver
  keeps loop/progress guards intact without granting result reuse. Selected
  control-plane reads and
  `get_agent_result` may replace a byte-identical repeat with a compact receipt
  only while the exact prior paid projection remains in active context and the
  receipt is both at most half its characters and strictly fewer model tokens.
  Compaction, content change, or a failed size gate restores the full result.
  The launch-bounded tracking map stores only digests/call/evidence IDs, never
  a second result body, does not compete with expensive result-cache slots, and
  is released at terminal settlement. A conservative replay of failed
  conversation `mtdx825fjmhmx5` changed 99 selected calls from 49,486 to 24,797
  projected characters (-49.89%) and 13,553 to 6,619 counted GPT-5.6 tokens
  (-51.16%); this is a historical counterfactual, not observed billed savings.
  Readable 2026-08-25..30 logs had no dedup hit for the newly added non-Project
  families, so that expansion is preventive correctness, not a savings claim.
- Stopped allocating an empty on-disk directory for every swarm session.
  SubAgent creates its transcript parent lazily on the first real stream chunk;
  after rehydration, one cancellable startup worker scans a launch-profiled,
  hard-capped number of immediate entries and uses atomic `rmdir` only on empty
  directories. Transcript files, nested content, symlinks, and concurrent
  writers remain untouched, and shutdown bounded-joins the worker. The current
  root has 5,811 empty directories among 7,542 (77.05%). On a temporary fixture
  with those 5,811 empty directories plus 200 valuable directories, cleanup
  took 0.0568 seconds, preserved all valuable directories, and reduced a
  missing cross-turn log lookup from 18.218 to 0.592 ms median (-96.75%). These
  are pure local-filesystem measurements, not end-to-end startup latency.
- Made no-ID `await_agents` delivery-aware and delta-oriented. Results returned
  by await/get-result, automatically injected from the background inbox, or
  restored with durable `delivered` evidence cannot immediately satisfy later
  no-ID waits again; explicit IDs and `get_agent_result` remain replayable. The
  ledger retains only IDs present in the bounded completed-result map. A
  delivery-aware replay of one 739-tool-row production trace found that 91 of
  92 no-ID waits returned only previously delivered payloads, repeating 221,140
  characters / 70,652 raw tool tokens. Ninety were await-only model rounds;
  their following calls historically cost ¥16.4289 (10.72% of that trace's
  regular main-loop API cost), which is a counterfactual opportunity ceiling,
  not an observed saving.
- Restricted MCP exact-name/alias/intent phrase boosts to tools already reached
  by inverted postings when that candidate set is sparse. Because those same
  fields build the postings, any phrase match containing a searchable term must
  be in that set; termless punctuation such as `++` keeps the complete scan.
  Candidate density at or above seven eighths uses contiguous catalog iteration
  instead of paying near-full dictionary lookups, avoiding a regression for
  broad generic terms. No retained index was added. With 256 tools, an
  unmatched short query changed from 0.1686 to 0.0176 ms median (-89.91%,
  9.58x), while exact-name and broad-query measurements remained within 0.2%.
- Precomputed MCP's deterministic read-first fallback order in each bounded
  content-addressed catalog index, and recorded the maximum possible exact or
  intent-phrase length. Empty/low-signal requests no longer normalize risk and
  sort the same catalog per conversation, while longform queries that cannot
  equal a name/alias or fit inside an intent stay on inverted postings without
  a redundant full-catalog scan. The tuple adds one shared existing-name
  reference per tool (2,088 bytes for 256 tools; about 8.16 KiB across the lean
  four-index bound and 65.25 KiB at the 32-index hard ceiling). For 256 tools,
  empty-query selection changed from 0.0855 to 0.0119 ms median (-86.08%,
  7.18x), an unmatched short query from 0.2319 to 0.1686 ms (-27.30%, 1.38x),
  and an output-identical 8,000-character fresh query from 1.9073 to 1.7500 ms
  (-8.25%, 1.09x).
- Added an exact-query digest prefix to MCP sticky selection state so an
  unchanged long prompt reuses its stable schema order before Python regex term
  iteration. The existing normalized term digest remains beside it, preserving
  punctuation-equivalent matching and compatibility with live hexadecimal
  states from older code. Both SHA-256 values occupy one 64-byte object (97
  bytes in the measured interpreter versus 105 for the former hex string).
  With 256 tools and an unchanged 8,000-character query, selection changed from
  1.6129 to 0.0139 ms median (-99.14%, about 116x), with p95 changing from
  1.6525 to 0.0148 ms.
- Made MCP sticky-state expiration an ordered-prefix operation on its existing
  LRU instead of scanning every active conversation on every request. State
  touches now use the process monotonic clock, so wall-clock corrections cannot
  invalidate the ordering; capacity eviction, the strict 24-hour boundary,
  pressure clearing, and authority semantics remain unchanged. With 4,096 live
  states, `_prune_states` changed from 0.3740 to 0.000481 ms median (-99.87%,
  about 778x), with p95 changing from 0.4009 to 0.000524 ms.
- Cached MCP private retrieval text beside the bridge's generation-bound rows
  and fingerprint. Stable rounds reuse one immutable mapping instead of joining
  server/tool descriptions and workflow metadata for every request; catalog
  replacement, real disconnect, and effective disabled-set changes invalidate
  all three projections together. Older bridges use the same pure projector,
  and internal rows restore the legacy `server_id` text when that redundant
  public-snapshot field is absent. With 256 tools and one sticky scope,
  output-identical `_build_mcp` changed from 0.591 to 0.049 ms median (-91.74%,
  12.11x), p95 from 0.613 to 0.055 ms, and 100-round traced peak from 0.108 to
  0.066 MiB (-38.85%).
- Stopped resource-budget resolution from probing an adaptive default before
  consuming an already valid launch-materialized or operator value. Positive
  values still pass the same minimum/hard-ceiling clamp; missing, malformed,
  zero, and negative values still use the cached system snapshot, and unknown
  budget names still fail. Tool Search additionally resolves its process-wide
  term capacity once, so catalog-index and sticky-state checks do not repeatedly
  scan the data layout. An output-identical 256-tool pre-request build changed
  from 9.971 to 0.842 ms median (-91.56%, 11.84x), with p95 changing from
  10.850 to 0.859 ms.
- Replaced generic `deepcopy` dispatch for ordinary JSON ToolContract trees
  with a memoized dictionary/list clone while retaining `deepcopy` for custom
  extension values. Provider schemas, private search documents, execution
  documents, aliases, cycles, and default arguments remain independently
  mutable per request; no process-level contract or authority cache was added.
  On a real 64-tool surface, registry assembly changed from 1.906 to 1.648 ms
  median (-13.54%, 1.16x), and execution-document compilation from 1.306 to
  0.944 ms (-27.71%, 1.38x).
- Made executable tool-catalog de-duplication linear for large dynamic MCP and
  plugin surfaces. Assembly now scans any prepopulated authority once, then
  updates one request-local name set on append instead of rebuilding a set from
  the growing list for every tool. Existing prepopulation, same-spec de-dup,
  cross-spec ownership, ordering, visibility, and execution authority semantics
  remain unchanged. With contract compilation held constant, 256 tools changed
  from 4.826 to 0.649 ms median (-86.55%, 7.44x), while 1,024 tools changed from
  67.026 to 2.654 ms (-96.04%, 25.25x).
- Cached the MCP bridge's model-visible catalog projection and content
  fingerprint per catalog generation. Stable request rounds now reuse one
  deterministically ordered tuple of authoritative tool references instead of
  sorting twice, allocating a rich public snapshot, and hashing identical
  content again. Catalog replacement, real disconnect, and effective disabled
  tool changes invalidate atomically; duplicate settings writes and idle stdio
  parking retain the generation. Public snapshots remain isolated shallow
  copies, older bridge implementations retain their compatibility path, and
  retrieval still fails open. With 256 tools and an 8,000-character query,
  output-identical projection plus selection changed from 5.712 to 4.464 ms
  median (-21.86%, 1.28x), p95 from 6.563 to 5.160 ms, and 100-round traced
  peak from 0.972 to 0.618 MiB (-36.45%).
- Replaced MCP pre-request Tool Search's every-tool × every-query-term scan
  with a deterministic inverted index while preserving the exact TF/DF score,
  dependency order, sticky wire-schema order, and fail-open authority boundary.
  Content-addressed indexes are now a launch-derived LRU (4 lean, 8 at the
  8 GiB reference, 32 distributed/hard ceiling) instead of retaining every
  historical catalog fingerprint. Sticky owner/conversation state now enforces
  capacity on every write, including the formerly bypassing small-catalog call
  path; it keeps 1,024 lean / 2,048 reference / 4,096 distributed states for at
  most 24 hours, stores a SHA-256 query identity rather than user text, bounds
  retained used/active tool names to 32/64, and clears under memory pressure.
  Isolated old/new replay returned byte-identical tool-name arrays while a
  256-tool, 8,000-character query changed from 48.11 to 13.13 ms median
  (-72.71%, 3.66x); a 20,000-word stress query changed from 971.40 to 40.74 ms
  (-95.81%, 23.84x). Thirty-two catalog revisions changed retained heap from
  22.129 to 3.079 MiB (-86.09%, four-index lean bound), and 1,024 maximum-size
  sticky queries changed from 8.299 to 0.596 MiB (-92.82%).
- Made per-owner/device browser working-tab affinity a launch-budgeted LRU
  instead of a process-lifetime dictionary. It now shares the browser client
  registry's route ceiling (64 lean, 2,048 distributed, hard maximum 8,192),
  expires after 30 minutes without a real tool action, and clears under memory
  pressure without importing the browser stack. Actual target resolution
  renews affinity; display-only title reads do not. An action receipt that
  proves the tab closed now forgets it immediately, so the next call safely
  reseeds from `list_tabs` instead of repeatedly targeting a stale ID. In a
  10,000-route fixture, retained heap changes from 2.017 to 0.024 MiB (-98.81%;
  0.029 MiB peak); bounded remember costs 17.35 ms total versus 1.94 ms for the
  raw dictionary, about 1.54 microseconds extra per route.
- Bounded login-wall remediation across its whole lifecycle: owner/process
  admission is now reserved before the synchronous browser probe and handed to
  the at-most-ten-minute background poll, eliminating duplicate same-route
  bridge work and preventing unbounded daemon-thread growth. Limits derive
  from the launch-probed browser-poll budget (lean fallback 4 process / 2 per
  owner; hard derived ceiling 64 / 8); saturation returns the current fetch's
  existing clean login-wall failure without opening another tab. The 15-minute
  cooldown is now a 32-entry lean / 512-entry ceiling LRU, and the 20-second
  live-session result cache is a pressure-clearable TTL/LRU (256-entry lean,
  8,192 hard ceiling). A 10,000-route fixture changes combined retained heap
  from 6.005 to 0.189 MiB (-96.85%; 0.207 MiB peak); bounded construction costs
  17.61 versus 12.24 ms total, about 0.54 microseconds extra per route.
- Bounded the exact provider-usage token tier by a launch-probed LRU (personal
  fallback 128, 8 GiB reference 256, distributed 4,096, hard ceiling 8,192).
  The former TTL was only enforced when the same conversation was queried
  again, so one-shot conversation IDs accumulated for the process lifetime.
  Full-capacity insertion now reclaims expired entries before evicting the
  least-recently-used optimization anchor; either miss safely falls through to
  the next local token counter, and memory pressure can clear the reconstructible
  set. Expiration and replacement now share one lock, preventing an expired
  reader from deleting a concurrent fresh write. In a 10,000-conversation
  fixture, retained heap changes from 3.900 to 0.060 MiB (-98.47%; 0.066 MiB
  peak), while full-capacity recording averages 57.15 microseconds per provider
  completion.
- Rejected signal-free application-log lines before the Daily Optimizer parses
  timestamps or runs its signal regex groups. Common lines use cheap marker
  checks with an 8,192-character lowercasing ceiling; longer lines retain exact
  case-insensitive semantics through an allocation-free path. On one frozen
  11,943-line tail, every output field is unchanged while the pure main
  projection changes from 348.76 to 54.18 ms median (-84.47%, 6.44x) with
  0.106 MiB additional traced peak. The separate post-apply pass measured only
  19.21 ms / a 5.33% maximum fusion opportunity, so it remains independent of
  action loading and durable metric writeback.
- Folded each optimizer audit/error snapshot into all of its projections in one
  streaming pass. Audit JSON now produces owner-filtered event counts, model
  switches, and tool-error clusters together; error timestamps produce recent
  excerpts and signature clusters together. No full parsed-entry cache is
  retained, and the audit tail is released before loading the error tail. On
  the current preloaded 9,803-line audit and 1,705-line error snapshots, exact
  output-equivalent projection changes from 232.90 to 154.60 ms median
  (-33.62%, 1.51x), while additional traced peak remains about 0.03 MiB. This
  microbenchmark excludes tail loading and database collectors.
- Reused one immutable, request-local tail per eligible optimizer log and
  released each log family before loading the next. Distributed runs still
  avoid unowned application/error logs; owner-filtered audit evidence remains
  available. Post-apply metrics now count every tracked block domain and all
  tool failures in one candidate-prefiltered application-log pass instead of
  rereading and rescanning the tail twice per action. With the current bounded
  tails and ten tracked domains, equivalent application-log processing changes
  from 1,718.71 to 370.17 ms median (4.64x), total logical tail reads from 97.01
  to 9.00 MiB (-90.72%), and traced peak remains 28.50 MiB. Database collectors
  are excluded, so this is not an end-to-end optimizer-run claim.
- Merged daily-report per-conversation statistics and 800-character transcript
  construction into one message traversal. The digest retains at most the
  first 128 visible turns (the shortest possible rendered rows already exceed
  the character budget before that bound), keeps only the six tool names the
  transcript can render, and still counts every activity/tool fact. Missing
  timestamps retain their split behavior: transcript-visible as legacy text,
  but report activity uses the conversation fallback epoch. On the frozen
  1,163-message sample, rounds and the exact 846-character digest are unchanged;
  decoded-message processing changes from 1.371 to 1.250 ms median (-8.9%) and
  0.215 to 0.061 MiB traced temporary peak (-71.8%). These figures exclude JSON
  decoding and are not an end-to-end report-generation claim.
- Replaced daily-report calendar and exact day-count transcript hydration with
  the owner-scoped `conversation.activity_dates` storage projection. Callers
  send explicit local-midnight millisecond boundaries, so the Sidecar performs
  timezone-free `[start,end)` bucketing and returns one distinct-conversation
  count per interval. Turn-native rows select only the typed top-level
  `timestamp` scalar plus row creation fallback in 64-conversation batches;
  frozen pre-Turn archives load four at a time and retain the same malformed,
  missing-timestamp, clone, and owner semantics. On the frozen July 2026 owner
  month, the old application path projected at least 2,912.35 MiB of message
  material across 1,336 candidates; the new result is 127 bytes. Its direct
  legacy-heavy Sidecar calculation is still 9.10 s / 255.22 MiB peak, so this
  records a cross-process/application-allocation win, not eliminated archive
  parsing or an unmeasured end-to-end latency claim.
- Pruned daily-report backfill candidates before transcript hydration. A report
  for one day now combines the existing `updated_at >= day_start` test with
  `created_at < day_end`; conversations created later cannot represent work
  performed that day (and cloned conversations deliberately receive a new
  creation epoch). On the frozen 2026-07-01 owner sample this reduces candidates
  from 1,749 to 203 and message material from 3,697.59 MiB to 53.86 MiB, avoiding
  3,643.73 MiB / 98.54% of irrelevant hydration before the exact per-message
  timestamp filter. Lazy batch failures now discard the whole reconstructible
  day instead of leaking an exception or feeding a partial day to the LLM.
- Bounded the Daily Optimizer's recent-conversation tool-distribution scan at
  the repository boundary. It now lists at most 200 owner-scoped metadata rows,
  hydrates transcripts through the shared four-ID batches with recursive
  oversize-frame splitting, and excludes settings from both phases. Lazy
  hydration failures are inside the collector's best-effort boundary, so a
  later bad frame returns empty evidence rather than leaking an exception or a
  misleading partial distribution. The current owner's latest 200 rows contain
  only 0.508 MiB of projected transcript/settings material, while the largest
  four-row transcript group is 0.219 MiB, reducing current maximum body-frame
  material by 56.9% while removing the one-archive-frame growth shape. This is a frame-peak
  measurement, not a total-run latency claim; the bounded path deliberately
  trades additional local round trips for protocol and allocation safety.
- Removed the transcript-sized projection hidden in Project Brain Attention's
  provenance-title lookup. Board and Charter items now share one deduplicated,
  owner-scoped `conversation.list` call with messages and settings explicitly
  excluded; an empty settings whitelist becomes SQL constant `{}` rather than
  reading and decoding the stored blob. Human-gated items and the waiting count
  also reuse one immutable Board snapshot instead of issuing two reads. On the
  current largest owner conversation, resolving one title changes from 59.97
  MiB wire / 364.25 ms direct median / 1,126.89 MiB traced peak to 0.36 KiB /
  0.324 ms / 0.004 MiB, while lookup failure still degrades to the existing
  ID-only provenance chip. The collaboration-bar summary now forwards its
  request-local Board and pending-proposal snapshots into that same Attention
  authority instead of reading both again. On the current largest snapshots,
  this removes one 200-event / 128.04 KiB Feed projection and one 34-task /
  27.48 KiB Board projection per summary (0.97 ms direct median and 0.62 MiB
  traced temporary allocation combined), without a TTL or cross-request cache.
- Reused two more request-local Project Brain authority snapshots. Conversation
  Influence now passes its already-read Board into the prompt-shape renderer,
  and Status parses pending proposals plus its recent-block prefix from one
  200-event Feed read instead of fetching another 80-event window. On the
  current largest samples this removes 27.48 KiB / 0.331 ms / 0.105 MiB traced
  allocation per Influence read and 50.67 KiB / 0.241 ms / 0.21 MiB per Status
  collection. Supplied empty snapshots are authoritative, failure still
  degrades each view to its existing safe default, and no result survives the
  request.
- Made Team/Peers and project-feed provenance title resolution metadata-first.
  Stored titles now come from one owner-scoped transcript-free ID projection;
  only an `Untitled` row probes its revision-matched first eight messages, and
  a missing opening user message or epoch change takes the old exact full-read
  fallback. On the current catalog, 4,747 of 4,786 conversations (99.19%) take
  the metadata path. The largest current row changes from 59.97 MiB wire /
  358.66 ms direct median / 1,126.89 MiB traced peak to 0.36 KiB / 0.324 ms /
  0.004 MiB. The largest current Untitled sample is only 4.91 KiB and pays a
  bounded 0.44 ms extra for metadata plus head probing; fallback tests preserve
  late-first-user and concurrent-revision behavior exactly.
- Removed two full-history costs from explicit conversation references without
  changing delivered context. Readable (`raw=false`, including `@`-reference
  injection) transcripts now request their first three and selected recent
  page directly from the owner-scoped repository, preserve absolute message
  numbers, and take the old full snapshot on malformed page shape, epoch
  mismatch, or a requested tail above the Sidecar's 500-message bound. Raw
  debugging retains its dynamic serialized-budget fit but starts from a
  revision-matched 64-message tail probe plus the head; if every candidate
  fits and older rows might also fit, it performs the old full read rather
  than under-delivering. The structured human
  digest likewise probes its bounded suffix for the last substantive message,
  then reads only the exact anchored tail and head; an anchor outside the
  probe or any page-epoch mismatch takes the coherent fallback. On the largest
  current 1,163-message project conversation, the ordinary first-3/last-60
  read stays selection-identical while bytes fall from 13.04 MiB to 400 KiB
  (-97.01%) and uninstrumented direct-read median from 46.12 to 40.56 ms; the
  frozen archive still uses its latency-protecting full-decoder fallback, so
  traced Sidecar peak changes only from 178.35 to 173.45 MiB. Separately, a
  raw probe on that sample preserves the exact 70,324-character JSON result
  while wire falls to 428 KiB (-96.80%) and direct read-plus-fit median falls
  from 47.59 to 44.06 ms. A
  default first-3/anchored-last-100 digest keeps identical rows while response
  bytes fall from 13.04 MiB to 642 KiB (-95.19%) and direct median remains
  41.13 versus 40.46 ms. Separately, a
  no-keyword `list_conversations(limit=20)` now pushes that deliverable bound
  (plus one possible current-conversation replacement) into storage instead
  of requesting 10,000 metadata rows. On the current 4,786-row owner catalog,
  direct median / traced peak / wire change from 57.9 ms / 21.41 MiB / 1.66
  MiB to 0.22 ms / 0.07 MiB / 7.9 KiB. Keyword listing retains the complete
  title-plus-body candidate behavior while merging at most 200 body IDs with
  an exact-Unicode, authority-side title scan that returns only deliverable
  rows. A common 20-title hit changes the old 10,000-row candidate read from
  59.31 ms / 21.41 MiB / 1,698 KiB to 1.22 ms / 0.26 MiB / 7.05 KiB; even a
  complete miss scans only bounded lightweight pages (28.19 ms / 0.49 MiB)
  and returns an empty two-byte list.
- Stopped project-summary generation from hydrating conversation middles that
  its prompt deliberately discards. After the metadata freshness gate, stale
  work now reads owner-scoped first-eight and last-eight message pages and
  proves they contain the exact first two / last six visible turns. Missing
  visible edges, malformed page shapes, or differing `rev`/`msg_count` epochs
  fall back to one coherent full snapshot; a concurrent summary winner is
  still rechecked before dispatch. The repository exposes the Sidecar's
  validated `before_sequence` cursor, and frozen pre-Turn archives can scan a
  bounded JSON prefix as well as a suffix under the same 128 KiB work budget.
  On the largest current project conversation (1,163 messages), the selected
  prompt source is byte-identical while projection bytes fall from 13.04 MiB
  to 181 KiB and traced peak from 178.35 to 13.51 MiB (-98.64% / -92.42%);
  uninstrumented direct-read median remains flat at 46.08 versus 45.56 ms.
  Current logs contain 18 successful project-summary generations, so this is
  a measured per-generation resource reduction, not an end-to-end latency or
  lifetime-RSS claim.
- Bounded legacy-autopilot close-out resolution without weakening historical
  correctness. A live run now resolves its owner-scoped settings pin from one
  metadata-only conversation read; a disarmed run or report anchor inspects a
  128-message tail and performs the old full read only when a miss plus the
  authoritative `msg_count` proves an unloaded prefix. Frozen JSON suffix
  scanning has a 128 KiB code-unit work budget and an average-size preflight,
  so large windows immediately use the existing C-backed full-decoder fallback
  instead of spending seconds in a Python reverse scan. On the largest current
  pinned sample (203 messages), the common path changed from 37.94 MiB / 258.6
  ms / 560.8 MiB traced peak to 14.7 KiB / 0.42 ms / 0.21 MiB. The largest
  current disarmed-summary sample's directly matching tail retained full-read
  latency (42.1 versus 41.1 ms) while reducing response bytes from 8.15 to 4.44
  MiB; before the scan guard that same bounded read took about 4.07 seconds.
  Resolver call-sequence tests pin tail-hit, exact-fallback, and empty-anchor
  behavior; randomized nested-JSON tests pin suffix equivalence.
- Marked eleven background conversation probes as explicitly metadata-only.
  Project-brain config/title/existence/drain, scheduled-turn config, autopilot
  baton settings, task-result parent checks, compaction change notification,
  and turn-list/revision existence checks no longer hydrate transcripts merely
  to read settings, title, revision, or presence. The Sidecar remains the
  owner-scoped authority; no cache or consistency window was added. On the
  current largest frozen archive, one such probe changes from 13.04 MiB /
  120.8 ms / 178.35 MiB traced peak to 1.1 KiB / 0.18 ms / 0.017 MiB
  (-99.99% wire and peak, -99.85% median read time). An AST contract pins all
  eleven call payloads to `derive_messages=False`.
- Kept shared-project 429 admission memory across the upstream's minute-scale
  recovery window without retaining an aggressive backoff. After 30 quiet
  seconds a family now decays to one one-second serialization seed instead of
  disappearing: the first recovery probe remains immediate, while concurrent
  followers are spaced. State retires after 120 quiet seconds or the existing
  two-success recovery signal, remains capped at 256 families, and never parks
  slots or alters health. The current log has 497 explicit per-minute shared
  limit responses; 117 arrived 30–120 seconds after the preceding same-family
  rejection, followed by 157 more same-family rejections within 15 seconds.
  These are opportunity counts, not claimed avoided requests. The executable
  five-arrival boundary admits one immediately, reserves three at 1/2/3 seconds,
  and leaves the fifth unreserved to recheck instead of recreating a herd.
- Collapsed proactive-poll conversation status from two full transcript reads
  to one authoritative two-message tail projection shared by the LLM decision
  and its audit record. The owner-scoped repository now exposes the Sidecar's
  bounded `message_window`; metadata-only and windowed reads omit the unrelated
  archive-sized `search_text` column at SQL projection time, while full reads
  retain it. Frozen pre-turn archives locate and decode only the requested JSON
  suffix, with the existing full decoder retained as an integrity fallback. On
  the current largest 1,163-message archive, one LLM poll's conversation reads
  fell from two 13.04 MiB / 120.8 ms projections to one 11.3 KiB / 12.3 ms
  projection: wire bytes -99.96%, median read time -94.90%, and traced Python
  peak -93.43%. Metadata-only reads are 1.1 KiB / 0.18 ms on the same row.
- Split settlement's project-summary trigger from the compatibility API that
  returns cached text. The lifecycle hook now calls a pure owner/conversation
  coalescing entry, so it no longer performs a metadata read whose result it
  discards before the worker performs its authoritative freshness gate. Every
  settled turn saves one Sidecar RPC: a fresh summary falls from two metadata
  reads to one, while stale work falls from caller metadata + worker metadata +
  full transcript to worker metadata + full transcript. Cached-return callers
  retain the existing `ensure_summary(blocking=False)` behavior.
- Moved project-summary freshness admission ahead of transcript hydration.
  Non-forced refreshes now read the Sidecar's authoritative metadata-only
  `msg_count`/settings/rev projection and return cached text immediately below
  the six-message growth threshold; stale work then loads the full snapshot and
  rechecks freshness so a concurrent summary winner prevents a duplicate model
  call. Forced refresh still goes directly to the transcript. In a controlled
  50,000-message × 1,200-character JSON projection, a fresh check transfers a
  152-byte metadata document instead of a 58.603 MiB transcript (-99.9998%).
- Removed the full-transcript temporary working set from project-summary prompt
  extraction. A bidirectional edge scan now finds exactly the first two and
  last six visible user/assistant turns, preserving byte-identical prompt
  selection for empty, short, long, multimodal, and `originalContent` inputs
  without parsing or retaining discarded middle turns. In a controlled 50,000-
  message fixture with whitespace-trimmed 1,200-character turns, median source
  construction fell from 34.246 ms to 0.005 ms (-99.99%, 6,922×) and traced
  temporary peak from 62.278 MiB to 0.019 MiB (-99.97%). An executable 10,000-
  turn middle raises on content access, proving the optimized path never reads it.
- Replaced process-random and variable-size project-brain freshness keys with
  versioned SHA-256 digests. Status hashes the exact bounded synthesis source;
  Watch hashes that stable digest with the full bounded item text. Restarts no
  longer make unchanged Watch cards stale merely because Python selected a new
  hash seed, while sibling summaries, block text, owners, and active-peer counts
  that actually reach the model now invalidate the projection. Heartbeats and
  prompt-truncated tails remain free. Status/Watch keys are fixed at 74/73
  characters; on the controlled maximum rendered-evidence fixture the stored
  Watch key falls from 3,739 to 73 characters (-98.05%).
- Made the project-status stale view reuse the history snapshot it already
  loaded. It now submits the warm directly to the bounded coalescing lane
  instead of entering the public non-blocking builder and reading `latest` a
  second time. The wire's `refreshing` bit is now the lane's actual admission
  verdict: saturation/rejection returns `false` rather than causing fruitless
  client polling for work that never entered the queue. Tests fail if this path
  re-enters the snapshot reader and cover both accepted and rejected admission.
- Collapsed Watch batch refresh's repeated read fan-out without weakening its
  concurrency guard. The owner/project-scoped `watch.list` result now supplies
  every item row and all items share one immutable pillar-state snapshot;
  direct/manual item refresh still loads its own current state. Each stale item
  retains a separate model call, response trail, fingerprint, `updated_at` CAS,
  and conflict-winner reload, so a concurrent edit cannot publish an answer to
  old text. In the executable 20-item fixture, the high-level read boundary
  falls from one list + 20 item gets + 20 pillar joins (41 calls) to one list +
  one pillar join (2 calls), while all 20 independent persistence guards run.
- Closed the optional-LLM policy gap across project-brain enrichment. One
  `optional_llm_dispatch_kwargs()` authority now couples the launch-profiled
  finite 429 allowance with immediate shared-contention deferral. Conversation
  summaries and automatic titles use it directly (fixing the title path's
  previously documented-but-missing deferral); background status snapshots and
  batched/on-open Watch refreshes now also enter request-local strict
  billing-stop admission and yield before transport when foreground work has
  already exposed provider-family pressure. Cached projections/fallback titles
  survive every skip, while manual Watch analysis and status/Watch Q&A retain
  their attended dispatch policy. This prevents reconstructible workers from
  spending requests or holding their bounded lanes behind known contention.
- Removed the million-Python-call hot path from Paper prompt-injection
  sanitation. The exact Unicode Cc/Cf normalization contract is now a compact
  232-code-point C-level translate/regex plan, while cheap required-literal gates skip
  eight expensive directive regexes on ordinary text; the four special
  Unicode ASCII-case equivalents deliberately retain the full regex path.
  A test derives all current Unicode Cc/Cf code points from the stdlib database
  and proves byte-for-byte parity, including the Unicode exception attacks.
  On the controlled one-million-character/169-section Q&A fixture, sanitizer
  median fell from 258.178 ms to 14.723 ms (-94.30%, 17.54×), whole context
  assembly from 282.397 ms to 42.987 ms (-84.78%, 6.57×), and traced peak
  temporary memory from 9.011 MiB to 3.258 MiB (-63.84%). This benefits every
  repeat Q&A/Deepen prompt without retaining another copy of the paper body.
- Put every agentic Paper workflow inside one finite paid-call envelope, not
  just an exact-repeat breaker. Report, Q&A, Deepen, Insight, Recommend,
  Survey, and Ideate now account every upstream dispatch through the guarded
  chassis, reserve the last admitted agent call for tool-less evidence synthesis,
  and expose an additive `agentUsageV1` snapshot with tokens, actual dispatch
  attempts, pricing coverage, limits, and the forced-final reason. Finite
  stage defaults range from 8–10 dispatches and 160k–480k logical tokens;
  malformed/zero overrides fall back safely and hard ceilings remain 32 calls
  and 2,000,000 tokens. Exact call+world repetition keeps its canonical
  `no_progress` semantics. A provider that emits tool calls after its tool
  authority is removed halts before execution as `agent_budget_ignored`.
  In the executable unmetered/always-unique adversarial fixture, a four-call
  envelope executes three tool rounds and completes on the fourth tool-less
  dispatch instead of having an unbounded path.
- Made Paper Q&A starts content-addressed after ingest. A browser with a valid
  paper hash no longer recovers, serializes, or uploads the parsed paper for
  every question; a cold server source read is capped at 1,000,000 characters,
  then a launch-probed 600-second owner+hash TTL/LRU serves repeat starts after
  an ownership-existence recheck that neither selects the body nor evaluates
  its SQL length. Deletion and cross-owner hash matches fail closed, the hash
  itself is the source revision, process memory relief can reclaim the cache,
  and only an explicit HTTP 400 `paper_source_required` may retry once.
  Network/5xx/stale-paper outcomes remain one-shot, and compatibility starts
  retain the returned hash so later questions take the cheap path. In the
  executable one-million-character start fixture, JSON wire size falls from
  1,000,159 to 143 bytes (-1,000,016 / -99.9857%). In a warm in-memory SQLite
  microbenchmark, 500 owner/hash checks over a one-million-character row fell
  from 191.543 ms with `length(parsed_text)` to 1.432 ms without it (-99.25%,
  133.77×); an executable SQL-shape guard forbids the body scan on this path.
- Bounded the static prefix repeated by every Paper Q&A agent round. Generated
  report and raw-paper sections now share one 60,000-character relevance
  budget instead of independently admitting up to 60,000 each; the ten most
  recent dialogue messages additionally share 24,000 characters with an
  8,000-character per-message ceiling. Questions stop at 8,000 characters and
  source admission at 1,000,000. CJK bigram scoring now retrieves relevant
  Chinese tail sections instead of degenerating to document order. In a
  controlled 11-section-per-source/ten-long-message projection, selected
  source plus history fell from 293,500 to 78,855 characters
  (-214,645 / -73.13%) while both relevant tails remained present. All seven
  agentic Paper owners (Report, Q&A, Deepen, Insight, Recommend, Survey, and
  Ideate) now compose through one finite-progress policy: three consecutive
  identical tool-call/world rounds trip the shared ledger before a fourth
  duplicate execution. Halted work becomes an error rather than a partial
  success; synchronous research aborts likewise cannot leak partial artifacts,
  while the three interactive task owners retain their explicit aborted view.
- Made Paper report starts content-addressed after ingest. A browser with a
  canonical `paper_hash` now sends a hash-only `/report/start`; live work wins
  before cache/image/source reads, a cache hit performs no paper-body read, and
  only a true task miss projects at most the 120,000 prompt characters from
  that owner's library row. An explicit 400 source miss may retry once with a
  120,000-character body; stale or ambiguous failures cannot trigger the paid
  retry. A representative 120k ASCII start body fell from 120,104 to 136 wire
  bytes (-119,968 / -99.89%). The compatible `paper.library.identity` query now
  accepts an optional projection ceiling: report titles and Insight identity
  request zero text, while Podcast fallback requests at most its 40,000
  character prompt budget. Force regeneration resolves source before aborting
  the old task, so missing legacy data cannot destroy useful live work. Paper
  hash reads/writes now bypass the feature-owner override cache through an
  explicit live-state port; switching papers cannot reuse a hash learned from
  the prior report response.
- Moved Paper report/review/rebuttal start and regenerate orchestration into the
  typed report runtime. A per-kind generation fence now rejects stale success
  and failure continuations even when two starts target the same paper; a Stop
  pressed during the slow `/start` round-trip still aborts the authoritative
  task as soon as its ID arrives. Cache-hit starts reuse the canonical reopen
  projection and persist metadata only. Rebuttal drafts now match the backend's
  40,000-character ceiling at the input, memory, localStorage, and request
  boundaries, and retain at most 32 papers. Removing the parallel retained
  command owner shrank `paper/report.js` by 12,231 bytes (115,599 to 103,368)
  and `paper-reader.js` by 129 bytes (35,018 to 34,889); total retained source
  fell by 12,360 bytes (3,230,449 to 3,218,089), leaving 24,213 bytes below the
  architecture ceiling.
- Collapsed every canonical Paper cache reopen from four sequential Sidecar
  queries (base, Insight, terminology backfill, checkpoints) to one bounded
  owner-scoped `paper.report.reopen` aggregate (-75%). The request groups at
  most eight companion keys by selectable base language; the Sidecar returns
  only the group belonging to the preferred/fallback row it actually selected,
  so the unused language is neither JSON-serialized nor copied into the Web
  process. `/start`, fused `/lookup`, and legacy `/cache` now share one
  projection owner; ordinary lookup/cache worker submissions fall from four to
  one (-75%), while the title-repairing start hit falls from five to one (-80%).
  Pure artifact keys moved behind a dependency-free module, so a cache reopen
  no longer imports the checkpoint, terminology, or Insight generation engines.
- Fused Paper report reopen into the existing lookup endpoint behind an
  explicit `include_cache` capability, so rolling clients keep their zero-read
  task-only lookup while current clients resolve live work, preferred cache,
  and the other plain-report language in one request. A plain-report total miss
  falls from three HTTP calls to one (-66.7%); an active cache hit or Review
  miss falls from two to one (-50%). The owner-scoped repository selects
  preferred/fallback base rows with one portable query instead of two, and the
  canonical reopen projection reads the Insight sibling once instead of twice.
  The typed owner now generation-fences same-paper language races and owns the
  reopen/manual-start policy. Removing that retained implementation shrank
  `paper/report.js` by 10,238 bytes (125,837 to 115,599) and total retained
  source by 9,946 bytes (3,240,395 to 3,230,449), leaving 11,853 bytes below the
  architecture ceiling.
- Moved report/review language preferences, report snapshots, and validated
  reading anchors into the typed Paper runtime. Reconstructible language and
  position maps now retain at most 2,048 entries each, malformed legacy anchors
  are discarded, and the report snapshot LRU retains at most 12 artifacts.
  Removing the parallel retained owners shrank `paper/report.js` by 3,391 bytes
  (129,228 to 125,837) and `paper-reader.js` by 6,457 bytes (41,475 to 35,018).
  Total retained-runtime source fell by 9,848 bytes (3,250,243 to 3,240,395),
  clearing the 3,242,302-byte architecture ceiling with 1,907 bytes of
  headroom while preserving per-language reload and mid-document position.
- Moved Review reading-language persistence, translation polling, UI state,
  and teardown from the largest retained Paper presenter into its existing
  typed runtime owner. The retained `paper/report.js` owner shrank by 8,673
  bytes (137,901 to 129,228), its three parallel state variables disappeared,
  and reconstructible per-paper language preferences now retain at most 2,048
  validated entries. Switching language/paper aborts paid work and generation-
  fences late completion; `/start` and `/lookup` no longer rejoin a carrier
  whose abort fence is already set. Translation terminal polling sends the
  full artifact in either `done.text` or a caught-up top-level snapshot, not
  both; this removes one full artifact (up to 2,000,000 characters/4,000,000
  UTF-8 bytes, about 50% of the prior normal terminal payload) while preserving
  reset/trim recovery.
- Made `paper_translations` the sole normal persistence authority for Babel
  output: reader detail no longer selects/decodes the duplicate bookshelf JSON,
  and cache-hit/completion capture is local-only instead of another library
  PUT. First-save legacy migration and the old full-list/get operations remain
  intact for rolling clients. Translation output is now capped at 2,000,000
  characters and 4,000,000 UTF-8 bytes at both worker and Sidecar boundaries;
  oversized legacy rows become explicit cache misses, never truncated results.
  The worker no longer rebuilds every cumulative prefix, the browser no longer
  reparses every cumulative Markdown prefix, and the terminal event no longer
  duplicates the full text under `result`. At the 128-chunk ceiling this removes
  up to 64.5 final-text-equivalents of prefix joining per side; the controlled
  600,040-byte Babel library write falls to zero (-100%).
- Split browser paper-library persistence into explicit metadata, QA, local
  Babel capture, and all-state scopes. Same-paper writes serialize and same-tick
  scopes coalesce into one partial PUT; a first full save is marked persisted
  only after server acknowledgement so rejection remains safely retryable.
  Folder/title/report metadata no longer retransmits either large JSON field;
  QA saves no longer retransmit Babel, while first-save compatibility remains.
  In a controlled 50×4K-character QA fixture, payloads fell from 802,450 bytes
  to 202,330 for QA (-74.79%) and 82 for metadata (-99.99%).
- Batched paper-bookshelf PDF visibility checks into one requested-filename
  `scandir` snapshot outside the async event loop. Each unique matching file
  pays at most one `DirEntry.stat`, duplicate small stubs share one validation,
  and direct detail checks now use one stat instead of `exists` plus stat.
  A complete snapshot still hides truly missing files, while directory,
  per-file stat, or validation-read failures fail open and never become
  destructive evidence. For a healthy 10K-paper shelf, high-level filesystem
  metadata operations fall from about 20,000 per listing to at most 10,001
  (roughly 50% fewer, before any `readdirplus` stat reuse).
- Split normal paper-bookshelf boot into an owner-scoped metadata-only summary
  query and one on-demand ID detail query. Summaries never select or decode
  `parsed_text`, `qa_history`, `images`, or `babel_cache`; the browser restores
  only the active paper, generation-fences rapid selection, retains at most two
  reconstructible details, and never writes unloaded fields during metadata
  edits or failed hydration. The pre-summary full-list endpoint remains intact
  for rolling/static-client compatibility and still uses one report-correlated
  SQL statement. At the 200K-character row cap, a 10K-paper initial shelf drops
  parsed-body transport from 2.0B characters to at most one 200K detail
  (10,000x), or zero with no active paper; the retained API section also shrank
  by 94 bytes.
- Folded the bookshelf `hasReport` projection into its owner-correlated SQL
  `EXISTS` expression instead of issuing one `paper_reports` lookup per row.
  The existing `(user_id, paper_hash, lang)` primary-key prefix serves the
  probe, any owned language variant still counts, foreign-owner reports do
  not leak, and the wire shape is unchanged. A 10K-row bookshelf now executes
  one Sidecar SQL statement instead of 10,001.
- Bounded research-harvest PDF transport with the parser's existing 200 MiB
  hard ceiling, enforced from honest `Content-Length` and again while reading
  chunked bodies. Downloads retain only 1 MiB in memory before rolling into a
  lifecycle-bound temporary file, then materialize one bounded byte string for
  validation/parsing; responses and temporary storage close on every exit.
  Deterministic oversize rejection no longer pays the transient retry. The
  transport-only worst-case residency drops structurally from roughly 400 MiB
  (`chunks` plus `join`) to about 201 MiB, with at most 200 MiB temporary disk.
- Reused bounded titles already returned by research discovery throughout
  harvest instead of discarding them and resolving every parsed paper again.
  Hints cap at 500 characters; missing titles use one 20-id Atom batch in a
  research run (two batches for the generic 40-paper ceiling), and partial or
  failed batches never fan out into serial retries. Direct interactive
  single-paper harvest retains robust retry/HTML recovery, cache-only batches
  issue no title request, and abort is checked before batch I/O. For 20 fresh
  search-derived papers with titles, worst-case title HTTP calls fall from 60
  (two API attempts plus one HTML fallback each) to zero.
- Batched survey report reuse into one owner/language-scoped excerpt query for
  at most 40 deduplicated paper hashes. The Sidecar selects only 6,000 bounded
  characters and timestamps—never model or report metadata—and a batch outage
  falls back to already-bounded parsed text without exploding back into
  per-paper retries. A 40-paper survey's storage round trips/queries fall from
  at most 41 (one body projection plus 40 report reads) to exactly two (-95.1%).
- Bounded survey gap-map verification before any external grounding: at most
  12 clusters, 40 method rows, 20 gaps, and 40 ids per entry survive, while
  all truncation and suppressed work is exposed in `verification_budget`.
  Unknown citations are capped at 20 unique ids and resolved with one arXiv
  Atom `id_list` request, with no serial retry/HTML fallback; loaded-corpus
  citations remain free and authoritative, and cancellation is checked before
  the batch. The previous title path could amplify 20 ids into 60 HTTP calls.
  A 100-row/50-id adversarial fixture now makes one batch (or 20 injected test
  probes), suppresses 1,300 excess probes, and leaves its input unchanged.
- Replaced research survey/harvest full-bookshelf reads with one owner-scoped,
  arXiv-indexed projection over at most 40 requested papers. Harvest cache
  probes now return metadata and exact text length without transporting paper
  bodies; survey bodies and reports are server-truncated to 6,000 characters
  and exclude auxiliary JSON/report metadata. Batch harvest rejects oversize
  or non-string input before storage/network work and prefetches once without
  weakening parser-version invalidation or storage-failure fallback. At the
  storage contract ceilings, 40 target bodies plus reports fall from up to
  1.2B transported characters to 480K (2,500x); a 20-paper cache probe against
  a 10K-row shelf falls from about 200,020 DB queries to one.
- Enforced the research idea count on model output before any title lookup,
  novelty retrieval, or per-idea rubric, so a model returning 1,000 rows can
  fan out only the requested hard maximum of 12. Rubric judging now performs
  one serial cache/slot warm-up and runs the bounded remainder with the launch-
  probed 1..2 text fan-out; result commit remains in original idea order,
  shared usage accounting is lock-safe, 429 attempts are bounded, and abort or
  failure stops queued admission while draining in-flight work. A controlled
  12-call/80 ms A/B reduced median judge wall time from 967.6 to 567.8 ms
  (-41.3%) with identical ordered output and unchanged call count.
- Bounded and memoized the ideation evidence path without weakening its novelty
  gate. A run verifies at most 20 self-reported prior-art ids per idea, shares
  exact title outcomes and exact successful/no-match retrievals across ideas,
  but retries transport failures. Per-judge rubric policy is now one stable
  system prefix and every background research job has task-scoped dispatcher
  affinity. In a synthetic 12-idea worst case, repeated title probes fell from
  240 to 20, identical retrievals from 12 to one, and the English judge exposes
  9,856 repeated-prefix characters; billed cache savings remain provider-
  dependent and are not claimed.
- Unified standalone research request budgets across the LLM tool, HTTP start,
  engine, recipe, dedup key, and crash manifest. Requests now admit 3..12 ideas,
  at most 20 canonical seed papers, and a 2,000-character direction; search
  adapters are stopped after 20 rows even when they ignore their requested
  limit. Different idea counts or seed corpora no longer join the wrong live
  task, seeded jobs resume with the same corpus, and a failed start settles its
  atomic claim instead of poisoning dedup until the two-hour retention sweep.
  Synthetic 1,000-row search iterators now consume and harvest exactly 20 rows.
- Enforced production research's 12-result limit while consuming each of its
  three fixed query-lane iterators instead of trusting an adapter's
  `max_results` argument and materializing its complete return. A synthetic
  adapter offering 1,000 rows per lane now yields only 36 retained raw rows
  instead of 3,000; ordinary adapters already honoring the limit are unchanged.
- Added durable exact-input reuse for motion scene visual QA and moved the
  result validation/hash/atomic-cache primitive from the slide recipe into the
  shared visual-QA owner. Each scene retains one successful result behind a
  64 KiB file cap; exact contact-sheet pixels, prompt/theme, model, token and
  dispatch settings must match. Identical two-run fixtures reduced VLM calls
  from two to one, changed pixels reran, outages stayed uncached, and cached
  actionable findings still entered repair. Slide cache schema and behavior
  remain unchanged through compatibility wrappers. Writes now evict older
  entries to their declared byte ceiling, and refuse a single oversized row,
  instead of writing a cache that its next bounded read must discard.
- Made the optional motion craft corpus genuinely opt-in. The selected
  blueprint still travels in every frame packet, but ordinary scenes no longer
  install the 104 KB/104-file deep catalog or build and discard its 27,289-
  character index; `allow_craft_browse` retains the full install/index/tool
  path. Eight default prompts remained byte-identical while same-process
  construction fell from 125.268 to 17.736 ms (-85.8%) and traced peak
  allocation from 1,210,228 to 1,165,722 bytes (-3.7%).
- Added a work-conserving bounded window for independent motion scene authors.
  Its worker count is the smaller of launch-probed text/image fan-out and a
  hard ceiling of two; shared craft/font stores prewarm serially, and any
  incomplete dependency forces the existing one-worker path. Full HTML lives
  only in active futures, while static gates and no-regression commits remain
  on the caller thread; abort/fatal failure stops queued admission and active
  drafts stay recoverable. A controlled eight-call/80 ms scheduler A/B reduced
  median wall time from 640.9 to 320.6 ms (-50.0%) without changing call count.
- Added one frozen, lazily prepared motion scene-author context for the shared
  guide prefix. Initial scene authors, transient retries, and visual-QA repairs
  reuse it, while exact prompt text, per-scene loops, and resumed-draft adoption
  stay unchanged; a fully resumed film never prepares unused guidance. On an
  eight-scene same-process A/B, guide reads fell from 24 to 3 and median local
  prompt construction from 22.329 to 15.612 ms (-30.1%), with identical
  319,939 output characters and effectively unchanged traced peak allocation.
- Reordered motion scene-author prompts so the shared frame, hard requirements,
  composition contract, craft guide, and skeleton precede scene id, duration,
  narration, assets, and frame packet. Per-scene tool loops, token ceilings,
  quality gates, draft recovery, and template degradation remain independent.
  On an eight-scene fixture with deliberately divergent ids, the common prefix
  grew from 140 to 20,347 characters, exposing about 141K additional repeated
  prefix characters to provider caching without claiming billed savings.
- Added one frozen slide-author batch context for the deck-level theme block,
  design bible, and PPTD cheatsheet. Exact per-page prompt hashes, page briefs,
  images, sources, repair findings, and YAML checkpoints remain independent;
  cache preflight and bounded author workers reuse only immutable common text,
  and already-aborted work exits before preparing it. An eight-page first-run
  construction A/B (eight cache hashes plus eight authors) reduced median local
  prompt setup from 15.793 to 1.036 ms (-93.4%) and traced peak allocation from
  911,432 to 563,699 bytes (-38.2%).
- Reordered slide page-author prompts so the immutable deck, theme, text-style,
  grounding, design-bible, and PPTD schema contract precedes page number/type,
  brief, assets, and sources. Per-page bounded author/repair rounds, exact input
  hashes, cache reuse, fallback, and visual QA remain independent; author input
  v2 invalidates pages cached under the old layout. On an eight-page synthetic
  design-contract fixture, the common prefix grew from 30 to 17,092 characters
  in Chinese and from 59 to 17,380 in English, exposing about 120K repeated
  prefix characters to provider caching without claiming billed savings.
- Reordered long-form sibling section prompts so their immutable report
  instructions and complete research packet precede the section-specific
  heading, and construct that prefix once per batch. Independent section calls,
  bounded fan-out, quality gates, exact-input checkpoints, and crash resume are
  preserved; the prompt-layout checkpoint revision prevents reuse of prose
  authored under the old ordering. On a 30-card/eight-section fixture, the
  common prefix grew from 53 to 21,218 characters in Chinese and from 89 to
  21,336 in English, exposing about 149K repeated prefix characters to provider
  caching without claiming billed savings.
- Canonicalize ordinary string content, tool-result strings, and standard text
  blocks directly inside the owning field scan instead of routing each through
  the recursive mixed-media normalizer and then rebuilding a filtered text
  list. Input/output text markers, multi-block ordering, tool identities,
  images, unknown-block fallback, and OpenAI/Anthropic equivalence remain
  unchanged. A randomized same-process 241-message A/B reduced median capture
  time from 3.160 to 3.013 ms (-4.7%) with unchanged peak allocation.
- Reused one immutable-configuration stdlib `JSONEncoder` for complex
  top-level wire fields instead of paying the `json.dumps` wrapper and encoder
  construction on every field. Per-call circular-reference/iterator state
  remains local and parallel reentrancy is executable-tested; exact stdlib
  output, primitive shortcuts, malformed fallback, and standalone/live parity
  remain unchanged. A randomized same-process 241-message A/B reduced median
  capture time from 3.373 to 3.146 ms (-6.7%) with unchanged peak allocation.
- Unified canonical field normalization and temporary tool-alignment-key
  extraction in one message scan. The fields-only compatibility projection,
  exact established tool keys, envelope parity, marker slots, and every raw
  evidence view remain unchanged, while live capture no longer rereads each
  message's type, role, content, and tool calls after building its fields. A
  randomized same-process 241-message A/B reduced median capture time from
  3.436 to 3.301 ms (-3.9%) with unchanged peak allocation.
- Assemble combined whole-message raw evidence from field fragments in one
  final join instead of first copying every serialized `key: value` pair and
  then copying those intermediates again. Exact stdlib JSON, field hashes,
  marker detection, invalid fallbacks, and standalone parity are unchanged. A
  representative 241-message A/B improved 3.400 to 3.362 ms (-1.1%) and
  328,301 to 326,548 traced peak bytes; the bounded risk case—one 8 MiB
  serialized field—improved composition from 7.548 to 2.728 ms (-63.9%) and
  peak allocation from 25,166,311 to 8,389,020 bytes (-66.7%).
- Removed construction-only `role` and diagnostic `brief` values from retained
  canonical wire rows. Tool identity is folded directly into the alignment key
  and ordinary keys derive from the already-built field fingerprints; legacy
  rich rows remain readable by `canonical_key`, while diff, marker, envelope,
  raw-byte, and field attribution stay unchanged. A randomized same-process
  241-message A/B reduced median capture time from 3.632 to 3.417 ms (-5.9%)
  and canonical retained size from 136,406 to 129,778 bytes (-4.9%); traced
  peak allocation fell from 335,696 to 329,301 bytes (-1.9%).
- Made combined wire-evidence marker stripping proportional to actual marker
  placement instead of conversation length. The top-level serialization
  already required for raw fingerprints now detects an encoded
  `cache_control` key; marker-free messages reuse their original read-only
  graph, while marker-bearing, malformed, and non-string-key messages retain
  conservative recursive stripping and exact standalone parity. A randomized
  same-process 241-message A/B preserved all evidence and reduced median
  capture time from 3.988 to 3.523 ms (-11.7%), with traced peak bytes falling
  from 336,256 to 335,368.
- Added exact stdlib-compatible fast encoding for primitive top-level wire
  fields and moved canonical tool-argument parse/sort/dump to orjson. Complex
  raw values still use the transport-matching stdlib serializer, malformed
  arguments retain their stripped-string fallback, and protocol-envelope
  equivalence remains unchanged. A same-process 241-message A/B reduced median
  combined capture time from 4.753 to 3.978 ms (-16.3%); a 61-sample run held a
  3.955 ms median with a 3.948–3.961 ms interquartile range.
- Converted canonical per-message semantic fields to process-local keyed
  integer fingerprints and derive ordinary alignment keys directly from their
  sorted field tuples. Tool identities, envelope equivalence, exact culprit
  attribution, stable hoisted/static/routing digests, and standalone/live
  parity remain unchanged, while the hot path no longer creates one UTF-8/MD5
  digest per semantic field or JSON-serializes each ordinary field map for its
  key. The representative 241-message capture fell from 6.037 to 4.675 ms and
  334,392 to 320,252 traced peak bytes (-22.6%/-4.2%).
- Unified whole-message and field-level raw-wire evidence around one
  top-level-value serialization per message. The live combined capture now
  reconstructs the exact default JSON object representation from those pieces,
  while standalone APIs retain identical outputs and malformed JSON retains
  its typed fallback behavior. Process-local raw hashes are keyed integers,
  avoiding duplicate UTF-8, MD5, and hex allocations; stable hoisted-region
  digests remain unchanged. On a representative 241-message capture, median
  time fell from 9.454 to 6.037 ms and traced peak bytes from 360,303 to
  334,392 (-36.1%/-7.2%).
- Replaced Codex cache-health's retained conversation-sized `_wire_bytes`
  history with an O(1) prior message count and process-local prefix digest.
  Each new observation validates its prior-length hash prefix before declaring
  append-only growth; region/routing proof, mutation rejection, and one-round
  legacy rich-state migration remain intact. A representative 241-message
  health entry fell from 76,323 to 1,502 retained bytes (-98.0%), reducing the
  existing 4,096-entry worst-case payload from about 298 MiB to 5.9 MiB; one
  steady validation takes 0.026 ms with about 4 KiB temporary allocation.
- Packed cache-break's fallback message evidence into fixed-width,
  payload-free tuples of process-local keyed integer fingerprints. This removes
  one dict and multiple UTF-8/MD5/hex allocations per prefix message; nested
  reasoning evidence uses sorted orjson bytes, field attribution and legacy
  live-reload baseline replacement remain intact, and retained state is bounded
  by message/field count rather than text size. On the representative
  241-message/24-tool steady path, the full detector fell from 1.815 to
  0.403 ms and 94,314 to 40,034 traced peak bytes (-77.8%/-57.5%); fallback
  construction itself fell from 1.520 to 0.165 ms (-89.1%).
- Reused cache-break's prior aggregate/per-tool schema hashes when a validated
  final provider-bound tools-region digest proves the catalog bytes unchanged.
  First use, digest changes, and missing or malformed evidence still serialize
  the source schemas, preserving exact changed-tool attribution, projected-only
  `prefix_mutation` classification, and the capture-failure fallback. On the
  representative 241-message/24-tool steady detector path, median time fell
  from 3.049 to 1.815 ms and traced peak bytes from 204,367 to 94,314
  (-40.5%/-53.9%).
- Replaced cache-break's four-pass client-prefix fallback (two aggregate
  content hashes plus two per-field maps) with one per-field traversal through
  the larger prior/current immutable boundary. Cheap slices now provide the
  same-range mutation comparison and next-round baseline; exact
  `msg[index].field` attribution, compaction suppression, and authoritative
  post-translation wire precedence remain unchanged. The delimiter-free
  aggregate hash and its duplicate `CacheState` slot were removed. On a
  representative 241-message prefix, fallback evidence construction fell from
  4.669 to 1.521 ms and 743,402 to 92,896 traced peak bytes
  (-67.4%/-87.5%).
- Bounded provider wire evidence to its actual consumer lifetime. The raw
  usage mapping still carries complete semantic/message-byte/field-byte,
  marker, region, and routing evidence through FloorRetry, cache settle, and
  cache-break accounting; retained `apiRounds` and live `round_usage` events
  now exclude those growing graphs and the separately recorded nested billing
  carrier. All public usage/cost/trace/dispatch fields and the bounded static
  prefix experiment join remain, while the persistence sanitizer continues as
  defense-in-depth for legacy producers. A representative 241-message record
  fell from 135,865 to 145 encoded bytes (-99.89%); median event serialization
  fell from 0.0745 to 0.0002 ms and traced peak bytes from 262,177 to 1,057.
- Consolidated the final transport's semantic, whole-message-byte, and
  field-byte cache fingerprints into one read-only history traversal. The
  live request boundary now derives every view from one canonical-key build
  plus serialization-backed marker detection; only a marker-bearing message
  needs a marker-free projection. Standalone diagnostic APIs retain
  byte-for-byte-equivalent outputs and malformed-capture fallback remains
  best-effort. On a representative 241-message/24-tool request, the
  complete fingerprint cluster fell from 13.787 to 9.323 ms and 399,394 to
  368,334 traced peak bytes (-32.4%/-7.8%).
- Reused the canonical request-body message projection for successful per-round
  Request Inspector snapshots instead of repeating a conversation-sized wire
  sanitize/copy. Body-construction failures still emit the independently
  sanitized diagnostic snapshot and re-raise the original typed error; private
  Responses/Anthropic replay sidecars are excluded, while the full live event,
  storage-v2 delta, replay, and consumer payload contracts remain unchanged. On
  a representative 241-message/24-tool round, snapshot emission fell from
  7.492 to 0.706 ms and 166,736 to 68,512 traced peak bytes
  (-90.6%/-58.9%).
- Retired the cross-conversation big-prefix admission/residency gate after its
  finite per-key cache-pool premise was disproved by owner infrastructure facts
  and production A/B evidence. The gate ran only after a provider slot was
  reserved, so even on multi-key models it could not reroute work; it delayed
  the already-selected key and then sent the same request. A fixed third
  distinct 200k-token prefix incurred 1,500.1 ms under the former default with
  no cache-hit benefit. Sync dispatch now retains only the independently proven
  same-conversation write-visibility settle guard, matching async behavior and
  removing over 1,000 net lines of runtime/state-heavy gate machinery and tests,
  including resident maps, conditions, semaphores, and nine private
  `TOFU_BIG_PREFIX_*` knobs. Prefix estimation moved to its sole cache-settle
  owner; an executable source boundary prevents the cross-conversation wait
  from returning.
- Carried the root round's already-paid full-prompt admission count through
  same-model provider-slot retries instead of scanning long message histories
  again for both completion-window clamping and cache-settle classification. The
  private sidecar accepts only positive non-boolean integers, is discarded on
  model fallback, remains on the canonical body across transport retries, and
  is stripped by OpenAI/Responses/Anthropic wire boundaries; missing or invalid
  evidence retains the prior estimators. On a fixed 240-message/24-tool fixture,
  one adapt-plus-classification attempt fell from 13.605 to 0.030 ms and 101,857 to 1,685
  traced peak bytes (-99.8%/-98.3%). Evidence validation, zero-traversal reuse,
  model-fallback rescanning, completion clamps, caller retention, and all three
  provider projections guard the boundary.
- Made streaming provider-slot adaptation copy-on-write for message history.
  Gemini signature injection and cache-marker/Claude wire preparation still
  receive a deep, caller-independent graph, preserving slot-swap idempotency
  and prompt-cache bytes; read-only OpenAI/Responses projection now reuses the
  canonical list instead of cloning the entire conversation on every slot pick
  or retry. On a fixed 240-message/720-nested-block GPT-5.6 fixture, isolated
  adaptation fell from 11.987 to 0.016 ms and 722,820 to 1,153 traced peak
  bytes (-99.9%/-99.8%). With the ordinary context-window clamp included, the
  complete adaptation fell from 33.37 to 21.39 ms (-35.9%) and 722,672 to
  4,306 peak bytes (-99.4%). Mutation-family isolation, caller immutability,
  Responses wire projection, Claude/Gemini swap stability, cache markers, and
  sync/async dispatch tests guard the boundary.
- Routed synchronous send-input translation through the existing
  resource-probed, owner-round-robin translation lane instead of creating one
  executor thread plus one heartbeat thread per request. Attended work may
  advance only within its own owner's backlog, preserving cross-owner fairness;
  the HTTP request thread now emits heartbeat status while waiting. The same
  45-second budget includes lane wait, removes queued work atomically, and
  propagates cancellation through running provider dispatch/retry loops. A full
  finite queue immediately sends the original input with a visible
  `server_busy` reason. Owner propagation, owner-local priority, saturation,
  timeout cancellation, heartbeats, and the absence of request-local carrier
  threads are executable resource contracts.
- Removed two corpus-sized metadata rebuilds from the common memory-prefetch
  turn. Selected evidence now hydrates through bounded exact-ID probes instead
  of listing every record again, and Composer reuses the completed prefetch's
  authoritative available/empty outcome instead of scanning merely to render
  its static memory hint. Ranking and paper context request an explicit
  ten-field `retrieval` view while complete CRUD/API records retain their
  existing shape. The metadata cache freezes parsed frontmatter recursively
  once and serves an immutable no-copy view internally; public mutable lists
  and nested installer objects remain caller-owned, cyclic/unfreezable
  metadata bypasses the reconstructible cache, and that degradation is
  observable. On a fixed settled 1,365-file/two-selection fixture, selected
  hydration fell from 73.77 to 0.53 ms and 3,147,490 to 20,612 traced peak
  bytes (-99.3% each); complete prefetch fell from 193.90 to 99.82 ms (-48.5%)
  and 5,196,049 to 1,925,633 peak bytes (-62.9%). Retained summary size fell
  from 2,224,356 to 1,000,428 bytes (-55.0%), while hint rendering reuses
  prefetch evidence with zero directory scans instead of five. Exact-I/O,
  projection-residency, immutable-aliasing, malformed-cache, availability,
  cold/warm parity, and complete-record tests guard these boundaries.
- Collapsed each durable-memory corpus listing into one closed, sorted
  `scandir` snapshot. Flat records reuse their `DirEntry` stat fingerprint and
  package discovery consumes the same snapshot instead of listing and probing
  the directory again; hidden entries, symlink refusal, flat-before-package
  precedence, scope isolation, and fail-soft I/O behavior remain unchanged.
  A matching metadata fingerprint is now quarantined for 2.1 seconds because
  valid coarse-clock filesystems can preserve device, inode, size, mtime, and
  ctime across an immediate same-size edit; those bounded re-reads are exposed
  as `unstable` cache misses. On a fixed 1,365-file fixture, pathname type
  probes fell from 4,101 to zero, cold/warm pathname fingerprint calls from
  2,730/1,365 to 1,365/zero, cold latency from 71.26 to 53.39 ms (-25.1%), and
  settled-cache median latency from 25.05 to 14.23 ms (-43.2%). Cold,
  fresh-quarantine, settled, ordering, symlink, and disappearing-directory
  tests make both the correctness window and resource budget executable.
- Closed the cooperating-writer gap between memory revision checks and atomic
  replace/delete. Update, delete, toggle, clear, and every merge source now
  resolve a candidate, acquire deterministic directory-local mutation shards
  in sorted order, then re-resolve and refresh the revision while those locks
  remain held through publication or deletion. Same-record threads and POSIX
  processes are linearizable: concurrent different-field updates compose, two
  toggles remain two toggles, and a late update can no longer resurrect a
  deleted or merged source. Unrelated shards still publish concurrently.
  Control artifacts are capped at 16 zero-byte sidecars per durable directory,
  not one per historical memory; active in-process locks remain weakly held.
  On a fixed 60-update fixture, the uncontended durability boundary added
  0.119 ms median latency (0.325 to 0.444 ms). Non-cooperating external editors
  retain fingerprint-conflict protection. Thread/fork races, delete/merge
  ordering, independent-shard concurrency, double-toggle semantics, and a
  2,000-path sidecar-ceiling test cover the boundary.
- Replaced the remaining full-frontmatter scan for single-ID memory CRUD and
  bounded merge-source lookup with exact canonical path probes. One ordered
  store-directory iterator now owns precedence for both listing and direct
  lookup (server skills, server memories, then primary/extra legacy globals,
  memories, and skills), while one-time legacy migrations still run before
  either view. Missing IDs read no documents; merge reads only its validated
  2–32 sources. IDs now enforce their documented filesystem-basename contract
  before path construction, rejecting POSIX/Windows separators, NUL, and dot
  traversal components. On a fixed steady-state 1,365-file get fixture,
  frontmatter reads fell from 1,365 to one, returned characters from 140,480 to
  2,208 (-98.4%), median latency from 68.21 to 0.16 ms (-99.8%), and traced peak
  allocation from 3,459,516 to 14,724 bytes (-99.6%). A 128 KiB executable peak
  budget plus precedence, multi-root, migration, missing-ID, package, merge,
  and pre-I/O traversal tests guard the repository seam.
- Made durable-memory creation one locked ID-allocation/atomic-publication
  boundary, so same-name calls from concurrent threads or cooperating POSIX
  processes retain every body instead of selecting the same path and replacing
  one another. The lock is one hidden sidecar per memory directory; active
  in-process path locks are weakly retained so visiting many projects cannot
  grow a permanent lock registry. After the first collision, suffix selection
  uses one directory snapshot and bounded in-memory lookups while preserving
  the lowest available `_N`, package/file collision behavior, broken symlinks,
  and the 192-byte generated-ID limit. On fixed allocator fixtures, a
  1,000-entry collision fell from 1,002 stat probes / 2.57 ms to one stat plus
  one snapshot / 0.90 ms; 200 repeated creates fell from 20,300 to 201 stat
  probes plus 199 snapshots and from 52.32 to 24.04 ms. Thread/process races,
  write-failure cleanup/retry, lookup budget, Unicode suffixes, and lock
  reclamation are executable contracts.
- Reworked memory get/update/delete/toggle/clear/merge around revisioned
  metadata summaries instead of materializing every body for an ID lookup.
  Get/update/toggle hydrate exactly one target; delete/clear/merge hydrate no
  source bodies, and merge deletes from one resolved source set instead of
  rescanning for every deletion. Filesystem revisions are checked across
  read/write/delete boundaries: an update/toggle/delete race returns a
  retryable conflict (HTTP 409), while a source changed after merge replacement
  creation is preserved and reported in `failed_ids`. Snapshot-I/O,
  source-only merge lookup, external-edit
  preservation, partial-merge, validation, and API-status tests cover the new
  failure semantics.
- Made durable-memory retrieval metadata-first and streaming without changing
  BM25 scores or its established 2,000-character body ranking window. System
  hints and paper context no longer read any bodies; automatic prefetch ranks
  frontmatter, then hydrates at most its two selected records; explicit search
  consumes one bounded body at a time. Across the retained 1,365-file corpus,
  cold prefetch/hint reads fall from 3,440,220 to 464,644 characters (-86.5%);
  explicit search is bounded to at most 3,049,873 cold / 2,585,229 warm
  characters (-11.3% / -24.9%). On the fixed 1,365-record scorer fixture,
  traced peak allocation fell from 19,347,555 to 650,829 bytes (-96.6%) while
  elapsed time fell from 393.5 to 362.6 ms. One shared streaming BM25 core now
  owns search, prefetch, and generic snippet scoring; unclosed frontmatter is
  capped at 65,536 characters and symlinked memory/package entries are refused.
  Exact-score parity, allocation, bounded-read, selected-only hydration,
  empty-query, metadata-only, and symlink tests make the resource budget
  executable.
- Compacted the five resident memory contracts from 1,090 to 907 tokens
  (-16.8%, 183 tokens per memory-enabled round) while adding one shared
  tool/API/storage resource contract. New writes cap titles at 160 characters,
  descriptions at 512, Markdown bodies at 32,768, and tags at 32 × 64;
  generated Unicode IDs stay within 192 UTF-8 bytes, merge fan-in is 2–32, and
  search queries are validated before any corpus load. The limits exceed the
  observed maxima across 1,365 retained memories and reject atomically without
  pruning legacy durable state. Empty searches now skip full-corpus I/O, and
  omitted merge tags correctly union source tags while explicit `[]` clears
  them. Provider-preflight, HTTP 400, exact-file preservation, Unicode,
  pre-scan rejection, legacy top-k clamping, migration, skill-isolation,
  prefetch, registry, and charter tests cover the boundary.
- Compacted the resident coding `write_file`/`edit_file` contracts from
  405/475 to 291/340 tokens; together they fell from 877 to 628 (-28.4%, 249
  tokens per coding/research/long-writing round) while making their validity
  rules executable. Whole-file writes now require exactly one
  explicit source (`content`, including an intentional empty string, or a
  prior-round `content_ref`) using the provider-tested `oneOf` subset. Its
  annotation-free emergency projection has an explicit 120-token structural
  floor rather than silently dropping validation to meet the former 100-token
  synthetic fixture.
  Empty paths remain refused before I/O with payload salvage and slice indices
  remain safely clamped by the executor. Anchored edits retain
  read-before-edit, insert-without-echo, wrap rejection, unique/replacement-all
  matching, ordered execution, and partial-failure semantics while rejecting
  empty paths/anchors, empty batches, and batches over the executor's 30-edit
  ceiling before they consume a write round.
- Compacted the server-side browser download and page-preview schemas from
  367/357 to 314/299 tokens; together they fell from 721 to 610 (-15.4%, 111
  tokens per round containing both). Download calls now require at
  least one URL/text/selector target while preserving selector-over-text
  resolution, authenticated browser fallback, cookie isolation, typed staging
  receipts, and separate final-path authority. Preview calls encode the
  executor's exact path-or-URL rule plus its 320–3840 × 240–2160 viewport and
  0–15 second settle bounds, preventing missing/ambiguous-source correction
  rounds and oversized render requests before execution.
- Compacted the always-attached `todo_write` schema from 381 to 320 tokens
  (-16.0%, 61 tokens per tool-capable long-task round) while retaining full-list
  sync, no unfinished deletion/completed reopening, reasoned replans, nested
  child auto-pop, single-active-item, and completion-honesty semantics. Its
  schema now requires `parent_todo_id`/nonempty child items for `push` and a
  reason for `replan`, preventing avoidable model correction rounds. The shared
  schema/core/resume contract bounds each checklist to 24 items, nesting to six
  levels, IDs to 64 characters, steps to 512, reasons to 2,048, current-state
  history to an observable eight-entry tail, and serialized resume state to
  1.5 MB. A four-byte-Unicode worst-case test measures 1,297,374 bytes; raw tool
  rounds retain the complete durable audit when reconstructible sidecar history
  is reclaimed.
- Compacted the resident `ask_human` schema from 456 to 308 tokens (-32.5%,
  148 tokens per attended/project round). Irreversible decisions, subjective
  preference/product intent, unrecoverable facts, context/tool inspection,
  reversible defaults, free-text/choice modes, and Markdown/QR presentation
  remain explicit. The canonical contract now bounds questions at 32K units,
  choices at 16, labels at 1K, and descriptions at 8K; choice mode requires a
  nonempty option list instead of relying on a Web-only fallback. Semantic,
  provider-preflight, repair, QR, virtual-user, lifecycle, and presentation
  tests cover the schema and cap it at 325 tokens.
- Compacted the resident `update_search_settings` schema from 744 to 521
  tokens (-30.0%, 223 tokens per search-settings-capable round) while retaining
  pure-read-first behavior, exact fast/balanced/deep presets, profile overrides,
  legacy knobs, cost/latency trade-offs, safe clamping, global persistence and
  hot reload, environment-shadow honesty, and result reporting. Fixed the
  write-approval projection to include `profile` and `overrides`; profile-only
  global changes can no longer be mislabeled as a read-only inspection.
  Semantic and rendered-dialog tests cap the schema at 550 tokens and cover
  the complete live write surface.
- Compacted the enabled `generate_image` schema from 754 to 345 tokens (-54.2%,
  409 tokens per image-capable round). Generation versus incremental editing,
  preservation of unmentioned source content, exact result-reference reuse,
  detailed English scene/edit prompts, local/remote source routing, aspect and
  resolution enums/defaults, project/server save behavior, multi-root prefix
  semantics, and optional vtracer/background-removed sibling SVGs remain
  explicit. Semantic owner tests cap the schema at 400 tokens; 151 image,
  streaming, plan-mode, isolation, and registry tests cover the unchanged
  execution paths.
- Compacted the default-uncapped project `project_board_block` schema from
  1,036 to 509 tokens (-50.9%, 527 tokens per resident project round). Its
  autonomy gate still permits human blocking only for irreversible cost,
  taste/policy/product intent, or facts unverifiable from the repository;
  uncertainty still routes to a robust long-term decision, charter proposal,
  and journal trace. Self-expiring/class-aware cooldowns, billed re-dispatch
  avoidance, sibling path holds, structured `Needs you` questions, immediate
  answer re-dispatch, nonexistent-card refusal, one-word trade-offs, and
  operator-facing reason/option guidance remain model-visible. Semantic owner
  tests cap the schema at 550 tokens; all 162 board tests cover the unchanged
  persistence and recovery behavior.
- Compacted the two largest resident scheduler schemas: `schedule_create` fell
  from 1,061 to 553 tokens and `timer_create` from 1,015 to 406; together they
  fell from 2,073 to 956 (-53.9%, 1,117 tokens per project round). Local cron
  order, one-shot/off-minute behavior, task types, deployment code gates,
  target/tool inheritance, predicate exit/regex rules, independent polling,
  durable immediate return, human-only restart prohibition, authoritative
  continuation, exhaustion, and code/hybrid auto-promotion remain explicit.
  The timer schema now matches its executor: `continuation_message` plus either
  `check_instruction` or `condition_command` is valid, so a deterministic
  predicate can start as a zero-LLM watcher instead of paying for a redundant
  poll model. JSON-Schema, adapter-path, semantic, and 600/450-token budget
  tests lock the corrected contract.
- Compacted the default-uncapped project `project_message` schema from 506 to
  323 tokens (-36.2%, 183 tokens per resident project round). The single
  coordination contract still frames the target as another agent, requires an
  imperative claim/boundary/handoff/warning rather than human-facing status,
  preserves queued-next-round non-interruption and the per-target rate ceiling,
  and permits exactly one purposeful reply via the supplied full ID. Its
  `wake=true` fresh-turn default and cheaper `wake=false` mailbox-only path stay
  explicit in both tool and parameter prose; semantic tests enforce all of
  these boundaries plus a 350-token ceiling.
- Compacted the always-on `inspect_image` schema from 652 to 321 tokens
  (-50.8%, 331 tokens per model round). Source-first rendering, single-region
  workflow, shared-image downscaling risk, grid-assisted coordinates, uploaded
  `/api/images/` references, supported formats, read-only behavior, mixed
  fractional/pixel boxes, EXIF-then-clockwise-rotate geometry, crop-over-zoom
  precedence, and defaults remain explicit. Owner tests bind those semantics to
  the unchanged JSON shape and enforce a 350-token ceiling.
- Compacted the project/explicit-reference `get_conversation` schema from 602
  to 347 tokens (-42.4%, 255 tokens saved whenever the family is resident).
  The shorter contract still forbids speculative retrieval, makes raw=true the
  structured unsummarized default, identifies the metadata/tool-round loss in
  readable mode, requires checking the parseable whole-message head+tail
  `DELIVERED N of M` window, and preserves clamp disclosure plus positive,
  exclusive `before` paging. Semantic owner tests cover each boundary and cap
  this frequently paid long-task recovery schema at 350 tokens.
- Compacted both runtime variants of the research-critical `fetch_url` schema:
  filter-on fell from 686 to 386 tokens (-43.7%) and filter-off from 527 to
  288 (-45.4%). Both retain remote-vs-local routing, relevant Page Links,
  inline text-like assets, binary server staging, selected-browser authenticated
  fallback without browser Downloads, authorized final-destination copy/move
  receipts, and concurrent object-only batches. The enabled variant additionally
  retains the large-HTML whole-page relevance gate, accurate non-narrow reason,
  explicit keep/drop failure, no passage selection/summarization, bypass cases,
  and content cap. Semantic tests enforce 400/300-token budgets across the live
  Settings toggle rather than freezing one import-time variant.
- Halved the schema multiplier for multi-root path guidance. The same rule is
  necessarily attached where each of eight nested/top-level `path` values is
  chosen, but its projection now states absolute path, `rootname:subdir`, and
  bare-relative-to-primary semantics in 99 rather than 204 characters. Across
  the six core coding tools the multi-root premium fell from 397 to 189 tokens
  (-52.4%, 208 tokens per round) with no path resolution or authority change;
  a regression test caps that full-surface premium at 200 tokens.
- Compacted the two always-paid coding evidence tools without weakening their
  execution contracts. `grep_search` fell from 738 to 435 tokens while keeping
  the persistent/FUSE-safe index, ignored-directory and case policy, short-
  literal guidance, Rust regex semantics, context/count/result controls, and
  20-operation batch. `read_files` fell from 781 to 392 while retaining wide
  1–2-file reads, focused 3+-file reads, authoritative ranges, the 512-KiB
  whole-file boundary, read-before-edit, 20-item/24k aggregate guidance,
  relative/absolute/home/file-URI routing, and native image/PDF/Office/text
  behavior. Together the coding floor drops 692 tokens per round (-45.6% for
  this pair); semantic owner tests cap them at 475/450 so a future cost win
  cannot silently delete the behavior that prevents repeated reads or recursive
  scans.
- Removed the duplicate multi-agent role catalogue from system context and
  compacted its single detailed owner, `spawn_agents`. The seven live roles,
  exact per-role tool lists, denylist, shared artifact tools, wrong-role
  recovery, independent-work gate, one-call parallel launch, dependency DAG,
  fire-and-forget receipt handling, no-fabrication rule, and objective-quality
  contract remain model-visible. The tool schema fell from 1,697 to 975 tokens
  and the conditional `<parallel_execution>` block from 568 to 66; together an
  enabled round fell from 2,265 to 1,041 tokens (-54.0%, 1,224 tokens). The
  context provider now reserves 128 rather than 1,000 tokens, and owner tests
  cap the schema at 1,050 while proving the catalogue appears exactly once.
  The no-implicit-budget test now proves byte-equivalent schemas, exact token
  diagnostics, and budget zero directly instead of requiring an intentionally
  wasteful greater-than-4,000-token fixture.
- Compacted the always-paid coding `run_command` contract from 1,470 to 709
  tokens (-51.8%, 761 tokens per project round); the standalone no-project
  projection is 686 tokens. The former usage matrix repeated the same shell vs
  file-tool choice across three prose blocks and again in parameter help. One
  bounded contract now retains the no-placeholder/tool-loop rule, unlimited-by-
  default timeout and user Stop, fresh subprocess/cwd semantics, dedicated
  read/search/find/edit/write paths, FUSE-safe grep interception, browser-cookie
  isolation, modern text-tool availability, credential selection, and all JSON
  validation fields. A 750-token owner test covers both project and standalone
  variants, so future guidance growth must make its per-coding-round API cost
  explicit.
- Bounded runtime-derived `web_search` vertical prose without reducing its
  capability enum or credential-aware availability. Identifier rules and
  examples already have one static owner, so each loaded domain now contributes
  only its normalized, at-most-96-character purpose plus the load-bearing
  partial-availability warning. On the fully resident six-domain runtime the
  schema fell from 1,241 to 926 tokens (-25.4%, 315 tokens), returning the paper
  task's eight required wire tools from 4,207 to 3,892 tokens under its 4,000-
  token target; all 34 executable tools remain in the same frozen authority and
  the fixed discovery/execution pair remains 477/500 tokens. An owner-level
  1,000-token regression budget now prevents upstream metadata growth from
  silently recreating the paid prompt floor.
- Reused the first exact provider-schema fingerprint across stable root-agent
  rounds. The wire boundary still computes and persists the full insertion-
  order-sensitive SHA-256 on the first round, then seals it into the opaque
  call-local evidence; the root hook carries only that 64-character digest
  into a later body with the same stable tool-list object and model. Token and
  fingerprint reuse share one ordered schema-object identity check. On the
  same real 50-tool / 20,979-token surface, 80 complete `prepare_request`
  calls fell from the C107 baseline of 329.8 to 262.1 ms (-20.5%, 67.8 ms
  saved), including the cold first fingerprint; all 80 diagnostics exactly
  matched. Protocol conversion, copy-on-write schema shaping/repair, changed
  order/object/model, invalid digests, and fresh/fallback bodies recompute the
  exact digest and cannot overwrite the source receipt. The receipt remains
  hook/call-local and adds no schema copy, task/global cache, capacity knob,
  durable state, provider byte, or prompt text.
- Reused prompt-admission's selected-tool count in the final provider-wire
  diagnostic when the provider projection is byte-shape stable. The previous
  diagnostic serialized the same large schema with sorted keys and entered the
  token authority again, then separately serialized it for the required exact
  fingerprint. On a real registry-assembled 50-tool surface (94,176 JSON
  characters / 94,646 UTF-8 bytes / 20,979 tokens), 80 complete OpenAI
  `prepare_request` calls fell from 402.5 to 327.9 ms (-18.5%, 74.6 ms saved),
  including evidence creation and identity validation. Every emitted token
  count and fingerprint matched the independent path. A short-lived opaque
  evidence object carries the count proof while referencing the
  existing immutable wire-catalog copy; reuse requires the same model and
  every final schema object in the same order. Responses/Anthropic conversion,
  Tool Search, PTC, Swarm, budget compaction, cache-marker rewriting, schema
  repair, changed order/objects, invalid evidence, and nonempty zero counts all
  fall back to local counting. The sidecar is stripped from every provider
  protocol and adds no schema copy, task/global cache, durable state, resource
  knob, provider byte, or prompt text.
- Reused the final selected-tool schema decomposition across root-agent rounds.
  Admission's canonical full-request counter still verifies every prompt, but
  it no longer serializes and tokenizes the unchanged 46-tool catalog a second
  time on every round merely to split the total for diagnostics and compaction
  budgets. On 80 consecutive real GPT-5.6 request frames, admission fell from
  667.3 to 603.1 ms (-9.6%, 64.2 ms saved); the cold first frame was unchanged
  at 16.5 ms, the schema remained 18,753 tokens, and all 80 complete
  measurements exactly matched the independent path. A forced-summary path
  also measures the unchanged schema only once. Reuse is confined to one loop
  hook and requires the identical tool-list object and model; a different
  object/model, invalid evidence, or zero for a nonempty surface forces exact
  recounting. The hook retains only its existing list reference, a model name,
  one integer, and one 64-character digest, adding no task/global cache,
  durable state, resource knob, provider byte, or prompt-text copy.
- Reused final prompt-admission evidence in round-context telemetry instead of
  independently tokenizing the same selected tool schema and every historical
  tool result again. The canonical Tiktoken pass now optionally returns only a
  call-local `id(string) -> exact tokens` map for `role=tool` string contents.
  After BodyBuilder sanitization, telemetry reuses a count only when the final
  body still references that same immutable string; rewritten strings, block
  content, invalid hints, non-Tiktoken tiers, missing schema evidence, and a
  suspicious zero for a nonempty tool surface all retain exact fallback
  counting. On 80 real GPT-5.6 request frames passed through the real
  BodyBuilder, all 4,037 tool-result occurrences retained a safe identity and
  telemetry fell from 182.1 to 23.1 ms (-87.3%); median/P95 work fell from
  2.29/2.99 to 0.28/0.30 ms. All 80 complete telemetry snapshots matched the
  independent reference. Producing the hints added 9.6 ms (+1.6%) to the
  80-round gate, leaving a net 149.4 ms reduction across counting plus
  telemetry. The map is capped at 4,096 identities, contains no text reference,
  is filtered from admission history/audit/task state, and is popped during the
  immediate body-preparation call. This adds no cache, capacity knob, worker,
  timer, durable field, request field, or provider-visible byte.
- Reused conservative entropy-floor counts across growing model rounds without
  weakening prompt admission. After the canonical BPE pass had populated the
  bounded text-digest cache, the compaction gate still regex-scanned every
  historical CJK/base64/prose string again on every round. On the same 80 real
  GPT-5.6 frames used for the BPE optimization (50.9 MB decoded, 13–180
  messages), that floor alone took 1,262.3 ms and pushed the complete gate to
  1,733.9 ms. Digest reuse reduced the floor to 140.8 ms (-88.8%) and the full
  gate to 587.5 ms (-66.1%); median/P95 floor work was 1.69/2.88 ms. All 80
  floor totals exactly matched the uncached reference, including the 73 rounds
  where it exceeded Tiktoken and remained the admission authority. Exact BPE
  and model-independent heuristic integers now share one content-free LRU
  entry; a heuristic-only entry consolidates when an exact encoding arrives.
  The cohort needed 86 heuristic values, served 4,096 hits, and caused no
  eviction. No second cache or capacity knob was added, and a resident-budget
  test caps the two-integer value payload below 512 KiB even at the existing
  4,096-entry hard ceiling.
- Reused stable large-text BPE counts inside the canonical full-request
  Tiktoken counter. It previously called `encode_batch` on every historical
  message and the unchanged tool schema every round, bypassing the existing
  content-free digest cache even though production prompt history grows by a
  small tail. On 80 consecutive real GPT-5.6 request frames (13–180 messages),
  that consumed 1,010.6 ms. The counter now looks up every reusable text of at
  least 512 characters, then sends all short text and unique cold misses
  through one batch. The implementation took 388.6 ms (-61.5%); median/P95
  per-frame work fell from 12.88/16.21 to 4.58/7.52 ms, while the cold first
  frame held at 8.01 versus 7.94 ms. All 80 token totals were exact matches.
  The cohort used 87 existing encoding/length/SHA-256 entries, recorded 4,175
  hits, and caused no eviction under the launch-probed capacity. Duplicate
  large text within one request is encoded once, hashing/tokenization stay
  outside the cache lock, and failed batches publish no entry. No prompt text,
  cache, worker, or resource ceiling was added; short/changing text and
  distinct tokenizer encodings retain exact recounting.
- Removed the completion-window clamp's second full prompt scan from admitted
  root-agent rounds. On 80 real GPT-5.6 request frames (50.9 MB decoded,
  13–180 messages), the redundant `cheap_estimate(messages)` pass took
  1.278–1.285 seconds, while the complete clamp took 1.284–1.315 seconds;
  median per-round clamp work was 17.1 ms and P95 was 24.0 ms. The immediately
  preceding fail-closed admission already counted the same final messages plus
  selected tools with the canonical counter. Its v2 total now travels only
  through the current dispatch call stack into the body builder and completion
  clamp, avoiding retained prompt state and cross-round/model reuse. The clamp
  keeps its existing 10% + 512 safety reserve and now covers tools as well as
  messages. With identical token values, all 80 rebuilt bodies were equal and
  total construction fell from 1,430.6 to 137.1 ms (-90.4%); warmed clamp work
  was 0.326–0.352 ms for all 80 rounds. Independent body builders, fallbacks,
  and missing, boolean,
  non-integral, zero, or negative evidence retain the former local scan.
- Corrected the final provider-prompt admission boundary to count the selected
  tool schema exactly once. Its canonical token counter already measured the
  complete request, but admission labelled that result `messageTokens` and
  added a separately measured schema again. Across 827 production guard
  receipts this created 15.93 million phantom tokens (19,261 per request on
  average); schemas were 18,939 tokens at the median and the duplicate was
  20.9% of the reported total at the median. Of 18 dispatch-guard compactions
  recoverable from application logs, 17 started while the canonical complete
  request was only 101.9K–120.9K tokens, below the 128K ceiling; only one was
  genuinely above it. Admission v2 now uses the canonical complete-request
  count as `totalTokens` and subtracts the diagnostic schema estimate only to
  expose `messageTokens`. The exact selected round surface overrides any stale
  task carrier copy, and the exceptional heuristic fallback also includes it,
  preserving fail-closed context safety. The change avoids premature lossy
  summaries and their model calls without changing durable history, tool
  authority, the 120K first-round target, or the 128K hard ceiling.
- Fused the Request Inspector snapshot-delta projector's growing-history scan.
  The old hot path canonicalized every unchanged message twice for comparison,
  then serialized the complete shared prefix a third time for its integrity
  hash. On 80 real request snapshots (31.8 MiB full input, 1.02 MiB projected),
  that consumed 562.4 ms: 312.4 ms comparing messages, 183.2 ms rebuilding
  prefix hashes, and 66.8 ms hashing tools. One scan now canonicalizes each
  current message once, compares content-free full SHA-256 fingerprints, and
  feeds the same canonical bytes into the unchanged prefix-hash algorithm.
  The resulting implementation took 292.5 ms total (48.0% less), with median
  per-round work falling from 6.93 ms to 3.68 ms. Projector retention is
  one 32-byte digest per message rather than another prompt-object graph;
  terminal/FIFO release, stored v2 bytes, v1 compatibility, exact rebuild, and
  honest degradation remain unchanged.
- Stopped turn-native executors from rewriting their complete segment timeline
  into `task_results` at every five-second recovery checkpoint and again at
  terminal settlement. A read-only local-store projection found 5,951 nonempty
  segment copies across 9,115 task rows: 878.3 MB retained, 432 KB P95 and a
  23.2 MB maximum; the largest copy embedded 21.8 million characters of tool
  results already owned by its canonical Turn. Re-encoding representative
  0.35/4.43/23.21 MB copies took 2.31/23.82/181.62 ms median locally before
  socket copying or WAL I/O. The cold task API reads only
  bounded metadata/event cursors and terminal content, while every structural
  event atomically commits the Turn projection plus replay log, so these copies
  were reconstructible write amplification rather than recovery authority.
  Tasks with a complete turn/attempt identity now persist `segments=None` in
  executor diagnostics while keeping the in-memory timeline unchanged.
  Inline, headless, and virtual-user carriers still persist their sole
  task-result structural copy; existing rows remain readable for legacy
  segment backfill, and no stored user data was deleted.
- Re-armed round-trip efficiency guidance for genuinely long serial tool
  tasks without turning productive reads into a hard limit. A read-only replay
  joined 59 logged 2026-08-29 completions to 46 durable task projections and
  found 5,441 tool calls. Existing L0
  budgeting already reduced the 2,838 streamed file-search/read results from
  16.35 million raw tokens to 3.50 million model-visible tokens (80.3%), and
  only one call remained exactly cache-reusable across the real write/FIFO
  boundaries, so neither tighter result truncation nor speculative-cache reuse
  was the next bottleneck. Instead, 952 of 1,626 file-reading model rounds
  contained only one read. Across the 29 tasks that received the former
  one-shot efficiency correction, 516 of the next 1,005 observed rounds were
  still single-tool rounds. The correction may now recur only after 24 more
  completed rounds and both the local-PTC and generic batching lanes share one
  four-hint task cap. Replaying that policy adds just 27 sparse correction
  opportunities across the cohort. Persisted witnesses recover the consumed
  shared budget instead of falsely exhausting a resumed task; damaged values
  remain bounded, a safety correction still preempts an efficiency hint, and
  the two hint lanes cannot stack in one round. Native/off PTC policy, real
  user steering, productive-receipt checks, force-stop thresholds, tool
  authority, and unlimited task round policy are unchanged. The maximum four
  prompts plus four durable witnesses measure 2,859 bytes against an executable
  3 KiB ceiling and add no worker, timer, or process-global state. All 86
  breaker, root-loop, metadata, and tool
  orchestration tests pass, as do targeted Ruff, bytecode, documentation-line,
  and diff gates. No frontend artifact was built, published, or deployed.
- Removed avoidable TreeIndex project walks after bounded-memory eviction.
  In the current-format 2026-08-29 log cohort, 197 index builds consumed 180.8
  seconds; 80 builds totaling 52.2 seconds repeated the same root inside the
  45-second base-freshness window, and 70 of those retained the same file
  count. Production has one build entrance, but direct background `warm`
  callers previously skipped the canonical local blob after an LRU miss. The
  existing worker now restores that blob before walking: a base-fresh snapshot
  ends the job, while a stale-but-trusted snapshot is installed for immediate
  service and still receives the established refresh. A synchronized write
  still removes its superseded blob immediately; if its write-fresh memory
  entry is later selected as an LRU victim, the current parallel columns are
  atomically checkpointed under the index lock before removal. Per-entry
  revision validation also prevents an older asynchronous persist from
  reinstalling superseded columns after the write hook has removed its blob.
  Clean entries add no write, entries above the current loadable-entry budget
  are never checkpointed, and persistence failure leaves the blob absent so
  callers keep the honest rebuild fallback. Disk-loaded snapshots still
  restart at the conservative 45-second interval, external changes retain the
  900-second hard trust boundary, and ignore-rule invalidation remains
  destructive. This adds no worker, timer, root, entry, or canonical-blob
  capacity. All 56 TreeIndex,
  grep/find, freshness, single-flight, lifecycle, fault, and resource-bound
  tests pass; neighboring configuration, documentation, architecture, Ruff,
  bytecode, and diff gates also pass. No frontend artifact was built,
  published, or deployed.
- Removed unconditional warm-tail cache-settle latency from the generic LLM
  path without weakening cold or metered-write protection. The 2026-08-28/29
  application-log cohort contains 3,100 settle holds totaling 3,441.30 seconds;
  2,511 Kimi generic holds account for 1,966.88 seconds. Every generic hold has
  a preceding cache record, and 2,145 (85.4%) followed a healthy warm read with
  no metered write and fewer than 4,096 uncached tokens. The successful stream
  now classifies whether a write actually needs visibility time. Positive
  metered cache creation, cold/missing telemetry, and unmetered warm tails of
  at least 4,096 tokens retain the existing adaptive 1.5-second hold; an
  explicit Anthropic zero creation or a smaller unmetered tail clears the
  conversation's generic settle clock. Replaying the cohort projects 1,664.49
  seconds (27.7 minutes, 84.6% of generic wait time) removed while bounding a
  skipped round's possible extra prefix processing to 4,095 tokens. The
  threshold is operator-tunable, invalid values fail to the safe default, and
  missing usage remains conservative. Codex subscription send-spacing and
  unmetered-write visibility policy are unchanged. The change adds no schema,
  timer, worker, or process-global state. All 350 distinct focused and
  neighboring cache, token-convention, sync/async dispatch, contention,
  fallback, health, startup, and accounting tests pass, as do documentation,
  architecture, targeted Ruff, bytecode-compilation, and diff gates. No
  frontend artifact was built, published, or deployed.
- Removed empty turn-source lease-reaper transactions from the Sidecar's sole
  writer. The live authority's historical cohort contains 12,663
  `queue.reap` receipts over 8.16 days (about 1,551/day); 12,573, or 99.29%,
  are the legacy empty `{"conv_ids":[]}` result. Clean-result receipt
  suppression already stops new empty receipt growth, but the normal
  maintenance tick still entered a writer transaction for a zero-row `UPDATE`
  and then issued a separate all-conversation query. The current application
  log records 31 reaper failures under storage pressure: 30 writer-acquisition
  timeouts and one Sidecar-unavailable fence. Normal maintenance now opts into
  `tofu.queue.reap-probe/v1`; the existing read-pool grouped query returns the
  same oldest-first dispatch list plus an exact `hasExpiredLeases` bit. Only a
  positive capability echo and bit enter the atomic repair command. Thus a
  healthy clean/unleased tick falls from two Sidecar RPCs to one (50% fewer),
  from one writer admission/transaction to zero, and retains zero receipts;
  genuinely expired leases keep the original two-call atomic repair path.
  Startup still force-reclaims predecessor leases. A newer application against
  an old Sidecar sees its legacy bare-list response, performs the former repair,
  and reuses that already-read list without adding a third RPC. Malformed or
  unknown opt-in shapes fail closed. Lease taking, live-task guards, the
  oldest-first four-dispatch herd bound, and immediate retry of an unleased row
  left by submit failure remain unchanged. The change adds no schema, index,
  timer, worker, or process-global state. One hundred queue, Goal, autopilot,
  startup, maintenance, and ownership tests pass; the full storage/process
  boundary passes 161 tests with one environment skip. Ruff, bytecode,
  documentation, architecture, and diff gates pass. No frontend artifact was
  built, published, or deployed.
- Fused durable prompt-cache accounting into the already-required guarded task
  checkpoint. The 2026-08-28/29 application-log snapshot contains 6,414
  `CacheRoundRecord` rows; the old cache-fact path can call two serialized
  conversation-settings RMWs after a warm round, costing up to four Sidecar
  RPCs, two writer transactions, and two permanent command receipts when both
  facts change. The live authority contains 28,281
  `conversation.settings.update` receipts (8.06% of 350,766 total and
  1,317,427 response bytes), while the current log records 22 failed
  `lastTurnCacheRead` writes and one failed `cachePrefixHWM` write under writer
  timeout/WAL pressure. A normal durable task now stages only those two bounded
  positive integers and negotiates the independent additive
  `tofu.task-results.checkpoint.cache-settings/v1` capability. A matching
  Sidecar merges HWM by maximum and applies the latest cache-read baseline in
  the same owner-qualified transaction as the task snapshot, so the new/new
  path adds no Sidecar RPC, writer admission/transaction, or receipt beyond the
  checkpoint already owed after tools and at terminal settlement. Identical
  ambiguous replay may repair HWM but cannot overwrite a different last-read
  value already committed by a newer task. The exact guard, cache-contract,
  and commit echoes are required before candidates clear; returned
  authoritative values refresh the existing 30-second read caches without a
  follow-up query. A newer concurrently staged value survives compare-and-pop,
  and old Sidecars retain independent per-fact legacy fallback, clearing only
  facts proven durable. The change adds no schema or new process-global state.
  Cache/accounting tests pass 279 cases, the full storage/process-boundary
  ladder passes 161 with one environment skip, and 67 replay, finalization,
  interruption, release, inspector, ownership, and carrier lifecycle tests
  pass. Ruff, bytecode compilation, documentation, architecture, and diff
  gates pass. No frontend artifact was built, published, or deployed.
- Removed steady-state task-result checkpoint read amplification and bounded
  reconstructible writer pressure. A 2026-08-29 application-log snapshot has 138
  task-result writer-acquisition timeout warning rows (97 direct and 41
  coalesced summaries); one long task accounts for 63 rows, while the same
  pressure window contains 30 queue-lease reap timeouts. A task birth now
  negotiates the additive `tofu.task-results.checkpoint.guard/v1` contract and
  caches its version only after the Sidecar echoes that exact capability. A
  new-manager/new-Sidecar steady checkpoint therefore falls from
  `conversation.get` + `record.get` + command to one command (66.7% fewer
  Sidecar RPCs); an old peer that ignores the additive request members keeps
  both compatibility reads. The guarded backend-neutral transaction shares
  the owner-qualified conversation delete/purge lock, then takes the task key
  lock and atomically enforces parent, owner, status-regression, abort-tombstone,
  identical-replay, and CAS semantics without leaking foreign key collisions.
  Running snapshots are reconstructible diagnostics and now get one 500 ms
  maintenance-lane admission instead of as many as five user-lane attempts;
  task birth and terminal diagnostics retain user priority and five bounded
  attempts. Admission and exhausted CAS pressure raise into the existing
  best-effort checkpoint path rather than returning the `False` value reserved
  for a proven ownership/recovery fence. The change adds no schema, receipts,
  or process-global state. The full storage/process-boundary ladder passes 163
  tests with one environment skip, and 58 neighboring replay, terminal-release,
  interruption, first-checkpoint, tool-deadline, provider-isolation, carried
  event, and Turn-lifecycle tests pass. Ruff, bytecode compilation,
  documentation, architecture, generated-contract, and diff gates pass. No
  frontend artifact was built, published, or deployed.
- Removed the empty conversation artifact-list hydration request without
  embedding an unbounded feature payload in Conversation Sync. The frozen
  access log contains 134 `GET /api/v1/artifacts` responses; all 134 were 200,
  exactly 64 bytes, and empty. The generated snapshot client now opts into
  `artifactHint=has-any`; the owner-scoped Sidecar snapshot transaction answers
  with one bounded `LIMIT 1` `hasArtifacts` bit. `false` commits an empty
  presentation model and removes the separate HTTP/Sidecar/list-query path,
  while `true` still fetches the metadata required to render real artifacts.
  The selector is part of both admission and snapshot single-flight identity:
  old clients retain the old response shape, and a missing field from an old
  server/Sidecar retains the legacy fetch. A weak conversation-shell generation
  fence also prevents an older list response from overwriting a newer negative
  snapshot. The catalog no longer owns a second hydration path. Ninety-seven
  focused and neighboring tests pass, including route/schema, owner-scoped
  Sidecar, wire-shape isolation, rolling fallback, and late-response browser
  harnesses. Retained source is 3,241,296 bytes (1,006 below its ratchet), up
  501 bytes from C89. The private 35-chunk graph is 3,574,296 / 1,051,384
  gzip-9 bytes, only +514 / +272 from C89; all non-main raw chunk bytes are
  unchanged, and its 113 manifest rows close without publishing or deploying.
- Fused the two server-authoritative conversation-settings request pairs
  without copying `lib/conv_config/` policy into the browser. The frozen access
  log contains 55 config resolves, 257 settings resolves, and 232 settings
  PATCHes; 48/55 config resolves were followed by a settings resolve within two
  seconds, while 224/232 settings PATCHes followed a settings resolve within
  two seconds. A healthy send now asks the existing config resolver to include
  canonical settings using a distinct stored-conversation snapshot, reducing
  two sequential HTTP requests to one. A healthy server-owned settings write
  now sends raw snapshot + toolbar inputs to
  `PATCH /api/v1/conversations/:id/settings/resolve`, which resolves and commits
  through the existing owner-scoped command, notification, conflict, and
  idempotency boundary, reducing resolve + PATCH to one request. A DOM-free
  typed owner owns envelope stripping and rolling-upgrade compatibility: an
  old config response gets its former one-read fallback, while a fused-write
  404 probes only once per page before the legacy pair; non-404 failures never
  fan out into a second mutation. Local drafts remain cache-only, active send
  paths preserve their distinct workbench projection, and server-only settings
  survive the partial merge. Moving snapshot projection and compatibility
  state out of retained script shrinks retained source 657 bytes from C88 to
  3,240,795 (1,507 below its ratchet). The private 35-chunk graph grows only
  1,021 / 302 gzip-9 bytes to 3,573,782 / 1,051,112; main grows 1,021 / 294 to
  1,023,417 / 314,545 and every other chunk is raw-byte-identical. All 181
  focused/neighboring resolver, settings, catalog, command, ownership,
  idempotency, send, and API checks pass, as do TypeScript, runtime
  composition/reachability, styles, generated contracts, i18n, actions,
  documentation, architecture, source-budget, private build/sourcemap, diff,
  py_compile, and targeted Ruff gates. Nothing was published or deployed.
- Fused project selection and recent-history persistence into one bounded
  command path. The frozen access log contained 133
  `PUT /api/v1/project/paths` requests and 131 separate
  `POST /api/v1/project/recent` requests, commonly adjacent within one second.
  Conversation restore now carries only its primary recent intent in the
  existing PUT, while an explicit workbench apply carries a primary-first
  bounded prefix (all roots for N≤32); the ordinary healthy paths therefore
  fall from two HTTP requests to one and from 1+N requests to one. The browser
  preserves every authoritative project root beyond that projection cap. The
  route validates the complete bounded subset before
  project mutation, persists it only after successful reconciliation, and
  treats recent-history failure as fail-soft so reconstructible navigation
  cannot roll back a valid project selection. Background/status reconciliation
  omits the intent and remains side-effect-free. The owner-scoped
  `project.recent.touch_many` Sidecar command caps a batch at 32 paths × 4,096
  characters, deduplicates and locks in stable order, commits one transaction
  and one replay-safe receipt, and returns only `{touched}`. The browser's old
  second transport and runtime owner are removed; identical in-flight PUTs
  coalesce only when path, access, and bounded recent intent all match.
  Retained source shrinks 127 bytes to 3,241,452 (850 below its ratchet).
  Against C87, the private 35-chunk graph shrinks 215 / 40 gzip-9 bytes to
  3,572,761 / 1,050,810; main shrinks 46 / 28 to 1,022,396 / 314,251,
  Project shrinks 169 / 31 to 27,182 / 8,879, and background is unchanged raw
  at 11,562 bytes (+2 gzip-9 to 4,492). All 159
  focused/neighboring HTTP, retained-runtime, project, Sidecar, receipt,
  ownership, composition, and budget checks pass (one optional backend case is
  skipped), as do TypeScript, runtime reachability, styles, generated
  contracts, i18n, actions, documentation, architecture, source-budget,
  private graph/sourcemap, diff, and targeted Ruff gates. Nothing was published
  or deployed.
- Revision-gated the full conversation-catalog refresh behind authoritative
  Turn Sync convergence. An Aug 29 access-log sample through 16:28 contained
  1,822 `GET /api/v1/conversations` reads (959 conditional `304`, 863 full
  `200` responses totaling 35,866,290 bytes, 41,560 bytes mean), commonly in
  three-tab bursts. A positive `conv_changed.rev` that is already applied is
  now a no-op; a newer hint wakes Conversation Sync and a DOM-free typed owner
  rechecks TurnState/catalog revision after 150 ms, reducing the healthy
  converged path from one full-list request per visible tab to zero. The ledger
  is capped at 64 conversations. Revisionless metadata changes, unknown or
  malformed hints, overflow, still-stale state, hidden-page resume, and
  refresh failure retain conservative authoritative recovery; teardown cancels
  pending work. Retained runtime is 3,241,579 bytes (723 below its ratchet).
  Against C86, the private 35-chunk graph is 3,572,976 / 1,050,850 gzip-9
  bytes (+1,537 / +491); main is 1,022,442 / 314,279 (+1,537 / +505), while
  background is byte-identical. All 67 focused/neighboring catalog, sidebar,
  invalidation, and Turn-delta checks pass, as do TypeScript, runtime
  reachability, styles, generated contracts, i18n, actions, source-budget,
  documentation, architecture, private graph/sourcemap validation, diff, and
  full Ruff gates. Nothing was published or deployed.
- Replaced the normal Codex earned-reset refresh re-poll with an owner-scoped
  Push completion receipt. The existing first OAuth status read still starts
  the bounded daemon and remains authoritative; its sanitized result now rides
  `oauth/codex-reset`, cancels the browser's fallback, and can prompt without a
  second HTTP request. Lost frames, old runtimes, publication failures, and
  capacity deferral retain bounded reconciliation; teardown explicitly
  unsubscribes, malformed frames fail closed, and account data never crosses
  the Push Hub owner boundary. Healthy refresh periods fall from two OAuth
  status requests to one. Retained first-screen source remains byte-identical
  at 3,240,582 bytes (1,720 below its ratchet); against C85 the private
  35-chunk graph is 3,571,439 / 1,050,359 gzip-9 bytes (+884 / +316), all in
  the lazy background owner (+884 / +306), while main is 1,020,905 / 313,774
  (0 / -8). All 30 focused and 141 neighboring OAuth, Push, webhook, and
  background-budget checks pass, as do runtime, styles, generated contracts,
  TypeScript, i18n, actions, source-budget, docs, architecture, and full Ruff
  gates. Nothing was published or deployed.
- Removed the dedicated deployment-feature request from ordinary first-screen
  startup. `GET /api/v1/server-config` now carries the same live
  `feature_flags` projection as the retained `/api/v1/features` compatibility
  endpoint; a newer browser against an older server, malformed piggyback data,
  or an isolated projection failure still uses that endpoint. The DOM-free
  typed loader validates at most 256 boolean keys, requires the two flags that
  drive first-screen presentation, ignores only envelope metadata, suppresses
  unchanged repaints, and single-flights only the active fallback request.
  Success or failure immediately releases the flight. The backend authority
  filters invalid, reserved, and base-colliding plugin names and is shared by
  both routes; optional projection failure cannot fail server config. A real
  hashed-artifact boot falls from seven requests to six and records zero
  `/api/v1/features` calls. Retained authoring source grows only 531 bytes from
  C84 to 3,240,582, leaving 1,720 bytes below the architecture ratchet. The
  final private 35-chunk graph is 3,570,555 / 1,050,043 gzip-9 bytes (+1,096 /
  -52; main 1,020,905 / 313,782, +1,096 / +1). All 44 focused and neighboring
  backend/frontend checks, 30 hashed-runtime smoke checks, and nine sourcemap
  ownership checks pass, as do TypeScript, runtime reachability, actions,
  architecture, source-budget, and diff gates. Nothing was published or
  deployed.
- Unified the two overlapping first-screen `/api/health` reads without merging
  backend-liveness and Sidecar-readiness verdicts. A typed, no-cache
  coordinator shares only the active request, snapshots `ok`/status, and
  exposes one lazy memoized `json()` Promise so the storage owner can decode
  the body without stealing it from another consumer. Settlement—success or
  failure—immediately releases the flight; later recovery and restart probes
  stay live, proxy 401/403 remains a reachable-backend verdict, malformed JSON
  remains storage-fail-soft, and the opener's existing abort deadline bounds
  the shared request. Healthy boot now issues one health GET instead of two.
  Retained authoring source grows 154 bytes to 3,240,051 (2,251 bytes below the
  architecture ratchet). Against C83, the private 35-chunk graph is 3,569,459 /
  1,050,095 gzip-9 bytes (+372 / +127; main 1,019,809 / 313,781, +372 / +138).
  All 23 focused availability/background-budget checks pass; a real hashed
  artifact records exactly one health request across both owners, and all 29
  runtime smoke plus five sourcemap ownership checks pass. Nothing was
  published or deployed.
- Collapsed folder-catalog startup to one failure-preserving, single-flight
  request. `Api.folders.list()` and its reading-library twin now accept the
  wrapped `{items}` contract plus the legacy bare array, but throw on transport
  or malformed success instead of converting an outage to empty data. The
  retained owner shares one Promise across boot/reconnect/push refreshes,
  commits a genuine empty list after one GET, preserves the last good tree on
  failure, exposes the rejection so startup cannot run pinned-folder migration
  against uninitialized state, and keeps its existing bounded recovery chain.
  This cuts both zero-folder startup and first-load outage from two folder GETs
  to one and bounds concurrent refresh bursts to one request. Retained authoring
  source shrinks 589 bytes to 3,239,897 (2,405 bytes below the architecture
  ratchet). Against C82, the private 35-chunk graph is 3,569,087 / 1,049,968
  gzip-9 bytes (+273 / +139; main 1,019,437 / 313,643, +273 / +116).
  A real hashed-artifact boot records exactly one `/api/v1/folders` request;
  all 24 focused/neighboring checks, 28 runtime smoke checks, and five
  sourcemap ownership checks pass, as do TypeScript, runtime reachability,
  actions, documentation, architecture, source-budget, generated-contract,
  and diff gates. Nothing was published or deployed.
- Eliminated the per-turn MCP context rail's unconditional first-screen
  `/api/v1/mcp/tools` request. A deterministic `mcp_tool_summary` now
  piggybacks on the already-required server-config response and on MCP catalog
  / per-tool mutation responses while Settings is active; only expanding one
  server's tool panel requests detailed schema rows. The bridge computes the
  summary under its existing lock from cached live/parked catalogs, counts only
  model-enabled tools, copies no description or input schema, and fails soft so
  an optional projection cannot fail a successful config/catalog/mutation.
  The retained rail is a bounded synchronous projection with no request, boot
  hook, timer, or schema cache. A 240-tool measurement fixture shrinks the
  serialized inventory from 233,761 to 211 bytes (-99.91%) and ordinary page
  startup removes one HTTP request. Retained runtime grows only 101 bytes to
  3,240,486, preserving 1,816 bytes of ratchet headroom. Against C81, the
  private 35-chunk graph is 3,568,814 / 1,049,829 gzip-9 bytes (+166 / +120;
  main 1,019,164 / 313,527, -50 / +109). All 86 distinct focused and
  neighboring MCP, Settings, context-rail, startup, API-shape, parking, and
  failure-contract checks pass, as do 34 final private hashed-artifact/runtime
  checks, TypeScript, runtime reachability, actions, documentation,
  architecture, source-budget, and diff gates. Nothing was published or
  deployed.
- Removed the topbar network badge's duplicate four-second staleness watchdog.
  The existing Push owner already arms an exact per-ping timeout, emits
  `timeout`, force-closes a half-open WebSocket, emits `offline` from `onclose`,
  and reconnects with jitter; the badge now projects those canonical events
  plus the typed conversation-SSE aggregate instead of independently guessing
  transport state from elapsed wall time. This eliminates 900 idle callbacks
  per hour per tab without adding a request, socket, timer, or listener. Badge
  reinitialization and page teardown now release the Push and SSE subscriptions
  independently, so one faulty optional unsubscribe cannot leak the other, and
  its DOMContentLoaded hook is one-shot. The retained adapter shrank from 7,965
  to 6,303 bytes; aggregate retained source fell to 3,240,385 bytes, restoring
  1,917 bytes of architecture-ratchet headroom. Against C80, the private
  35-chunk graph falls 141 / 89 gzip-9 bytes to 3,568,648 / 1,049,709 (main:
  -141 / -95 to 1,019,214 / 313,418). All 23 focused and neighboring
  network-badge, half-open Push, socket-generation, ping/pong single-writer,
  proxy-budget, and reconciliation tests pass, as do 32 final hashed-artifact
  checks, runtime reachability, actions, architecture, source-budget, and diff
  gates. Nothing was published or deployed.
- Replaced the browser-console relay's permanent recursive flush timeout with
  the 7,047-byte browser-global-free `createClientLogFlushScheduler` owner.
  An empty tab now owns no relay timeout or visibility subscription,
  eliminating about 240 direct/LAN or 60 constrained-proxy idle callbacks per
  hour per tab. The first accepted line owns one jittered 15/60-second delay;
  repeated demand coalesces, hidden demand keeps only one resume listener,
  visible resume restores one delay, and asynchronous settlement schedules a
  successor only when the bounded queue still contains value. Offline/push
  preflight retains the unsent queue behind one bounded retry, while transport
  failures still drop the already-claimed batch instead of amplifying an
  outage. Manual flush, the client/server kill switches, original-console
  priority, recursion suppression, pagehide beacon, duplicate folding, and the
  400 queued / 200 wire / 800-character limits remain intact; synchronous API
  failure is now fail-soft as well. Moving direct/proxy profile resolution into
  the typed owner shrank the retained adapter from 6,232 to 5,807 bytes and
  kept aggregate retained source at 3,242,279 bytes under its 3,242,302-byte
  ratchet despite a concurrent unrelated writer. A controlled same-snapshot
  private build measures the feature at +1,641 / +658 gzip-9 bytes (main:
  +1,641 / +689); the final 35-chunk graph is 3,568,789 / 1,049,798 gzip-9
  bytes (main: 1,019,355 / 313,513). All 28 focused and neighboring relay,
  proxy-budget, API-isolation, and runtime-composition tests pass, as do 31
  final hashed-artifact checks, TypeScript, runtime reachability, actions,
  architecture, source-budget, and diff gates. The missing Compaction Viewer
  generated-output `.ignore` entry found by the composition suite was also
  restored, so default discovery cannot ingest that derived bundle. Nothing
  was published or deployed.
- Replaced the Collaboration Bar's permanent 15-second discovery interval with
  one immediate post-wiring refresh and explicit conversation/project/push
  signals, eliminating 240 idle callbacks/hour per tab while improving first
  project-state discovery from as much as 15 seconds to immediate. The
  8,814-byte DOM/timer-free `createPresenceSummaryController` now owns one
  displayed-scope summary, one same-scope shared request flight, a generation
  guard that prevents late A→B responses from repainting B, and an LRU-bounded
  local mirror of 32 roots × 128 conversation IDs. Push bursts retain one
  disposable 300 ms demand debounce; repeated direct refreshes no longer issue
  duplicate in-flight `brainSummary` calls, failures remain non-fatal, and
  teardown clears both pending demand and retained state. The retained runtime
  grows only 390 bytes. Against the unchanged C78 graph, the private 35-chunk
  build is 3,566,419 / 1,048,875 gzip-9 bytes, +2,845 / +869 (main:
  1,017,373 / 312,675, +2,845 / +883). All 59 focused and neighboring
  Presence/Collaboration Bar/Project Brain tests, hashed-artifact timer/source
  smoke, TypeScript, runtime reachability, actions, architecture, source-budget,
  and diff gates pass. Nothing was published or deployed.
- Eliminated the long-lived-tab build watch's dedicated five-minute interval,
  visibility listener, and `/api/health` request. Literal `buildProbe: true`
  now rides the existing authenticated push ping only on the first,
  five-minute, and visibility-resume probes; the priority pong carries an
  optional validated `buildId`, while ordinary four-second pings remain pure
  echoes and old peers remain fail-quiet. The 4,573-byte DOM-free
  `createBuildWatchController` preserves idle gating, one bounded pending
  build, the 30-minute stale-busy ceiling, one-notice semantics, session reload
  guarding, late-subscriber replay, and lifecycle teardown. Optional manifest
  failure cannot drop the liveness pong. An ordinary tab therefore removes 12
  HTTP requests and 12 dedicated timer callbacks per hour plus one listener,
  with no new socket, task, queue, or server polling owner. On a controlled
  same-snapshot counterfactual the retained runtime grows only 164 bytes and
  the private 35-chunk graph 1,521 / 533 gzip-9 bytes (main: +1,521 / +571),
  finishing at 3,563,574 / 1,048,006 gzip-9 bytes (main: 1,014,528 /
  311,792). All 45 focused and neighboring build-watch/push/RTT/auth/resource
  tests, hashed-artifact idle-timer smoke, TypeScript, runtime reachability,
  actions, architecture, source-budget, and diff gates pass. Nothing was
  published or deployed.
- Replaced the command elapsed/deadline chip and Timer Watcher countdown's two
  permanent 1 Hz intervals with the 3,778-byte typed
  `createDemandScopedPresentationTicker` owner. An ordinary visible page now
  owns no tool elapsed timer or visibility listener, eliminating 7,200 idle
  callbacks/hour. Rendering either live clock creates one shared one-shot
  timeout/listener; repeated demand coalesces, hidden tabs clear the timeout,
  visibility resume ticks immediately, and the first tick with no matching DOM
  nodes releases the complete lifecycle. The manifest already retains the
  core and rich tool-round sections adjacently, so the obsolete rich-renderer
  boot scan/re-render pass and one duplicated motion-card dispatch branch were
  removed; source comments and fixtures now describe that real residency
  contract. Retained runtime fell from 3,238,441 to 3,236,760 bytes. The final
  private 35-chunk graph is 3,544,644 / 1,043,850 gzip-9 bytes, +627 / +234
  over C76 (main: 997,906 / 308,324, +627 / +198). All 51 focused and
  neighboring tool-render/timer tests, final hashed-artifact idle-clock smoke,
  TypeScript, runtime reachability, architecture, and source-budget gates pass.
  Nothing was published or deployed.
- Replaced the Swarm panel's permanent 20-second reconciliation interval,
  independent 800 ms Unconfirmed timeout, and unconditional 1 Hz elapsed
  ticker with the 6,205-byte typed
  `createSwarmReconciliationScheduler` owner. A page with no rendered,
  unresolved Swarm now owns zero reconciliation timers and zero visibility
  listeners (eliminating 180 visible-tab callbacks/hour). Rendering demand
  owns one timeout/listener; hidden tabs cancel both reconciliation and elapsed
  clocks, visibility resume checks immediately, all entry paths share one
  status-request Promise, and settlement removes the lifecycle. The existing
  20→40→80→120-second detached-task backoff now sleeps to its true earliest
  deadline instead of waking every 20 seconds merely to skip a request, cutting
  steady 120-second browser wakeups from six to one without increasing API
  traffic. Unconfirmed's fast probe advances that same timer, while the 1 Hz
  ticker starts only for a live elapsed node and stops on its first empty DOM
  tick rather than after 60 idle ticks. The classic section shrank to 56,220
  bytes and the retained-runtime total to 3,238,441 bytes. The private 35-chunk
  graph builds at 3,544,017 / 1,043,616 gzip-9 bytes; the typed lifecycle adds
  3,053 / 1,113 over the prior graph (main: +3,053 / +1,101) in exchange for
  zero idle clocks. All 92 focused and neighboring Swarm tests plus TypeScript,
  runtime reachability, architecture, and source-budget gates pass. Nothing was
  published or deployed.
- Moved the 37,331-byte Compaction Viewer drawer, snapshot/history renderers,
  raw-copy/download flow, and 2-entry / 8 MiB payload LRU into the
  manifest-owned `compaction-viewer-presenters` runtime behind a dedicated
  typed feature. A 730-byte retained adapter composes the 7,340-byte typed
  `CompactionHistoryState` owner with the context bar and endpoint ports:
  requests are single-flight,
  warm navigation has a 15-second freshness window, and the LRU is capped at
  32 conversations × 64 newest rows with at most 32 tracked requests. The
  exact server row count is stored separately, so the bounded projection never
  makes the context bar silently report 64 when a conversation has more
  snapshots. Explicit inspection still force-refreshes and retains the full
  list only while the drawer is open. Close/navigation and rapid archive
  selection now invalidate late list/summary responses, and the loaded owner
  subscribes directly to `tofu:language-change`, replacing the former
  `_cvOnLanguageChange` hook that had no caller. The startup main chunk fell
  from 1,014,208 / 312,290 gzip-9 bytes to 994,226 / 307,025
  (-19,982 / -5,265). First viewer demand adds 22,768 / 6,925, a split-boundary
  cost of 2,786 raw / 1,660 gzip-9 bytes; the complete 35-chunk graph adds
  2,786 / 1,631. Private hashed-artifact smokes proved eleven detailed cold/
  hydrate/open/close-race conditions plus six checks against the final typed
  graph, while 47 focused and neighboring tests plus runtime,
  action, TypeScript, architecture, and source-budget gates pass. Nothing was
  published or deployed.
- Moved the 103,746-byte Debug Panel and Request Inspector presentation,
  payload/trace renderers, task list, TurnStore subscription, and 3/15-second
  polling into the manifest-owned `diagnostics-presenters` runtime behind a
  dedicated typed feature. The 7,092-byte `debug-state` authority remains
  resident from boot and preserves the bounded 80-line diagnostics ring,
  200-error dedupe set, 20-task snapshot log, shared clipboard fallback, and
  tool-round identity resolver, so faults and debug-mode row actions are not
  delayed until a panel opens. Five cold entries route through one domain and
  six action receivers left the eager table (80→74). New-chat and conversation
  navigation now consult an optional loaded-owner lifecycle port instead of a
  bare classic-scope function; a closed or never-loaded inspector therefore
  imports no renderer, starts no poll/subscription, and makes no hidden
  `/debug-messages` request. A frozen `DebugShellState` keeps conversation,
  visibility, config, cache, and request authority live across late loading.
  The startup main chunk fell from 1,059,409 / 325,596 gzip-9 bytes to
  1,014,208 / 312,290 (-45,201 / -13,306). First diagnostics demand adds
  46,745 / 14,173, a split-boundary cost of only 1,544 raw / 867 gzip-9 bytes;
  the complete 34-chunk graph adds 1,544 / 878. Private hashed-artifact smoke
  proved thirteen cold/open/close conditions and caught a missing retained
  `_findRenderedNativeTurnNode` publication before release. All 40 core
  diagnostics/split tests and 37 non-published architecture/action neighbors,
  plus closed-world runtime/action/TypeScript, architecture, and source-budget
  gates pass. Nothing was published or deployed.
- Moved Local Control's 67,298-byte modal, capability probes, downloads,
  diagnostics, three-second poll, and browser-assisted desktop relay into the
  manifest-owned `local-control-presenters` runtime behind a dedicated typed
  feature. Only the 2,379-byte merged badge projection remains retained, so an
  ordinary coding, writing, or research session parses no Local Control
  workbench and performs no browser/desktop status request, localhost scan, or
  panel timer. Five entries route through the lazy domain; a frozen
  `LocalControlShellState` keeps the independent browser/desktop permission
  flags live, while the optional `LocalControlPresentationState` lets the
  retained badge distinguish confirmed disconnection from an unloaded,
  unprobed owner. Extension download and Chrome LNA guidance moved with their
  sole UI consumer. The native-agent `#tofu-agent-relay` path remains an exact,
  explicit boot exception: it prepares the owner and starts the 30-minute
  relay watch without opening the modal; ordinary modal open deliberately does
  not scan localhost. The startup main chunk fell from 1,079,748 / 333,186
  gzip-9 bytes to 1,059,409 / 325,596 (-20,339 / -7,590). First Local Control
  demand adds 24,503 / 9,135, a split-boundary cost of 4,164 raw / 1,545
  gzip-9 bytes; the complete 33-chunk graph adds 4,164 / 1,553. Private hashed
  artifact smoke proved sixteen ordinary-entry conditions plus eight deep-link
  conditions, including idle absence, `misc` isolation, retained badge paint,
  first-action loading, dual status requests, poll disposal, zero ordinary
  localhost probes, and deep-link relay discovery. Runtime/action/TypeScript,
  architecture, source-budget, and 248 focused/neighboring tests pass. Nothing
  was published or deployed.
- Moved the retained Project workspace presenter into the manifest-owned
  `project-presenters` runtime and a dedicated typed Project feature. The
  always-needed Project state/bar/SSE lifecycle remains retained, as do the
  7,027-byte write-approval, subprocess-stdin, and apply-code presenters in a
  new `execution-interactions` section, so common coding work never downloads
  the folder workbench. The 18 modal action receivers now live in the lazy
  owner; Studio and Escape reach its open/close entries through late ports.
  A frozen `ProjectPresentationShellState` keeps conversation identity,
  conversation arrays, project state, and session storage live across switches
  without feature-registry snapshots. Clearing a project calls an optional
  loaded-owner reset object and therefore cannot fetch the panel just to close
  it. Recent-project persistence and background rescan moved to the eager state
  owner because both run independently of the modal. The typed browse
  coordinator and 12 static SVGs now enter only with Project demand; the SVG
  move reduced retained runtime debt to 3,239,813 bytes while leaving the modal
  JS at 43,429 authored bytes. The startup main chunk fell from 1,102,927 /
  340,373 gzip-9 bytes to 1,079,748 / 333,186 (-23,179 / -7,187). First Project
  demand is 27,351 / 8,924; main plus its route costs 3,918 raw / 1,505 gzip-9
  bytes over the former main-plus-`misc` path. The complete graph adds 3,248 /
  1,434 across 32 chunks. A private hashed-build smoke proved sixteen
  conditions including idle absence, `misc` isolation, live state after
  demand, recent/browse requests, drop arbitration, and close cleanup.
  Closed-world runtime/action/TypeScript/architecture/source-budget checks and
  141 focused/neighboring tests pass. Nothing was published or deployed.
- Moved the retained local Knowledge Workbench presenter (40,644 authored
  bytes) into the manifest-owned `knowledge-presenters` runtime and a dedicated
  typed Knowledge feature. Its open/close entries no longer share the generic
  `misc` feature: frequent write-approval, stdin, Human Guidance, and cost
  actions cannot pull the corpus catalogue, preview, upload, search, polling,
  or its Escape listener into memory. The 20 Workbench action receivers are
  generated inside the lazy owner; the retained chat-drop guard still reads
  the live private modal flag, so Knowledge drops never become chat
  attachments. Four lexical shell dependencies are validated at evaluation.
  The startup main chunk fell from 1,126,455 / 346,629 gzip-9 bytes to
  1,102,927 / 340,373 (-23,528 / -6,256). First Knowledge demand adds 24,235 /
  7,127, a split-boundary cost of 707 raw / 871 gzip-9 bytes; the complete
  JavaScript graph adds 707 / 879 across 31 chunks. A private hashed-build
  smoke proved twelve conditions including idle owner absence, `misc`
  isolation, one first-action route, private action publication, modal
  open/close, and the first authoritative catalogue request. Closed-world
  runtime/action/TypeScript checks and 32 focused/neighboring tests, including
  both real-browser Workbench flows, pass. Nothing was published or deployed.
- Moved the retained My Day calendar/report presenter (45,259 authored bytes)
  into the manifest-owned `myday-presenters` runtime. The open, close, and
  generation entries still route through the typed My Day feature, while the
  shell Escape handler now closes through a late registry port. Typed TODO and
  quick-action policy continue to compose with the presenter on first demand;
  the owner-scoped cache/digest/reminder controller remains in the independent
  background graph, so idle reminder preloading does not pull report markup,
  polling, or calendar rendering into the startup runtime. All seven shell
  dependencies are declared and validated at the generated ESM boundary.
  Duplicate empty/TODO SVG assets now have one typed lazy owner, and an unused
  retained status-icon table is gone; the retained section fell to 41,064
  bytes and total authored My Day code fell by 2,240 bytes. The startup main
  chunk fell from 1,150,774 / 352,647 gzip-9 bytes to 1,126,455 / 346,629
  (-24,319 / -6,018); the existing typed My Day chunk grew from 6,157 / 1,972
  to 30,105 / 8,505. Main plus first My Day demand is 371 raw bytes smaller and
  adds only 515 gzip-9 bytes at the split boundary; the complete 30-chunk graph
  is also 371 raw bytes smaller and adds 511 gzip-9. A private hashed-build
  smoke proved ten conditions including
  idle owner absence, dynamic-only residency, one first-action route, owner
  publication, modal open/close, and authoritative daily requests. Closed-world
  runtime/action/TypeScript checks and 50 focused/neighboring tests pass. Two
  additional Paper/Research visual cases read a concurrently published stale
  Paper chunk and fail outside this source/private-build boundary. Two stale
  pet guards now follow the typed repository's single digest-publication choke
  instead of requiring retired presenter boot logic. Nothing was published or
  deployed.
- Moved the retained single/batch Image Generation presenters (37,309 authored
  bytes before the split) into a manifest-owned `image-generation` runtime.
  Ten open/select/generate/cancel/retry entries now route through the dedicated
  Image feature, while upload previews, Enter/Esc/mobile send, Settings model
  visibility, and native Turn retry/cancel use optional live registry ports.
  The four tiny per-conversation selection scalars remain in the composer and
  cross one stable getter/setter state object, avoiding both captured lazy
  values and feature-registry override drift after a conversation switch. The
  ambient five-second model-catalogue timer is gone: first Image demand owns
  the request, concurrent requests coalesce, and a late server-config response
  refreshes only an already-loaded picker. The startup main chunk fell from
  1,166,636 / 357,280 gzip-9 bytes to 1,150,774 / 352,647
  (-15,862 / -4,633); first Image demand adds 20,743 / 6,363, an aggregate
  split-boundary cost of 4,881 raw / 1,730 gzip-9 bytes. A private hashed-build
  smoke proved ten conditions including zero pre-demand model requests,
  first-action loading/execution, one on-demand request, rendered models, and
  private action resolution. Closed-world runtime/action/TypeScript checks and
  79 focused/neighboring tests pass. Stale timeout/P0/seam guards now read the
  authored runtime graph and recognize the canonical batched service table.
  Nothing was published or deployed.
- Moved the retained Update, Timer, and Optimizer presenters (83,534 authored
  bytes) into one manifest-owned `utility-panels` runtime. Five early entries
  route through a dedicated typed feature; a single idle preload preserves the
  ambient update check, push-first Timer badge, Optimizer count, and polling
  lifecycles, while an earlier user interaction wins the race and loads the
  same owner immediately. Both badges now use pre-land-safe declarative
  actions, eliminating Optimizer's late native listener and its first-click /
  double-toggle hazard. Settings reads the update pill through an optional
  live registry port, so it can load first. Artifact-level jsdom smoke proved
  main-only startup, Settings-before-utility, first-click execution, and
  private action publication. That smoke also found and closed two supply-side
  seams that static free-name checking could not: `getConvById` and the stable
  push subscribe/reconnect functions are now explicitly published by their
  canonical retained owners, while mutable feature flags and active
  conversation identity remain live registry reads. The startup main chunk
  fell from 1,202,424 / 366,848 gzip-9 bytes to 1,166,636 / 357,280
  (-35,788 / -9,568); the idle chunk is 38,126 / 11,035. After idle preload the
  split boundary costs 2,338 raw / 1,467 gzip-9 bytes in aggregate, but removes
  those presenters from parsing/evaluation and local API scheduling on the
  critical first screen. Existing Update/Timer behavior, closed-world runtime,
  action, TypeScript, and 72 neighboring tests pass. The obsolete
  `window.modelPricePresentation` fallback was also removed; feature actions
  remain private. Nothing was published or deployed.
- Restored the access-matrix global model toggle on logical header and root
  wire rows after a checkpoint had removed the markup while leaving the
  renderer contract and `.stg-mx-gtoggle` styles behind. Wire-pool tests now
  inspect one `<tr>` at a time, so negative assertions cannot cross into the
  next wire row when markup grows. Responses-provider coverage now reads the
  authored Settings, conversation-lifecycle, and provider-template owners
  instead of treating the generated main runtime as a universal text bundle.
  The obsolete classic-bundler Settings-panel test was replaced by a current
  Vite-domain contract that proves single ownership, single entry routing,
  late-evaluation boot behavior, and mobile-wrapper rebinding. All 113
  neighboring Settings/matrix/provider tests pass. A private build keeps the
  main chunk byte-identical at 1,202,424 bytes; the repaired control adds only
  320 raw bytes to the demand-loaded Settings JavaScript. Nothing was
  published or deployed.
- Moved the complete Settings-only comfort layer (progressive disclosures,
  preference accordions, quiet control surfaces, provider deep-link pulse,
  and mobile modal refinements) out of the retained stylesheet and into the
  lazy Settings feature. The independent `.remote-*` project-folder picker is
  now the only rule family in the fifth retained Settings section. All
  declarations match the pre-move baseline except the Tools override already
  transferred in the preceding slice, and the built CSS preserves the order
  Devices → Tools → comfort → provider surfaces. Retained Settings authoring
  fell from 136.4 to 125.4 KiB; the always-loaded source asset fell 11,228 raw
  / 1,936 gzip-9 bytes, while the first-demand Vite Settings CSS grew 8,537 raw
  / 1,511 gzip-9 bytes. The aggregate first-open path is therefore also 2,691
  raw / 425 gzip-9 bytes smaller, rather than merely shifting cost between
  chunks. Boundary tests, neighboring Settings/CSS contracts, style
  composition, source budgets, TypeScript, and a private Vite build pass; main
  JavaScript and main CSS are unchanged. Nothing was published or deployed.
- Moved all twenty `.tools-inv-*` rules, including the Settings comfort
  override, out of the eager retained stylesheet and beside the lazy typed
  `tools-inventory.ts` owner. A declaration-level comparison against the prior
  generated stylesheet proves the selector/body set is unchanged, and neither
  eager stylesheet retains a second Tools inventory owner. Retained Settings
  authoring fell from 138.8 to 136.4 KiB. The startup minified Settings asset
  fell 1,802 raw / 249 gzip-9 bytes (107,482 / 18,387 to 105,680 / 18,138),
  while its first-demand Vite CSS grew 1,797 raw / 351 gzip-9 bytes (6,302 /
  1,714 to 8,099 / 2,065); users who never open Settings avoid the former,
  while users who do pay a 102-byte aggregate gzip split-boundary cost.
  Focused Tools rendering/ownership tests, style composition, source budgets,
  TypeScript, and a private Vite build pass. Nothing was published or deployed.
- Moved the Devices-tab `.devices-*` presentation rules beside the typed
  `devices.ts` owner and import them only with the lazy Settings feature. The
  independent `.remote-*` project-folder picker remains in the eager retained
  stylesheet because it can render before Settings opens; the new feature CSS
  remains ordered before the existing provider-surface overrides, preserving
  the prior cascade. This clears the last frontend source-budget failure:
  retained Settings authoring fell from 142.4 to 138.8 KiB. Under the actual
  content-hash CSS minifier, the startup Settings asset fell 1,233 raw / 214
  gzip-9 bytes (108,715 / 18,601 to 107,482 / 18,387), while first Settings
  demand adds 1,163 raw / 276 gzip-9 bytes to its existing lazy CSS chunk.
  Focused ownership, class-coverage, style-composition, TypeScript, and private
  Vite-build checks pass; main JavaScript and main CSS are unchanged. The
  private Vite build was not published or deployed.
- Replaced the six retained Orchestration HTTP owners and the retained
  saved-Flow catalogue with typed startup owners. The canonical endpoint
  generator now emits one immutable `request-contracts.generated.ts` registry;
  `api-client.ts` projects its path/query/body/CAS rules through the shared
  typed transport and installs the generated facade into the stable
  `Api.orchestrations` placeholder. `flow-catalog.ts` keeps one single-flight,
  30-second-fresh immutable snapshot, revokes invalidated reads, preserves the
  last good list on failure, and isolates observer/diagnostic faults. This
  closes the real startup edge where the chat Flow Picker called API and error
  helpers that existed only in the lazy Studio chunk. The same artifact-level
  smoke loaded the main chunk alone, rendered a custom saved Flow, exercised a
  first-read 503 without throwing or losing its failure notice, then loaded the
  Studio chunk and resolved `openOrchestration`, `openTaskMode`, and
  `closeTaskMode`. It also exposed and fixed feature-flag loading through the
  retired `window.Api` path; startup now uses the lexical private API port.
  Retained-runtime debt fell from 176 sections / 3,271,318 bytes to 169 /
  3,240,997, passing the byte ceiling by 1,305 bytes with every other debt
  counter at zero. Relative to C62, the eager chunk grew 10,528 raw bytes to
  1,202.42 kB / 367.68 kB gzip, the lazy Orchestration chunk fell 18,840 raw
  bytes to 433.82 kB / 125.98 kB gzip, and their combined JavaScript fell
  8,312 raw bytes / 1.40 kB displayed gzip. The private build was not published
  or deployed.
- Deferred the complete Orchestration Studio and Task Mode runtime until either
  surface is requested. Eighty-eight existing retained sources plus the three
  typed owner barrels now load through the manifest-owned
  `orchestration-presenters` feature graph; the startup runtime keeps only the
  5.8 kB saved-Flow picker used by the chat toolbar. The manifest can declare a
  typed registry import, and the shared action analyzer publishes only
  registry members that are actually named by authored controls, closing the
  `openTaskMode` route without restoring a browser global or a hand-maintained
  export list. A private same-worktree Vite build reduced the startup main
  chunk from the C61 baseline of 1,643.27 kB / 494.96 kB gzip to 1,191.90 kB /
  364.92 kB gzip (-451.37 / -130.04 kB); the first Orchestration request loads
  a 452.66 kB / 130.14 kB gzip chunk. Direct module-graph smoke coverage loaded
  both chunks and resolved `openOrchestration`, `openTaskMode`, and
  `closeTaskMode`; runtime composition, action, closed-world binding, and
  TypeScript checks pass. Removing the eager 353-member registry binding also
  lowered retained-source debt by 11,381 bytes to 3,271,318 bytes, though the
  pre-existing byte ratchet remains 29,016 bytes over its ceiling with every
  other architecture debt counter at zero. The private build was not
  published or deployed.
- Deferred the complete retained Settings presentation family until Settings
  is requested. Sixteen existing sources, including the state head and MCP /
  OAuth presenters, now compose into the manifest-owned `settings-presenters`
  runtime; no retained section was added, so composition remains 174 authored
  sections (176 including prelude/epilogue in the architecture audit).
  Reassignable shell and Settings state crosses live private-registry
  accessors, while stable functions/objects are declared runtime services.
  Onboarding's immediate `_oauthLogin` joins the Settings feature route so its
  open/switch/login sequence survives one dynamic-import window. Declarative
  actions no longer require hand-maintained lazy exports: one shared analyzer
  scans retained sections, typed owners, and HTML fragments, and the composer
  publishes only receivers defined by each generated runtime. Raw-section
  section-requirement and STT harnesses now compile their typed owners and use
  the same `featureRegistry` boundary as production. A current private Vite
  build reduced the startup main chunk from the C60 baseline of 1,890.67 kB /
  558.74 kB gzip to 1,643.27 kB / 494.96 kB gzip (-247.40 / -63.78 kB); the
  user-triggered Settings chunk is 320.73 kB / 85.78 kB gzip. The private
  module-graph smoke loaded that chunk with every declared dependency and
  routed entry present. This is primarily a delivery-residency saving; moving
  live ports into the manifest-generated boundary also lowered retained source
  debt by 922 bytes from the C60 baseline (the pre-existing byte ratchet still
  fails), and the build was not published or deployed.
- Deferred the retained Paper report and reader presenters until Paper is
  actually requested. `paper/report.js` and `paper-reader.js` now compose into
  the manifest-owned `paper-reader-presenters` runtime instead of the startup
  runtime; late typed-owner state and all markup action receivers cross one
  explicit `featureRegistry` boundary. The landing-page draft input now calls
  a registered setter instead of using an assignment form the safe action
  grammar could not execute. Raw-section regression harnesses connect native
  owners through the same registry boundary, including patched negative
  controls, rather than relying on shared `eval` lexical scope. In private
  same-worktree Vite builds, the startup main chunk fell from 1,954.30 kB /
  577.83 kB gzip to 1,890.67 kB / 558.74 kB gzip (-63.63 / -19.09 kB), while
  Paper became a demand-loaded 124.16 kB / 33.55 kB gzip chunk. This is a
  delivery-residency improvement, not retained-source retirement: the
  architecture audit still reports 176 retained sections and 3,283,621 bytes
  against the 3,242,302-byte ratchet, with every other debt counter at zero.
  The build was not published or deployed.
- Bound browser Conversation Sync residency to current user-visible value.
  Changing `activeConvId` now explicitly re-evaluates only the previous and
  current warm stores: an outgoing settled conversation closes its EventSource
  immediately, while a background `pending`/`running` Turn retains live
  delivery. This fixes the missing lifecycle edge where state-driven pause
  policy never reran on a sidebar-only selection change, so every visited
  conversation could retain another five-second durable-log poller. In a
  stable 2026-08-29 live sample, 43 conversation SSE subscriptions coexisted
  with two Push subscribers and zero active tasks; storage advanced at 8.1
  queries/second, matching the stream heartbeat estimate of 43/5 = 8.6. A
  read-only authority aggregate found 2,948 Turns and 1,543 Attempts, all
  terminal. The resource regression visits 32 settled conversations and
  permits one open source, then zero after disposal. These are observed
  pre-fix evidence and a tested counterfactual, not a deployed saving claim.
- Made conversation-catalog validators describe applied UI state rather than a
  merely decoded HTTP response. ETag, total count, and 500-row applied-snapshot
  identity now commit only after synchronous merge/render succeeds; a failed
  projection therefore retries with one unconditional request instead of first
  spending a conditional `304` and then downloading the same full page. The
  existing pure Turn-read boundary prevents catalog scans from constructing
  coordinators, while the loader now also fails without amplifying any future
  projection exception. In a frozen 2026-08-29 access-log window the catalog
  issued 554 requests and transferred 19,389,792 response bytes; 82 matching
  client warnings showed the historical stack failure across two long-lived
  tabs. This is a tested counterfactual for the failure path, not a deployed
  traffic-saving claim; ordinary healthy refreshes retain the single-304 path.
- Normalized model-produced `get_conversation(before=0)` to the omitted latest
  window at the tool execution boundary. The provider schema still requires a
  positive cursor, positive cursors retain exclusive paging, and malformed,
  negative, boolean, fractional, plus `limit=0` inputs still fail explicitly.
  In a frozen 2026-08-29 application-log window, 14 zero-cursor failures across
  three long tasks were all followed by another `get_conversation` call; 13
  retries arrived within 60 seconds (median 13.5 seconds). Under those same
  calls this removes the failed tool result and its repair delay; it is a
  counterfactual task-round opportunity, not a deployed API-cost claim.
- Removed the historical-message cost N+1 from conversation rendering. Missing
  legacy cost stamps now join one bounded 512-entry browser micro-batch per
  synchronous render, deduplicate both queued and in-flight fingerprints, and
  converge through one authoritative Surface repaint after an exactly aligned
  server response. Partial/failed batches remain retryable and never become a
  fabricated no-charge cache entry; settled Turns still use their persisted
  cost and the browser still performs no pricing math. A frozen access log had
  266 single-message calls and zero batch calls, with repeated 18–19 request
  bursts in one second. Under the same 19-miss render, the new contract makes
  one batch request and zero single requests; this is a tested counterfactual,
  not a deployed traffic claim.
- Made incremental-translation API admission last for the Task/Turn instead of
  one five-minute accumulator lifetime. A bounded three-field task-local state
  now carries the preview-call count, one-429 circuit, and one-shot pressure
  notice across idle worker eviction; a closed circuit/count rejects later
  narration before recreating a thread or queue. The worker retains only that
  small shared state rather than the full task. Terminal reasoning still uses
  the ordinary translation budget, and finalize/stamp/cancel clear preview
  state without changing final delivery or durable commit. In a frozen
  2026-08-29 00:00:41–02:27:25 log window, 28 explicit preview `1/1` upstream
  429 exhaustions belonged to 19 tasks; 9 were later exhaustions on a task that
  had already tripped its preview circuit. Those 9 are a counterfactual
  request-saving opportunity under the same event sequence, not deployed
  savings; terminal translation attempts are deliberately excluded.
- Made SQLite deep-clean analysis report actionable semantic reclamation
  instead of relying on the physical freelist alone. The read-only analyzer and
  offline deleter now consume one Sidecar-owned transport-retention selector;
  the analyzer reports exact eligible row/payload totals for settled Sidecar
  and legacy attempt frames plus streaming/structural task events under the
  requested positive finite TTL.
  One machine-readable plan combines those results with deferred indexes,
  conditional legacy-conversation mirrors, and verified-copy capacity; it
  emits a stopped-server command only when the safe copy fits and never
  auto-selects the rollback-free low-space mode. All queries retain the shared
  60-second deadline and partial-result contract. On the live 87,625,744,384
  byte authority, the new default one-day analysis completed 12 tables plus all
  four retention sources in 22.275 seconds and found 1,381,718 rows carrying
  37,151,362,727 exact encoded payload bytes eligible for transport
  deletion; another 12,965,955,996 payload bytes are conditional legacy-mirror
  candidates that the offline pass must prove semantically before deletion.
  These are logical payload opportunities, not claimed file-size savings; no
  compaction, rollback retirement, server stop, or authority mutation was run.
- Made My Context profile consolidation obey optional-work admission before
  spending API capacity. The reconstructible background pass retains its
  one-actual-429 ceiling, but now honors recorded billing stops and immediately
  yields to an active shared provider/model contention gate without transport,
  sleeping, or advancing the probe clock; healthy capacity and an atomically
  reserved due probe still proceed. Expected deferral is informational and a
  later eligible turn remains the retry lifecycle. In a frozen 2026-08-29
  00:32–04:02 application-log window, all 19 profile-consolidation failures
  exhausted `1/1` upstream 429 attempt and had a same-thread shared-project
  contention record. That is evidence of repeated contended work and an
  optimization opportunity, not a claim that all 19 historical calls would
  have been skipped because due probes remain admissible.
- Kept long-running tools synchronous to the model while adding runtime-level
  asynchronous behavior: sequenced/coalesced `tool_progress` frames with
  bounded reconnect replay, owner-scoped Stop fanout to registered process
  groups, TERM/grace/KILL/reap cleanup, and incremental large-output spooling.
  Oversized command results now persist behind opaque owner-scoped artifact
  references and return one bounded head/tail model result; timeout, interrupt,
  and cancellation retain partial artifacts, and production startup reclaims
  abandoned transient spools. Interactive Codex-style yielded shell sessions
  remain deferred until a real independently resumable workload requires them.
- Made terminal chat residency reconstructible instead of one-hour hot-memory
  retention. Normal root-chat finalization now uses the canonical immutable
  terminal stamp, fixing successful tasks whose `finished_at` was absent and
  whose cleanup age therefore fell back to task creation time. On a live
  read-only sample, all three terminal chat tasks lacked that timestamp; the
  five-task registry retained about 34.1 MB of serialized events in aggregate,
  and the three terminal tasks accounted for 663 of roughly 1,145 retained
  events (not an attribution of exact bytes or Python RSS). Owner-scoped
  `GET /api/v1/tasks/{id}`, bounded `/events`, and SSE now fall back to
  `task_results` plus the durable event log after registry eviction/restart,
  preserve sparse absolute sequences, and return `interrupted` as terminal.
  Intermediate replay pages use a compact Sidecar projection and load the
  cumulative answer/thinking only once on the caught-up terminal page. This
  permits a launch-probed hot terminal TTL of 600..1,800 seconds in personal
  mode (600 seconds on the 8 GiB/probe-failure profiles), 3,600 distributed,
  with a 60..86,400 explicit hard range; active tasks are never TTL-evicted.
  In the same snapshot all three terminal tasks were already 1,305..2,674
  seconds old, so the 600-second reference policy would make them eligible for
  cold residency, but the sample does not claim deployed RSS savings.
- Bounded the task-local tool-result reuse cache without disabling streaming
  prefetch. Every production writer now uses one launch-probed FIFO (64..256
  personal entries, 128 on the 8 GiB reference, 64 on probe failure, 512
  distributed, 1,024 hard ceiling); pressure evicts only an old optimization
  receipt, so a later call safely executes live. Budget/offload rewrites refresh
  the compact receipt, write-triggered freshness invalidation is unchanged, and
  terminal settlement releases the entire cache alongside API messages and
  Flow snapshots instead of pinning it for the remaining hot task TTL. A frozen
  six-hour window had 35 terminal snapshots with 1,330 entries (median 4, p95
  134, maximum 466); applying the 128-entry reference ceiling to those snapshots
  retains 745, a 585-entry/44.0% residency opportunity rather than a measured
  lifetime peak or RSS saving. In the same window, 2,084/2,093 speculative
  injections matched a receipt within 60 seconds, so prefetch remains enabled.
  Across seven local 100,000-store loops, the bounded writer measured 1.734 µs
  median per store versus 0.970 µs for the equivalent bare FIFO (0.763 µs
  incremental policy overhead).
- Added zero-request shared-contention deferral for explicitly reconstructible
  LLM work. The existing provider/model gate now has an atomic immediate-only
  admission: a due probe is reserved, while a still-blocked family returns a
  typed `request_not_dispatched` signal without moving its probe clock. Chat,
  sync-stream, async-stream, and `smart_chat` share the contract; waiting
  remains the default for interactive, terminal, durable, and Swarm work. Only
  project-summary refreshes and non-terminal incremental-translation previews
  opt in; both retain their existing title/final-translation fallback, finite
  429 budget, strict billing admission, cancellation, and bounded worker lanes.
  In a frozen six-hour log window, 11 of 30 project-summary 429 records arrived
  within 30 seconds of an earlier same-family 429; among 27 incremental
  translation threads, at least one first 429 did too. These 12 records are a
  conservative arrival-window opportunity, not a claim that exact historical
  admission instants or deployed savings are observable from response logs.
- Isolated local L2 summary dispatch from the parent conversation's prompt-cache
  lifecycle. Automatic and manual summaries now bind a stable, content-free
  digest of validated owner + conversation + L2 stage to both dispatch affinity
  and the streaming body; the parent affinity is restored on every success,
  failure and fallback. This replaces random Codex summary session IDs without
  letting summary writes arm or overwrite ordinary-round settle state. The
  extra entries use the existing one-hour/4,096-entry bounds and add no worker
  or queue. In a frozen six-hour window, 129 summaries had 43 pre-summary holds
  totalling 84.61 seconds and 100 matched next-round holds totalling 218.61
  seconds. Their 303.22-second sum is a counterfactual interference opportunity,
  not a deployed latency or cache-hit claim; model choice, summary content,
  compaction thresholds, retry, archive and usage accounting are unchanged.
- Replaced stable tree-index 45-second rebuild loops with conservative adaptive
  refresh. The first snapshot and any changed sorted path+size columns retain
  45 seconds; exact unchanged rebuilds advance to 90 and at most 180 seconds.
  Disk reload/process restart resets to 45 seconds, Tofu writes remain
  synchronously updated or invalidated, and the existing 900-second hard trust
  boundary is unchanged. Results served beyond 45 seconds now expose compact
  snapshot age/schedule evidence and state that external path/size changes may
  not yet be reflected, while grep still reads existing file contents live.
  The frozen six-hour log window contained 580 rebuilds and 814.2 aggregate
  build-wall seconds. A deliberately incomplete file-count-only replay (less
  strict than the shipped full path+size comparison) retained 214 builds and
  339.3 seconds, so its 366-build/474.9-second reduction is an optimistic upper
  projection, not a deployed saving. At the 600,000-entry hard cap, the shipped
  equality check measured 0.744 ms median across seven 20-call loops.
- Added an owner-scoped slow-directory circuit to `grep_search` after live
  rg/GNU timeouts. The first scan still runs, preserves partial results, and
  records no file content; for five minutes, only equivalent `(target,
  include)` scans by the same authenticated owner fail fast with a narrowing
  hint. Index hits, narrower paths, changed globs, other owners, and ownerless
  legacy calls remain eligible. The process-local registry defaults to 256
  content-free entries, has a 1,024 hard ceiling, and expires on cooldown or a
  successful equivalent query. In the frozen six-hour window, a 300-second
  replay would have skipped 16 of 26 completed 60-second scans (960 scanner
  process-seconds) while admitting ten probes. The content-free admission check
  measured 3.35 µs median on a miss and 4.10 µs with an open circuit over five
  200,000-call loops; these are local projection/microbenchmark results, not a
  deployed latency or resource claim.
- Recovered a recurring read-only model-tool sentinel before it could consume a
  failed tool cycle. `get_conversation(before=0)` now becomes an audited,
  UI-visible `zero_cursor_omission` repair at the unified pre-contract ingest
  boundary, meaning the same thing as an omitted cursor: read the latest/default
  window. Its provider schema now declares `minimum: 1` and explicitly tells
  models never to send zero. Boolean false, negatives, fractions, `limit=0`,
  public repository calls and direct strict executor calls remain invalid. The
  frozen six-hour log window contained 15 zero-cursor failures that returned no
  conversation bytes; this prevents those failures but does not claim every one
  would otherwise have caused an additional API round or a deployed saving.
- Made cgroup memory relief attributable before changing its policy further.
  Each pass now samples Tofu process RSS plus shared cgroup usage/cache around
  the heap and log-page windows, records duration, returns the same byte-level
  evidence to callers, and exposes bounded Prometheus totals/latest gauges.
  Only `process_rss` is described as Tofu-owned; shared window deltas explicitly
  remain non-causal because siblings allocate concurrently. A read-only sample
  of the old worker observed one reported 297.7 MB cgroup drop alongside an app
  RSS fall from about 1,531 MiB to 1,276–1,307 MiB while cgroup file cache grew,
  supporting heap trim rather than removal. Tests now redirect every synthetic
  pressure snapshot to a temporary journal instead of polluting the live
  incident record. A conservative three-full-snapshot microbenchmark measured
  247.9 µs median and 280.2 µs p95 across 1,000 loops; the real path reuses its
  before/after usage probes. Sampling runs only when relief already runs and
  makes no deployed savings or end-to-end latency claim.
- Replaced the fixed ten-minute retry cadence after structurally ineffective
  shared-cgroup relief with a bounded adaptive cooldown. Five consecutive
  sub-material reclaims now schedule probes at 600, 1,200, 2,400, then 3,600
  seconds by default; a genuinely material reclaim or aggregate pressure below
  the relief trigger re-arms the base delay. Pressure journaling, OOM detection,
  request admission and process-local RSS relief remain active on every relevant
  path. In the frozen always-ineffective model this reduces aggregate cache
  flushes from 17 to 8 over two hours and from 149 to 30 over 24 hours, without
  adding a thread or weakening the independently owned RSS boundary. The live
  shared cgroup motivating this change was 95.38% full (207 GiB of 217 GiB),
  including 146.5 GiB file cache and 18.6 GiB anonymous RSS, while the Tofu app
  plus Sidecar accounted for about 2.5 GiB RSS; these are diagnostic and
  projected counts, not deployed resource or latency savings.
- Extended the serialized storage-frame budget into every application/worker
  client process, rather than bounding only the Sidecar half of the loopback
  transport. All `StorageClient` instances in one process share the existing
  128..512 MiB personal / 1 GiB distributed profile. A response reserves its
  declared JSON body after the four-byte header and releases immediately after
  decode; command responses drain before reconstructible reads. A five-second
  client-pressure timeout may replay a read but remains an ambiguous,
  single-attempt command failure. Process-local Prometheus metrics expose
  current/capacity/peak bytes, waits, rejections, admitted bytes and observed
  response totals/maxima. On the reference profile this independently caps raw
  response decoding at 128 MiB instead of the slot-only 8 × 64 = 512 MiB; the
  measured high-capacity profile caps 12 × 64 = 768 MiB at 512 MiB. The
  acquire/observe/release path measured 1.969 µs best and 2.016 µs median over
  five 500,000-iteration loops, about 0.14% of the prior 1.41 ms median physical
  commit acknowledgement. This adds no thread and does not claim decoded-result,
  kernel-buffer, deployed-RSS, or end-to-end latency savings.
- Recovered commands from a Sidecar handler-capacity burst without weakening
  at-most-once behavior. The only response emitted before the accept loop reads
  a request now carries a fail-closed literal
  `request_not_dispatched=true`. The synchronous client may replay that command
  within its existing three-attempt transient ceiling, with the same command
  ID/payload and a new transport request ID; missing/false/string proof,
  timeout, EOF, backend failure, and any possibly executed command remain one
  attempt. Completed retry and bound-exhaustion counters are exposed locally
  and through Prometheus. The motivating live window had one handler-capacity
  rejection that caused authoritative task-event persistence to withhold frame
  374; this policy would grant the proven pre-dispatch request two more bounded
  local attempts, but does not claim they would have succeeded or alter API
  spend.
- Bounded the Sidecar's serialized-frame memory multiplier independently of
  active-handler count. Requests now reserve their declared body bytes before
  allocation, responses reserve one maximum frame before encoding, and both
  directions share a response-priority FIFO byte budget: 128..512 MiB in the
  personal launch profile (128 MiB on the 8 GiB reference and probe failure),
  1 GiB distributed, and an explicit 128 MiB..8 GiB range. This changes the
  reference slot-only envelope from 8 × 64 = 512 MiB to 128 MiB, the measured
  high-capacity profile from 12 × 64 = 768 MiB to 512 MiB, and distributed from
  4 GiB to 1 GiB, without reducing useful RPC slots. A five-second bounded wait
  closes before dispatch under request pressure; completed responses drain
  first so resource protection does not preferentially lose committed results.
  The wire decoder also replaced fragmented chunk accumulation plus `join`
  with one exact `recv_into` buffer. A frozen 46 MiB/64 KiB-fragment benchmark
  cut traced Python peak allocation 92.09 → 46.00 MiB and median receive time
  30.31 → 22.27 ms across seven untraced timing trials. New Sidecar/Prometheus
  metrics expose reservation pressure and request/response byte totals/maxima.
  These are enforced admitted frame-body bounds and local microbenchmarks, not
  a whole decoded-object, oversized pre-rejection serialization, kernel-buffer,
  deployed-RSS, or end-to-end claim.
- Reduced personal Sidecar allocator sawtooth without changing storage
  concurrency or durability. Idle-edge `malloc_trim(0)` still runs only at zero
  active RPCs and above the launch-derived 128..384 MiB RSS threshold, but its
  personal cooldown is now 60 seconds instead of 300; distributed replicas keep
  300 seconds and explicit 30..3,600-second overrides remain. A frozen current-
  generation window contained 13 trims: median RSS fell 826.6 → 304.6 MiB,
  median reclaimed bytes were 439.5 MiB, and the maximum pre-trim RSS was
  920.6 MiB. At equal allocation rate the one-minute policy projects about
  409 MiB median pre-trim RSS; this is not a post-deployment saving claim.
  Sidecar and Prometheus metrics now expose RPC/process residency plus cumulative
  and latest trim duration, so the admission-fenced latency cost is measurable.
- Halved the expected frequency of fastpath's database-sized shadow rewrites
  without making its recovery tail unbounded. The effective rebase trigger is
  now one quarter of authority bytes, capped per local/durable WAL at 2% of
  launch-time free disk and 16 GiB; both trigger copies together remain within
  a 4% envelope. Concurrent copy-window commits retain their separate final
  publication-capacity check rather than being described as part of that cap.
  Shipper startup rechecks the local-front and durable-shadow filesystems and
  takes the smaller ceiling, while a failed secondary probe keeps the bounded
  launch value. Every physical commit now observes that threshold: once reached,
  the fair writer refuses later transactions before `BEGIN` with typed,
  retryable `database_busy`, while the raw shipper checkpoint bypasses the
  fence and reopens admission after truncation. One already-started bounded
  commit segment remains atomic. This prevents unbounded WAL growth during a
  long image copy or failed capacity preflight without cancelling resumable
  work, blocking reads, or weakening the prior durable recovery point. Sidecar
  metrics and Prometheus now expose published generations, actual database/WAL
  chunks copied in the Sidecar lifetime, durable progress, local WAL/headroom,
  pressure state/activations/rejections, fail-closed observation failures, and
  the resolved threshold. The hot local WAL `stat` measured 870 ns per physical
  commit (500,000 loops, best of five), about 0.06% of the previously measured
  1.41 ms median acknowledgement; group commit amortizes it across logical jobs
  and no thread, queue, or cache was added. The
  motivating frozen window completed 19
  roughly 82-GiB generations and wrote 1.56 TiB of database images; at equal
  churn the new 16-GiB threshold projects about ten generations and 0.7–0.8
  TiB fewer full-image writes. This is a bounded policy projection, not a
  post-deployment saving claim; explicit lower overrides remain supported.
- Added request-local strict billing-stop admission for optional translation
  and reconstructible LLM enrichments without changing the Settings contract
  for attended Agent calls. A recorded key-wide 402 or matching key/model quota
  stop now defeats a stale manual ON only while choosing one of those
  candidates, cannot be resurrected by the provider last-resort rule, and
  cannot escape through `smart_chat`'s direct default-key fallback. Healthy
  sibling models/providers remain eligible. If
  policy rejects the whole pool, `DispatchNoAdmissibleSlot` now terminates
  before transport, 300-ms cooldown polling, translation outer backoff, or
  per-segment fallback fan-out; sync and background delivery retain one typed,
  retryable `no_slot`/503 failure instead of a generic 500.
  Project-summary refresh, automatic conversation titles, daily-report
  analysis, and optimizer proposals reuse the boundary and keep their existing
  empty/deterministic fallback; explicit scheduler prompts/polls remain out of
  scope. Those reconstructible calls now also share a manifest-backed ceiling
  on actual upstream 429 responses (personal 2, distributed 8, hard cap 16),
  rather than inheriting the dispatcher's intentionally open-ended interactive
  rotation. Capacity polling is free of this count, and title generation skips
  its second dispatch after a terminal no-slot or exhausted-budget result. The
  day contained 38 project-summary and nine title generations. After the
  motivating key-wide stop, three later summary refreshes alone sent eight
  additional 429 probes to that same manually-ON key.
  In the motivating production log, the first key-wide 402 was recorded at
  23:16:07 but a persistent manual ON admitted at least 12 further live 402s
  across translation, Agent, and summary work over the next 4m55s. This change
  prevents that repeated spend for covered optional work; it does not claim
  post-deployment savings and deliberately preserves interactive user
  supremacy, transient-429 rotation, daily reset, and explicit re-enable.
- Versioned the project FileHistory JSONL representation without changing its
  public full-snapshot API or rewriting live data at startup. New rows use
  exact-base deltas with a full anchor at least every 64 records; a validated,
  disposable tail index makes latest-ID and adjacent-diff reads independent of
  history size, while one process-wide 100,000-pair LRU reuses pinned-version
  scans across files. Appends remain fsynced and now take an advisory process
  lock; torn rows cannot poison later appends, broken delta chains recover only
  at full anchors, uncertain GC deletes nothing, and compaction refuses corrupt
  logs. Declared round paths are normalized/de-duplicated and undo/redo metadata
  lookup no longer builds 2,000 history summaries. On a read-only copy of the
  2,162-row legacy-log copy, retained rows projected from 220,660,523 to
  20,689,094 bytes (90.62%). The running development process then reached its
  normal maintenance boundary without a manual migration or restart: the log
  recorded 2,163 → 2,000 rows, 246,371,142 → 20,683,387 logical bytes (91.60%),
  and 743 unreferenced blobs removed. Median full replay fell from 1.748863 to
  0.102553 seconds (94.14%); latest-ID and adjacent-diff medians were 0.000578
  and 0.000574 seconds on the resulting v2 log.
- Moved frozen historical inline Turn images off the generated browser's
  `refs` snapshot hot path without rewriting durable user data. Eligible
  completed images now become authenticated, owner-cache-partitioned,
  projection-revision-fenced URLs; the Sidecar returns only one requested
  encoded image, and the application boundary enforces strict base64,
  magic-byte MIME, 8 MiB/image and 20 images/Turn ceilings. Full snapshots,
  persistent projections, replay/model reconstruction, and modern attachment
  refs remain unchanged. The contract-generated binary response uses private
  immutable caching, ETag and `nosniff`; wrong owners/missing images are 404
  and stale revisions are 409. In a read-only active-authority counterfactual,
  four duplicated historical images dominated one 26-Turn sample: the current
  `refs` JSON fell from 11,908,769 to 4,882,869 bytes (59.00%), and Brotli-q2
  from 3,539,836 to 855,374 bytes (75.84%). This is a pre-deployment wire/parse
  opportunity, not claimed measured traffic savings or historical disk
  reclamation. The shared magic-byte detector is now a dependency-free module,
  so the compatibility decoder no longer eagerly loads the LLM body,
  sanitization, and model-catalog stack merely to recognize an image.
- Closed the tool-result compression/loop failure exposed by conversations
  `mtc6xp7kka0hls` and `mtcyxfbwqx03h0`. V2 now budgets the final escaped
  envelope without a fixed tiny fallback, keeps conversation page evidence
  ahead of large settings, exposes artifact continuation on every V2 wire,
  and treats explicit file ranges as authoritative. The semantic loop guard
  now spans registered idempotent range/cursor/limit tools, counts only
  repeated model-visible projections as no progress, issues one executable
  recovery correction, and then stops continued waste.
- Stopped `turn.event.record` from assigning an unchanged canonical
  `projection_json` BLOB/JSONB value, including both full-fold structural
  frames and slim text-cadence frames. The same transaction still performs
  the projection-revision CAS, status/settlement changes, attempt-event patch,
  carried task event, conversation revision, sync change, and terminal search
  projection; only the redundant large-column assignment is omitted. Per-type
  Sidecar metrics now expose skipped assignments and canonical bytes. A
  read-only audit resolved through the live fastpath lease found 6,887 empty
  patches among 13,878 attempt frames since process boot (49.63%), carrying
  3,923,713,036 bytes of `projectionBytes` evidence; 6,111 unambiguously
  non-slim frames accounted for 3,893,916,098 bytes. These are a conservative
  pre-deployment write-amplification opportunity, not a claim of measured WAL
  savings or historical disk reclamation.
- Versioned the private Request Inspector snapshot delta so interleaved
  `request` and post-tool `state` frames share one chronological `(task, turn)`
  message baseline. The live SSE event and server-rebuilt payload remain full
  and byte-identical; constant tool schemas keep their existing content-hash
  dictionary. Unversioned v1 rows retain their frozen kind-scoped meaning,
  exact v1 rows also seed a safe v2 migration baseline, restart boundaries
  begin with a self-contained zero-prefix v2 row, and unknown versions degrade
  explicitly. A read-only replay resolved through the active fastpath locator
  rebuilt all 1,564 current v1 frames without degradation; applying the real
  v2 projector and task-event codec reduced their stored projection from
  32,728,847 to 22,281,348 bytes (31.92%) and reduced resident baseline chains
  from 56 to 32. This suppresses future durable write/disk growth and removes
  the separate duplicate kind baseline; it does not claim to reclaim 4.263 GB
  of historical snapshots or delete any separately inventoried recovery artifact.
- Added one bounded post-dispatch correction for long serial single-tool
  episodes without changing the resident prompt, tool schemas, authority, or
  explicit thinking depth. After six consecutive successful model rounds each
  issue exactly one approved inspection/command-family tool, the harness may
  append one fixed `_isMeta` reminder to group independent direct calls, use
  existing batch arrays, or combine bounded read-only shell verification.
  Dependencies, writes/state changes, approvals, polling, interactive/MCP
  tools, failed receipts, parallel rounds, safety corrections, and genuine
  user steering remain direct boundaries. The correction shares a one-per-task
  budget with the more specific local `execute_tools` adoption hint and
  persists only a bounded content-free witness. In 12 highest-round production
  conversations, 61 qualifying chains covered 615 of 1,890 tool-bearing model
  rounds; 249 rounds occurred beyond the sixth-round threshold across 29
  assistant turns. That is a counterfactual opportunity ceiling, not claimed
  saved calls. A separate 2,437-round cache audit found no large cache-write /
  zero-read misses, focusing this change on model round-trip count rather than
  weakening the healthy provider-cache path.
- Removed duplicate project reconciliation from conversation open and made the
  remaining boundary idempotent end to end. A cold conversation now waits for
  authoritative Turn settings and restores its project once (the executable
  retained-runtime contract was 2 calls before and is 1 after); warm opens
  still restore immediately. Identical browser `setPaths` calls share one of at
  most 16 in-flight keys and receive independent response clones, while path or
  read-only differences never coalesce. After directory validation, an exact
  server primary/root/access match returns current state without rotating the
  undo session, clearing counters, rechecking cross-DC, or requesting another
  tree-index warm; any missing, extra, reordered-primary, or permission drift
  retains the existing prune/register behavior. The frozen log held 184 project
  path writes versus 55 config loads and 187 recent-path writes; 64 writes kept
  the same primary and named-root set, an explicit opportunity upper bound
  because historical logs did not record access flags. Normal write-driven
  index refresh and explicit project changes remain unchanged.
- Made the Memory summary path body-free and warm-cacheable without caching
  authority. `summary=1` now stops at the closing frontmatter delimiter, while
  detail, search, injection, and mutation retain full-body reads. A dual-bound,
  launch-probed LRU caches only parsed frontmatter behind exact file
  fingerprints; every hit reconstructs provenance, eligibility, and owner
  scope. On the current 1,356-memory corpus this removed 3.10 MiB of retained
  body data and reduced measured peak Python allocations by 63.1%; after one
  cold fill, median summary enumeration fell from 1.216 seconds to 0.064 seconds
  (94.7%). The Memory panel still receives the complete searchable metadata set
  but replaces a fixed 100-card page instead of synchronously mounting all
  1,356 cards, a 92.6% live-card reduction; controls and regression tests cover
  page, filter, edit, and deletion boundaries. The remaining metadata response
  is deliberately visible as the next server-pagination opportunity.
- Stopped reopening an already-hydrated sidebar conversation from downloading
  its bounded Turn snapshot again. The retained `loadConversation` bridge now
  delegates warm state to the typed cursor/SSE `wakeConversation` boundary;
  that owner still performs the sole cold-store fallback, and explicit
  cursor-expiry, sequence-gap, projection-gap, and reset recovery remain full
  authoritative snapshots. A frozen trace of one large conversation contained
  at least 66 generated-client `tail-96` snapshot responses totaling 144.4 MiB
  and 10.714 seconds of server work (2.188 MiB mean; logger-coalesced hidden
  requests excluded). Because the trace also contains unknown cold opens, those
  totals are an opportunity envelope; a retained-runtime behavior test pins the
  exact per-selection contract as warm `hydrate=0, wake=1` versus cold
  `hydrate=1, wake=0`.
- Replaced the cost-experiment report's restart-from-zero legacy BLOB query
  with an owner-scoped resumable repository scan. Each semantic storage RPC
  advances at most 256 task-result records, internally materializes eight
  BLOBs at a time, returns only compact exact-match outcomes plus a monotonic
  cursor, and the report has a truthful 10,000-source-row ceiling; current
  conversation ownership, exact experiment/window checks, the 5,000-result
  cap, and fail-closed truncated decisions are unchanged. The frozen 82 GiB
  authority held 1,906 task results / 177,187,806 payload bytes, all on the
  legacy path. Its former JSON/JOIN/LIKE statement took 2.249 seconds and
  produced five 4.94 MiB false candidates whose experiment string appeared
  only in task prose, not an outcome; the new full repository walk took
  0.166575 seconds across eight resumable pages (13.5x faster), returned zero
  true outcomes, and its largest measured eight-row materialization was
  3,805,845 bytes. The production log contained five report HTTP 500s at
  15.149–15.407 seconds; a timeout could previously restart the same cold scan
  on each of three read attempts, whereas a retry now repeats only one bounded
  page.
- Removed guaranteed-failing Turn-authority work from sanctioned post-settlement
  observers. When an already-terminal task emits `round_committed` or
  `preference_learned`, the manager now writes its storage-projected task event
  directly to standalone cold replay before live push; the producers' existing
  settled-Turn CAS paths remain the sole projection writers. Every other late
  delta/tool/phase/lifecycle frame still enters `turn.event.record`, fails
  closed, and plants the cooperative zombie-abort fence. In the frozen log,
  commit-round threads caused 25 and profile consolidation caused one of 62
  `attempt-not-live` warnings (41.9% combined): each paid an attempt lookup
  guaranteed to reject, falsely marked completed work aborted, and withheld the
  promised live observer push before falling back to standalone replay.
- Completed post-compaction same-role classification without weakening the
  provider-safety merge or hiding genuine duplicate turns. The previous
  original-edge classifier covered `objective anchor → retained-user wrapper
  → current user`, but automatic Layer 2 emits no wrapper when the summarized
  old region has zero eligible user rows, leaving the relocated real anchor
  directly beside the current user. L2 now re-inserts a shallow anchor copy
  with private structural identity; the two final wire builders reduce known
  producer identity to one short-lived boolean, and the merge removes it on
  merged, non-merged, and singleton paths. The default API-field projection is
  unchanged, unmarked real-user adjacency still warns, authoritative messages
  are not mutated, and model-visible roles/content remain byte-identical. The
  frozen log exposed 689 physical warnings plus 2,521 coalesced occurrences of
  this index-3/4 shape. A 940-row debug-wire inspection contained each real
  user request exactly once, so this is a bounded local log-I/O and alert-signal
  improvement, not a claimed API-call or token saving.
- Made resident local Programmatic Tool Calling actionable without changing its
  cache-stable gateway schema or forcing model behavior. After three productive
  model rounds each issue one reviewed read while the latest orchestration
  decision proves local `execute_tools` reached the wire, the post-dispatch
  guard may add one fixed `_isMeta` adoption hint, at most once per task. Native
  and disabled lanes cannot receive it; writes, approvals, and semantic judgment
  stay direct; a same-round loop-safety correction wins; and genuine user
  steering remains a hard boundary. Context Composer, engine-authored
  stall/stream/loop corrections, and pure peer/swarm inbox evidence no longer
  replace the real current-user intent or split the structural read chain.
  Terminal results retain at most one bounded, content-free
  `programmaticAdoptionNudges` witness, while only canonical `programRuns` prove
  adoption; damaged restored counters/evidence fail safe. One frozen 93-call
  coding task used 107 tools, 7.149 million prompt tokens (6.945 million cache
  reads), ¥23.05, and 2,286.3 seconds; its retained decision tail projected the
  local gateway 45 times without a program run. Five eligible serial-read chains
  occupied 24 model rounds. Perfectly collapsing each whole chain gives a
  19-call theoretical ceiling, while counting only calls after each third-read
  detection gives a nine-call counterfactual opportunity ceiling. Neither is
  claimed as realized savings; live post-hint adoption and quality evidence are
  still required.
- Removed repeat API work and multi-model capacity waits from the synchronous
  language-correction micro-classifier used before send-path translation. A
  process-lifetime 512-entry LRU now keys the exact bounded classifier prompt
  with a random process secret and retains only the 16-byte digest plus a valid
  language code—never user text, failures, or `unknown`. Repeated text therefore
  reuses its deterministic temperature-zero result, while one upstream 429 now
  yields immediately to the actual translation/main task. The frozen log cut
  contained 16 successful corrections (all resolved to Chinese) and 11 rejected
  wire attempts across eight calls; three of those rejections were redundant
  second probes and are removed by the new attempt ceiling.
- Made the optional web relevance gate cheaper, faster, and semantically
  reliable without disabling deep-research filtering. Gate-mode provider calls
  now cap output at 32 tokens, stop after one upstream 429, skip extracted pages
  below 6,000 characters, and select at most 6,000 query-focused characters
  from larger pages. The exact `§§IRRELEVANT§§` verdict is no longer also sent
  as a provider stop sequence, which could erase the verdict and make the
  fail-open caller retain an unrelated page as an empty-response anomaly. In a
  frozen seven-search cut, 108 page reviews sent 736,112 characters and added
  41.1 seconds across search pipelines; replaying the new local gates reduces
  that envelope to at most 80 calls / 417,573 characters (25.9% fewer calls,
  43.27% fewer input characters). Ten of 17 completed empty results occurred
  without any 429, matching the erased-sentinel failure signature. Settings and
  tool help now accurately describe gate mode as binary relevance selection,
  not page rewriting.
- Stopped optional My Context consolidation from replaying old long prompts or
  rotating through cheap-model capacity while foreground work is rate-limited.
  The latest real user message now solely decides whether a paid review is
  warranted, while up to four recent user messages remain available as grounded
  model context after that gate; a short acknowledgement can therefore no
  longer inherit eligibility from an earlier coding request. The advisory pass
  also stops after its first upstream 429. In the frozen log cut, 25
  consolidation threads produced 40 rejected wire attempts but only three
  logged profile updates, so the attempt ceiling alone would have removed at
  least 15 repeated rejections (37.5%) without changing foreground or swarm
  dispatch policy.
- Made incremental auto-translation yield API capacity to foreground coding,
  writing, and research turns. Reconstructible narration previews below 256
  characters now spend zero provider calls, and every other preview stops the
  Turn's remaining preview work after its first upstream 429; terminal
  reasoning and the authoritative final translation retain the ordinary retry
  budget and durable commit path. Both gates are launch-profiled, bounded, and
  operator-overridable. One frozen log cut contained 962 incremental translation
  calls, 832 rejected translation wire attempts, and 499 translations that
  eventually settled only after one or more 429s; among 917 non-last calls, 298
  (32.5%) were below the new character floor (an upper bound because legacy
  traces do not distinguish narration from reasoning). This removes cheap-tier
  request overhead and prevents optional previews from amplifying a shared App
  model-RPM storm into main-agent latency.
- Closed the pre-first-call prompt-cache accounting gap around L2 and other
  prefix compaction. When a new task thread compacts inherited history before
  its first provider response, a warm sibling or durable baseline now creates
  a lifecycle-bounded local guard, suppressing the expected post-summary read
  drop and preserving the L2 saved/re-billed ROI pair. This fixes the observed
  false `turn_boundary_rebill` signature (`204800 -> 0`, `gap=0.0s`) that
  coincided with a real `528717 -> 9722` token compaction. Cold conversations
  still allocate no state, existing-thread notification remains O(1), and
  steady provider rounds no longer scan sibling states or consult the durable
  round-1 baseline after their first completed call. In an isolated 4,096-state
  / 5,000-lookup benchmark, that steady-round gate fell from 0.583393 seconds
  for the former sibling scan to 0.001323 seconds (99.77% less, about 441x);
  this measures the local accounting seam, not end-to-end provider latency.
- Extended the lossless private projection codec to frozen pre-Turn
  conversation archives during explicit physical offline deep-clean. The pass
  selects at most 64 rows / 64 MiB of source payload, never fetches an
  individual document above 64 MiB, verifies an exact canonical
  decode/re-encode/decode round-trip, writes only a strictly smaller result,
  checkpoints each write page, and is write-free on a repeat run. On the
  largest observed archive, stored bytes fell from 62,997,304 to 39,433,860
  (37.40%); parse plus hydration fell from 0.258 to 0.174 seconds and measured
  peak allocation from 893.45 to 521.55 MiB (41.63%). Across the 25 largest
  archives, 13 were eligible and saved 50,701,494 of 529,085,607 bytes (9.58%).
  Transforming the largest document itself measured 0.484 seconds / 833.36 MiB
  peak under the stopped-server, 64 MiB-document budget. Plain archives remain
  readable, malformed or future markers fail closed, and public transcript
  semantics do not change.
- Added an explicit offline path to reclaim frozen pre-Sidecar conversation
  mirrors without treating redundant data as disposable user state. The
  read-only deep-clean report inventories `conversations`,
  `conversation_messages`, and `conversation_turns` and emits the exact opt-in
  command. Retirement proceeds per owner/conversation only when the legacy
  array, the normalized rows reconstructed from lossless `meta` plus their
  translation overlay, and the current Sidecar archive hash to identical
  canonical JSON; missing, malformed, unequal, legacy-Turn-linked, ambiguous
  cross-owner, or over-budget records remain untouched. Selection is capped at
  64, each JSON document at 64 MiB, and each separately checkpointed delete
  transaction at 128 MiB of measured payload. The observed authority carries
  exactly 12,965,955,996 logical payload bytes (about 12.08 GiB) in this frozen
  mirror family in addition to its current archive. Read-only probes matched
  all three witnesses for 100/100 bounded samples and again for the 25 largest
  legacy arrays (535,225,565 bytes total; 62,997,304-byte maximum). Physical
  retirement remains opt-in, requires the stopped-server lease and `--confirm`,
  preserves current authority rows, and still passes integrity/parity before
  publication.
- Accelerated and memory-bounded legacy task-event metadata recovery without
  widening the ordinary 25-row typed-deletion transaction. The compatibility
  path now classifies up to 100 blank rows per commit, while a metadata-first
  preflight independently caps stored-page materialization and decoded payloads
  at 4 MiB. Oversized stored rows are never fetched into the Sidecar; compressed
  rows declaring a larger expansion are never decompressed. Their durable
  payload remains intact under the conservative structural horizon and an
  opaque progress marker prevents head-of-line starvation. A read-only authority
  sample contained 3,365,519 blank rows occupying 17,471,389,938 stored bytes
  (5,191 B average, 15,044,529 B maximum; 36 rows exceeded 4 MiB). At the
  unchanged 16-commit / 30-second backlog cadence, the common small-row drain
  ceiling rises from 13.33 to 53.33 rows/s, reducing the theoretical recovery
  window from about 70.1 to 17.5 hours (4x) without increasing commit count.
- Isolated Autopilot virtual-user carriers from their parent Turn authority.
  Carrier creation now strips inherited message/turn/attempt identity, and the
  shared conversation-attempt predicate fails closed for inline/VU carriers even
  when a shallow-copied config leaves stale ids behind. Wrapped carrier frames
  still persist on the parent stream and the carrier keeps its independent
  task-event replay; only the second, conflicting projection writer is removed.
  One read-only production trace exposed 1,561 parent frames plus 1,553 carrier
  frames oscillating between 39/9 tool rounds and 73/17 segments: 1.081 GB of
  attempt-event payload accumulated in about 24.2 minutes. Replaying that chain
  and diffing only consecutive parent states reduced the comparable 1,560 parent
  frames from 983,837,782 B to 1,015,627 B (99.90%); the additional 97,147,393 B
  of carrier attempt frames disappear while their task-event log remains.
- Bounded every `TaskRuntime` process-memory replay window by both event count
  and compact serialized bytes, and reduced the launch-derived terminal-record
  target per task kind. Probe-failure / 8 GiB reference / distributed profiles
  now use 64/128/512 records, 1,024/2,048/4,096 events, 2/4/8 MiB ordinary
  tails, and 4/8/16 MiB complete-event ceilings; overrides and explicit
  constructors cannot exceed hard caps. Byte pressure keeps one contiguous
  newest suffix; a valid larger event occupies the window alone, while an
  individually oversized or unencodable event advances the cursor and resets
  only reconstructible memory replay. Runtime/Prometheus statistics expose
  actual bytes and all ceilings. A read-only 1,324,355-row event sample averaged
  3.4 KiB and peaked at 5.36 MiB, motivating the separate single-event budget.
  Production deduplication now inherits the resolved task-record target and
  cannot widen it.
  In an isolated 1,000-event benchmark at about 64 KiB/event, retained replay
  fell from the old item-only 62.586 MiB envelope to 63 events / 3.943 MiB
  (93.70% less); serialization plus append took 17.17 ms and preserved the
  absolute 937→1,000 cursor window.
- Bounded unified Push-WebSocket residency by process connections, owner
  connections, event items, serialized bytes, and single-frame bytes. The 8
  GiB reference profile now admits 64 local sockets / 12 per owner; each
  retains at most 1,000 event references / 4 MiB with a 2 MiB frame ceiling.
  Distributed mode uses 256 / 64 / 1,000 / 16 MiB / 8 MiB, and every override
  remains hard-capped. Fan-out serializes a local frame once for all targets;
  byte saturation preserves the existing drop-oldest/reconnect contract,
  while an unencodable or individually oversized frame disconnects for durable
  reconciliation. In an isolated 1,000-frame benchmark at about 64 KiB/frame,
  retained backlog fell from the old item-only 62.523 MiB envelope to 63 frames
  / 3.939 MiB (93.70% less); serialization plus admission took 11.03 ms.
- Made lint discovery independent of Git-ignore behavior for the legacy
  in-tree undo/redo store. Ruff now prunes the exact authoritative
  `lib/.project_sessions` path before traversal, while same-named directories
  in user projects remain eligible. The observed installation contains 15,904
  mutable session directories; a `--no-respect-gitignore --show-files` check
  still enumerated 1,428 `lib/` source files and zero session artifacts.
- Unified Local Knowledge PDF validation, text extraction, scanned-page OCR,
  visual/source persistence, and repository commit under the same classic-
  parser lease; the public visual
  helper is admitted too, so it cannot bypass pooled/direct work. At the 8 GiB
  reference capacity of three documents, the 50 MiB input gate and unchanged
  160 MiB visual-candidate limit bound aggregate compressed sources to 150 MiB
  and accepted visuals to 480 MiB instead of multiplying with request count.
  OCR/visual traversal defaults remain 80 pages and now cannot exceed the
  launch-derived classic page policy; the existing 160 assets/160 MiB per-
  document quality envelope remains. OCR stops immediately when the bounded
  knowledge text output is full instead of spending CPU on discarded pages,
  and batch/upload/reindex boundaries report capacity failures as retryable.
  Launch-budget lookup now uses a bounded numeric-environment fingerprint:
  after warm-up, an isolated 10,000-call benchmark reduced the complete
  knowledge visual policy from about 18,064 us to 11.27 us (99.94%), while the
  cached classic lookup itself took 4.43 us; changing any relevant environment
  value naturally selects a new cache key.
- Bounded classic local PDF parsing behind one launch-derived admission policy
  shared by direct calls and the process pool. The 8 GiB reference now uses one
  child, three aggregate unfinished PDFs, 512 pages, 4 MiB of returned text,
  and a 1,024-second wait ceiling; distributed mode uses 4/16/2,048/16 MiB and
  the hard ceilings are 16/64/4,096/64 MiB/3,600 seconds. A request value of
  zero now selects that finite policy instead of an approximately one-billion-
  character ceiling (99.58% lower at the reference profile), and the reference
  child count is 75% below the former four-process default. Page traversal,
  structured-output cleanup, images (64 at 2,048 px), and returned metadata all
  enforce the same bounded-prefix contract. Executor saturation fails before
  retaining another compressed PDF; a timed-out running child keeps admission
  until true settlement and is never duplicated by an in-process retry, while
  deterministic child failures likewise propagate once. Public parse overload
  and timeout responses are retryable 503/504 results. Personal/distributed
  child pools now release parser/model RSS after 60/600 idle seconds (explicit
  `0` retains them), guarded by an activity generation so new work cannot race
  stale retirement. The arXiv SSE bridge is
  now a non-blocking one-item latest-value slot: an isolated 10,000-update
  benchmark retained one item / 4.84 KiB instead of 10,000 / 1,092.59 KiB
  (99.56% less), including when the consumer disconnects.
- Replaced the durable task-event batcher's fixed 10,000 arbitrary-sized
  waiting objects with one launch-derived item-and-byte policy. The 8 GiB
  reference profile now admits 512 waiting objects / 64 MiB; distributed mode
  admits 4,096 / 512 MiB, and one RPC remains below 500 events / 60 MiB. In an
  isolated CPython benchmark, filling the old and reference queues with unique
  4 KiB event objects used 55.88 MiB and 2.86 MiB respectively (94.9% less);
  the required 4 KiB serialization preflight took a 3.52 us median. Oversized,
  unserializable, item-saturated, and byte-saturated events now fail before a
  Sidecar call. Confirmation timeouts remove unclaimed work and release its
  payload before the natural-key direct fallback, avoiding a redundant later
  batch, while claimed ambiguous commits remain dedup-safe. Repeated bounded
  shutdown calls can continue waiting for the same ordered drain.
- Removed synchronous subscription-file parsing from every outbound-webhook
  fan-out event. A one-second cross-replica/write-through projection now makes
  local creates/deletes immediate while every actual request still rechecks
  uncached revocation authority. In a 64-subscription isolated benchmark,
  1,000 event-side lookups fell from 83.2 ms to 0.267 ms (about 311x). Creation
  now checks owner/process capacity inside one cross-process atomic update,
  preventing concurrent lost writes. Personal mode retains at most 64
  subscriptions, 128 immediate + 64 retry items and 16 MiB between them;
  distributed defaults remain finite at 2,048/2,048+1,024/256 MiB. Event JSON
  is encoded once, capped before enqueue (512 KiB personal, 1 MiB distributed),
  and both queues enforce item plus byte ceilings. Five real attempts and the
  signed receiver envelope are unchanged. A finite subscription-keyed failure
  gate turns a transient outage into one recovery probe per cooldown instead
  of one first attempt per queued event; deferral spends no attempt. Overload
  and delivery logs use secret-safe URLs and power-of-two checkpoints.
- Replaced the storage writer/event batcher's fixed 100,000/200,000 latency
  rings with one recent-sample policy derived from the launch-probed SQLite
  writer queue budget (2,048 fallback, 4,096 on the 8 GiB reference profile,
  32,768 distributed/hard ceiling). Metric snapshots now copy under a short
  lock and sort outside the commit path. In an isolated CPython benchmark the
  reference windows cut retained sample memory from about 9.2 MiB to 0.25 MiB
  (97.3%) and combined median sort time from about 7.2 ms to 0.12 ms (98.3%).
- Made background and fallback failure boundaries observable without changing
  their public error semantics. Abortable response-header posts and recoverable
  agent workers now hand results across threads through `Future` outcomes;
  scheduler bookkeeping still settles before callers observe completion, and
  abandoned responses still close opportunistically without replacing a user
  abort. Parallel motion/podcast synthesis, final stream-delta flushes, direct
  download fallback, search import/linkage recovery, artifact-envelope parsing,
  and restart preparation now either emit bounded secret-safe diagnostics or
  propagate unrelated programming errors instead of silently swallowing them.
- Bounded provider access-matrix diagnostics behind a lazy provider-fair process
  lane derived from the launch-time read-only tool budget. Forced refresh can
  no longer overlap a live provider generation; personal/distributed profiles
  cap aggregate cell calls at 1..4/8, pending provider jobs are finite, and
  saturation fails retryably before any upstream spend. Durable terminal
  snapshots now release private headers and work closures from memory, while
  disk-only `running` state after a restart becomes an actionable error instead
  of an endless poll. Oversized key/model/wire-id products stop at the 401st
  sentinel instead of materialising the entire rejected Cartesian product.
- Closed the streaming read-only tool prefetch lifecycle on every model-round
  exit. Provider break, abort, and exception now cancel queued futures and
  shut down the per-round pool instead of leaving submitted read/search
  closures to drain without an owner. Prefetch workers reuse the existing
  launch-probed `TOOL_MAX_PARALLEL_WORKERS` value with the historical hard
  ceiling of four; retained speculative calls fall from 32 to eight per round,
  while excess model occurrences remain visible and execute through ordinary
  dispatch. Successful prefetch still waits for useful in-flight work and
  injects the same validated cache receipts before its idempotent final close.
- Bounded the default in-process rate limiter by both identity cardinality and
  exact sliding-window events. Dynamic object paths now share their route
  template bucket; long endpoint/client strings retain only SHA-256 identities;
  personal mode keeps 512..4,096 LRU buckets (1,024 on the 8 GiB reference) and
  derives a 128-events-per-bucket aggregate ceiling capped at 1,048,576. Stale
  cleanup now uses each bucket's own window, while `deque` expiry removes the
  former O(limit) list rebuild on every hot request. Authenticated API-key
  token pairs reuse the same entry ceiling instead of surviving deletion until
  restart. Capacity evicts old process-local enforcement state to preserve the
  documented fail-open posture; distributed/multi-worker authority remains the
  Sidecar backend. In a local 50,000-identity benchmark the 8 GiB profile cut
  retained memory from 21.41 to 1.07 MiB (95.0%); 1,000 checks against a
  10,000-event hot bucket fell from 3.393 s to 0.00123 s (about 2,760x).
  Sidecar rows now carry their exact window expiry and every check prunes at
  most 256 globally expired events through an age-leading index, so identities
  seen only once no longer grow SQLite/PostgreSQL forever. Schema v43 resets
  only legacy rate-limit rows whose old format had no safe expiry.
- Made post-update task capacity follow the code that computed it: the
  long-lived Supervisor now reports a content-addressed source generation,
  reloads itself without interrupting its worker, and owns the subsequent
  deferred worker replacement. Resource defaults carry policy/provenance so
  future in-place generations replace only system-generated values, never
  explicit overrides. `doctor` reports manager/worker budget drift, and queued
  conversations now show their real FIFO position, active/capacity slots, and
  elapsed host wait while provider 429 backoff remains a distinct phase.
- Bounded the sole SQLite writer's waiting operations with one launch-probed
  8..64-job personal budget (16 on the 8 GiB reference host; distributed 128,
  hard ceiling 1,024). Saturation now returns retryable `database_busy` before
  retaining another decoded request/operation closure, and acquisition timeout
  removes a still-queued job immediately instead of keeping its potentially
  large payload until the writer eventually scans it. Metrics expose capacity,
  rejections, and early cancellation while weighted lanes, group commit,
  deadlines, rollback, and the already-drained cancellation fence stay intact.
- Closed the remaining unbounded translation-provider wait path by reusing
  `TOFU_TRANSLATE_QUEUE_CAPACITY` across background scheduling and the shared
  MT/LLM gate. Personal mode now retains at most 4..32 provider waiters
  (distributed 128, hard ceiling 1,024) in addition to its 1..2 active calls.
  Saturation raises one typed retryable `server_busy` result, performs no model
  rotation/backoff, and no longer turns a batch failure into N isolated queue
  attempts; FIFO ordering, cooperative cancellation, cache/identity fast paths,
  and the finite upstream-429 policy remain unchanged.
- Turned Tool Search's former 16,384-entry raw-string LRU into a launch-probed
  short-text working set (personal 512..4,096; 1,024 on the 8 GiB reference
  host). Queries are now bounded to 512 characters and namespace/cursor fields
  to 128 before catalog work or echo; catalog descriptions/private hints above
  1,024 characters retain full-fidelity ranking but bypass the cache. This
  prevents a plugin or long task from converting an item-count limit into
  arbitrary resident source bytes while preserving short hot-set CPU savings;
  a local 20,000-call/600-character microbenchmark measured 16.9 ms cached
  versus 2,318.2 ms uncached (137x), while the 8 GiB profile caps retained LRU
  key text at 1,048,576 characters.
- Split auto-research generation from durable publication. Survey, ideation,
  and evaluation now checkpoint before one bounded terminal publish stage, so
  a storage outage retries only unconfirmed idempotent rows and cannot report
  clean success; resuming the same workdir spends no additional model calls.
  Successful runs write the two canonical rows once after evaluation instead
  of performing three incremental upserts (33.3% fewer Sidecar commands). The
  two independent primary judges now overlap behind a hard two-call ceiling;
  the conditional tiebreaker and total API-call count are unchanged, while the
  normal evaluation critical path becomes the slower call instead of their sum.
- Made long-form report starts atomically join identical live work and bound
  brief/standard/deep outlines to exactly 3/5/8 unique headings. Excess model
  headings can no longer trigger up to seven unbudgeted section calls; thin or
  duplicate outlines retry/fail before prose generation. Independent sections
  now overlap behind the launch-probed 1..2 personal per-job fan-out
  (distributed 4, hard ceiling 8) without adding logical API calls, and each
  success checkpoints independently. Six-hour research freshness plus exact
  prompt-input and Markdown-input digests prevent stale prose or source labels
  from surviving a resume while preserving semantically unchanged sections.
  Conversation-scoped reports require a confirmed artifact ID before
  publishing `done`.
- Bounded post-report terminology repair to 60 gaps and raised its measured
  parse-safe batch from 10 to 15 terms. A 30-gap report now sends two shared
  60,000-character prefixes instead of three (33.3% fewer model calls/input),
  while larger runs warm one cache-friendly report prefix before overlapping
  remaining batches behind the production fan-out. Research judges, long-form
  prose, and terminology repair now share a launch-probed finite upstream-429
  budget and two hard-error slot attempts instead of inheriting unbounded 429
  cycling from interactive dispatch.
- Bounded paper-podcast synthesis before adding speed: short/full scripts now
  retain at most 24/64 segments, TTS input chunks clamp to 200..4,096
  characters, a job admits at most 160 chunks, and ordered assembly rejects
  parts above 32 MiB or aggregate input above 192 MiB. Independent segments
  overlap behind a launch-probed 1..2 personal TTS fan-out (distributed 4,
  hard ceiling 8); failure/abort stops new admission, in-flight work settles,
  output order and pauses remain deterministic, and cancellation no longer
  spends the chunk retry. Script draft, repair, revision, and critic model calls
  now propagate cancellation and share the finite production 429/two-slot
  retry budget, so aborting during the former 1–3 minute script phase cannot
  become a late generic error or an unbounded provider wait.
- Reused the same bounded TTS budget for motion-video scene narration: up to
  16 scenes / 64 chunks now overlap behind the 1..2 personal fan-out while
  preserving scene order and stopping admission after failure. Scene/job WAVs
  are capped at 32/192 MiB, cancellation skips retry, mixed provider WAV
  parameters fail closed, and failed batches remove reconstructible partials.
  Versioned manifests bind ordered text, source/target timing, voice, speed,
  alignment, tail padding, byte sizes, and SHA-256 content before crash-resume
  reuse; direct SRT jobs persist that checkpoint immediately, eliminating both
  stale-audio reuse and paid re-synthesis after a later-stage interruption.
  Topic scripts, report-to-beat rewrites, and multi-round scene authors now
  also use the finite production 429 budget and two hard-error slot attempts;
  task cancellation reaches in-flight script/author requests and transient
  backoff, late script replies are discarded, and the engine stops immediately
  after an interrupted author instead of continuing local gates and rendering.
- Made slide production restart-cheap and resource-bounded. Independent page
  authors now overlap behind the launch-probed 1..2 personal LLM fan-out while
  preserving deck order and stopping admission on abort; every successful page
  atomically records the exact prompt/model/round policy plus bounded YAML
  size/SHA-256 and zero-LLM validation, so unchanged pages make zero model calls
  after an author-stage interruption and one changed/corrupt page reruns alone.
  Fallback pages deliberately do not cache. Outline, author, layout-QA, and
  visual-QA calls propagate cancellation and use two hard-error attempts plus
  finite production 429 budgets. Image preflight now has a separate 1..2
  personal fan-out (hard ceiling 4), two hard attempts, and finite 429 budget;
  generated/caller/remote images are bounded before decode/read, copied or
  downloaded incrementally to atomic files, and exact byte/hash manifests avoid
  paid regeneration or download after a crash. URL-addressed remote filenames
  remove restart overwrite collisions; stale reconstructible media is reclaimed.
  Per-page VLM reviews now overlap under the same LLM fan-out and cache only
  validated findings keyed by exact prompt/model/token/pixel digests: an exact
  rerun makes zero VLM calls, while changing one page rechecks only that page
  and the deck contact sheet. Rendering rejects oversized geometry before
  Chromium, caps page/batch PNG output at 32/192 MiB, publishes atomically,
  checks cancellation between pages, and replaces the fixed 400 ms-per-page
  sleep with font/image readiness plus two animation frames (removing a 4.8 s
  mandatory wait floor from the default 12-page deck). Stage cancellation now
  settles the slides task as `aborted` rather than the generic error branch.
- Reused the validated shared translation engine for whole-paper artifacts and
  raised semantic slice packing from 2,400 to 8,000 characters. A read-only
  sample of 40 non-empty local papers projects 1,175 -> 426 sequential calls
  (63.7% fewer; median 25 -> 9). Automatic requests can reuse per-slice cache
  and configured MT, while `force` and strictly pinned models get fresh reads.
  Source work is capped at 1,000,000 characters, 128 slices, and two hours;
  empty/refused/truncated/over-generated slices and unconfirmed persistence now
  fail the task instead of publishing placeholder or partial success. Canonical
  Paper Reports now apply the same artifact invariant: an empty terminal body,
  storage exception, or unconfirmed write cannot publish `done`; explicitly
  cache-isolated experiment arms remain non-persistent by contract. Podcast
  `script_only` and audio-complete outcomes likewise require a confirmed cache
  row instead of treating `saved=false` as durable success.
- Bounded initial browser hydration for linear Turn-native conversations. The
  generated v3 client now requests a 96-Turn tail with exact `syncSeq`-CAS
  history paging; identical page requests coalesce, stale pages fail closed,
  and the keyed Surface prefetches 64-Turn pages while revealing 20-Turn
  batches through the same reducer.
  Branch-bearing conversations retain the full snapshot until bounded lane
  discovery can preserve branch reachability. On the largest local 1,624-Turn
  sample this cut refs wire bytes 95.28%, Brotli-q2 bytes 95.82%, service median
  90.36%, and browser parse/validation/materialization median 93.02%.
- Hardened tool-call identity across the harness. Provider IDs are now treated
  as batch-local correlation tokens: Continue grouping is attempt-aware,
  every ordered in-response occurrence executes independently across
  root/swarm/sequential runners, recycled IDs are repaired before source
  history, and only proven retransmission at one stable stream slot is
  suppressed. SSE ambiguity fails closed,
  compaction/evidence/Responses replay pair adjacent occurrences, and native
  program parents use unique backend IDs. Custom client handoffs are task-keyed;
  approval, guidance, and stdin waiters cannot be overwritten and settle on the
  first response/timeout/cancel decision.
- Made the local Agent scheduler and conversation state machine describe the
  same fact. A bound task remains durably `pending` while it waits in a finite,
  launch-sized FIFO; the physical worker entry is now the fenced
  `pending` -> `running` transition, so reload/reconnect can distinguish queue
  wait from execution without relying on a transient phase string. Normal and
  Flow chat share the same scheduler. Personal root concurrency now scales up
  to 32 from CPU and memory/RSS headroom (4 on the 8 GiB reference, 18 on the
  64 CPU / 64 GiB reference), while per-task tool fan-out remains capped at 4.
  A reaper-proven wedged Python call is quarantined behind a bounded replacement
  budget; queue saturation, startup failure, cancellation, restart recovery,
  queue wait, rejection reason, and abandoned-thread recovery are explicit and
  observable.
- Isolated started chat model dispatches from frontend and storage observers. While
  an upstream stream is being consumed, task events stay in the bounded
  in-memory replay and no Sidecar write, presence update, synchronous push or
  webhook listener, or cross-process database abort probe can block its
  callback path; the provider boundary performs one sampled checkpoint and
  resumes cumulative durable-before-visible projection. Explicit owner Stop,
  provider/network verdicts, deadlines, and runtime/process failure remain real
  termination boundaries. The native async `/api/v1/chat/stream-direct` relay
  now also finishes a started upstream request after client disconnect, drops
  unobservable relay chunks, and retains its admission lease until settlement.
- Reduced the durable task-event batcher's idle gather window from 5 ms to
  1 ms. A real SQLite Sidecar benchmark held sequential batching at the same
  80 transactions while reducing median acknowledgement from 5.47 ms to
  1.41 ms (74%); an eight-stream / 480-event run still grouped exactly eight
  events per transaction and used the same 60 physical commits as the 5 ms
  window. Durable-before-visible ordering and the 500-row batch ceiling are
  unchanged, and the configured windows are now observable in batcher metrics.
- Extended the shared local token authority to repeated tool results instead
  of adding a second context-telemetry cache. General text keeps the existing
  4 KiB admission floor; only caller-proven reusable telemetry uses the 512-
  character floor, avoiding measurable hash/LRU cost on one-off medium text.
  On a 362-message /
  353,381-character / 80-tool long-task fixture, per-round telemetry fell from
  34.54 ms median to 1.88 ms (94.6%); with the tokenizer warmed, 25 rounds fell
  from 0.870 s to 0.066 s (92.4%). The fixture used 182 of this machine's 256
  launch-probed entries without eviction. Keys contain only tokenizer
  encoding, character length, and a SHA-256 digest; personal profiles stay
  within 64..512 entries,
  distributed defaults to 1,024, and the hard ceiling remains 4,096. Numeric
  hit/miss/eviction evidence is observable, no prompt/tool text is retained,
  and changed content or tokenizer encoding is always recounted. Request
  construction and all emitted telemetry fields are unchanged.
- Moved attempt-aware tool execution grouping, round/attempt labels, tool-family
  classification, label/color/icon-key policy, and synthetic-program value
  normalization out of the retained renderer and into pure typed conversation
  presentation owners. The shared trusted SVG registry is typed too; a literal
  callsite audit found and restored the previously blank `info` glyph. Public
  behavioral tests replace source-slice fixtures, and a parallel-safe NC harness
  no longer shares generated filenames across pytest workers. The tool-round
  SVG tables now have a pure typed asset owner too.
  Push-frame ownership comparison also takes explicit local/frame owner IDs in
  `core/frame-identity.ts`; unresolved, unscoped, and foreign frames fail closed
  without embedding the browser's current-user state in policy. The adjacent
  injected current-user controller validates payloads, coalesces concurrent
  probes, retries failures, and exposes an explicit reset lifecycle.
  HTML escaping and trusted-template branding now share one DOM-free typed
  owner; typed and lazy features import it directly, while retained renderers
  receive explicit composition aliases instead of two load-ordered classic
  sections. Error normalization remains in `api/errors.ts`; localized labels,
  safe cards, legacy mojibake repair, and bounded fallback-cause formatting now
  live in a separate DOM-free typed presentation owner with injected translator
  and icon ports. The action generator now distinguishes literal handler bodies
  from presentation-time interpolations and scans both composed retained code
  and authored TypeScript. Its browser-global receiver map falls from 326 to
  315 while preserving DOM-assigned actions, keeping the migrated offline
  Recover control callable, and restoring the previously undiscovered Paper
  landing action. Chat-model capability taxonomy is now an isolated typed
  server-projected controller with a validated atomic replacement and
  defensive snapshots; the toolbar's stale duplicate fallback (which omitted
  `tts`) and unused dispatcher-set mirror are gone. Together these migrations
  now include the vendor-grouping policy: its brand detector is injected into
  one immutable typed owner, toolbar and Settings no longer carry divergent
  missing-module rules, and the three transitional `globalThis.modelGroup*`
  bridges are gone. Public policy/rendering tests replace source mutation
  fixtures. Adjacent alias/family folding is now a pure typed projection of
  backend metadata, while recent-model persistence is a separate injected,
  validation-on-read controller with a five-ID hard bound; their three legacy
  browser-global bridges and mixed retained owner are gone too. Brand detection,
  immutable SVG/color assets, live-catalog display naming/natural ordering, and
  role-avatar snippets now have four focused typed owners; Settings no longer
  reaches into a private collator, and the 47 KB mixed `settings/branding.js`
  section is retired. Fetch-like response projection and canonical result-error
  recovery now use the immutable typed `core/http-result.ts` port; retained
  orchestration consumers receive it lexically and the duplicate
  `api/http-result.js` section is retired. The reconnect work pool is now a
  typed core owner too: both offline-conversation and pageshow/online
  live-attempt sweeps explicitly share four lanes, collect per-conversation
  failures, and no longer depend on a disconnected runtime-global pool.
  Explicit single-conversation refresh now lives in an injected application
  command too: toolbar/swarm consumers use one module-private binding, the
  former `runtimeScope.refreshConversationRuntime` API is gone, failures use
  localized reconnect copy, and presentation faults cannot replace the Turn
  runtime error. Cookie-capture completion is now composed directly from its
  typed controller after push initialization, with lifecycle-owned
  unsubscription; the unused manual frame/global API and retained adapter are
  gone. Fetch-response byte decoding and newline framing now have one typed
  core owner imported directly by the lazy arXiv ingest module; the mutable
  feature-registry/global seam and retained SSE reader are gone. Per-Turn
  translation claims now use a typed, clock-injected registry with the existing
  three-minute expiry plus stale pruning and a 256-live-claim hard ceiling;
  its three runtime globals and retained guard are gone. Conversation catalog
  title/full-ID/default-setting queries are pure typed functions over injected
  inputs now; live retained consumers keep lexical wrappers while their three
  runtime globals and mixed reducer section are gone. Marked parser policy is
  typed and explicitly installed now; single tildes remain literal because a
  strict-tokenizer miss no longer asks Marked v12 to fall back to its permissive
  rule. The mixed cache/Markdown section and its two unreachable console
  helpers are gone. Translation display now reads the authoritative Turn
  projection directly through the typed view model; the unreferenced
  message-era display/fingerprint model, its runtime globals, and a stale test
  for a nonexistent history sweep are gone. My Context preference resolution
  and undo now share an injected typed controller with explicit rollback;
  their retained section, five ambient dependencies, and two direct runtime
  globals are gone while markup continues through the central action table.
  Shared fullscreen/download image actions now use one typed DOM controller;
  replacing an overlay or closing it by backdrop/Escape unregisters the sole
  key listener, and temporary download anchors are exception-safe. The
  translating/connecting send-preparation Turn is now an injected typed
  controller keyed to the initiating conversation, so view switches cannot
  redirect cleanup and scroll faults cannot invalidate its state transition.
  A bounded consumer audit then removed unreachable Autopilot VU side-channel
  projection, retired project-scan polling, obsolete mode aliases, and other
  definition-only helpers; current VU/transcript rendering stays on the
  authoritative TurnStore and active controls keep their explicit action-table
  entries. Live Swarm rich HTML now reconciles inside the typed classic
  ConversationSurface renderer, preserving panel/card identity, reader
  disclosure and focus while the unreachable retained morph helper is gone.
  A second exact-consumer pass removed four more definition-only adapters for
  force-finish, dated report generation, Studio builtin selection and Turn
  projection patching, plus the orphaned force-finish locale key.
  Local catalog-change reconciliation is now an injected typed application
  owner: busy conversations keep authoritative timestamps, invalidation stays
  a wake hint, and sidebar work is bounded to one pending animation frame.
  Memory and Skills now share one lazy typed ZIP-upload transport with input
  validation, one active request, and a fixed listener set while retaining
  their own scope, copy, diagnostics, and refresh policy.
  My Day's six TODO/stream mutators now form a typed lazy controller over
  explicit API, selected-report, cache, render, and diagnostic ports;
  optimistic rollback and server-owned cycle status remain behavior-tested
  while its quick-action launcher makes prefill-before-conversation an
  executable intent order with explicit tool-mode ports. The remaining read
  cache is now a typed repository keyed by the resolved owner, capped at 96
  512-KiB reports plus 24 128-KiB month overviews (51 MiB estimated maximum);
  its v3 upgrade drops the former unscoped/unbounded cache. One idle-loaded,
  disposable background controller owns the cache-first day digest and the
  three-hour reminder, with exactly two timers and a 16-owner reminder ledger;
  the retained panel no longer opens IndexedDB or registers boot work.
  The conversation catalog's reconstructible IndexedDB cache is now a lazy,
  disposable typed owner. Its v6 schema drops ownerless v1–v4 rows and the
  short-lived transitional v5 shape; every operation resolves authenticated
  identity, current-owner clear never erases
  another owner, and global entry plus row-byte ceilings cap the estimate at
  56.25 MiB without requesting persistent browser storage.
  Together these migrations removed thirty-three retained sections (209 → 176)
  and lowered their byte ratchet from 3,638,891 to 3,343,701; the retained
  authoring budget tightened from 3405 KiB to 3240 KiB. A generated-runtime
  call-graph gate now rejects closed unreachable chains, and the first pass
  removed fifteen dead compatibility helpers plus their stale explanations.
  That pass also exposed typed Paper/Skills/Memory consumers whose retained
  presentation ports had never entered the private service table; the ports
  are explicit now, and Podcast/Video fail visibly if composition omits them.
  The Skills feature now keeps a minimal lazy entry separate from its typed
  Settings panel owner. Podcast and Video's remaining retained presenters ship
  in a manifest-owned Paper lazy bundle with lexical model/tick dependencies,
  rather than occupying the main retained runtime or relying on load order.
  Paper and standalone Research's eighteen always-required typed owners now
  retain their focused source modules but ship through one 35.2-KiB gzip
  `paper/panel-owners.ts` composition chunk instead of eighteen unconditional
  requests. This first lowered total Vite JavaScript from 1410.8 to 1398.7
  KiB; after adding the optional conversation-cache and 2.1-KiB dialog chunks,
  the current graph is 1401.7 KiB and the main entry is 577.2 KiB, both under
  their unchanged ceilings. `toggleResearchMode` now crosses the same declared
  lazy Paper domain as `togglePaperMode`.
  The retained endpoint facade now has one explicit ESM lexical binding backed
  by the private runtime registry, so startup no longer depends on implicit
  browser-global name resolution. Recent-model persistence resolves storage
  through the typed browser-capability boundary rather than probing
  `window.localStorage` from retained composition.
  Page/network live-attempt recovery now has a typed, disposable application
  controller over the shared four-lane pool; net latency reads the typed health
  store through one lexical subscription, and the obsolete health/timer
  section, public service pair, duplicate initialization, and repaint alias are
  gone.
  Global backend liveness now has a separate typed owner whose push/browser
  signals only enter a two-probe confirmation gate; proxy auth denial remains
  explicitly non-fatal, and recovery keeps the existing push/stream/catalog
  resynchronization. Sidecar readiness has an independent typed warning owner
  with one visibility-aware, single-flight recovery probe. Both tear down with
  the page lifecycle; the mixed retained monitor section, its two action-table
  receivers, startup ambient function, and test-only browser globals are gone.
  Confirm, alert, prompt, and multi-option choice now share one lazily loaded
  typed dialog controller as well. It bounds the page to one active overlay, settles a
  replaced or destroyed dialog to its safe default, reads prompt input at the
  actual confirmation boundary, and owns every key listener, animation frame,
  live-check interval, exit timer, and focus restoration. The retained dialog
  section and direct runtime-global publishers are gone; public DOM/Promise
  behavior tests replace source-rewriting fixtures.
  Task Mode's frontend regression family now follows the same owner boundary:
  49 duplicated legacy-directory, aggregate-bundle, and source-shape tests were
  reduced to 24 non-overlapping public behavior contracts, all green against
  the exact typed owner or explicitly composed owner graph. The native test
  adapter gives each graph an isolated `.native/` path and preserves registry
  property descriptors, so live composition-root getters cannot be flattened
  into order-dependent snapshots. Its list, run replay, commands, mutations,
  workspace, view registry, focus, accessibility, and teardown semantics remain
  exercised without reconstructing the deleted `static/js` tree.
  Turn provenance is now a DOM-free typed presentation owner over the exact
  Conversation Sync block fields. The retained tool monolith lost 372 lines of
  memory, My Context, related-chat, MCP-login, and learned-preference rendering;
  its lexical bridge injects only the generated translator and trusted icon
  port. A 23-assertion native-owner contract replaces the obsolete classic-file
  harness and covers inline Markdown, failure precedence, lifecycle placement,
  immutable inputs, escaped translated copy, and hostile action-ID round trips.
  This reduced retained runtime by 17,781 bytes without adding a section.
  Write-gate refusal classification, generated-i18n interpolation, badges, and
  safe path notices now form a second DOM-free typed owner. Structured and
  legacy refusal facts converge before the three retained write-card slots;
  unknown kinds remain safely visible while invalid paths/counts fail closed.
  Nineteen owner assertions plus five wiring checks replace a 324-line suite's
  duplicated full-render cases, source-amputation NEUTER, temporary files, and
  locale-JSON source scan. This removed another 5,798 retained bytes.
  Compaction labels, bounded line diffs, write/single-edit/batch cards, and the
  generic tool-result viewer now form a third DOM-free typed presentation
  owner over projected round metadata and explicit trusted header slots. The
  retained dispatcher keeps branch order but lost 345 lines / 18,045 bytes;
  after its ESM composition port, this tranche reduces aggregate retained debt
  by 17,598 bytes. Twenty-seven exact-owner assertions now cover immutable
  inputs, hostile interpolation, LCS and large-diff fallback, legacy operation
  derivation, write-gate placement, generated i18n, and the 120,000-character
  result ceiling; the retained jsdom suite no longer duplicates that policy.
  Tool-catalog search, web/fetch rows, vertical-domain merging, and engine
  breakdown now form a fourth DOM-free typed presentation owner. It never
  mutates projected items, accepts only HTTP(S) links, and bounds actual scans
  as well as display: 512 catalog records / 64 cards / 8 arguments, 100 web
  rows, 64 vertical records / 256 sources / 512 items / 12 rows per card, and
  32 engines / 512 URLs. Generated English/Chinese messages disclose each
  truncated family. At the extraction boundary the retained dispatcher lost
  298 lines / 18,069 bytes; the current shared-tree byte ratchet moves from
  3,317,322 to 3,300,866. Thirty-five exact-owner assertions and six narrow
  wiring checks replace the source-coupled search suite, while a 52nd wire
  snapshot makes `search_tools` an explicit dispatcher-family contract.
  Read/inspect/preview thumbnails and generated/edited image cards now form a
  fifth DOM-free typed presentation owner. It localizes all image-card copy,
  never mutates projected results, rejects active or ambiguous image/SVG URLs,
  scans at most 64 descriptors, and renders at most 16 tiles with a visible
  localized limit. The retained dispatcher lost 182 lines / 10,665 bytes; its
  composition port leaves the touched retained sections 10,457 bytes smaller;
  the current shared-tree byte ratchet moves from 3,300,866 to 3,290,691.
  Thirty-five exact-owner assertions, ten viewer-lifecycle assertions, and six
  narrow wiring checks replace the source-copy/amputation fixture; the existing
  52-round byte snapshot proves ordinary image markup remains unchanged.
  Browser JavaScript execution cards now form a sixth DOM-free typed
  presentation owner. Serialized arguments have an 80,000-code-unit pre-parse
  gate; code, description, and result displays are capped at 65,536, 4,096, and
  120,000 units with localized visible notices. The owner escapes every
  projected field, localizes fallback/status copy, and never mutates inputs.
  Its retained dispatcher branch lost 49 lines / 2,708 bytes; the composition
  port leaves its two retained sections 2,337 bytes smaller, while the current
  shared-tree ratchet moves from 3,290,691 to 3,288,591. Eighteen exact-owner
  assertions and five narrow wiring checks cover malformed/hostile/oversized
  inputs, and the 52-round byte snapshot preserves ordinary card markup.
  Running and settled shell/code cards now form a seventh DOM-free typed
  presentation owner. Serialized arguments, command/description text, live
  output tails, settled output, legacy status tails, and QR descriptors all
  have explicit observable budgets; QR URLs share the image-source allowlist,
  which now validates the complete Base64 payload grammar. Retained code keeps
  only timer ticks, interrupt I/O, and expansion-state lifecycles. The
  dispatcher lost 192 lines / 10,813 bytes; its composition port leaves the
  touched retained sections 10,442 bytes smaller and moves the shared-tree
  ratchet from 3,288,591 to 3,278,149. Twenty-two exact-owner assertions, five
  narrow wiring checks, eleven interrupt-lifecycle assertions, ten public QR
  assertions, and the 52-round byte snapshot replace the legacy static-i18n
  scraper while preserving ordinary command markup byte-for-byte. The explicit
  typed tool-policy set now emits as one eager `tool-presentation` chunk, so it
  remains startup-required and total-budgeted while leaving the main entry
  independently cacheable at 560.2 KiB gzip. The main-entry ratchet moves from
  580 to 561 KiB; the total-JavaScript ceiling remains 1,410 KiB and counts the
  21.4 KiB eager policy chunk exactly once.
  Pending write-tool cards now form an eighth DOM-free typed presentation
  owner. Generic risk fields, batch/single diffs, commands, and content
  previews have explicit scan, item, input, line, and text budgets with
  localized visible elisions. Approval IDs move out of interpolated action
  source into escaped `data-approval-id` attributes read by one static
  restricted action, closing the code/data ambiguity without moving approval
  authority out of the existing resolver. The dispatcher drops 108 lines /
  7,539 bytes; its composition port leaves the touched retained sections 7,263
  bytes smaller and moves the isolated shared-tree ratchet from 3,278,149 to
  3,270,886. Sixteen exact-owner assertions, nine public dispatcher checks,
  the live write-partition risk matrix, a real action-registry click, and four
  reviewed approval snapshots cover the new boundary. The eager policy chunk
  is now 22.8 KiB gzip, the main entry 559.2/561 KiB, and the complete emitted
  JavaScript graph 1,406.8/1,410 KiB under the repository's fixed gzip-9
  measurement.
  Synthetic context-injection rows now form a ninth DOM-free typed
  presentation owner. Swarm updates, peer messages, operator steer, and
  intent-stall nudges share one closed dispatch order and explicit XML, item,
  identity, title, Markdown/raw-text, error/path, and prompt budgets; omitted
  content is localized and visible. Markdown sanitization, file icons, live
  conversation-title lookup, and translation cross named composition ports,
  while retained code keeps only chronological placement and peer-jump
  lifecycle. Nine ambient rendering helpers leave `tool_rounds.js`, shrinking
  it by 324 lines / 18,147 bytes; the composition port leaves touched retained
  sections 17,717 bytes smaller and moves the isolated ratchet from 3,270,886
  to 3,253,169 bytes. Nineteen exact-owner assertions plus a real delegated
  peer-jump click replace private-function extraction and its shipped-source
  guard, lowering implementation-face audit debt from 593 to 592. Four
  formerly uncovered lanes extend the byte-parity battery from 52 to 56 rows
  with all original rows unchanged, and 25 focused tests cover title
  attribution, public dispatch, stall provenance, chunk policy, and budget
  accounting. The eager policy chunk measures 25.2 KiB gzip, the
  main entry 557.1/561 KiB, and total emitted JavaScript 1,407.2/1,410 KiB.
  Human Guidance now forms a tenth DOM-free typed presentation owner.
  Awaiting, expired, skipped, and submitted states share one immutable port;
  legacy option JSON, option count, identifiers, questions, labels, and
  descriptions have explicit pre-parse/scan/display budgets with visible
  elision. Response IDs and original choice labels move out of interpolated
  action source into escaped datasets read by static restricted actions.
  `tool_rounds.js` loses both ambient Human Guidance render helpers, shrinking
  by another 132 lines / 7,322 bytes; the composition port leaves touched
  retained sections 6,959 bytes smaller and moves the isolated ratchet from
  3,253,169 to 3,246,210 bytes. Twenty-three exact-owner assertions exercise
  frozen inputs, malformed legacy values, every bound, dependency failure, and
  real click/keyboard dispatch with hostile data. The reviewed 56-row wire
  snapshot changes only the live choice action string; the other 55 outputs
  remain byte-identical. The eager policy chunk is 26.8 KiB gzip, the main
  entry 556.4/561 KiB, and total emitted JavaScript 1,408.0/1,410 KiB.
  Shared-tree integration also removed a duplicate retained `_renderToolSlot`
  that shadowed sibling-title disambiguation, routed the storage-recovery DOM
  contract to its exact typed availability owner, and cleared two stale
  runtime source/seam references without restoring retired compatibility code.
  The adjacent Swarm push subscription now shares its typed transient-Turn
  owner, including idempotent start/destroy and unsubscribe; the retained
  wiring section and its source-mutation test are gone while authoritative
  rebase, overlay isolation, and hydrate-before-release stay behavior-tested.
  Metadata-only browser startup now has a typed coordinator whose dependency
  surface cannot hydrate or dispatch a Turn. Folder completion/retry remains
  independent of catalog failure, active presentation still converges, and
  three source-order/location test files were replaced by one public behavior
  contract plus a machine-readable zero-tolerance lifecycle-classifier rule.
  Migration-only tests that scanned deleted compatibility owners were retired
  in favor of their existing typed behavior suites; missing-path findings stay
  at zero and the implementation-face test ratchet tightened from 603 to 594.
  Three more timer tests that pinned the removed seed/backoff/deferrability
  implementation were retired; server-clock, phase-label, async recovery, and
  ConversationSurface behavior remain covered through their public boundaries.
  Production-bundle verification also exposed and closed an ESM
  boot-order leak: the API registry and its orchestration collaborators now
  resolve through the private runtime scope until the epilogue publishes the
  single compatibility global.
- Stopped conversation wake hints from reloading the entire Turn graph while
  its ordered v3 stream is healthy. Push and BroadcastChannel invalidations now
  only wake/reopen the coordinator from its durable cursor; an expired cursor
  still receives `sync.reset_required` and performs exactly one authoritative
  snapshot recovery. The retained production log contained 14,839 indicated
  heavy snapshot responses across ten conversations (25.85 GiB of response
  bodies and about 0.98 aggregate handler-hours); the focused browser contract
  now holds six separately scheduled wake hints plus six warm browser/network
  resumes at one initial snapshot. Visibility, online, push-reconnect,
  periodic, and live-attempt recovery now reopen from the durable cursor too;
  cold stores and typed cursor resets retain their authoritative snapshot.
- Made large fastpath SQLite backups resumable and gave their built-in task a
  launch-probed deadline. Production held an 89.3 GB shadow while the canonical
  backup was 297 hours stale; the nightly job failed exactly at its fixed
  1,800-second boundary. Replacement generations now fsync a source-fingerprinted
  progress witness every 256 MiB, survive timeout/restart, reject changed-source
  progress, and always recopy the WAL tail while preserving the prior published
  pair. The original recovery-point time now survives retries into the canonical
  manifest, so a late completion cannot masquerade as a fresher backup. On this
  authority the maximum repeated database-image copy falls from
  89.3 GB to less than 256 MiB (approximately 99.7%); the current resource probe
  resolves the bounded 30-minute..6-hour default to 21,600 seconds across the
  scheduler and online/offline maintenance CLI. Resume progress/count/
  bytes are observable, and focused tests cover same-process resume, new-process
  discovery, source invalidation, shutdown, concurrent writes, and old-pair
  preservation. Shadow-volume admission runs before checkpointing and credits
  only the validated, actually allocated durable prefix; publication rechecks
  concurrent WAL growth and refuses a short read. Doctor now
  distinguishes this deployment's 816.4 GiB allocated legacy-snapshot footprint
  from its 1,135.9 GiB sparse/logical size, preventing operator cleanup estimates
  from overstating reclaimable disk by 319.5 GiB.
- Hardened validation around concurrent source changes and the Project Brain
  lazy-runtime boundary. The architecture scanner now skips files that vanish
  after enumeration, reports the number actually checked, and still surfaces
  other I/O failures; focused tests cover each outcome. Frontend contract
  fixtures now execute the typed content-translation owner, runtime-scope
  checks include manifest-declared lazy owners, and the free-translation-call
  guard recognizes dynamic arguments. Compressed two module guides back under
  their cataloged line budgets without changing their contracts.
- Made Codex subscription cache settling tail-adaptive without weakening cold
  starts or the 4.2-second per-key routing guard. Warm rounds now pay the extra
  five-second visibility window only when their uncached suffix reaches a
  configurable 8,192-token bound. A 95,723-line production-log replay matched
  1,217 prior-round usage records and projects removal of 872 holds / 2,493.07
  seconds (70.4% of matched wait), while retaining 345 cold/material-tail
  holds; focused tests cover the boundary, operator override, invalid values,
  sync timing, and async timing.
- Reduced browser startup parsing and closed four frontend resource ratchets.
  Project Brain is now a manifest-owned lazy retained runtime (29,268 gzip
  bytes) and no longer contributes 102,852 raw bytes to the main entry. The
  4,503-key English and Chinese catalogs now emit as content-hashed JSON data,
  with concurrent loads coalesced and failures retryable, instead of being
  parsed as JavaScript. At the production graph boundary this reduced total
  Vite JavaScript from 1,601,399 to 1,430,160 gzip bytes (−10.69%); the main
  entry is 589,025 gzip bytes. Its content-translation overlay is now a typed
  owner with a globally coalescing 6-request / 256-pending scheduler,
  12,000-character per-item, 128-entry / 512,000-character memory, and
  512-entry IndexedDB bounds. This removed one retained section and ratcheted
  retained bytes down by 14,076. CI ceilings now hold retained runtime at 3,554
  KiB, main at 580 KiB, ordinary async chunks at 120 KiB, and total JavaScript
  at 1,410 KiB.
- Made the MCP bridge preserve `CallToolResult.structuredContent` across the v2
  `structured_content` rename and merge it with text content under one dedupe
  rule plus the `MCP_MAX_RESULT_CHARS` budget, so a structured-only tool no
  longer returns empty. The catalog snapshot now surfaces the full
  `Tool.annotations` hints (readOnly/destructive/idempotent/openWorld) and
  `outputSchema` under both SDK spellings while the read/write partition still
  keys off `readOnlyHint`. Added hope/llm env_specs (`HOPE_MCP_TOOL_PROFILE`,
  `HOPE_MCP_LOG_LEVEL`, `LLM_MCP_LOGIN_TIMEOUT`, `LLM_MCP_MAX_PARALLEL`,
  `LLM_MCP_LOG_LEVEL`, `LLM_MCP_CA_BUNDLE`) and corrected the drifted
  auto/progressive MCP exposure docs (auto now preselects at most 8 native
  schemas by intent). Ambiguous MCP discovery now normalizes both Hope's
  `read|write|destructive` and LongCat's `none|auth|mutating|destructive`
  vocabularies so declared read-only tools win the bounded starter set.
- Bounded durable swarm-panel amplification without clipping authoritative
  child results. Live frames, resumable checkpoints, and reload snapshots now
  share a 2,000-character tool-detail budget; completed snapshots retain at
  most 30 timeline rows and 32 KiB of serialized timeline data per agent,
  prioritizing recent evidence. Older rows remain as lightweight usage/edit
  facts, and every omitted call or detail is marked in projection metadata and
  rendered honestly after reload. This reduces a measured four-agent snapshot
  from roughly 600 KiB to about 130 KiB while keeping full final answers.
- Removed the largest remaining browser snapshot mirror without changing the
  default Conversation Sync API or replay format. The generated client now
  requests a reference view in which completed, uniquely matched tool segments
  borrow input/result bodies from their sibling `toolRounds`; the coordinator
  restores renderer-compatible fields by object reference before TurnStore.
  The same view omits opaque Responses/Anthropic replay bodies that have no
  authored browser consumer. Completed API rounds retain UI token, cost,
  cache-break, dispatch, quota, and trace facts while dropping server-only
  stream/routing/pricing evidence; running/resumable/failed and ambiguous
  records remain full. Concurrent full/reference callers still share one
  authority read and the request-local projection never mutates it. On a real
  174-tool completed Turn this reduced the production `orjson` body from
  4,435,548 to 2,414,719 bytes (−2,020,829, 45.56%) and gzip-level-1 bytes from
  1,310,774 to 615,972 (−694,802, 53.01%); `segments` fell from 1,619,065 to
  69,414 bytes, `toolRounds` from 2,444,074 to 2,130,604, and `apiRounds` from
  291,619 to 133,911. The request-local transform measured 0.910 ms median /
  0.932 ms p95 over 200 in-memory runs.
- Renamed the server-staging capability to the canonical
  `browser_download_url_to_server`, retained the old name only as an execution
  alias, and made its eager routing dependencies declarative on `ToolSpec` so
  search, fetch, browser, and bilingual download intents expose it on the first
  round. It can now resolve a page link by visible text or selector without
  copying truncated signed URLs, clicking, or exporting cookies. Streaming
  search/fetch workers bind the exact task owner/device in every thread;
  transient fetch failures no longer poison the dedup cache; live browser
  observations opt out of result reuse independently of idempotency. Local Tool
  Search adds bilingual download concepts, compact 24,000-character result
  pages, exact-name migration, and frozen original-intent/latency coverage.
- Made invalid legacy swarm recovery self-healing without weakening ownership.
  Startup now quarantines a resumable session that lacks a positive owner via
  one Sidecar transaction, retains every child checkpoint as evidence, and
  excludes the row and its message payloads from later recovery scans. The
  command re-checks owner state so a concurrent repair wins; Tofu no longer
  repeats the same default-deny error on every boot or invents personal owner
  `1`.
- Restored the canonical storage projection on turn-native carried task
  events. The atomic carrier had bypassed `messages_snapshot` prefix/tool
  deltas and usage-diagnostic trimming, making cumulative request snapshots
  grow quadratically on disk; live frames remain unchanged, while carried and
  standalone rows now share one projection and cold replay still rebuilds the
  full inspector payload. Terminal events also release their in-memory
  snapshot baseline immediately.
- Bounded `/api/push` slow-client degradation. Event sockets still absorb a
  short burst by retaining the newest 1,000 frames, but a lane that loses at
  least 256 frames across 15 seconds now disconnects for normal reconnect
  reconciliation instead of paying indefinite fan-out, queue-replacement, and
  per-frame warning costs; saturation logs are structural and episode-bounded.
- Aligned every live LLM stream transport with native Codex's rolling idle
  contract. Sync SSE, async SSE, and Responses WebSocket attempts now stop only
  after 300 seconds with no transport activity; comments, keep-alives, metadata
  events, reasoning, text, and tool traffic all renew the window. The former
  semantic-progress deadline is no longer armed, its environment names migrate
  as deprecated aliases for the transport-idle duration, and true silence uses
  the existing bounded stream-interruption retry path. Historical
  `semantic_progress_timeout` timelines remain readable.
- Routed in-place update and idle-HEAD restarts through Hypercorn/Quart's
  bounded production shutdown before the main thread calls `execv`. The old
  thread-owned path skipped the lifespan, orphaned its Storage Sidecar under
  the unchanged worker PID, and made the fresh image wait on its own project
  lease; one observed restart failed storage startup twice and required a
  73-second manager recovery. Restart now stops admission, quiesces producers,
  closes transports, releases the sole storage authority, preserves the PID
  and endpoint only after cleanup, and falls back to manager recovery if the
  final exec cannot replace the stopped image.
- Removed repeated network-volume metadata work from the Python package import
  boundary. An isolated cold `import server` spent 78.9 seconds wall time but
  only 1.67 seconds on CPU: `lib` initialization alone held 32.0 seconds, with
  eager config-directory creation, repeated reads of one `features.json`, and
  a pricing import that pulled in the shared HTTP stack. Config paths are now
  filesystem-pure until a writer creates their parent, all flags resolve from
  one reloadable launch snapshot, credential-key creation owns its missing
  parent, and pricing loads HTTP only inside an explicit online refresh. A
  repeat import of the early instance-lock module fell from 1.784 to 0.930
  seconds (48%) while leaving pricing and HTTP modules unloaded.
- Routed managed server-worker bytecode for network/userspace checkouts into a
  verified host-local cache instead of repeatedly reading every `.pyc` body
  through the source mount. Project and interpreter fingerprints isolate the
  namespace; a manager-held shared lease protects live workers while
  launch-time symlink-safe LRU maintenance enforces a probed 16..64 MiB
  personal budget (128 MiB distributed, 512 MiB hard cap), 100,000 entries, 64
  namespaces, seven-day TTL, and a 256 MiB free-space reserve. Operator Python
  policy wins and any cache failure falls back to the unchanged launch. In a
  four-run full `import server` comparison, network-cache baselines were 6.934
  and 5.697 seconds, local first fill was 8.707 seconds, and local reuse was
  4.274 seconds (about 32% below the baseline mean) for 32.3 MB / 1,785 files.
- Removed tofu-search, trafilatura/lxml, and PDF extraction from ordinary server
  boot, handler registration, empty-input failures, unrelated config reloads,
  and the first conservative search-tool schema. One concurrency-safe activation
  boundary now installs the PyMuPDF classic policy, current config, browser, and
  auth providers at the first valid search/fetch use; independent research,
  paper, browser, PDF-export, and API entry points cross the same resource guard.
  Three unchanged-process imports averaged 3.477 seconds and 100,712 KiB peak RSS
  versus the measured 6.316-second / 155,696-KiB eager baseline (about 45% less
  startup wall time and 35% less peak memory); first search pays the deferred
  import once. Before activation, `vertical=auto|off` remains available without
  importing metadata; runtime-derived explicit verticals appear on later turns.
- Split the generated API v4 release latch from its dormant strict DTO/OpenAPI
  runtime. Ordinary v1 server boot now imports only dependency-free constants;
  the same generator still owns every artifact, while Pydantic adapters and the
  canonical document load on the first actual v4 response. All 22 v4 contract
  tests retain strict validation and generated-artifact drift checks. In three
  follow-up imports, Pydantic stayed absent and mean peak RSS fell from 100,712
  to 93,041 KiB (about 7.6%); mean wall time was 3.400 seconds versus 3.477 in
  the immediately preceding three-run sample.
- Removed dormant paper generation engines from HTTP route registration. The
  paper-deepen task authority now lives in a lightweight runtime module, while
  its agent loop loads only when a section is actually deepened. Podcast route
  seams remain monkeypatch-compatible but load the worker on first use, and a
  background interruption sweep no longer pulls in the LLM script or TTS/audio
  stages. Isolated boot checks changed all four optional modules from loaded to
  absent while retaining one thread; three-process peak RSS averaged 93,076 KiB
  versus the immediately preceding 93,456 KiB (380 KiB lower). Wall samples
  were noisy (3.403 versus 3.216 seconds), so no startup-time win is claimed.
  Grounded blocking/streaming recommendation later joined the same boundary;
  its route/ownership/grounding suites passed 43/43, while the next three-run
  wall sample remained noisy (2.959 versus 2.878 seconds) and RSS was flat.
- Deferred the remaining request-scoped execution graph behind real work. Plain
  global-model resolution now returns without importing the ephemeral BYO
  dispatcher; inline and registered BYO paths mint/dispose through one lazy
  lifecycle seam shared by native agent-run and both compatibility APIs. Paper
  QA and translation routes likewise keep only their task runtimes resident and
  load engines inside the spawned task. BYO/compatibility tests passed 88/88 and
  paper QA/translation tests passed 12/12. Three isolated imports averaged
  2.878 seconds versus the immediately preceding 3.403 seconds (about 15.4%
  lower); peak RSS was unchanged within noise (93,060 versus 93,076 KiB).
- Moved paper report execution behind its existing server-owned task boundary.
  Route registration, cache hits, polling, dedup, and abort now retain only the
  lightweight report runtime; the worker imports inside the spawned task. This
  also removes task handlers, the executor, and the complete tool registry from
  ordinary boot without weakening the registry's import-complete plugin
  contract. Report/review/rebuttal/abort/injection suites passed 104/104. Three
  isolated imports averaged 2.466 seconds versus the preceding 2.959 seconds
  (about 16.7% lower), while peak RSS fell from 93,008 to 89,431 KiB (about
  3.8%); all four deferred module families and optional search/v4/BYO stacks
  remained absent with one thread.
- Replaced the eager `lib.swarm` package facade with lazy compatibility exports.
  Importing the role registry for Orchestration Studio OpenAPI previously also
  initialized agents, schedulers, integration state, task handlers, LLM
  dispatch, and project tools. Child-module imports now remain focused, while
  historical package symbols and `from lib.swarm import persistence` still
  resolve on first use. Swarm suites passed 195/195 and orchestration authoring,
  service, HTTP, and application-port suites passed 121/121. Three isolated
  server imports averaged 2.411 seconds versus 2.466 seconds; peak RSS fell from
  89,431 to 87,105 KiB (about 2.6%), with role metadata present but swarm agent,
  integration, and project modules absent.
- Made `lib.llm` a lazy compatibility facade so pure stream-result/verdict
  consumers no longer initialize HTTP policy, sync/async chat transports, SSE
  clients, or shared network helpers during route registration. All 66 historic
  package exports still resolve from their focused owners, and child-module
  imports such as diagnostics remain compatible. Verdict/boundary tests passed
  38/38; body/cache, transport, timeout, Responses, Anthropic, dispatcher,
  health, error, and provider-pin neighbors passed 405/405. Three server imports
  averaged 2.375 seconds versus 2.411 seconds; peak RSS fell from 87,105 to
  81,127 KiB (about 6.9%), with typed stream evidence resident but all five LLM
  transport modules absent.
- Extended that request-loaded boundary through the 31-symbol
  `lib.llm_dispatch` facade, the task-stream call seam, and the shared LLM error
  taxonomy. Route registration no longer constructs provider discovery or
  dispatcher state; transport modules still resolve the identical concrete
  retry exception tuple on use. Skill catalog/install, webhook delivery and
  validation, and arXiv title/PDF routes likewise retain their historical
  monkeypatch seams while loading the shared HTTP/SafeFetch stack only for an
  explicit egress. Focused boundary, dispatch/retry/task-stream, webhook,
  paper, skill, approval, and registry suites passed 460/460. Three isolated
  server imports averaged 2.187 seconds versus the preceding 2.375 seconds
  (about 7.9% lower); peak RSS fell from 81,127 to 74,048 KiB (about 8.7%). The
  process retained one thread with `requests`, `urllib3`, HTTP/SafeFetch,
  dispatcher/discovery, and online skill modules all absent.
- Extended focused startup imports through general translation, Daily Report,
  and Optimizer. Translation route registration now retains its pure transforms,
  typed refusal, and task authority but defers LLM/MT, worker, incremental, and
  PPTX execution. Daily Report defers storage readers, aggregation, generation,
  and scheduler integration; Optimizer retains only its narrow storage/action
  authorities while deferring analyzer, proposer, applier, and orchestrator.
  Compatibility exports and route-level monkeypatch seams remain intact.
  Translation suites passed 219/219, Daily Report and neighboring lifecycle/API
  suites passed 183/183, and Optimizer suites recorded 141 passes with one
  intentional skip.
  Wall samples varied with cold-cache state, so no time win is claimed; the
  final three-process peak-RSS sample averaged 72,839 KiB versus 74,048 KiB
  before these boundaries (about 1.6% lower).
- Made the shared production and motion-video package surfaces lazy without
  weakening task discovery, deduplication, restart, checkpoint, or quality
  semantics. Server boot now establishes the shared and motion task runtimes
  while keeping stage, research, manifest, render, audio, and gate modules
  dormant. A controlled same-process expansion of the old 99-export graph cost
  19–26 ms and exactly 528 KiB additional peak RSS across three runs. Startup
  boundaries passed 4/4, the production substrate/lifecycle ladder passed
  43/43, and 436 motion behavior tests passed; the sole unrelated census
  failure exposed and then closed a missing display handler for the new
  `browser_download_url_to_server` tool. Its server-staging intent now renders as a
  human URL label without changing acquisition, ownership, or permission
  behavior, and the download/display suites pass 47/47.
- Removed XML/Atom parsing and the shared text-language cascade from ordinary
  paper/log route registration. arXiv fetch/search routes now keep patchable
  request seams plus one lightweight syntax-error identity and load the feed,
  search, and title adapter only for a real request. The 23-symbol paper-review
  facade resolves venue/prompt/text owners independently, while the language
  endpoint imports the fastText/heuristic cascade only when called. arXiv
  boundary, retry, error-surface, and ingest suites passed 26/26; review,
  language, translation-direction, endpoint, and log-route suites passed
  112/112. Restoring the old arXiv graph in the same process added a stable
  500–508 KiB peak RSS; its 20–155 ms timing was storage-noisy. Restoring the
  language/review-text graph cost 9–17 ms with no measurable peak-RSS change.
  The same log adapter now request-loads log-clean and tool-round file-change
  projection policies; their boundary/route/policy suites passed 72/72, and
  restoring the seven modules cost 11–16 ms with no measurable peak-RSS change.
- Kept Orchestration Studio's application boundary late-bound all the way
  through Python imports. HTTP registration still creates the single bounded
  orchestration `TaskRuntime` and canonical wire/OpenAPI metadata, but concrete
  authoring, definition, durable-run store/service, runtime start/mutation, and
  human-gate implementations now load at their first authorized operation.
  The resident orchestration graph fell from 97 to 69 modules. Restoring the
  exact seven old service imports in the same process returned it to 97 and
  added 21–29 ms plus 264 KiB peak RSS across three runs. Import/container,
  definition/run service, HTTP, CAS, runtime-start, and mutation suites passed
  208/208.
- Deferred cryptography/Fernet until a real authenticated secret operation.
  Provider metadata, BYO resolution, and the `lib.secret_envelope` contract can
  now register without loading the cipher implementation or reading/creating
  the personal deployment key; seal/open and logical-outbox payload codecs keep
  the same owner/purpose/record binding, key cache, and typed failures. BYO,
  egress, Provider Setup, logical-outbox, and startup suites passed 71/71.
  Re-importing the exact old Fernet graph after server boot loaded 22 modules
  and added 31–51 ms plus 3,476–3,600 KiB peak RSS across three runs.
- Restored automatic My Context learning after completed interactive turns; a
  finalizer refactor had retained the learner and its UI but dropped the only
  production call site, leaving About me, Work rules, and Response preferences
  empty. The repaired pass is off the response hot path and single-slot bounded.
  It reviews short turns only when they contain a durable-context signal, accepts
  at most two concise items with verbatim real-user evidence, rejects ungrounded
  or conversational filler and one-off task details, de-duplicates writes, and
  preserves the existing per-item undo trail and owner scope.
- Bounded Conversation Sync reconnect amplification at both ownership and
  principal scope. One live process exposed 205 conversation SSE streams for
  only 5 running tasks; the local code-server proxy simultaneously held about
  5.94 GB in socket receive queues (roughly 4.73 GB ESTABLISHED and 1.21 GB
  CLOSE-WAIT) because downstream disconnects did not retire infinite upstream
  heartbeat streams. Generated EventSource URLs now carry a page ID and
  monotonic generation: equal/new reconnects synchronously supersede the old
  broker subscription, wake its wait, and release its exact shared lease;
  delayed generations and residual capacity failures receive HTTP 204 to stop
  native retry. A current identified page can evict the oldest local
  conversation zombie before retrying the distributed-safe principal slot,
  while chat/remote leases remain protected. The shared launch-probed ceiling
  is 8..24 in personal mode (12 on the 8 GiB reference/fallback), 64 in
  distributed mode, and hard-capped at 128. Owner tombstones reuse the bounded
  browser-client registry with a 128-entry active-stream floor, and bounded
  metrics distinguish admission,
  supersession, eviction, stale generation, and capacity refusal without IDs.
  A 10-second body-start deadline closes the narrow pre-generator disconnect
  gap, so even an unconsumed admitted response releases its broker entry and
  slot. Conversation command startup failures also preserve their validated,
  persisted `task_start_failed` envelope instead of passing it through the
  generic 500-detail redactor and losing the user-visible retry guidance.
- Replaced per-root project tree scan pools with one launch-probed process-wide
  pool and made retained paths a cross-root budget. Two concurrent roots could
  previously create 32 scan workers plus two builders, while four retained
  600,000-path indexes represented roughly 491 MiB in sampled build-row objects
  alone. On the retained 4,775-file FUSE tree, two-pass median rebuild time was
  1.369 s at 4 workers, 0.283 s at 8, and 0.412 s at 16; the personal profile
  now selects 2..8 shared workers (8 on the 8 GiB reference machine), 50,000..
  600,000 total retained paths (409,600 on the reference), and 2..4 secondary
  root slots. Both pools retire after the exact active batch, oversized older
  disk indexes are declined before loading their body, write-freshness updates
  discard their superseded disk snapshot before an LRU eviction can resurrect
  stale paths, concurrent walk futures drain before publishing, and every
  override retains an explicit hard ceiling.
- Reused exact owner-scoped Project Brain snapshots across request-local views.
  Project context preparation now queries sibling conversations once for both
  prompt digest and UI metadata, and an active board once for both claim gating
  and prompt rendering. This removes up to two duplicate Sidecar reads per
  project task and prevents those paired views from drifting between queries;
  the public string digest API and pull-based full board tool remain unchanged.
  The conversation repository now pushes an optional owner/project predicate
  into SQLite/PostgreSQL before ordering and limiting. Context preparation
  therefore reads at most 24 sibling headers with only `projectPath` and
  `projectSummary`, while the cross-conversation tool and stranded-work
  dispatcher no longer retrieve full settings or transcripts merely to filter
  by project. A projected path witness keeps mixed-version reads fail-closed.
  A read-only aggregate on the retained authority measured the old selection
  at 4,786 headers / 15,246,228 raw settings bytes; the new selection was 24
  headers / 21,656 raw settings bytes, with about 3,498 bytes in the two values
  that cross the projection. These are payload bounds, not an end-to-end
  latency claim.
- Let fixed-policy automatic context compaction use the long task's observed
  survival instead of assuming every task has only one request left. The
  original one-round cache-rewrite gate remains through round 3; completed
  rounds 4/8/16/32/64 earn bounded 2/3/4/5/6-round exact-ROI horizons, capped
  by the resolved remaining API-round budget. In the retained production log,
  the earliest decline for each of nine distinct long tasks projected
  1.18–3.89 rounds to repay and every task then ran past break-even, leaving a
  rough 70.3M-token prompt-exposure upper bound. Retry witnesses now include
  the evaluated horizon, so each newly earned step rechecks exact economics;
  hard-window, manual/reactive, adaptive, evidence and anchor gates are
  unchanged.
- Moved typed shared-project TPM and app/model RPM contention control in front
  of network I/O. Once one `(provider_id, model)` request reports the shared limit, every later
  sync/async chat or stream task reserves a family probe after its local cache
  gates instead of first spending another large rejected API request. A later
  production translation exposed the remaining fixed-cadence cost: 746
  rejected starts in one 600-second failure window (6,849 shared-limit INFO
  lines in the retained 200,000-line sample). Continuing streaks now adapt
  from 1 to 2, 4, 8, and at most 15 seconds; a deterministic ten-minute
  simulation caps one continuously rejected family at 50 starts, at least
  91.7% below a one-per-second policy. Automatic unpinned work chooses the
  eligible family whose probe is due first, while explicit provider/model
  boundaries remain intact. Deep queues still recheck in abortable
  three-second slices without a capped wake-up herd, slot parking, or health
  penalties; per-wire rejection records move to DEBUG, bounded live gate state
  enters dispatch status, waits enter queue accounting, and two consecutive
  successes after reserved probes drain clear an intermittent limit.
  Shared-limit accounting no longer adds the obsolete per-key 0.5-second
  cooldown/error score, preserving the conversation's warm cache namespace.
- Bounded incremental auto-translation by user-visible value instead of Turn
  lifetime. One retained long Turn completed 254 model-backed preview
  translations while 46.2% of the wider 3,053-call sample contained fewer than
  100 input characters; a single preview could also occupy its worker for the
  generic 600-second background deadline. The personal profile now permits at
  most 32 preview segments per accumulator and 30 seconds per preview (256/60
  for distributed deployments), while the final deliverable remains outside
  that allowance. On the measured Turn this bounds the full lifecycle at 33
  model-backed translations, at least 87.0% below the retained 254-call prefix.
  A failed preview disables only reconstructible enrichment, settled terminal
  work preserves final reasoning while evicting older queued previews, and
  completed segment translations still merge with the authoritative Turn; the
  final translation can no longer be lost or delayed behind an unbounded
  intermediate stream.
- Removed steady-state subscription-egress route noise without changing route
  probes, caching, failover, or authorization. The retained 200,000-line sample
  contained 4,525 healthy `[Egress]` INFO records for 2,264 decisions because
  every request repeated both the selected path and aggregate verdict. Those
  unchanged decisions are now DEBUG-only; route failures, recoveries, topology
  invalidation, and credential-free status remain visible at their existing
  levels.
- Removed prompt previews from same-role wire-repair warnings. They now retain
  only role/index and bounded content-shape evidence, allowing duplicate-log
  coalescing across conversations without persisting user or assistant text.
- Bounded live `run_command` recovery state at the same 100,000-character
  limit as its settled result, retaining an explicit prefix/tail marker and
  total count instead of repeatedly copying an unbounded subprocess log. A
  measured grep accidentally emitted 10,328,512 characters: once its Turn
  patch crossed 4 MiB, 425 subsequent frames rebuilt the same oversized
  projection and the worker reached the 6 GiB RSS guard. Completed rounds now
  strip the redundant live buffer from Turn persistence, while a carried
  oversized structural frame retries atomically with a slim text projection
  and opens a 30-second probe circuit so the exact task event remains durable
  without a deterministic retry storm.
- Separated frontend deployment freshness from runtime artifact integrity.
  Explicit source-checkout start/restart and packaging gates still reject or
  rebuild locale chunks that predate their authoring catalogs, while the ASGI
  lifespan now validates only the complete atomically published Vite graph.
  An RSS recycle, OOM, or abnormal worker exit therefore reuses the same graph
  already trusted by hard refreshes instead of turning a harmless source edit
  into a five-attempt crash loop; missing or corrupt published assets remain a
  fatal startup error, and runtime recovery never acquires a Node dependency.
- Added an explicit single-checkout Git mode for model-only development where
  per-task worktrees and model-driven merges are too expensive. Tool execution
  is completely independent of Git checkpoint state: no dirty baseline, lock,
  configuration, or Git failure can reject or delay a project tool. Terminal
  settlement instead creates a best-effort workspace checkpoint through an
  alternate index and a short Git-only advisory lock, advances the development
  branch by CAS, and never checkout/reset/stash/merge/pushes. Concurrent
  conversations may be coalesced into the same snapshot; bytes arriving after
  capture remain dirty for the next settlement. Failed tasks are preserved as
  WIP commits, while `refs/tofu/stable` advances only when the exact
  stable-to-checkpoint delta passes the configured gate and the canonical
  checkout remains identical before and after verification. Export resolves
  stable only for repositories that explicitly opt in and have activated a
  task-end baseline; arbitrary attached projects retain HEAD/no-auto-commit
  behavior. Forbidden paths and semantic suffixes come from the same policy as
  isolated integration.
- Made large fastpath activation restart-safe and removed its shadow-copy write
  amplification. The 87.6 GiB live classic authority exceeded both the
  Sidecar's 30-second ready wait and Hypercorn's 60-second lifespan limit, so
  each manager retry truncated the private copy and reread up to 82+ GiB.
  Seed copies now durably checkpoint every 256 MiB, resume only against an
  unchanged source fingerprint, publish bounded byte progress through a
  renewable 30-second stall watchdog inside a 900-second hard limit, and give
  the outer lifespan the same budget plus 60 seconds. Completed copy ranges
  are explicitly released from page cache. A fingerprinted first activation
  hard-links the immutable classic database as shadow generation 1, consuming
  no second database-sized allocation or data copy. Later rebases use one
  stable sequential database-image copy plus the concurrently-written WAL
  prefix; this replaces SQLite backup's page-at-a-time network writes, which
  repeatedly restarted under live commits and remained stuck around 646 MiB.
  Startup also reclaims at most 64 exact private snapshot artifacts whose
  recorded creator PID is dead, so a killed rebase cannot accumulate one large
  orphan per restart; live-owner and unrecognized names are never touched.
  The rebase trigger now scales to one eighth of the authority under a
  launch-time disk-derived 64 MiB..8 GiB ceiling instead of copying an 87 GiB
  database after every fixed 64 MiB of WAL growth. Canonical fastpath backups
  now force and pin one checkpointed shadow generation (hard link on the same
  filesystem, sequential copy across devices) instead of running a second
  page-wise live backup; integrity, checksum, atomic publication, deadline,
  and single-file restore guarantees remain. `serverctl doctor` now reads this
  Sidecar manifest authority and separately reports the retired
  `db_snapshots/` published/interrupted footprint without deleting it.
- Bounded the BeeGFS/FUSE keepalive runtime. Its 15-second safety cadence now
  uses one interruptible Event deadline instead of thirty half-second sleeps,
  reducing quiet timer wakeups from 172,800 to 5,760 per day (96.7%). One
  persistent serialized probe replaces two newly-created stat threads per
  cycle (11,520 thread starts/day), and a kernel-stuck stat remains the sole
  in-flight generation instead of leaking more daemon threads after every
  timeout. Composite shutdown is bounded and refuses a duplicate until that
  exact probe has returned.
- Made desktop LAN discovery event-driven and lifecycle-owned. The responder
  now blocks on UDP-or-private-wake socket readiness instead of timing out every
  0.5 seconds (eliminating 172,800 idle application wakeups per day), starts at
  most one exact owner per request process, rolls back partial socket/thread
  startup, and wakes plus bounded-joins during normal server shutdown.
- Replaced the deterministic Git integration queue's fixed three-second idle
  Sidecar poll with generation-aware adaptive deadlines. Durable local
  submit/retry operations wake the worker immediately; a continuously empty
  queue backs off through 3, 6, 12, 24, 48, then 60 seconds, retaining a
  one-minute cross-process and abandoned-claim discovery bound while reducing
  empty reads from 28,800 to at most 1,440 per day (95%). Storage failures keep
  their independent 5..30-second circuit delay, and the worker now has a
  bounded exact-owner shutdown contract so timed-out generations cannot be
  duplicated. Only a lifecycle-armed task-worker process may start it, so a
  future API-only replica can persist work but cannot become a Git executor.
- Consolidated logging maintenance around actual work. Core and registered
  external retention now share one upgradeable 15-minute runtime (standalone
  launchers retain external-only coverage); the duplicate-tail worker exists
  only from the first suppressed delta through its exact quiet checkpoint and
  then releases retained record payloads; and an empty aggregate store sleeps
  to its hourly TTL boundary while new rows still flush on the original
  15-second batch window. On the sampled external-console server this reduces
  periodic logging workers from four to two and quiet wakeups from 968 to at
  most 5 per hour (99.4%+), without changing QueueListener isolation, exact
  occurrence deltas, failure backoff, final shutdown flush, or text-log
  authority.
- Added semantic, restart-safe backoff to remote provider model catalogues. One
  sampled provider had preserved its 44-row last-good catalogue through 80
  consecutive empty `/models` responses but still retried every six hours.
  Each provider now persists `next_attempt_at`: exact unchanged snapshots back
  off from 6 to 12 hours (50% fewer steady requests), failures back off through
  12, 24, then 48 hours (87.5% fewer at the ceiling), and the worker sleeps to
  the earliest provider deadline instead of waking at the base cadence. Model
  additions, metadata changes, and first-snapshot pending removals reset to the
  six-hour confirmation floor; Settings Save remains an immediate forced wake,
  last-good/lease/stale-result fences remain intact, and connection changes
  clear all old-account deadline state.
- Made the project-presence TTL sweeper batch-scoped. Presence is process-local
  and starts empty, yet boot recovery previously started a thread unconditionally;
  the sampled worker still retained it long after the final stale peer was
  reaped. Empty startup now owns zero presence threads, the first announce starts
  one bounded shared sweeper, and the worker releases its exact generation when
  the last peer disappears so a later announce can create a new batch safely.
  The 25-second active / 180-second idle TTLs, owner filtering, conflict advice,
  push failure semantics, and shutdown join remain unchanged; invalid intervals
  are bounded and a failed thread start rolls ownership back without failing the
  task's ephemeral announce path.
- Replaced netpath's fixed three-minute, dual-path sweep with per-path adaptive
  deadlines. The sampled live state held 12 active hosts, 11 proxy decisions,
  and 68,353 accumulated direct-path failures while the worker was still making
  fresh HTTPS probes. New paths and passive request failures still wake the
  worker immediately; repeated failures back off through 3, 6, 12, 24, 48, then
  60 minutes, healthy real traffic postpones redundant probes, and persisted
  last-use timestamps no longer reactivate every historical host for 24 hours
  after restart. A deterministic day-budget test cuts one stable host from 960
  active requests to at most 55 (at least 94.3%) while retaining hourly recovery
  detection; intervals have a 30-second floor and six-hour hard ceiling.
- Moved yesterday-report backfill onto the existing owner-scoped durable
  scheduler and removed the dedicated `daily-report-scheduler` thread that
  otherwise slept for six hours after its startup pass. The personal built-in
  runs at 00:00/06:00/12:00/18:00 local time, rebuilds only
  `reports:maintain` for the task row's owner, and executes off the 30-second
  scheduler tick. On boot, a bounded process-local hint is queued only when
  yesterday is actually missing; the Sidecar claim remains the duplicate-run
  authority. The historical backfill service, LLM analysis, atomic report
  save, failure visibility, and legacy start/stop imports remain intact, while
  distributed ownerless composition still installs no personal report task.
- Consolidated local-provider health and well-known-port discovery onto one
  lifecycle-owned monitor, removing the always-resident `local-autodiscover`
  thread. The sampled live process made 724 model-list requests across 362
  identical “Ollama answers but serves no models” cycles: known engines now
  start directly at `/v1/models`, and an open-but-empty endpoint backs full
  HTTP discovery through 2, 4, 8, then 15 minutes while cheap TCP topology
  checks remain every two minutes. A newly opened port or explicit Settings
  change still probes immediately, repeated empty state logs collapse to one
  transition, and the former start/stop API remains a thread-free compatibility
  facade. At steady empty state this cuts the default 1,440 requests/day to 96
  (93.3%) without delaying detection that a local service has started.
- Consolidated orphaned billing-reserve recovery onto one durably claimed
  scheduler task. The live open-mode personal server completed 379 scheduled
  Sidecar sweeps across the sampled logs and every result was `0/0`; a second
  silent `billing-janitor` thread queried the same operation on its own
  five-minute loop. Open/private and billing-disabled deployments now
  reconcile the built-in disabled and start no janitor thread. Active
  multi-user billing retains one five-minute crash-recovery cadence, the same
  idempotent release path and 30-minute TTL, now with the running-task guard
  moved into the canonical sweep. The legacy API and TTL environment name
  remain compatible without owning a second lifecycle.
- Scoped the swarm registry's five-minute cleanup timer to live sessions. The
  optional integration previously launched a recursive `threading.Timer` at
  import and kept one rotating thread resident even when no swarm had ever
  run. Import is now thread-free; the first session starts one shared timer,
  the last removal cancels it, and exact-generation checks prevent a canceled
  callback from resurrecting or detaching a newer timer. TTL, producing-agent
  protection, durable cleanup, and the 20-session ceiling are unchanged.
- Replaced the authenticated Codex model catalogue's fixed three-minute poll
  with semantic adaptive backoff. The live personal server logged 528
  successful refreshes of the same nine-row directory in roughly 27 hours;
  unchanged HTTP 200/304 responses and failures now step through 6, 12, 24,
  48, then 60 minutes, while login or a real normalized-row change resets to
  three minutes. Steady-state request volume falls from 480 to 24 per day
  (95%) without increasing the one-hour cache-freshness bound. Launch without
  a Codex credential creates no worker, credential removal ends it, and
  distributed roles decline the legacy ownerless token until an owner-scoped
  OAuth/catalogue repository exists.
- Made online-skill verification workers batch-scoped without weakening the
  process-wide ceiling. Concurrent explicit ClawHub searches still share one
  four-worker generation; the final active-batch lease now waits for bounded
  leftovers, closes every thread, and only then permits a lazy replacement.
  One Settings/model search therefore no longer leaves four `skill-online`
  workers resident until process exit, and diagnostics expose active batches
  plus live workers.
- Added idle retirement to the shared project refresh lane. Summary, status,
  and watch each previously retained two daemon consumers forever after their
  first event, even though their work is reconstructible and already bounded
  and coalesced. They now expose live/start/retirement counters, atomically
  arbitrate submit versus timeout exit under one condition, and rebuild their
  full capacity after a launch-probed 30..300-second personal quiet window
  (600 seconds distributed; explicit zero keeps the consumers resident).
- Made the two-worker project tree-index builder batch-scoped. A background
  warm previously left its `tree-index` executor resident forever after the
  filesystem walk and local index publication completed. Concurrent roots
  still share the same two-build ceiling and each build retains its bounded
  inner walk, but the exact executor generation now exits when `_building`
  reaches zero; stale refreshes recreate it lazily. An operational snapshot
  exposes active builds, executor state, and live worker count.
- Made subscription-route probe workers batch-scoped. A single cold route
  race previously left every thread it had grown (four in the observed live
  process, with a bounded maximum of 32) resident for the rest of the server
  lifetime. Per-host/route singleflight and the 32-way active race are
  unchanged, but the last exact-generation future now atomically detaches its
  executor and lets all workers exit; the next stale or cold route batch
  recreates capacity lazily. Topology reset also retires the old generation,
  whose late results remain unable to mutate current health.
- Added bounded idle retirement for the serving loop's burst-grown worker
  generations. The live process retained all 16 `tofu-sync` and eight
  `tofu-agent` threads after their work ended; CPython's executor workers never
  expire, and the process also showed many resident 8..65 MiB anonymous
  mappings. The loop now tracks local pending/active work (including
  cancellation before worker entry), retains at most two warm sync threads and
  zero idle agent threads, and publishes an equal-capacity lazy replacement
  only after a launch-probed 300..1,800-second personal quiet window and an
  `active=queued=0` boundary. Accepted old work still drains, future work uses
  the new generation, and metrics expose resident/retired threads; distributed
  mode uses 3,600 seconds and explicit zero disables retirement.
- Made the launch-probed numeric worker budget a real process-wide ceiling.
  The live server inherited `OMP_NUM_THREADS=62` while
  `TOFU_NUMERIC_THREADS=4`; because library-specific variables previously won
  unconditionally, a later NumPy/OpenMP import could still create a host-sized
  native pool. OpenBLAS, OpenMP, MKL, and NumExpr values are now clamped to the
  canonical 1..32 budget before optional imports, while smaller deliberate
  per-library limits remain intact.
- Added a bounded idle lifecycle for local MCP stdio process trees. The live
  personal server had seven npm/uv MCP groups resident for about seven hours,
  together holding roughly 1.07 GiB RSS even though only one credential probe
  ran every 15 minutes. Startup still discovers each authoritative tool
  catalog once; after a launch-probed 180..600 second personal idle window the
  owner task, pipes, launcher, and child exit while the small catalog/config
  snapshot remains logically connected. The next real tool call performs one
  serialized transparent reconnect, revalidates its catalog, and refreshes
  activity. Long calls hold an in-flight lease, remote transports are excluded,
  parked servers are skipped by liveness and credential sweeps, and the API
  reports `parked=true`. MCP maintenance no longer leaves the event loop's
  implicit `asyncio_0` worker resident after its first parking/reconnect;
  bounded calls own a transient one-worker generation, while periodic
  credential probes use per-server singleflight one-shot threads. Distributed
  mode uses an explicit 1,800-second warm window and every override is capped
  at one day.
- Added a physical budget gate for new SQLite recovery copies. The live data
  tree currently allocates about 637.6 GB including its backup subdirectory;
  retained verified backups plus the last deep-clean rollback point account
  for 463.5 GB. A new 87.6 GB same-volume snapshot would project recovery
  copies to 551.1 GB even though the shared 22 PB filesystem reports ample
  free space. Personal defaults now reserve at most half of the launch-probed
  data volume for recovery copies, with a 4 GiB floor, 512 GiB zero-config hard
  cap, 64 GiB lean probe-failure fallback, and an explicitly bounded override;
  distributed mode has a separate 1 TiB default. Backup admission counts
  allocated verified snapshots and retained rollbacks only when they share the
  target device, so an explicit NAS/Compose backup mount keeps its own budget.
  Rejection happens before a temporary copy is created and reports retained,
  projected, and limit bytes; no authority, verified backup, or final rollback
  point is automatically removed.
- Added bounded online self-healing for pre-typed task events on established
  SQLite authorities. The live index-only census found 3,365,519 blank-type
  rows out of 3,589,168 task events (93.8%); because blank metadata fails
  closed into the 30-day structural class, historical streaming deltas never
  reached their six-hour TTL. All 2,000 oldest/newest payload samples decoded
  successfully and 95.5% were deltas. After ordinary typed backlog drains,
  the legacy retention transaction now classifies one indexed page at a time:
  expired streaming rows are deleted, structural rows receive only recovered
  type/kind metadata, and opaque rows are retained with an internal progress
  marker. The ordinary maintenance caller keeps its 25-row commit page; stored
  payload materialization has an independent 4 MiB budget (one oversized row
  may progress under the existing 64 MiB event ceiling), and the SQL plan is
  pinned by tests to the old retention index with no temporary sort. Project
  streams and payload authority are unchanged; reclaimed pages can be reused
  even where network-storage policy correctly forbids online file compaction.
- Stopped legacy task-event retention from pre-discovering every event type on
  every 25-row page. The 87.6 GiB live authority has only the compatibility
  `(stream_kind, event_type, created_at_ms)` index, 32 task event types, and
  51,151 expired streaming rows. Across the retained Sidecar console history,
  `event.prune` was the writer holder for 612 of 1,103 parsed acquisition
  timeout diagnostics, including 113 durable event batches and 21 task-result
  checkpoints. Compatibility discovery now deletes each exact type as it is
  found and returns immediately after the first non-empty page; for the live
  backlog's leading type this reduces type-discovery seeks from 32 to two while
  preserving the 25-row transaction, 64-type fail-closed ceiling, structural
  classification, and separately committed writer-fair paging.
- Returned freed long-task and Sidecar heap arenas to the OS at bounded idle
  lifecycle edges. A live personal process rose from about 0.48 GiB to 2.6 GiB
  RSS while terminal persistence was already nulling full message/endpoint
  snapshots; after shared-cgroup pressure subsided, no later owner called
  `malloc_trim`. Terminal releases now set one process-wide flag consumed by
  the existing 60-second task-maintenance tick, coalescing any number of
  completions into one trim outside the persist path. The idle Sidecar was
  separately holding 572 MiB of anonymous private memory despite a 32 MiB
  SQLite writer cache; it now trims only on a zero-active-RPC edge, above a
  profile-derived RSS threshold and at most once per five-minute cooldown.
  Sidecar metrics publish attempts, successes, reclaimed bytes and last
  before/after RSS; no worker, queue, storage, or task concurrency limit was
  increased.
- Connected adaptive compaction's expected remaining-round horizon to both
  exact Layer-2 cache-ROI checks. The opt-in strategy previously admitted a
  positive-value candidate with its bounded six-round default, then silently
  re-applied the fixed policy's one-round gate inside the summarizer. In the
  current 3,812-round production cohort, 31 tasks crossed 128K input tokens;
  90.3% continued for at least three model rounds and the median continued for
  30. Adaptive preflight/adoption and their retry lower bound now use the same
  request-local horizon. Fixed compaction uses its separate bounded observed-
  survival horizon described above. Receipts expose the chosen horizon and
  policy.
- Kept local multi-agent lifecycle schemas atomic under an explicit tool-schema
  budget. The final fitter previously required `spawn_agents` but could hide
  `await_agents` and `get_agent_result`, forcing an avoidable discovery round
  after work was already launched. All three controls now remain visible as a
  functional floor; execution authority and the default uncapped tool surface
  are unchanged.
- Added resource-profiled, digest-only reuse for repeated large-text token
  counts. Stable tool schemas and compaction projections previously re-ran the
  tokenizer every model round even though 98.0% of 2,544 observed schema
  transitions were byte-stable. An 80,099-byte production catalog measured
  6.13 ms per warmed uncached count and 0.059 ms per cache hit (about 104×).
  Entries retain only encoding, length, SHA-256 and count—not prompt text;
  strings below 4 KiB bypass the cache. Personal capacity scales from the
  launch probe, distributed defaults to 1,024, and every override is capped at
  4,096 entries.
- Added lossless private compression for large durable task-event payloads.
  Forty-eight recent `messages_snapshot` rows occupied 49,929,821 bytes; zlib
  level 1 reduced them to 15,674,269 bytes (68.6%) with 12.17 ms median encode
  and 4.76 ms median decode time for a roughly 1.13 MiB frame. Canonical JSON
  below 64 KiB remains byte-identical, and a larger payload uses the envelope
  only when it is smaller. Semantic event reads and natural-key replay remain
  unchanged across SQLite and PostgreSQL; decoded/stored forms stay within the
  64 MiB RPC ceiling, while truncated, corrupt, length-mismatched, or unknown
  codecs fail closed as storage integrity errors.
- Extended physical offline deep clean into one bounded maintenance pass over
  the active `storage_events` transport. The 3,365,519-row v21 migration cohort
  still has blank projected types, but 8,191/8,191 rowid samples carried a
  parseable top-level type. The sample projects 3,317,857 streaming rows
  (1.44 GB payload) as immediately TTL-eligible and 47,662 structural rows
  (13.24 GB payload) as retained; zlib level 1 reduced the structural sample
  to 24.8%, an estimated additional 9.96 GB saving. The 4,096-row keyset page
  rides `INTEGER PRIMARY KEY (rowid>?)` and measured 9.59 ms for 2.36 MB on the
  live authority. Each write page stays within 4,096 rows and 64 MiB stored
  payload, then checkpoints the WAL. Explicit types use canonical 6-hour/30-day
  TTLs; retained blank rows recover their type/kind and large retained legacy
  JSON receives the new codec. Opaque or malformed blanks keep the conservative
  structural horizon, non-task streams are untouched, repeat passes make no
  writes, and `--no-vacuum` skips the pass entirely. Read-only analysis now
  reports exact encoded payload bytes and actual row counts instead of treating
  sparse `max(rowid)` spans as live rows; it includes a capped exact event-type
  breakdown and rowid-hole evidence. One shared 60-second SQLite progress
  deadline returns explicit partial results on a slow authority, bounding the
  diagnostic's own I/O cost.
- Reclaimed bounded, expired transport history from the frozen pre-Sidecar
  `attempt_events` and `task_events` tables during physical offline deep clean.
  A live authority held 90,055 legacy attempt frames with a sample-estimated
  26.7 GiB table weight; preserving only the newest frame for each of its 19
  attempts retains 8.36 MiB of payload while making about 90,036 older frames
  reclaimable. The same authority held 1,042,953 streaming task frames
  (248.35 MB payload) and 281,402 structural frames (4.28 GB payload).
  Attempt cleanup only removes an expired frame when a newer sequence exists;
  task cleanup consumes the canonical 6-hour streaming and 30-day structural
  policy, and durable `task_results` are never targeted. Each transaction is
  capped at 900 rows and 128 MiB of UTF-8 payload, checkpoints the WAL, and
  runs only when verified-copy or low-space reclaim will return the pages;
  index-only `--no-vacuum` windows leave the frozen tables byte-identical.
- Bounded verified-copy deep-clean rollback artifacts instead of accumulating
  one full authority per maintenance window. A live retained rollback occupies
  436,914,016,256 logical bytes / about 407 GiB allocated, while the compact
  authority is about 82 GiB. Successful future publications now keep one
  rollback by default (configurable with a hard ceiling of four) and only then
  retire older copies. Read-only analysis reports exact logical/allocated
  weight and age, verified SQLite backup weight, and a 256-entry shallow list
  of operator-managed root artifacts above 1 GiB; an explicit stopped-server
  `--retire-rollback <basename> --confirm` path rejects traversal, links,
  live-authority aliases and WAL/SHM
  companions, quick-checks the current authority, fsyncs the exact unlink and
  verifies the postcondition. Offline publication now also proves every
  second-stamped candidate/rollback name and companion absent before mutation;
  failure cleanup is ownership-gated, so a same-second file or dangling link
  is rejected rather than overwritten or unlinked. No current recovery point
  is deleted automatically.
- Kept large idempotent Sidecar commands replayable without increasing durable
  receipt growth. A settled-turn response duplicated its projection between
  the public result and post-commit sync notice, so an 18,000-character tool
  result produced 74,570 bytes and the previous 64 KiB receipt check rolled the
  whole mutation back. Private receipts above 64 KiB now get one zlib level-1
  attempt: the sample stores in 1,717 bytes in 0.216 ms, while a 362,570-byte
  response stores in 3,184 bytes in 0.642 ms. Stored receipts remain capped at
  64 KiB, decoded responses at 4 MiB, legacy small JSON is byte-identical, and
  corrupt, incompressible, or over-budget data still fails closed.
- Added lossless, storage-only interning for duplicated tool arguments and
  results across turn ``segments`` and ``toolRounds``. On a live 196-tool
  long-task projection, canonical JSON fell from 3,794,888 to 2,709,283 bytes
  (28.61%, or 1,085,605 bytes). The codec transform averaged 0.368 ms to encode
  and 0.414 ms to hydrate; end-to-end validated dump time stayed effectively
  flat (12.798 vs 12.815 ms) while parse-plus-hydrate fell from 10.906 to
  5.946 ms because less JSON is parsed. Only byte-equal fields with a unique
  ``toolCallId`` are referenced; semantic reads restore the original public
  projection, malformed codec metadata fails as storage integrity, and
  existing rows migrate lazily on their next normal write without a startup
  rewrite. Recovery chunking now charges a backend-neutral hydrated byte upper
  bound instead of counting compressed SQLite bytes or PostgreSQL JSONB keys.
- Bounded root model API rounds by deployment profile instead of treating an
  unset or zero `maxApiRounds` as unlimited. One failed long-task sample made
  150 model calls, accumulated 21.29 million input tokens and 9,757 seconds of
  provider time, and recorded $8.46 before ending without actionable output.
  Across the newest 100 conversations, 941 turn attempts had a median of two,
  p95 of 50, p99 of 112, and maximum of 177 calls; several valid completions
  exceeded 128. Personal and distributed defaults are therefore 192 and 512,
  explicit requests remain capped at 1,024, and one model-visible finalization
  reminder fires with at most 64 rounds left. The hard gate runs before the
  next provider call and retains the existing typed budget-failure settlement.
- Made dynamic large-response compression topology-aware. On a 3,643,636-byte
  live rendered conversation, personal-mode Brotli q2 took 15.8 ms versus q4's
  26.28 ms while remaining 476,734 bytes; gzip level1 took 33.86 ms versus
  level6's 98.13 ms while remaining 975,060 bytes. Only uncached dynamic bodies
  at or above 1 MiB use the low-CPU personal profile. Smaller responses,
  distributed bandwidth policy, static cache quality, off-loop execution, and
  wire semantics remain unchanged.
- Coalesced simultaneous owner/conversation sync snapshots before their
  Sidecar read, JSON parse, validation, and stable-segment projection. A live
  long-task snapshot reached 3,018,932 bytes; on an equivalent 2,981,387-byte
  synthetic shape, four independent parses took 20.09 ms median versus 4.72 ms
  for one parse plus four request envelopes (76.5% less parse/envelope CPU).
  The contract-validated HTTP encode now uses compact `orjson`; a current
  3,643,636-byte live rendered document encoded in 1.83 ms median instead of
  25.2 ms (13.8x), with the framework encoder retained as a fail-soft fallback.
  The 8 ms, launch-budgeted gather closes before authority execution, retains
  no completed result or TTL, fails open at saturation, and keeps HTTP
  responses plus request-time `pushWithheld` hints isolated.
- Coalesced simultaneous owner-scoped conversation-catalog arrivals before
  their Sidecar read. Four live browser tabs repeatedly produced four identical
  41.4 KB list responses in one second; arrivals inside the 8 ms gather window
  now perform one metadata query while retaining four independent HTTP
  responses. The launch-budgeted registry closes before query execution, holds
  no completed result or TTL, separates owners/projection shapes, and fails
  open at saturation, so later refreshes cannot inherit an older snapshot.
- Stopped argument-changing agent loops on stable, explicit tool authority
  failures. One sampled browser worker spent 78 model rounds, 547.1 seconds,
  and 3,156,164 tokens while 28 `browser_execute_js` calls all hit the same
  missing write grant because changing tab IDs defeated the exact-call guard.
  Browser access and write-grant denials now return canonical V2
  `retryable=false` errors; the shared chassis halts after three consecutive
  all-tools rounds with the same terminal code and spends at most one final
  tool-less round reporting the limitation. Legacy, malformed, retryable,
  mixed-success, and changed-code rounds fail open and reset this new streak;
  the pre-execution identical-call guard remains unchanged.
- Replaced task-event retention's event-type-leading scan/sort with mutually
  exclusive age-only partial indexes and bounded cadence. On the 82 GiB live authority,
  an empty tier probe previously took about 2.64 seconds and both tiers ran
  every 15 seconds; drained tiers now run every five minutes, backlog runs every
  30 seconds, and streaming drains before structural retention. Existing
  SQLite authorities with the old compound index now use exact-type, 25-row
  indexed pages (at most 64 type seeks) until offline deep-clean installs v2
  and retires v1; the measured authority had 45,077 expired streaming rows
  (33.76 MB) that can now drain without a table scan. Its 32-type empty-tier
  probe took 13.2 ms cold and 0.188 ms warm median, versus about 2.64 seconds
  for the retired cross-type plan. `storage_deep_clean.py --analyze` reports
  the active retention mode and compatibility limit. Authorities with neither
  index and abnormal type cardinality still fail closed; the external
  PostgreSQL migration owns the same index transition.
- Coalesced provider text microchunks before durable task-event sequencing:
  the first chunk remains immediate, while later chunks have a 100 ms / 256
  character ceiling and flush before every structural or terminal boundary.
  Two production long-task samples project 55.5–64.4% fewer text-event
  transactions (median chunk size: four characters). The buffer and its one
  daemon worker are scoped to an active model stream and reclaimed on both
  success and failure; tool pre-execution keeps its original callback identity.
- Coordinated typed shared-project TPM 429 retries per provider/model instead
  of letting every task poll every 0.3 seconds. Production evidence contained
  239 upstream rejections across 87 rounds (152 repeat probes, with as many as
  12 in 14 seconds); the first retry remains at 0.3 seconds, later probes are
  spaced one second apart and capped at three seconds without cooling slots,
  polluting health, disabling provider fallback, or losing cancellation and
  queue-wait accounting. Quiet state is reclaimed and the coordinator has a
  256-family memory ceiling.
- Replaced proactive L2 compaction's fixed 8K/5% retry hysteresis with an
  optimistic break-even token bound. A replay of 64 observed warm-cache
  declines drops candidate rebuilds to 9, while a cooling cache, explicit or
  reactive request, and the real context-window safety gate still reconsider
  immediately.
- Kept the local `execute_tools` and read-only `spawn_agents` provider schemas
  byte-stable when per-round programmatic-read evidence activates or expires.
  The policy remains task-owned, while fixed conditional guidance prevents a
  telemetry transition from invalidating the entire cached prompt prefix.
- Reused the automatic L2 preflight measurement across retry hysteresis,
  summary preflight, successful-compaction analytics, deterministic fallback,
  and the token-budget reminder, removing repeated full-transcript scans on
  unchanged rounds. The token-counter's broad heuristic prefilter is now lazy
  on usage-cache and local-tokenizer hits and reused by its fallback; cache hits
  also verify a bounded tail without copying the full recorded prefix. Three
  consecutive verified-cold rounds now supersede a stale durable warm-cache
  baseline; the existing one/two-round transient-miss guard remains intact,
  and expected economic declines no longer emit a false summary-failure warning.
- Made the every-round L1 context pass read-free when it has no mutation and
  deferred its conversation-sized telemetry token count until a real tool
  result is compacted. Cold image placeholders now persist through the owning
  Turn, so base64 payloads do not reappear on later request rebuilds; no-op
  diagnostics moved from INFO to DEBUG.
- Reduced the always-loaded browser shell by moving Task Mode/orchestration and
  provider-settings presentation into feature-owned stylesheets, while keeping
  source manifests and byte budgets authoritative. Project status, summary,
  watch, translation, and undo work now use bounded/coalescing queues or LRU
  buffers whose defaults come from the launch-time resource profile.
- Tightened the retained browser-runtime debt ratchet to 3,652,967 bytes after
  reusing the canonical icon registry and replacing bundled finish, tool,
  swarm, and collaboration-bar history with concise rendering invariants and
  pruning redundant inline UI narratives;
  newly added accessibility, Skills API, and tool-result behavior remains
  inside the smaller budget.
- Split the largest turn-finalization and stream-settlement routines along
  named semantic boundaries and tightened their executable size ratchets.
  Conversation search now runs through an independently bounded, replayable
  Sidecar projection so user writes never wait for search materialization.
- Added an adaptive, owner/workspace/interpreter-scoped local CPython bytecode
  cache for repeated Python workloads on network filesystems. It preserves the
  real subprocess and command, avoids cold-prefix cost for one-shot scripts,
  is bounded by launch-time disk budgets plus LRU/TTL cleanup, and can be
  forced with `TOFU_RUN_PYTHON_CACHE=1` or disabled with `=0`.
- Extended declarative plugin storage with version-witnessed put/delete,
  bounded atomic batches, prefix pagination, and a manifest-pinned read-only
  legacy scan. This enables sidecar-native plugin cutovers without exposing a
  driver, database path, transaction handle, or plugin-supplied SQL.
- Retired `list_dir` from new model tool epochs and route simple `run_command`
  `ls` requests through its bounded local/remote directory reader. Removed the
  hidden Kimi-K3 4,000-token schema cap: every model is uncapped by default,
  while an explicit local Tool Search budget is model-neutral, applied once at
  the final projection, preserves a non-negotiable coding-tool floor, and keeps
  a discovery path for every omitted executable tool.
- Added one durable, append-ordered activity timeline to each Turn for model
  requests, retry/wait status, real tools, malformed-tool isolation, errors,
  and model switches. The projection is capped at 128 rows / 96 KiB, survives
  reconnect and cold reload, reuses rich tool rendering, and removes duplicate
  tool, live-status, and fallback-footer presentation without creating fake
  tool calls or adding diagnostics to model context.
- Added an opt-in transactional logical recovery stream for SQLite and
  PostgreSQL: encrypted same-transaction mutation capture, bounded asynchronous
  publishing to private or explicitly shared POSIX storage, crash-idempotent
  segments, capacity backpressure, atomic checkpointed replay, projection
  verification, and fail-closed canary/cutover gates. The database remains the
  authority unless an operator separately supplies and passes promotion
  evidence.

### Fixed
- Kept obsolete browser extensions fail-closed while removing their repeated
  protocol-upgrade poll 426 rows from the application incident plane. The
  first structured rejection and the access-log record remain authoritative,
  while a stale client can no longer fill `app.log` and `error.log` every
  three seconds before it is upgraded.
- Repaired the pre-deliverable watchdog so normalized non-blank reasoning
  renews a rolling semantic-stall window instead of being killed by total
  request age. Keep-alives, blank reasoning, signatures, and protocol metadata
  cannot fake progress; text/tool output permanently disarms the watchdog.
  Real stalls retain one alternate-slot retry per uninterrupted streak,
  `no_actionable_output`, and `autoRetryExhausted`, while active long reasoning
  consumes none of that budget. Diagnostics now separate request duration from
  last-progress age and reasoning size/chunks; attempt logs reserve `OK` for
  typed `provider_finished` streams. The empty abnormal-stream recovery path is
  a named semantic boundary with its tightened size ratchet.
- Enforced explicit owner identity across Paper, Slides, Swarm, VLM, Video,
  tool artifacts, and test fixtures; repaired Swarm terminal-event ordering,
  provider-stream truncation handling, cache-usage normalization, and
  conversation-project restore harnesses. Unit tests now isolate memory stores
  from operator data and keep live published-repository drift checks out of the
  deterministic unit lane.
- Made repository verification deterministic across asynchronous conversation
  search projection, skill-install approval/task cleanup, generated runtime and
  stylesheet freshness, and no-change poison detection. Search assertions now
  wait on semantic visibility, standalone renderer harnesses declare their icon
  dependency, and mutation tests operate in disposable repositories.
- Managed restart now gives a just-stopped child Sidecar a bounded 10-second
  lease-drain window before applying the existing second-authority fence. This
  removes transient false 409 failures while keeping unknown and maintenance
  lease holders fail-closed. Trading's migration/startup summary is also
  routed into the retained business log instead of losing INFO records in the
  third-party vendor stream.
- Repaired the Turn-native chat surface at its DOM contract boundary: every
  main-lane Turn once again owns the retained avatar/content/context-rail
  shell, narrow panes receive the inline context fold, empty prose/actions/
  finish shelves take no space, and compact branch lanes do not inherit the
  main transcript furniture. Proposed plans now keep translated and streaming
  translated bodies inside their source Turn with a conversation-bound
  decision shelf. Provider-bound tool schemas reject same-level
  `required`/`properties` mismatches before Moonshot/Kimi. Deterministic
  400/404/422 request rejections no longer masquerade as model outages or
  trigger fallback, while 429 capacity limits keep waiting by default; a
  positive `TOFU_429_SATURATION_SECS` remains an explicit operator override.
- Made the Terminal-Bench 2.1 runner selectable across Tofu, DeepSeek Minimal,
  Codex, and Claude Code with private ATIF-v1.7 trajectory collection, strict
  route/provenance scoring, and layered failure attribution. Rootless QEMU
  preparation and trial defaults now derive a conservative bounded concurrency
  from one CPU/memory/headroom/disk probe. DeepSeek Minimal 1.0.2 terminates the
  complete guest process group after Bash timeouts; affected 1.0.1 trials fail
  closed as harness-invalid, while only audited non-timeout 1.0.1 paths remain
  score-compatible. Collected ATIF copies also normalize zero-valued token
  aliases from the matching redacted host audit without rewriting source runs.
- Root-corrected recurring SQLite writer stalls on oversized network-mounted
  authorities. The Sidecar now labels the blocking transaction phase, exports
  commit/queue/fast-path metrics, applies an adaptive bounded writer cache, and
  restarts in bounded time when kernel-blocked commit I/O cannot be
  interrupted. Bulk freelists are routed to verified offline compaction (which
  now installs deferred indexes), log-aggregate retries/sweeps cannot flood the
  writer lane, fast-path seeding capacity-checks the whole authority, and
  ineffective shared-cgroup relief enters a bounded cooldown.
- Tightened the stability boundary across startup, task dispatch, and the chat
  shell. Managed startup now distinguishes process liveness from dependency
  readiness and fails closed on missing frontend artifacts; rejected task
  submissions settle durably instead of leaving ghost-running work. During an
  active turn, draft-bearing composers expose independent Send and Stop touch
  targets, lost command acknowledgements recover from authoritative attempts,
  and default shell titles, dates, placeholders, and hints follow the selected
  locale without rewriting stored conversation data. Cold conversation
  hydration now claims both its runtime lane and coordinator snapshot flight
  before publishing connection health, so a health-render re-entry joins one
  read instead of recursively launching full snapshots. The observed failure
  coalesced roughly 929/933 browser stack-overflow reports from two clients;
  two sampled hours recorded a lower bound of 126 heavyweight sync responses
  and 181.9 MB returned. Historical turn-search repair also yields the first
  startup minute to user and recovery writes. Explicit starts during offline storage
  maintenance now enter a visible queued state instead of spawning a Sidecar
  crashloop, then resume automatically when the project lease is released.
  A paused worker crash-loop is now latched without recounting the same absent
  process on every manager poll.
- Restored the conversation-title lifecycle after the Turn-native frontend
  migration: new shells no longer persist the `New Chat` display placeholder,
  successful main-lane assistant and planner settlements once again invoke the
  optional AI title generator, and catalog refreshes retain the derived title.
  Agent Mode's saved-workflow pickers now keep loading/failure notices mutually
  exclusive with the empty state on desktop and mobile; keyboard opening waits
  until the animated menu can receive focus.

## [0.17.0] - 2026-08-25

### Changed
- **Browser conversations now have one normalized Turn document and one DOM
  owner.** Conversation Sync v3 feeds `ConversationTurnStore`, typed selectors
  feed `ConversationSurface`, and catalog/IndexedDB shells carry metadata only.
  Rendering, streaming, diagnostics, actions, scroll state, branches,
  translation, plans, artifacts, cost, and swarm projections use stable Turn
  or block identities instead of a parallel `messages` array. Conversation
  settings now persist through one Turn-independent metadata path with
  transport-owned idempotency keys; local drafts remain cache-only until their
  first accepted Turn. Long histories retain every Turn in the store while a
  Surface-owned, anchor-preserving DOM window bounds rendered nodes. Large
  application-shell structures now live in closed-set frontend fragments
  assembled by a behavior-free server seam.
- **Long-agent Kimi execution now has an opt-in, paired Codex comparison
  contract.** Global context plans and rebuildable task state bound repeated
  history; compiled tool contracts, 8k/24k result envelopes, and owner-scoped
  expiring CAS artifacts bound schemas and tool output; adaptive compaction and
  four-shape orchestration are isolated experiment arms. A loopback-only
  Responses→Kimi proxy pins Codex 0.149.1's 272k/244.8k local-compaction
  baseline, handles namespace tools, and measures translation CPU separately
  without extra model calls. Failed artifact persistence returns a bounded
  honest preview instead of an unusable recovery reference; turn-native manual
  compaction stores one public authority block and projects private v1 markers
  only for legacy readers. The compatible benchmark v2 contract freezes
  the 1,845-task matrix, Kimi pricing, paired quality/family/safety gates, and
  85% cost/P90 thresholds. Request-owned ToolContractV2 documents now perform
  the final pre-dispatch validation for root, nested gateway/ToolScript, SSE
  speculative-read, and swarm paths; missing v2 documents fail closed and
  typed refusals remain rejected through settlement. Timer polls and every
  full/research paper runner now freeze the actual model-visible tool epoch and
  use the same documents for execution; dynamic policy removal and malformed
  arguments cannot reach an unattended backend. Shipped Paper result ingress
  uses 8k/result and 24k/round V2 envelopes; oversized evidence is recoverable
  only through owner-scoped semantic artifact range/search tools, never a disk
  path, and duplicate/empty call IDs cannot evade the aggregate cap. The
  registered result-envelope control can still reproduce the bounded legacy
  baseline instead of being silently upgraded to V2. Explicit long-agent Paper
  requests are model+config fingerprinted, never join another arm, and bypass
  canonical report/deepen cache reads, writes, and second-pass mutations. Paper
  Q&A aborts also remain aborted instead of being projected as done. Kimi prompt
  arms now retain requested/resolved/effective profile, content SHA-256 and
  token proof per request/round; missing or mismatched adoption rejects the arm.
  Orchestration telemetry now separates wire projection from actual adoption,
  retains reason and expected savings, and derives its status only from real
  program/agent/model trajectories. Benchmark v2 rejects contradictory task
  evidence, and the final release gate requires both program and agent traces.
  A new read-only release compiler refuses to create the formal 1,845-task
  manifest unless the pinned SWE/TB catalogs and all five content-hashed,
  path-confined private custom packs are complete; immutable output cannot be
  silently overwritten or substituted with count-only placeholders. The SWE
  catalog is locked at all 500 name-to-digest pairs, and registry preflight,
  definition-cache loading, and pure compilation reject same-count drift. A
  private, bounded per-arm run store now freezes pair/role/experiment arm,
  rejects cross-arm or unresolved task records, reprices every recorded model,
  compaction, and paid-tool cost from provider usage, verifies content-addressed
  raw trajectory and Codex proxy metrics, and emits manifest-ordered JSONL only
  after exact completion. Its pair audit compares every fairness control. A
  formal Harbor `codex-kimi` profile now pins and re-verifies Codex 0.149.1,
  owns the loopback proxy across start/resume, strips Kimi credentials from the
  Harbor child environment, exposes one QEMU-only control route, and audits
  reconciled raw JSONL plus provider-usage shards per trial. Provider face and
  non-secret slot ID, the resolved Harbor executable hash, source commit and
  runner revision are now immutable bindings. An idempotent Harbor→BenchmarkV2
  exporter stores raw/proxy/ATIF artifacts and rejects hidden Harbor retries
  because version 0.21 deletes failed evidence. Release-eligible Harbor runs
  now preclaim every task before dispatch; immutable attempt starts/failures
  retain failed usage, paid tools, artifacts, and retry wall time. Full stores
  cannot finalize, and the paired reporter cannot derive infrastructure rate,
  without complete oracle-ready terminals, so discarded failed runs cannot be
  represented as a clean surviving sample. A symmetric formal `tofu-kimi`
  profile now runs the production public AgentRuntime with host-only credentials
  and exactly two exclusive guest tools. Its exporter strictly reconciles native
  events, sanitized runtime evidence, raw tool audit, ATIF, prompt/runtime/schema
  digests, usage/cache/timing, compactions, final output, and verifier lifecycle;
  candidate wall time receives no Codex-proxy adjustment. Timeout and
  cancellation paths retain a sanitized partial runtime snapshot, and an
  immutable candidate failure cannot record main/compaction usage that differs
  from that artifact. These paired SWE/TB
  launchers do not substitute for the still-missing 900 private task assets,
  their simulator launch adapters, or the paid paired matrix. The paired
  reporter re-derives quality/family, cost-per-success, raw/corrected P90,
  token/cache/context/schema/result, compaction, tool-search, incidents,
  retries, blind judges and actual orchestration from finalized stores; pilot
  reports are structurally unable to emit a release claim. Full paper
  now keeps 29 owner-scoped executable tools behind a 3,702-token Kimi wire
  surface (344 tokens for the fixed search/execute pair); hidden contracts are
  searchable rather than removed. Gateway replay receipts distinguish Kimi's
  recycled positional IDs by arguments/round/world, preserve failure verdicts,
  and are bounded to 256 entries. Root chat and endpoint Worker turns now use
  the same `run_agent_loop` lifecycle as swarm: typed policy hooks preserve
  provider continuation, budget/protocol gates, abort cleanup, chassis-owned
  timeout counting, semantic progress stops and checkpoint placement without a
  private ReAct loop. No Codex-leading claim is made before the complete matrix
  passes.
- **Daily reports now require explicit ownership across routes, services,
  storage, jobs, cost caches, and scheduled generation.** Personal-mode startup
  uses a restricted system principal and idempotently copies legacy flat report
  files into owner-scoped storage without overwriting destinations or deleting
  sources. Distributed mode neither invents an owner nor reads the flat-file
  fallback.
- **Personal-computer resource use now has one probed deployment budget instead
  of server-sized zero-config defaults.** Personal mode takes the minimum of
  host, affinity/cpuset and cgroup CPU capacity, combines physical/cgroup
  memory capacity with current headroom, and probes the resolved persistent
  data volume (including relocated/XDG layouts) for log retention plus a
  bounded SQLite startup reserve. It derives bounded task, executor,
  SQLite/Sidecar, numeric, allocator, log and RSS budgets; failed probes fall
  back conservatively and explicit settings still win. Distributed mode
  retains stable independently larger budgets. Every server/desktop/storage
  launcher materializes one probe
  snapshot into its child environment and installs the allocator limit before
  the child starts, preventing large freed payloads from leaving
  dozens of mostly-empty arenas resident. Compose now defaults to a 4 GiB
  limit, 512 MiB reservation, and 512-PID personal envelope. Permanent turns
  and user data are unchanged, and the existing offline deep-clean path remains
  explicit for historical transport bloat. A verified incremental low-space
  mode can reclaim that bloat on a personal disk that cannot hold two full
  database copies; it requires an independent backup and never runs online.
- **Project Brain Git integration is now fail-closed, terminal-safe, and
  topology-aware.** Isolated workspace creation can no longer fall back to the
  shared tree; merged/discarded records cannot be resurrected; repair
  checkpoints re-anchor to a moved writer HEAD; and stable promotion requires
  explicit acknowledgement when canonical HEAD and candidate diverge. Candidate
  gates reject forbidden/dependency paths, enforce declared write-sets, require
  configured project tests for semantic application/code configuration changes,
  allow pure deletion of historical forbidden artifacts, and validate JSON.
  Browser JavaScript syntax validation now parses `.js`/`.mjs` as ESM and
  `.cjs` as CommonJS, eliminating false quarantines for valid imports while
  malformed gate commands fail closed with an actionable error.
  Stable promotion uses its stronger release command when configured and
  otherwise reruns the candidate project gate instead of refusing semantic
  changes merely because the optional release command is absent.
  A gated `reconcile-head` action can safely merge committed canonical history
  into candidate without moving the canonical branch.
  The operator UI adds discard and divergence-specific confirmation. Idle
  worker polls are read-only, terminal history skips per-worktree Git scans,
  status is response-bounded, and polling is reduced to 30 seconds. See
  `docs/modules/project_integration.md`.
- **Experiments are now immutable, owner-scoped, plugin-defined, and
  promotion-safe.** A typed `tofu.experiments` registry separates strategies,
  metrics, and analyzers from product adapters; resolved specs pin provider
  versions, implementation digests, allocation, and a precommitted assignment
  horizon under one SHA-256 fingerprint. Historical plugin versions can coexist;
  generation-aware request plans and per-scan metric plans remove repeated
  provider/spec resolution from hot loops. Context-cost reports aggregate by
  conversation and default-deny promotion on incomplete pricing, missing
  semantic oracles/latency, SRM, mixed or legacy specs, unverified exposure,
  malformed/truncated sources, a still-running/unfilled fixed cohort, or
  confidence intervals that do not establish
  quality non-inferiority and cost reduction. Owner and experiment filters now
  precede the storage row cap. Installed capability metadata is discoverable at
  `/api/v1/experiments/capabilities`. Stopping seals an experiment ID
  irreversibly, and inference always uses the same first fixed cohort; see
  `docs/modules/experiments.md`.
- **Logging is now bounded, redacted, correlation-aware, and directly
  consumable by debugging models.** A single stream-policy registry governs
  application, audit, browser, process-console, raw-SSE, PostgreSQL, watchdog,
  and desktop logs; startup plus periodic maintenance enforces file/family and
  global budgets. WARNING+ floods are adaptively coalesced while preserving
  occurrence deltas, CRITICAL bypasses coalescing, and a compact rotating
  `incident.jsonl` index works without the database. The admin diagnostics API
  and `python3 -m lib.log_diagnostics` return ranked, selector-aware reports
  under a hard byte ceiling; the legacy digest falls back to this index when
  storage is down. Durable formatters recursively redact credential shapes and
  bound each record, including raw SSE and first-run console output; managed
  evidence is owner-only (`0600`, log directory `0700`). Historical oversized
  rotations are atomically compacted to complete-line bounded tails, and the
  machine-readable maintenance report distinguishes those compactions from
  active-writer rotations and deletions. See `docs/LOGGING.md`.
- **Conversation mutations no longer turn recoverable storage pressure into
  generic frontend HTTP 500 popups.** Turn/attempt/search routes preserve the
  Sidecar error taxonomy: missing authority is 404, transient
  busy/unavailable/timeout is 503 with `Retry-After`, optimistic or permanent
  authority conflicts are 409, and only integrity/protocol/internal failures
  are 500. Generated frontend writes reuse the same command id and body for up
  to three bounded retries, while `conversation_authority_conflict` permanently
  switches a normalized conversation away from v1 message-array writes.
- **The large-authority write path is bounded by changed data, not transcript
  size.** Attempt transport payloads and command receipts are capped; event
  retention, checkpointing, replay dispatch, queue-board persistence, and page
  reclamation run in bounded/idempotent units. Manual `/compact` now commits
  through atomic `turn.compact`, including recursive branch/attempt/event
  deletion and a tiny `requiresSnapshot` sync invalidation. Turn-native search
  uses 10 KiB per-turn derived fragments with transactional lifecycle updates
  and a retrying maintenance-lane historical backfill instead of rewriting a
  frozen conversation aggregate.
- **Adjacent false-failure producers were removed at their source.** Nested
  tool execution now snapshots the assistant tool-call carrier together with
  its result, Codex dispatch honors alias/cell/model/default precedence, memory
  requests without a project are constrained to global scope, and periodic
  optional-provider autodiscovery 404s no longer masquerade as application
  faults. Designed retained-user/TODO/Brain context carriers survive private
  field stripping without false same-role producer alarms; genuine duplicate
  user turns, provider failures, and storage failures remain visible.
- **Frontend migration fallbacks stopped being shadow production owners.**
  Turn state/projection/commands, HTTP transport, send-start leases,
  listener/timer lifecycles, file-size formatting, cookie-capture consent, and
  orchestration diagnostics now each have one statically imported typed owner.
  The retained runtime injects only ambient UI dependencies; `TofuModules` is
  restricted to lazy feature
  commands and diagnostics. Turn-v2 resume UI no longer computes the legacy
  settlement before overwriting it with server data. Task Mode regression
  guards now execute shared native Vite graphs and real locale inputs instead
  of reconstructing the deleted `static/js` module graph. See
  `docs/FRONTEND_MIGRATION_DEBT_AUDIT.md`.
- **Conversation synchronization now has one generated v3 contract and one
  browser stream owner.** The unused attempt-scoped SSE client was removed;
  snapshot, opaque cursor, replay, recovery, and health now belong to the
  conversation-scoped coordinator. The canonical OpenAPI document generates
  both Python and TypeScript artifacts, and clean-checkout/export plus
  freshness tests prevent either side from silently drifting. Semantic turn
  mutations now append compact change records atomically with their authority
  rows; replay gaps reset through a bounded snapshot instead of timestamp
  overlap. Commands share one owner-scoped, idempotent service and use a
  register/bind/start handshake so a worker cannot outrun durable attempt
  ownership. Timestamp-only v2 catch-up is rejected before any heavy read,
  and the connection badge is driven only by the authoritative conversation
  transport. Visible orchestration takeover now performs one revision advance
  with an exact patch instead of a two-step jump that forced snapshot recovery;
  every advancing attempt event is fail-closed unless it carries a replayable
  patch. See `docs/CONVERSATION_SYNC_V3.md`.
- **Frontend source ownership now has a shrinking, model-readable default.**
  The Settings tool inventory moved out of the 5 MiB retained runtime into the
  typed, lazy `features/settings/tools-inventory.ts` owner; obsolete classic
  action names and the dead `misc.ts` filename registry were removed. The main
  entry is 1,185 gzip bytes smaller (the code now loads with Settings), while
  total emitted JavaScript is also 65 bytes smaller. Source-shape budgets now
  cap ordinary modules at 100 KiB and separately ratchet the retained runtime,
  HTML shell, and two monolithic stylesheet owners; the wire-format generator
  no longer targets the retired `static/js` tree.
- **Gateway 5xx outages are now waited out indefinitely instead of failing
  the turn (owner directive 2026-08-20).** `TOFU_GATEWAY_OUTAGE_BUDGET_S`
  now defaults to `0` (disabled): when the whole upstream returns only
  502/503/504, the dispatch loops keep rotating (0.3s cycles, `abort_check`
  every cycle, HUD shows the climbing `stream.phase.retryGateway` attempt
  counter) until the gateway recovers or the *user* cancels — stopping is
  the user's call, never the system's. This mirrors the 2026-08-03 ruling
  for 429 contention (`_saturation_budget_secs`, also default 0) and the
  budget reader was converted to the same per-call env pattern, so the
  legacy bounded give-up (free the worker thread during a total outage)
  can be restored at runtime by setting a positive budget. The
  `llm_fallback` upstream-error envelope branch stays as defense-in-depth
  for when the cap is re-enabled. Pinned by
  `tests/test_dispatch_gateway_outage_cap.py` (storm-not-capped-by-default
  + knob-defaults-disabled cases).
- **Turn-pipeline latency campaign (2026-08-21, measured on the live Turn
  Trace fold: 34 tasks / 7.2 h of production event logs).** Non-LLM wall
  time was ~29% of every turn; this batch removes the three recoverable
  sinks. (1) **Tool dispatch wait** (median 2.4 s per tool, 7.3% of all
  wall time): same-round read tools now start immediately instead of
  queuing behind the serial write lane, with a write-sensitive guard that
  keeps the read cache correct across same-round writes
  (`tool_dispatch/_pipeline.py`). (2) **Fixed per-turn tickets**: the first
  round used to pay ~2.5 s of serialized prep — the conversation is now
  loaded once per request (was 2–3 reads), the orphan-task reaper is
  throttled off the hot path, system-context blocks assemble in parallel,
  the skills index is mtime-cached, and the SSE streamer wakes on events
  instead of a 50 ms poll. (3) **End-of-turn**: the terminal `done` frame
  no longer waits out the 300 ms event-batch window, the modified-files
  filesystem walk moved into the async commit thread (arriving on
  `round_committed`), and a duplicate whole-conversation re-serialization
  before `done` was removed. Plus FUSE-tax cuts across the local tools:
  batched edits now commit one atomic write per file instead of one per
  edit, `run_command`'s pre-spawn snapshot is a single-pass scandir with
  one stat per entry, `list_dir` dropped its per-subdir counting readdir,
  and v2 delta frames persist only the cumulative content/thinking patch
  instead of re-folding the whole turn projection. Commits `7efab787`,
  `156a225b`, `fb381486`, `9524862b`, `dd1f9432`, `e140584f`.

### Added
- **Tofu is now a consumable developer runtime instead of a checkout-shaped
  dependency.** The `tofu-agent` wheel exposes a typed embedded Python runtime
  and a secure `tofu-agent serve` HTTP/SSE sidecar over the production agent
  kernel, with no database or ChatUI application lifecycle. Transient tasks
  carry an explicit principal but skip every durable
  birth/event/result/index path. A bundled bilingual `/setup` control plane now
  lets operators choose a Provider template, discover models, run a real probe,
  and hot-apply one managed default without writing configuration code. Secrets
  are encrypted in an atomic database-free settings file, never returned to the
  browser, and survive restarts; env/CLI ownership remains a deterministic
  read-only override. Applications retain only a Tofu URL/token and omit model
  selection, while callers may still send endpoint/key/model per request. A
  source-free OCI `agent` target, sync/async Python SDK, dependency-free
  TypeScript SDK, idempotent HTTP 202 submission, cursor-resumable task streams,
  clean-wheel gates, and tag publishing workflow complete the distribution
  boundary. See `docs/DEVELOPER_RUNTIME.md`.
- **Turn Trace — the unified per-task timing interface + flame view.** One
  pure fold (`lib/tasks_pkg/turn_trace.py`) derives the hierarchical span
  tree of any task — turn → round → llm / tool / retry-wait / compaction /
  approval — from the persisted `task_events` log (no new instrumentation;
  timing can never drift from what actually happened). Served at
  `GET /api/v1/tasks/<id>/trace` and rendered as a time-axis waterfall
  flame graph inside the Request Inspector drawer (「耗时分析」entry).
  Strict accounting is a CI-asserted contract invariant: the summary
  buckets are a disjoint partition of the turn interval and whatever
  cannot be attributed is an explicit gray gap row — never a silent hole.
  Declared duration budgets (`_TOOL_BUDGETS_MS` / `_KIND_BUDGETS_MS`) flag
  over-running local-tool spans as an optimization worklist while LLM
  deep-think, user `run_command` workloads, approval time and upstream
  rate-limit waits are declared unbounded (never flagged). Every
  registered chat phase must declare a trace rule (drift-guarded).
  The trace now survives low-level event pruning and later regenerations in one
  owner-scoped, 96 KiB-capped document per generation attempt. Exact live prompt
  history and content-free browser phase/terminal-paint plus transport-health
  receipts are inspectable after the fact; receipt writes update only that small
  attempt document, without rewriting a large Turn or advancing conversation
  sync state. Request Inspector discovery now pages those owner-scoped attempt
  identities through a content-free partial index, so hot-registry eviction,
  event pruning, or the legacy global task-result scan cap cannot strand a
  retained trace with no UI entry point.
  Contract: `docs/TURN_TRACE_CONTRACT.md`; tests:
  `tests/test_turn_trace.py`, `tests/test_frontend_turn_trace.py`.

### Fixed
- **V2 live conversations no longer trigger multi-megabyte refetch storms or
  leave the network badge stuck on “重连中” (2026-08-23).** The incident had
  three amplifiers: timestamp-overlap catch-up repeatedly returned an unchanged
  growing turn, attempt replay rehydrated its full projection onto every page
  tail, and each cross-tab `conv_changed` hint eagerly hydrated background
  conversations. Native streams now carry revision-checked structural patches;
  catch-up requests dedupe by known projection revision before SQLite reads the
  heavy column; background notifications only mark stale state; and a live
  EventSource remains the sole ordered projection writer. Typed heartbeats and
  transport reopen now reset the shared silence clock immediately, while real
  `onerror`/storage-wedge states still show degraded. Legacy stream readers keep
  page-tail full-projection compatibility.
- **Turn Send/Regenerate no longer surface storage backpressure as opaque
  HTTP 500 popups (2026-08-23).** The incident was one failure chain, not two
  unrelated route bugs: high-rate `turn.event.record` writes were incorrectly
  scheduled on the interactive writer lane, and a drained SQLite group-commit
  batch marked every member as acquired before its operation began. A slow
  projection write could therefore starve a new turn for 4 seconds and a
  regenerate for 16 seconds. Event projections now use the event lane, each
  batch member arms acquisition only when it actually reaches the writer, and
  all turn-v2 storage failures retain their stable classification at HTTP
  (transient failures are typed `503 server_busy` responses with
  `storageCode` and retry advice). The generated conversation client snapshots
  the validated Send/Regenerate body and follows the OpenAPI-owned retry policy
  for network loss, 502/503/504, and typed transient storage failures, always
  with the original `commandId`, so a lost ACK cannot duplicate a turn;
  replay can also finish dispatch when the atomic create committed before its
  response was lost. The first-message conversation remains atomic: a timed-out
  create leaves no half-conversation, which explains the incident's missing
  second conversation ID without implying transcript loss.
- **The 441.7 GiB SQLite authority now actually drains its historical event
  backlog without recreating writer pressure (2026-08-23).** Production
  inspection found an 862,084-rowid legacy attempt-event span averaging
  484 KiB in stratified samples and a 4,109,666-rowid generic-event span.
  Attempt retention repeatedly selected the same
  oldest already-empty attempt window, so it stopped making progress; generic
  retention executed only one of its sixteen configured batches and wrote a
  permanent receipt for every cleanup tick. Candidate selection now requires
  a remaining transport row, generic cleanup commits and re-enters the fair
  maintenance lane between bounded batches, and backlog/reclaim work uses a
  30-second catch-up cadence before returning to the five-minute steady probe.
  SQLite incremental vacuum remains wall-bounded to single-page units.
- **High-frequency recovery state no longer floods permanent command
  receipts.** Seven days produced 330,952 receipts, including 203,250 task
  projection checkpoints and 47,362 mostly-empty workspace claims. Task
  results now use a dedicated version-witnessed checkpoint operation: an
  identical ambiguous-ACK replay returns the authority's existing version,
  while a stale different snapshot conflicts and cannot roll terminal state
  backward. This removes the extra whole-result JSON+SHA pass and the receipt
  insert from every streaming checkpoint. Clean `None` claim results, empty
  queue reaps, and naturally idempotent retention writes no longer create
  receipts; append, billing, counter, and dequeue operations retain their
  exactly-once protection.
- **Synthetic continuation turns preserve role alternation without deleting
  terminal metadata.** TODO/intent-stall continuation now persists the
  assistant response before its user nudge; empty assistant ghosts after an
  engine-authored user turn are filled with non-empty tombstone prose in place.
  Error envelopes, finish reasons, task IDs, provider replay blocks, and stable
  message IDs survive that repair, preventing both provider-visible `user,user`
  adjacency and the former loss of a stuck-task error bubble when a queued
  follow-up arrived.
- **Generation failures now explain themselves instead of leaking
  `generation_error`.** Turn-v2 projection carries the durable typed error
  envelope into the existing visible error card, while the finish chip uses
  its localized kind, message, and severity (including touch-accessible inline
  guidance) and clears stale errors after a successful retry. The high-volume
  exact tool-loop guard now gives the model one bounded strategy-correction
  round before stopping; if it still repeats, the card explains the repeated
  call, recovery options, and whether the failure is retryable. The terminal
  settlement boundary now normalizes every failure to a complete error
  envelope, legacy archive folding does the same, and a dry-run/CAS/backup
  migration repairs previously persisted malformed failures. A turn whose
  executor cannot start is now durably and idempotently classified as the
  retryable `task_start_failed` envelope instead of returning a partial 500.
- **Storage writer throughput collapse ("Storage writer acquisition timed
  out" floods, 2026-08-20).** Root-caused as a distinct failure class from
  the 2026-08-18 wedge: on a degraded network filesystem the single writer's
  one-fsync-per-commit ceiling fell below write demand, so every lane timed
  out at acquisition for 15+ minutes while no transaction was actually
  wedged. Three-layer root fix: (1) the writer now group-commits its drained
  backlog in one transaction with per-job SAVEPOINT isolation (one fsync per
  batch, automatic fallback to per-job transactions on batch-level failure);
  (2) a live v2 frame's replay-log row now rides inside the
  `turn.event.record` authority transaction instead of a second command
  (one frame = one transaction; skew between authority and replay log is no
  longer possible); (3) new opt-out storage fast path relocates the write
  front to a measured-local filesystem with a continuously-shipped durable
  shadow on the data dir — fail-closed activation probes (WAL semantics,
  free space, measured ≥3× fsync win), shipper-owned checkpoints, two-way
  boot reconciliation with a split-brain guard, bounded observable RPO. See
  `docs/TRB-fastpath.md`.

### Added
- **Generic tool-result viewer (chat UI).** Every settled tool round whose
  result has no dedicated renderer — `read_files` text, `grep_search`,
  `find_files`, `list_dir`, browser reads, MCP tools, etc. — used to collapse
  to a bare icon+title+badge line with the entire result invisible. These
  rows are now expandable (`<details>`): click to reveal the verbatim result
  in a monospace, scrollable pane with a stats (`N lines · M chars`) + copy
  header. JSON payloads are pretty-printed; over-long results are soft-capped
  at 120k rendered chars with a stated truncation note. Styling is
  theme-variable driven (dark/light/tofu) and covered by new characterization
  tests in `tests/test_frontend_tool_rounds_render.py`.

## [0.16.0] - 2026-07-31

> **Versions 0.11.0 – 0.15.2 were never released.** They exist as `VERSION`
> bumps (and, for 0.15.0 / 0.15.2, as orphan git tags) but no GitHub Release
> was ever published behind them — the last published release was v0.14.2, and
> the macOS build leg starved on a retired runner label before the release job
> could run. Rather than reconstruct changelog entries for releases that never
> shipped, their content is folded into this entry.
>
> This is a **minor** bump, not a patch: `VERSION` sat at 0.15.2 from
> 2026-07-23 while ~1250 further commits landed, adding six new top-level
> capability packages — four of which expose their own HTTP surface
> (`routes/api_v1/research.py`, `motion.py`, `skills.py`, `private_hosts.py`).

### Added
- **Auto-research pipeline (`lib/research/`, `routes/api_v1/research.py`).**
  Give it a research direction and it harvests recent literature into a local
  paper corpus (parsed once, then reused), surveys it to map what has already
  been done, and proposes scored ideas screened against that corpus so they
  are genuinely new rather than A+B recombinations. Rejected ideas are
  reported with the reason. Exposed as the `produce_research` tool.
- **Long-form research reports (`lib/longform/`).** research → outline →
  sections(×N) → assemble, published as a cited markdown artifact. The stage
  list is data-dependent (one stage per outline section), which the static
  video stage list never exercised.
- **Motion-graphics video pipeline (`lib/motion_video/`, `routes/api_v1/motion.py`).**
  Topic → researched script → real-TTS-timed storyboard → per-scene composed
  MP4 → concat → narration mux. Every fact card carries a real source URL and
  is credited on an end card. Per-scene authoring degrades to a template floor
  so a single bad scene can never fail the film.
- **Production Substrate (`lib/production/`).** The horizontal layer under
  every "one sentence → finished product" capability: a checkpointed stage
  graph where a stage's artifact is committed as soon as its gate passes, so a
  killed process resumes at the first unfinished stage. Crash-resume is a
  correctness contract here, not an optimisation.
- **Text-to-speech / narration (`lib/tts/`, `routes/api_v1/audio.py`)** and
  voice input (speech-to-text) with a mic button in the composer.
- **Skills as a first-class noun (`lib/skills/`, `routes/api_v1/skills.py`).**
  User-installed skill packages are now decoupled from model-authored
  memories: an always-visible `<available_skills>` index plus an
  `load_skill` progressive-disclosure loader. The model channel is
  read-only; install/uninstall/toggle are user-only.
- **Project Brain — cross-conversation coordination (`lib/conversations/`).**
  Charter with human-reviewed decisions, an epic board with claims and leases,
  an activity feed, direct peer messaging, path leases, and a "Needs you"
  attention surface that aggregates everything genuinely waiting on a human.
- **Request Inspector / debug panel.** Per-request snapshot store with
  server-side folding, a `</>` affordance on bubbles and tool rows, and
  incremental retention — so "what exactly went on the wire?" is answerable
  without a debugger.
- **Manual compaction (`/compact`).** Context compaction is no longer only
  automatic: an explicit command with a REST endpoint, a frontend card, and a
  live streaming summary.
- **Air / Pro / Studio capability dial.** One toolbar control replaces the
  separate Enhance/Tools/Mode toggles, selecting a coherent capability profile
  per turn.
- **Remote Worktree / Desktop Agent.** Run a project on a remote machine from
  the desktop app — per-user bridge tokens, a "Remote devices" picker group,
  tray connect, and `run_command` parity with the local path.
- **Auto-escaping `safeHtml` tagged template (`static/js/core/safe_html.js`).**
  The chat-render path built HTML by string concatenation, hand-wrapping every
  interpolation in `escapeHtml()` — correct but fragile (one forgotten wrap is
  an XSS hole). `safeHtml\`...\`` escapes EVERY interpolation by default, with
  an explicit `raw(x)` opt-out for trusted HTML. A lint rule in
  `tests/test_frontend_safe_html.py` blocks new bare template-string HTML
  sinks in adopted render files.
- **Frontend type-check harness (`tsc --checkJs`, no build step).** Root
  `tsconfig.json` + `static/js/globals.d.ts` catch cross-file global misuse —
  typos, stale renames, dead `typeof` guards — that the shared-`window`-scope
  design otherwise fails silently at runtime. Wired as `make typecheck` and
  enforced by a monotonically-decreasing error-budget ratchet.
- **Release gates.** `scripts/release_assets.py` (is the release complete? —
  per-platform assets plus a size floor that catches a hollow build) and
  `scripts/changelog_gate.py` (is this VERSION documented? — this file is now
  a build gate, which is why the nine-version gap above can never recur).

### Changed
- **SQLAlchemy Core table-definition layer (`lib/database/_core_schema.py`).**
  Tables are defined ONCE as Core `Table` objects and compiled to correct DDL
  and DML for BOTH backends (PG `JSONB`/`IDENTITY` ↔ SQLite `JSON`/autoinc,
  paramstyle, dialect-correct upserts), retiring the hand-maintained twin-DDL
  path. Compile-only: no SQLAlchemy Engine is opened; execution stays on the
  existing connection. Generated DDL is byte-equivalent to the legacy hand-DDL
  (`tests/test_core_schema_parity.py`). Adds `sqlalchemy>=2.0`.
- **Unified the LLM SSE streaming core.** `lib/llm/stream.py` (sync) and
  `lib/llm/astream.py` (async) each carried a ~480-line copy of the identical
  SSE parsing loop, so every fix had to land twice and the copies drifted.
  That logic now lives once in `lib/llm/_sse_core.py`; the two modules are thin
  transport shells keeping only retry/backoff and transport-native handling.
  Pure code-motion — anomaly fields are emitted byte-for-byte as before, locked
  by `tests/test_sse_core_parity.py`. Net −432 lines.
- **Account ↔ wire-face separation in provider config**, so one account can
  serve both OpenAI- and Anthropic-shaped endpoints without duplicate entries.
- **Web search and fetch extracted** to the standalone `tofu_search` package;
  the app seams via `lib/search_bridge.py`. `lib/fetch/` and `lib/search/` no
  longer exist in-tree.
- **Desktop release workflow is VERSION-driven, not tag-driven.** A tag is a
  product of releasing, not evidence of it; the gate now asks the Releases API
  whether a complete asset set exists.

### Fixed
- **Weak image-caption escaping in `renderMessage`.** The image tile's `title`
  tooltip escaped only double-quotes, leaving `<`/`>`/`&` unescaped. Now uses
  the full `escapeHtml()`.
- **Chat didn't re-render on language switch / debug-mode toggle.**
  `i18n.js::_onLanguageChange` and `settings/save_export.js::saveSettings`
  called `renderMessages()` behind a `typeof … === 'function'` guard, but that
  function never existed (the real repaint is `renderChat(conv)`), so the guard
  silently swallowed the no-op. Caught by the new `tsc --checkJs` harness.
- **Duplicate `common.close` i18n key** in `static/js/i18n.js` removed.
- **Desktop downloads 404'd during a release window.** URLs were built as
  `/releases/latest/download/<cached filename>`, whose two halves have
  different lifetimes. They now come from one API payload with the tag pinned.
- **Intel Macs could not install Tofu.** Only the arm64 DMG shipped; the macOS
  build is now a per-architecture matrix on live runner labels, and the release
  refuses to publish a partial asset set.
- Numerous fixes across tool lifecycle (per-tool completion events rather than
  round-barrier), streaming transport, cache accounting, MCP launching,
  scheduler, and the paper Reading Mode pipeline.

## [0.10.0] - 2026-05-09

### Added
- **Daily Optimizer (self-tuning loop).** New `lib/optimizer/` package mines the
  prior day's logs, audit events, and daily reports, asks an LLM for
  optimisation proposals, and either auto-applies whitelisted low-risk
  actions (currently `block_search_domain`, with TTL-based auto-revert) or
  stages everything else as `pending_review` for human approval. Runs nightly
  at 03:30 via the scheduler (`Daily Optimizer` task, auto-registered on
  boot). REST API in `routes/optimizer.py`; review UI in `static/js/optimizer.js`.
  Gated by `OPTIMIZER_ENABLED` setting.
- **Skills Store (curated catalogue + drag-and-drop installer).** Settings →
  Skills tab now has an App-Store-style layout (search + Catalogue/Installed
  scope tabs + category pills) backed by `lib/memory/catalog.py`. One-click
  install downloads a `.zip` over HTTPS (≤ 50 MB) and unpacks it via
  `lib/memory/installer.py`. Anthropic / OpenClaw / AgentSkills `.zip`
  packages can also be drag-dropped onto the tab; bundled `install.sh`
  scripts are surfaced as hints, never auto-executed.
- **Pluggable token counter.** New `lib/token_counter/` package routes token
  counting through provider-specific backends (Anthropic / Gemini / DeepSeek
  / HuggingFace / tiktoken / heuristic) with a usage cache, replacing the
  scattered ad-hoc estimators.
- **File-history store.** New `lib/file_history/` records per-file edit
  history so write tools and the diff viewer can show a coherent timeline
  of changes across a session.
- **Memory prefetch.** `lib/memory/prefetch.py` surfaces likely-relevant
  memories at turn start via the `<relevant_memories>` block, so the model
  doesn't have to call `search_memories` as a generic discovery step.
- **Compaction archive viewer.** New `routes/conversations_compaction.py`
  + `static/js/compaction-viewer.js` let you inspect the archived layers
  produced by 3-layer context compaction.
- **Conversation full-text search endpoints** moved into a dedicated
  `routes/conversations_search.py` Blueprint (extracted from
  `routes/conversations.py`).
- **Provider templates.** Added Meituan and Tencent provider one-click
  templates in Settings → Providers.

### Improved
- **`routes/chat.py` decomposition.** Extracted `chat_human_io.py`,
  `chat_queue.py`, and `chat_tool_state.py` so the chat blueprint is
  smaller and individual concerns (stdin/human-guidance responses,
  server-side message queue, tool-toggle PATCH) live in their own
  modules.
- **PDF parsing.** Added `lib/pdf_parser/docling.py` as an additional
  backend alongside the existing text/VLM/math paths.
- **Project tools.** New `lib/project_mod/gitignore_suggest.py` proposes
  `.gitignore` entries for files the indexer keeps re-scanning.
- **Multi-root workspace robustness.** Extra roots now persist across
  conversation switches (frontend sends `projectPaths`; backend
  `ensure_project_state()` accepts `extra_paths`). The system prompt's
  multi-root section explicitly warns about new-file creation in
  non-primary roots, since there is no auto-detection until the file
  exists.
- **`requirements.txt`.** Pin `lxml_html_clean>=0.4` so trafilatura keeps
  working on lxml 5.2+ where `lxml.html.clean` was extracted.

### Fixed
- Numerous small fixes in browser dispatch, conv_ref handling, image
  generation, LLM sanitisation, scheduler timer/manager, and trading
  decision routes (see file-level diffs).

## [0.9.3] - 2026-04-22

### Fixed
- **MCP launcher pre-flight check.** When an MCP server is configured with a
  `command` that is not on PATH (e.g. `uvx` without uv installed, `npx` without
  Node), we now emit a clear, actionable install hint instead of a cryptic
  `FileNotFoundError`. Covers uvx / npx / pipx / node / python3.

### Improved
- **Overleaf MCP auto-install resilience.** The catalog entry and migration
  rules now pin `overleaf-mcp-plus[compile]>=0.1.3`, the slimmer release that
  drops the unused playwright dependency (~100 MB faster first-run install).
- **Auto-migration upgraded.** Stale server entries from prior versions are
  rewritten on load even when only the args list differs — user-supplied env
  vars and credentials are always preserved.

## [0.9.2] - 2026-04-20

### Fixed
- Fixed Overleaf MCP server failing to launch with `FileNotFoundError: 'overleaf-mcp'`
  on machines where the package was not pre-installed. The curated registry entry
  now uses `uvx --from overleaf-mcp-plus[compile]` so the server is auto-fetched
  from PyPI on first launch, matching the behavior of the other MCP cards.

## [0.9.1] - 2026-04-20

### Improved
- Further optimized support for Claude Opus 4.7.

### Added
- Added support for the Overleaf MCP server in the curated registry
  (edit/read/compile/history on Overleaf LaTeX projects).

### Fixed
- Fixed incorrect retry behavior of the model when invoked by tools.

## [0.9.0]

- Previous release.
