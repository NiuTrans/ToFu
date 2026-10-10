# Tofu-DB engine guidance

## Scope

This package owns the Rust storage engine. Certification requirements and
backend selection live in `docs/STORAGE_TOFUDB.md`; selection support alone does not
prove full source-data migration or release readiness. Tie verification
evidence to the actual build and covered domains.

`build.rs` embeds the engine source digest reported by `--build-info`; its framing
matches `serverctl_pkg/native_engine.py`. Deployment metadata is not build evidence.

native_checkpoint_store.rs reserves journal namespaces. The explicit
native-runtime-checkpoints feature exposes NativeCheckpointStore over a supplied
shared authority; native_checkpoint_store_adapter.rs owns scoped CAS/byte bounds,
not journal transitions. Verify real reopen, abrupt process loss, blob/Keep and
VFS commit-fault tests, plus generic namespace denial and versioned codec neighbors.
Never open another authority, bypass logical-outbox capture or steal active leases.
embedded_checkpoint.rs binds OwnedLease to the running EmbeddedRuntime authority.
Verify generation-bound shared-WAL waits outside writer, failed/current replay,
source ACK, owner/close/reopen and close joining before driver retirement. The
opt-in lifecycle guard creates no worker/authority; closed/disarmed calls refuse.
embedded_checkpoint_control.rs owns volatile C ABI capability IDs and buffer
preflight; OwnedLease still owns transitions. Verify loaded-library SDK, one-shot
load/undersized buffers, monotonic IDs, owner/expiry/close and forgotten inflight
fences. Reuse embedded workers/watchdog; never route ordinary storage operations
into checkpoint control or automatically retry ambiguous commands.
NativeCheckpointStore captures native_checkpoint_outbox_v1 mutations in the
same transaction when outbox is configured. Verify authenticated passive replay,
current-version retry, scope/gap/old refusal and source/target sync fault recovery.
Replay uses one latest witness per task; never bypass target outbox enforcement
or infer a confirmed prefix without exact archive and target-witness evidence.
native_checkpoint_publisher.rs composes passive target replay with the existing
immutable engine sink. publish_pending releases the source writer during sink
I/O and ACKs only durable receipts. Verify lost batch/ACK, archive replacement,
archive/target restart evidence and exact archived-prefix proof; direct old
replay remains refused. Cross-domain/application routing is still pending.
Checkpoint C ABI finish freezes status/Suspend per live capability before I/O;
identical retries use OwnedLease.finish and changed candidates refuse. Verify
schema/parser closure, real C ABI retry, pause/reopen and metadata reclamation.
The per-capability mutex adds metadata only; no journal, receipt log or worker.

## Boundaries

- `src/entity.rs` owns root registration and escaped snapshot leases;
  `entity_pin_registry.rs` owns GC topology and exact byte accounting.
  Replica capture registers every live root under writer/GC exclusion without
  a count quota or capacity queue. Lifecycle tests live in
  `entity_pin_registry_tests.rs`; rationale is in `docs/STORAGE_PERFORMANCE_CHARTER.md`.
- `src/control.rs` exclusively owns the alternating, checksummed `CONTROL`
  slots and their publication order.
- `src/entity_overlay.rs` owns the deferred-application write overlay ("the
  WAL is the database"; `docs/TOFUDB_WRITE_PATH_OVERLAY.md`): delta codec,
  root-associated immutable read views, and durable replay
  cursors. `src/entity_overlay_index.rs` owns the two persistent AVL indexes;
  shared records and copied index paths are memory-accounted. Superseded
  positions may be omitted only with a committed same-key suffix witness.
  It is a persistence replica of the WAL tail, never a second
  authority; merges happen only through `EntityDatabase::merge_overlay*`
  with per-key original commit versions. The overlay is the default write
  path since 2026-09-21; `TOFU_DB_WRITE_OVERLAY=0` is an undocumented
  emergency escape hatch only. Draining is scheduled by the maintenance
  worker's soft-threshold background slices
  (`src/maintenance.rs::drain_overlay_in_background`); the commit-path
  trigger (`merge_overlay_if_needed`) is only the hard backstop. The
  background and idle paths capture one pinned prefix, prepare private pages
  outside authority, then fence the exact old physical root before publishing.
  Private bytes use exact transaction memory accounting without an aggregate
  admission quota; genuine allocation failures retain their typed boundary. A suffix keeps its
  durable cursor even when the captured tail was empty. Conflicting physical
  roots discard preparation before staging. Tests include a blocked actual
  source read with a successful concurrent foreground commit and the full
  merge/checkpoint I/O error, short-write and lost-sync matrix.
  The crash matrix lives in `src/entity_overlay_tests.rs`. The commit-path
  merge trigger (`merge_overlay_if_needed`) is the bounded hard backstop:
  one latency-adaptive chunk (cold start 64 distinct keys, range 1..2,048;
  independently at most 2,048 oldest delta positions plus the 2 MiB
  carried-WAL byte bound, including
  a safe prefix within one transaction) per tripped commit, never an unbounded
  full merge inside a foreground commit. `src/maintenance.rs` survives a
  poisoned state lock without stranding waiters: a terminal-record
  dropout still wakes every waiter and counts `dropped_terminal_records`
  (lock-free, folded into the metrics snapshot). Round errors carrying
  rotation backpressure or `WouldBlock` publication conflicts classify as
  conflict deferrals, never terminal worker failures — a terminal record
  reaches the listener health gate and recycles the daemon on a signal
  that is retryable by design.
- `src/versioned_document.rs` owns bounded byte/decoded JSON reads;
  `src/turn_projection_patch.rs` consumes private reconstruction buffers.
  `src/response_json.rs` validates encoded semantic responses without rebuilding objects.
  `turn.rs` consumes the scoped reader's parsed private object; legacy-message
  conversion moves projection/metadata fields and retains public/error/byte fences.
  `SyncSnapshotResponse` borrows private turns/settings for encoding; attempt
  RawValue bytes retain their original Value normalization and response bounds.
  Event publication reuses the measured byte witness only after freezing the
  final projection; text-head and legacy compact evidence retain separate paths.
  `src/canonical_json_bytes.rs` recognizes exact Value serialization; borrowed-string skipping requires input alias/offset proof, legacy JSON uses its original parser/serializer.
  Immutable `DocumentProjection` depth certificates compose the full response;
  `transaction_ir.rs::IrSlots` invalidates them before any mutable slot access.
  Final IR extraction moves the selected allocation and clears its slot/proof.
  `turn.rs::SettledAppendResponse` separately certifies receipt eligibility;
  `IrSlots` invalidates eligibility on mutation, overwrite and extraction.
  Eligibility never replaces response JSON/depth validation or replay fences.
  Only the actual selected `IrResponse` can reuse a proof at semantic validation;
  raw reads, literals, missing values and serialized replay remain unverified.
  `blob_decode_cache.rs` reuses verified compressed-chunk decoding only after
  the current immutable encoded allocation is read successfully; complete BlobId
  verification stays in `blob.rs`: `blob_whole_cache.rs` binds positive scoped
  evidence to the exact current manifest and complete ordered source allocations.
  Current reads and fresh-source verification remain mandatory; streaming/range
  reads retain their own checks. Both decoded/source capacities and bounded LRU
  records are charged; zero capacity releases empty roots. Verify `blob::` tests.
  `canonical_json_cache.rs` reuses volatile depth certificates only after
  `blob.rs::VerifiedBlobBytes` completes scoped content verification. Replicas
  share bounded metadata; source bytes, legacy conversion and all envelopes
  retain their validation boundaries. Empty indexes release their root nodes.
  Scope, error and ownership rules live in `docs/TOFUDB_JSON_MATERIALIZATION.md`.
- Entity point preparation/resolution/retention probes preserve OCC, read reuse and ownership accounting; rejected query-witness prototype and measurement boundaries live in `docs/TOFUDB_JSON_MATERIALIZATION.md#entity-point-read-attribution-and-rejected-query-witnesses`.
- `semantic_executor.rs::payload_object` consumes the parsed root; generic compiler parse timing/bytes and ownership evidence live in `docs/TOFUDB_JSON_MATERIALIZATION.md#owned-semantic-payload-decoding`.
- `src/authority_preparation.rs` owns private/accounted blob preparation, escaped
  snapshot leases, and commit-owned index/count/outbox effects. Business predicates
  validate before rebasing; index reads flush pending mutations on the original
  snapshot. Never publish private blobs before business validation. Immutable
  prepared payloads are minted only by `block.rs::PreparedBlockSource` capture
  methods. Blob publication uses ordinary dedup/WAL staging; private overlay
  pages use fresh prepared staging after the root fence, retaining fresh-block
  semantics and the original allocation. Metadata uses the preparation budget.
  Identity metadata is charged to the preparation lease. Verify its
  fault matrix plus `entity_optimistic_tests.rs` and the recency tests.
- `src/legacy_receipt.rs` owns source-global receipt identity and replay fences
  in a reserved per-tenant scope. The exact hashed request must explicitly
  identify the authenticated owner before replay; source-global identity must
  not alias existing user or tenant-global receipts. Operator import remains
  separate from these primitives and requires source/target certification.
- `src/wal.rs` exclusively owns active-log framing, hash chaining, bounded
  recovery, and tail truncation.
- `src/vfs.rs::VfsFile::write_all_vectored_at` preserves one original write
  boundary for wrappers/fault simulation; RealFile submits borrowed segments,
  advances short writes and retries Interrupted. WAL single-append framing
  remains byte-identical; verify `wal::tests` and `vfs::tests` plus the fault matrix.
- `src/request_profile.rs` owns optional request span/work attribution, bounded
  anonymous slow samples and `system.metrics.requestProfile`. `fsync_stats.rs`
  retains process/component totals. Measurement boundaries and observer-cost
  checks live in `docs/STORAGE_PERFORMANCE_CHARTER.md`.
- `src/history_payload.rs` owns immutable history offset indexes and bounded-level
  routing pages; publication belongs to Engine/CONTROL. Point reads verify content
  hashes; backup/GC retain all metadata and pruning relocates old-reader payloads.
  Authority prunes only outside the entity root's durable replay boundary;
  the first deferred write publishes that cursor atomically with its delta.
  Protocol and downgrade boundaries live in `docs/STORAGE_TOFUDB.md`.
- `src/engine_history_compaction.rs` reserves the physical publication lane,
  promotes retired history into payload segments off the authority mutex, and
  publishes CONTROL and prepares payload routes off that mutex, then reconciles
  current logical state. Maintenance releases its guard through publication.
- `src/engine_rotation_publication.rs` owns reserved predecessor publication
  through captured handles and successor-state reconciliation; `daemon.rs`
  releases authority during publication. Tests and paired slow-I/O measurement
  live in `engine_rotation_publication_tests.rs`; see `docs/TOFUDB_PUBLICATION_LATENCY.md`.
- `src/engine.rs` composes those primitives, owns the process lease, and visits
  contiguous replay suffixes one authenticated history segment at a time. A
  CONTROL flush failure inside `Engine::drop` never panics: it increments
  `fsync_stats` `drop_flush_failures` and logs one `drop_flush` warn line.
- `src/embed.rs` owns the embedded in-process transport (the daemon's
  lifecycle minus the listener) and `src/embed_ffi.rs` its C ABI. The FFI
  hardening contract (2026-09-22): every entry runs under `catch_unwind`
  (a contained panic maps to the `embedded_transport_error` envelope plus
  one `panic_containment` JSON line; open returns no live handle);
  executes use demand-created detached workers with resource-derived idle reuse under the execute
  watchdog (budget = request's remaining `deadline_unix_ms` + 30 s grace,
  clamped to [120 s, 3600 s], constants in `embed_ffi.rs`). A contained
  panic or a watchdog timeout poisons the handle: later executes fail fast
  with the same error code, and close/abandon free the handle with engine
  operations disarmed (`EmbeddedRuntime::disarm_drop`), never joining the
  abandoned worker; its late result travels by channel and can never touch
  the caller's freed buffer. Ordinary requests retain `embed::MAX_EMBEDDED_PAYLOAD_BYTES`;
  only contract-declared streamed operations admit the bounded aggregate.
  FFI per-operation payload/serialization limits remain authoritative; the
  encoded envelope adds 64 KiB. Input leases count actual copied payload
  capacity until the worker releases ownership, including late results.
  `contracts/storage_v2.json` records no aggregate byte admission; the
  launch-derived embedded memory hint is observation only.
  `src/protocol.rs` frame parsing fails closed with `InvalidData` on a
  1-byte read overrun (never `unreachable!`). Its frame writer submits three
  borrowed I/O slices, advances partial writes exactly and retries interruptions;
  zero progress, overrun and transport errors preserve failure and secret cleanup.
  Verify `protocol::tests::` and `listener::tests::` without widening deadlines.
- `src/embed_envelope.rs` owns borrowed recognized raw fields; validate all unknown values and settle duplicate keys before field-type admission. Parser/ABI parity and measurements live in `docs/TOFUDB_EMBEDDED_RESPONSE.md#borrowed-request-envelope-fields`.
- Encoded embedded responses and boundary probes: `docs/TOFUDB_EMBEDDED_RESPONSE.md`.
  `server.rs::response` owns immutable selected-payload evidence; consuming
  public Response extraction discards it. Only actual IR canonical/depth
  evidence permits direct transport encoding; notice envelopes stay unverified.
- FIFO job delivery, idle-worker reuse and lifecycle tests: `src/embed_job_queue.rs`; evidence and bounds: `docs/TOFUDB_WORKER_DELIVERY.md`. Caller/worker/handoff anchors are opt-in; retain the original scheduling policy and default-off reply packet.
- `src/authority_gc_online.rs` owns the daemon's two-mark epoch collector
  (`docs/TOFUDB_ONLINE_GC.md`): pin-inclusive marks, off-mutex mark/scan,
  relink-fence-checked deletion holds, and the `tofu-db-online-gc` driver
  thread. Load-bearing invariants: every reference-without-write decision
  (`put_deferred` cache/probe hit, idempotent loose republish) must record
  into `BlockStore`'s relink fence; built-but-unpublished segments may only
  be discarded when an error proves no catalog publish happened
  (`WouldBlock`); the offline exclusive collector in `authority_gc.rs`
  stays unchanged and certified. Mark traversals must keep their
  every-4,096-visit stop-flag checks, and both background drivers keep
  their boot grace + crash-safe bounded stop join (a slow phase never
  stalls startup or shutdown).
- `src/verify.rs` owns the offline integrity operators: `verify` (full
  read-only walk: CONTROL, selected predecessor/successor WAL chain via
  shared `wal::scan_selected_generation_chain`, history, payload catalog, entity graph, loose
  universe) and `salvage` (extract-reachable-with-explicit-loss into
  backup layout, overlay merged first, manifest last, fatal CONTROL fails
  closed). Neither repairs the source; recovery from a salvage target is
  the ordinary `restore` path. Verification phase timings include authority
  close; embedded driver-stop timings use fixed names under opt-in diagnostics.
- `src/block.rs` binds Bloom negative answers to the installed payload catalog
  publication. A stale filter falls back to exact routing/index probes; a
  rebuild cannot disprove membership in a later batch publication.
- `src/transcript_archive.rs` owns the live per-conversation transcript
  archive (`contracts/tofudb_ir_v1.json` `transcriptArchives`): a compact,
  incrementally sealed legacy-message page store so bulk hydration reads a
  handful of page documents instead of materializing every turn document.
  Load-bearing rules: only the `tofu-db-archive-seal` driver writes pages
  (window materialization on a pinned read replica, publication in a short
  `try_lock` hold with a fresh lane-count fence); every other lane
  mutation tombstones the archive and readers fall back to the exact
  per-document lane read — never serve archive content past a fence
  mismatch. `turn.rs` keeps the sealed-prefix + exact-tail read fusion in
  `legacy_messages_full` and the bounded window path.
- `src/online_backup.rs` owns the daemon's online incremental backup:
  pre-anchor overlay drain in certified bounded chunks (one short hold
  per chunk, contention defers the epoch), checkpoint anchoring under one
  short hold, off-mutex copy, manifest-last publication, retention prune,
  and the `tofu-db-online-backup` driver. Registered backup anchors must
  join every online-GC mark (authority.rs folds them into
  `online_gc_capture`); the copy+publish lease scope never overlaps the
  prune lease on the same path-locked BACKUP_LOCK; `system.backup` is a
  control operation that only rings a doorbell and waits on its
  connection thread.
- `src/authority_watch.rs` owns the authority-contention watchdog: a
  `tofu-db-auth-watch` probe thread tracks the longest continuous
  `try_lock` contention window and flips `system.health` ready past the
  10 s wedge threshold (the supervisor's liveness path is the recycler).
  Successful foreground lock acquisitions advance the authority-local progress
  counter; the watch clears sampled contention on handoff without taking the
  semantic layer. Other authorities cannot mask a wedged holder.
- `src/authority_gc_online.rs`'s epoch cadence has a doorbell-proof
  floor: no trigger (reclaim doorbell, volume pressure, idle timer) may
  start epochs faster than the minimum interval by start time, and the
  collector never exceeds a 50% duty cycle.
- `src/wal_retirement.rs` owns the one retired-generation cleanup slot;
  the existing pack worker deletes it outside the authority mutex.
- `src/migration_session.rs` owns the exclusive private-pipe operator boundary;
  its generated allowlist never extends the application wire protocol.
- `src/receipt_migration.rs` restores explicitly scoped command receipts via
  authority's live receipt/blob/family publisher. Preserve encoded response bytes
  and conflict fencing; never infer owners or rewrite historical request digests.
- `src/conversation_header/AGENTS.md` maps header and recoverable-trash import,
  including the committed-seal boundary required before range-pin publication.
- `src/conversation_header/migration.rs` preserves source header revisions and
  clocks; Turn/page imports reconstruct counts without advancing those values.
- `src/migration.rs` owns explicit offline import entry points; domain adapters
  must preserve source state and publish their indexes in the same transaction.
  `src/task_result/migration.rs` is the certified task-results adapter
  (`migration.task.result.*`): exact document bytes/version/timestamp plus
  atomic header, replay-projection, summary, live and cost-experiment index
  reconstruction with independent catalog enumeration.
- `src/compaction_archive_migration.rs` owns root archive snapshot import and
  independent enumeration. It shares atomic content/index publication with
  the live archive owner while preserving the live parent-read fence.
- `src/turn/snapshot_transfer.rs` owns the exclusive bounded Turn transfer
  buffer; it shares session admission with event/frozen uploads. Turn read
  bounds are distinct from ordinary live write/IR admission.
- `src/turn/text_tail.rs` owns revision-fenced live text pages under the existing
  projection head. Contract: `docs/STORAGE_TURN_TEXT.md`; bounds come from the
  native IR generator. Preserve exact replay, owner/CAS fences, snapshot readers
  and atomic head retirement. Verify its tests and the application storage parity
  suite; ordinary correctness does not establish conversation-capacity SLOs.
- `src/turn/sync_attempt_reference.rs` owns compact canonical Attempt-event
  references in the replay journal. Contract: `docs/STORAGE_EVENT_REPLAY.md`.
  Preserve expanded byte admission, same-owner source/version checks and atomic
  retention release; export emits public envelopes, never internal locators.
- `src/turn/tombstone_migration.rs` preserves source deletion clocks through
  atomic Turn claims and delta/age indexes; private get/scan validate both.
- `src/turn/attempt_migration.rs` owns terminal Attempt snapshots and their
  independent catalog; `src/turn/migration.rs` verifies retained Turn pointers
  against those imported attempts. `src/turn/attempt_event_migration.rs` owns
  exact event JSON/timestamps, maximum-sequence heads and independent catalog
  verification, retaining the ordinary active-parent read fence. Its single
  volatile upload buffer belongs to `migration_session::run`; event-specific
  document/page budgets do not widen ordinary document or protocol-frame caps.
  Its `ParentValidationMemo` is the session-scoped memo of validated event
  parents: offline-lane attempt snapshots are insert-or-match immutable, so a
  proven (parent → Turn) fact cannot change mid-session; events stream
  clustered by attempt and the memo avoids re-materializing each parent
  document (and payload blob) per event. It never crosses sessions and
  degrades to ordinary re-validation on overflow.
- `src/frozen_transcript.rs` owns immutable pre-Turn pages, complete manifests,
  history fallback and blob-sharing clones. Its ranges participate in conversation
  trash pins. `frozen_transcript/upload.rs` owns the single bounded page upload;
  the operator boundary excludes simultaneous event/page upload buffers.
- Keep serde_json's `float_roundtrip` feature: stored historical fractional
  timestamps must survive parse/serialize with identical binary float values.
- `src/raw_archive/migration.rs` restores exact compressed history through the
  live atomic index writer, refuses quota overflow, and owns offline catalog
  and scoped usage reads; only the offline path bypasses the active parent.
- Search projection storage/publication lives in `turn_search_projection.rs`
  (including the owner-level gram posting index: packed per-gram posting lists
  plus the exclusion namespace); pages/cursors and the lazy candidate filter in
  `turn_search_query.rs`, public text extraction in `search_text.rs`, and
  archive reads in `compaction_search_source.rs`. The `turn_search_worker.rs`
  maintenance round reads every source page from one batch-scoped pinned read
  replica — never under the foreground authority mutex; only the exact-token
  acknowledge touches the writer. Native limits derive from
  `contracts/conversation_search_v2.json` through `gen_tofudb_ir_contract.py`;
  the index design record is in `docs/STORAGE_TOFUDB.md`.
- `paper_build_artifact.rs` owns retained upload/lease/quota transitions;
  `research.rs` couples verified pins to workspace CAS. Receipt validation and
  limits derive from `paper_build_v1.schema.json`; payloads reuse the generic
  `versioned_document.rs` record/blob codec. Verify with the retained tests and
  retained-build scripts in `tests/tofudb/test_tofudb_differential.py`.
- `paper_document.rs` owns saved source bindings, build generations and reading
  CAS. Its document/association capacity is tenant-shared; publication reuses
  the retained pin transaction. Verify `saved_document_` native tests and
  saved-document differential scripts.
- `invocation.rs` owns immutable child birth evidence
  (`invocation.register_child`/`resolve_child`) over the task-result
  authority; `task_result/mod.rs` mirrors that identity in its compact header
  so generic checkpoints stay fenced, and `compaction_archive.rs` revalidates
  the exact birth record for every nonempty `archive_scope` (per-scope
  claims, chronological indexes, counts, and retention share the root layout
  behind an unambiguous discriminator).
- `plugin_rows.rs` dispatches manifest-declared row operations
  (`plugin.<namespace>.<operation>`) against the live manifest, sharing its
  physical row/unique-claim layout with `plugin_migration.rs`; admission
  admits the `plugin.` shape and execution revalidates namespace, operation,
  and kind (an unknown manifest operation is never a frozen-catalog
  rejection). `legacy_scan` answers the exact absent-table result because no
  pre-authority SQL table can exist natively.
- `src/automation_project.rs` owns durable project-binding CAS and active scope
  proof; generic target writes/deletes are forbidden. Private attempt execution
  config reads retain the existing owner/parent/turn proof and default to hidden.
- `src/automation_definition.rs` owns atomic prepared definition publication,
  subscriptions and revision-bound clock jobs; `src/automation_run.rs` owns
  occurrence/progress/cancellation and live clock-producer fences;
  `src/automation_approval.rs` owns reviewed proofs plus atomic canonical wake;
  `src/automation_presentation.rs` proves physical attempts and atomically indexes
  immutable run outputs. Their scopes, catalogues and bounds derive from `contracts/automation*.json` and generated
  IR constants. Verify `tests/automations/` and automation differential scripts.
- Never import SQLite/PostgreSQL libraries or expose physical records as the
  application contract.  Future semantic commands compile from the canonical
  contracts under `contracts/`.
  - Carve-out: the bundled `rusqlite` dependency in `src/tofusql.rs` is the
    single permitted SQLite use. It evaluates the read-only TofuSQL front over
    virtual tables backed by the entity B+Tree; it never opens a database file,
    never persists to storage, and enforces read-only by authorizer (never by
    convention). A poisoned authorizer/pin-set lock denies the query instead
    of panicking. The catalog it serves derives from
    `contracts/tofusql_catalog_v1.json` via `scripts/gen_tofusql_catalog.py`.
    No other module may link SQLite.
- Every allocation, file, queue, worker, and recovery scan needs a hard bound.
  Normal open must never migrate. Recovery scans at most one active WAL and,
  only when CONTROL owns it, one sealed predecessor (64 MiB each); see the
  generation ownership contract below.
- `src/payload_hints.rs` owns reconstructible positive point-read hints across
  opens. Only the current CONTROL catalog and verified payload bytes establish
  a hit; cache loss/corruption never proves absence or fails authority open.
  Clean, empty-WAL opens skip the recovery catalog pass; nonempty WAL replay
  keeps it. Regression gates live in block/engine/cache tests; bounds and
  measurements live in `docs/STORAGE_TOFUDB.md`.
- `src/payload_routing.rs` owns budgeted verified-prefix routes published with
  the payload catalog; unknown segments stay explicit fallback candidates.
  `src/block.rs` dedupe probes run behind a bounded candidate window (16):
  content-hashed IDs make manifest hash-ranges overlap almost entirely, so an
  unbounded newest-first scan costs O(segments) opens per probe. A filter-
  proven packed block beyond the window answers `PackedElsewhere` without
  restaging — staged chunks must stay disjoint from published segments, the
  invariant the checkpoint pack merge relies on. An unbuilt packed filter
  means "no catalog yet", never "packed".
- Resource budgets are probe-derived, not operator-tuned: since
  2026-09-23 the daemon and the embedded transport apply four write-path /
  index budgets from the launch probe at authority open
  (`DaemonResourceBudget::from_snapshot_with_volume_class` →
  `AuthorityDatabase::configure_write_path_budgets`): overlay merge budget
  (headroom/128, floor 8 MiB, ceiling the consumer's 256 MiB clamp),
  overlay WAL watermark (4/5 of `ACTIVE_LOG_ROTATE_BYTES` = the historical
  48 MiB, structural — never probed), checkpoint materialize batch
  (headroom/256 clamped 8..32 MiB, but pinned to the 8 MiB floor on
  observed network mounts where one fenced step costs ~300 ms per 8 MiB),
  and the open-segment verified-index cache (headroom/64, floor 64 MiB,
  clamp 16 GiB). Probe failure selects the lean profile whose floors are
  exactly the pre-probe defaults, so low-end or unobservable hosts behave
  byte-identically to before. The four `TOFU_DB_OVERLAY_BUDGET_BYTES` /
  `TOFU_DB_OVERLAY_WAL_WATERMARK_BYTES` /
  `TOFU_DB_CHECKPOINT_MATERIALIZE_BATCH_BYTES` /
  `TOFU_DB_PAYLOAD_SEGMENT_CACHE_BYTES` envs survive ONLY as explicit
  overrides with their historical clamps (a parsed 0 still disables the
  segment cache) — they are escape hatches for bulk offline imports and
  bisection, never a required tuning step; the resolved values are
  observable in the readiness `resourceBudget`
  (`overlayBudgetBytes`, `overlayWalWatermarkBytes`,
  `checkpointMaterializeBatchBytes`, `payloadSegmentCacheBytes`).
  `TOFU_DB_SEGMENT_RETIREMENT_DELAY_MS` (default 60000) floors how long a
  manifest-retired payload segment stays parked before the sweep unlinks
  it; `0` restores the pre-2026-09-21 immediate-unlink behavior for
  bisection. The deferred set is in-memory (≤64 entries, overflow
  force-reclaims the oldest) and crash-unsafe by design: a restart drops
  it and orphan GC reclaims the unreferenced files.
  `TOFU_DB_PACK_BYTES_PER_SECOND` is a fixed-rate escape hatch only: with
  no explicit rate the packer runs the adaptive feedback controller
  (`background_pack.rs`, foreground activity/latency signals, EWMA baseline,
  AIMD rate, idle-unpaced), and `TOFU_DB_DURABILITY` /
  `TOFU_DB_WAL_FLUSH_INTERVAL_MS` override the launch-probe automatic
  durability tier (`resource_probe.rs` volume classification).

## Verification

`engine.rs` has a bounded test-only checkpoint ID source: history fault probes
and replays must use the same segment IDs, compare full traces, and assert every
armed fault fires. Production still generates UUIDs; never skip unexercised cells.

Run `cargo test --manifest-path packages/tofu-db/Cargo.toml`.  Filesystem or
commit changes must add a deterministic crash/torn-write test before broader
application integration tests.

Platform-specific launch observation (`resource_probe.rs`) is gated with
`cfg(target_os = ...)`: keep the Linux `/proc`/cgroup path, the macOS
`sysctl`/`host_statistics64` path, and the Windows `GlobalMemoryStatusEx`
path disjoint, and never let a missing observation do anything but fall back
to the lean profile. `certify-resource-gate` stays Linux-only (`wait4`
peak-RSS). When touching these branches, re-run the cross-target type check
for all three OSes (no cross C toolchain needed):

```sh
for t in x86_64-pc-windows-msvc x86_64-apple-darwin x86_64-unknown-linux-gnu; do
  rustc --edition 2021 --emit=metadata --target "$t" --crate-type bin \
    packages/tofu-db/scripts/platform_branch_check.rs -o /tmp/pbc.out
done
```

A full `cargo build --release` for macOS/Windows requires that platform's C
toolchain (bundled C sources in `libsqlite3-sys`/`libmimalloc-sys`/`zstd-sys`);
build natively per OS or provide a cross C toolchain.

Release distribution never asks users to compile: `install.sh` installs a
verified per-platform prebuilt (musl-static on Linux) and only falls back to
a source build. Produce the assets with
`scripts/build_tofudb_prebuilt.py` (deterministic `tar.gz` + `.sha256`);
`install.sh` verifies and deploys them through
`scripts/install_tofudb_prebuilt.py` → `scripts/deploy_tofudb_engine.py`.
Keep the asset layout (exactly one engine binary per archive) and the
`<hex>  <name>` sidecar format in lockstep with those two scripts.

- `src/turn/model_context.rs` owns main-lane context proof reads; keep it in parity
  with `contracts/model_context_v1.schema.json` and `tests/model_routing/test_model_context_read.py`.

WAL worker scheduling and nested page-read probes: `docs/TOFUDB_WAL_BARRIER_SCHEDULING.md`.

Verified block read branches and source-range probes: `docs/TOFUDB_BLOCK_READ_PATH.md`.

Rotation window ownership, exact read pins, and probes: `docs/TOFUDB_ROTATION_WINDOW.md`.

Online sealed-WAL handover and crash cases: `src/engine_generation_tests.rs`; protocol and resource bounds: `docs/TOFUDB_ROTATION_WINDOW.md`.

Published query capture: `entity_query_view.rs` owns root/pin handoff;
`authority_query_view.rs` owns replica configuration. Contract and tests:
`docs/STORAGE_TOFUDB.md#snapshot-read-lane`, `entity_query_view_tests.rs`.

`server_bindings.rs` owns immutable dispatch bindings; embedded requests borrow
them under the runtime lifetime, while legacy builders own isolated overrides.
Binding preparation and snapshot-pin release have opt-in timing stages.

`entity_pin_registry.rs` owns atomic aggregate byte accounting and the locked
root registry. Reader waiter enrollment shares the registry predicate mutex;
registered-root release notifies only enrolled waiters. Verify
`entity_pin_registry_tests.rs` plus Entity quota, snapshot, GC and rollback tests.

`embed_reply.rs` owns fresh reply allocation and receiver-retirement anchors.
Reuse and single-slot experiments remain rejected; preserve standard sender-disappearance and
late-result disposal. Verify `embed_reply_tests.rs`; `docs/TOFUDB_WORKER_DELIVERY.md`.

Native whole-authority fastpath: `src/fastpath.rs` owns relocation lineage, the
source lease, classic-open fence and explicit lossless export. Its asynchronous
shadow uses the existing online-backup worker and native backup/restore fault
boundaries; never substitute a WAL-only or filesystem-only replica. Admission,
loss window, cadence and operations: `docs/TOFUDB_FASTPATH.md` and
`contracts/storage_v2.json`.
