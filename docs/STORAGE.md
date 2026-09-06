# Storage authority

Responsibility: the current durable-data boundary. This document describes the
running system; Git history contains migration plans and past measurements.

## One authority

The Storage Sidecar is the only process that loads a database driver, resolves
a database path or DSN, opens transactions, runs schema work, or executes SQL.
Application code calls named semantic operations through `StorageClient`:

```text
route / application service / worker
  -> owner-scoped repository or semantic operation
  -> StorageClient query | command
  -> authenticated storage.v1 RPC
  -> operation registry
  -> one Sidecar transaction
  -> SQLite or PostgreSQL adapter
```

There is no in-process database fallback and no runtime store switching. A
missing or unhealthy selected authority revokes readiness.

## Owners and entry points

| Concern | Owner |
|---|---|
| Client API, deadlines, supervision | `lib/storage/` |
| RPC server and operation dispatch | `lib/storage_sidecar/server.py`, `operation_registry.py` |
| Semantic operation domains | `lib/storage_sidecar/operation_domains/` |
| Transaction implementations | `lib/storage_sidecar/operations_pkg/` |
| Durable schema and migrations | `lib/storage_sidecar/schema.py` |
| SQLite/PostgreSQL adaptation | `lib/storage_sidecar/adapters/` |
| Deployment/preflight | `lib/storage_sidecar/config.py`, `preflight.py` |
| Operator CLI | `scripts/storagectl.py`, `python -m lib.storage_sidecar.migrate` |

The current machine versions are `storage.v1`, schema version 58, and operation
registry version 38. Code constants are authoritative; update this sentence in
the same change when either version advances.

## Tofu-DB pre-authority work
`packages/tofu-db/` is an isolated Rust certification target, not a selectable storage backend. Decoded `storage.v2` requests now cross an explicit authenticated session boundary: a zeroized 256-bit Hello capability is checked in constant time against an in-memory SHA-256 witness before a session can exist, owner/tenant scope is fixed for that session, negotiation happens exactly once, and every bounded response preserves correlation plus the negotiated schema. A transport-generic connection loop requires Hello first, emits a negotiation ACK, distinguishes clean EOF from truncation and shares hard-bounded RAII admission across connections. Semantic admission precedes authority acquisition, so invalid or already-expired work never waits behind storage I/O; admitted work uses only its remaining request/operation deadline to acquire the mutex and returns `deadline_elapsed` without executing when that budget expires. Its numeric-loopback-only acceptor caps connections at 64, fixes every connection stack at ≤2 MiB, and holds the authority mutex for one admitted request. Before opening the authority, the pre-authority `serve` command performs one bounded probe of process-affinity/cgroup CPU, host/cgroup memory capacity and headroom, and authority-volume free bytes; missing observations select a four-connection/16 MiB lean profile, observed pressure scales to one connection, all profiles stay below 64 connections/128 MiB frames, and observed free space below two WAL windows plus 16 MiB refuses writable startup. It then derives and clears its bounded environment secret, emits one credential-free readiness line containing only aggregate budgets, and exits after its inherited empty stdin pipe reaches EOF. Its first slice owns an exclusive process lease, two alternating 4 KiB
BLAKE3-checked `CONTROL` slots with checkpoint/manifest/active-generation witnesses, immutable 4 MiB content-addressed blocks, and one
CRC32C/BLAKE3-chained active log capped at 64 MiB. Commits checkpoint at a 60 MiB soft limit, packing active transactions into immutable ≤4 MiB history segments and publishing their manifest plus a new empty WAL through `CONTROL`,
and loads history only for an explicit snapshot. History maintenance retains a complete segment suffix through one v2 manifest and same-WAL `CONTROL` republication; the existing low-priority worker keeps 16 segments after a lean probe or 64 after a complete probe, requires a current Entity authority root and an already-empty active WAL, never forces a checkpoint or rewrites transaction payloads, and leaves retired immutable segments to backup-generation-aware GC. Explicit authority GC refuses live MVCC handles, first stream-matches at most 20 million loose-directory entries while retaining two paths and removes at most one strictly named, size-bounded `.new-*` block temporary before marking, then marks current Entity/capsule/semantic/history/active-WAL references with on-volume spill bounded by `min(1 GiB, 2% free)` or a 64 MiB lean fallback, republishes unchanged state before referenced-file deletion, and deletes at most 65,536 loose blocks/256 MiB per repeatable round. It then scans at most 4,097 payload-directory entries and removes at most one strictly named, size-bounded, manifest-unreferenced complete or temporary segment; the 4,096-entry manifest ceiling guarantees an overfull scan window exposes an orphan, while unknown entries fail closed. When no earlier orphan wins, it inspects one generation-selected hash shard of at most 16 manifest segments and rewrites or retires at most one. Only after no reclaimable victim remains may it pack at least 128 reachable loose blocks from one generation-selected shard, excluding IDs already present in its manifest segments and fitting both the 65,536-block/256 MiB segment bounds and the launch-derived temporary budget; sub-threshold shards are not rewritten. An executed no-victim shard advances the durable CONTROL cursor so repeated rounds cannot starve later shards. Partial segment rewrites, but not fully dead retirement, must fit the temporary-space budget. Its existing-authority-only operator command defaults to a non-deleting plan and requires `--execute`; GC is not periodic until mark traversal gains a resumable foreground-safe cursor.
Opening recovers only the selected active generation; a bad tail before its durable witness fails closed. Referenced blocks sync before the log, missing active references fail closed, and failed `CONTROL` publication makes the live authority restart-required before further authority work.
Batched entity COW roots use owner-scoped MVCC/OCC witnesses and commit every new page reference; every transaction pins its exact immutable root in a per-open database-instance registry, admits at most 64 live handles, exports content-free handle/distinct-root/oldest-sequence/retained-byte metrics, releases on handle drop, and rejects handles crossing a reopen boundary. Each transaction admits at most 6,144 distinct point witnesses and 1,024 range witnesses, checks new capacity before page I/O, and reuses a point witness on repeated reads. The registry conservatively accounts point keys, worst-case range leaf witnesses, and staged key/value writes across every live transaction under a 160 MiB process hard limit; pressure is retryable resource exhaustion and transaction drop releases its entire reservation. Authority-global persistent root pins use reserved owner-scoped identities and a transactionally maintained count record: catalogued create/remove admission performs bounded point reads, scales to one million durable pins, and an absent count may scan and upgrade at most 64 legacy pins. Pin, count, and business changes publish in the same OCC commit; reachability verifies the declared count, while malformed records, forward references, empty roots and reserved identities fail closed. A pin may capture a capsule of at most 64 sorted non-overlapping ranges: complete old subtrees are shared, boundary leaves are filtered, mixed-level fragments receive unary COW wrappers, and only the capsule root plus newly built pages enter the commit, so backup and GC never traverse unrelated authority pages. A transaction may retire at most 64 sorted non-overlapping key ranges under an exact current-root witness; wholly covered subtrees are detached without reading them, only boundary paths are repacked, overlapping writes fail before commit, and the capsule plus retirement publish atomically. Restore publishes a versioned root directory with at most 64 sorted non-overlapping range mounts, normally writes one directory block regardless of capsule size, merges mounted data into point/range reads, and gives base writes and tombstones precedence; repeated retirement clips mounts and may publish an explicit empty base instead of a synthetic tombstone. Explicit lazy consolidation atomically replaces one mounted prefix with the same logical rows in base, advances or removes that mount, handles at most 999 rows/8 MiB per transaction, and collapses an exhausted directory back to a direct root; it is operator/maintenance work and never runs during open. After readiness, one 512 KiB-stack low-priority worker round-robins at most 64 explicit scopes, attempts one transaction per probe-derived 250 ms or lean 1 s interval, never waits for a busy foreground mutex, and exponentially backs empty authorities off to 60 s; terminal maintenance errors stop daemon admission. Roots without mounts remain direct B+Tree pages, so normal open still reads one bounded root node. Explicit reachability walks stream the Entity page graph and root directories without a database-sized visited set, reject child level and key-range transplants, and cap work at 20 million pages with an 8,192-node DFS frontier; only the current `CONTROL` graph declares persistent roots, while incremental backup copy, restore and retention GC traverse all declared graphs. Blobs use 1 MiB deduplicated zstd chunks, while fenced streams use immutable 2 MiB segments and bounded cursor pages. A versioned family-transaction container canonically combines an entity root, multiple stream commits, one command receipt, one logical outbox record and at most 2,048 deduplicated block references under the existing 256 KiB inline bound; entity/stream recovery remains compatible with original single-family records. The synchronous pre-authority coordinator binds transaction handles to one authority UUID, stages entity/stream/blob work, publishes exactly one family transaction, and advances all in-memory witnesses only after durability. Receipts use the existing domain-separated SHA-256 command key and 200-byte ID, 64 KiB stored/4 MiB decoded response limits; small results stay in the entity index, larger encoded results use an atomically referenced blob, and operation/request mismatches or concurrent first delivery conflict before a second business commit. The native logical-record codec uses AES-256-GCM with random 96-bit nonces, exposes only an eight-byte key fingerprint, binds owner/sequence/schema/request routing fields as AAD, limits clear payloads to 4 MiB, and rejects metadata/ciphertext transplantation. When configured, the authority requires exactly one outbox capture for each business transaction; owner-scoped OCC metadata assigns continuous sequences, applies pending-byte backpressure before blob staging, stores records above 4 KiB through atomically referenced blobs, and deletes only the next record through an idempotent ACK transaction. Pending reads materialize at most 64 records/8 MiB. The publisher validates a whole contiguous single-owner batch before side effects, performs sink I/O without borrowing the authority writer, accepts only identity-matched durable receipts, and leaves lost acknowledgements pending for idempotent retry; counters contain no user content. The native owner-bound sink reuses Engine CONTROL/WAL durability in an explicit absolute directory, holds an exclusive lease, reserves two active-log windows at capacity preflight, rejects gaps and cross-owner records, and resolves old retries with one exact active lookup or one bounded 4 MiB history-segment read. One isolated worker runs sink I/O; its 16–64 MiB launch-headroom-derived budget counts queued plus active data, deadlines do not cancel potentially durable writes, shutdown stops admission and drains to an explicit deadline, and ambiguous sink state terminally fails queued work. An automatic relay round-robins at most 64 unique explicit tenant/owner scopes through the one aggregate queue, releases the authority mutex during sink I/O, commits ordered identity-matched ACKs, accepts foreground wakeups, and uses a bounded fallback interval; retrying scopes cannot starve healthy scopes, while ambiguous authority state and thread panics fail closed. Arbitrary I/O errors, ENOSPC, short writes, and lost syncs during sink append plus capture or ACK failures recover only a complete prefix. Every WAL path, including pre-published reference commits, poisons the live authority on an ambiguous append failure until reopen recovery selects the prefix. The engine durability primitive admits at most 64 transactions and 8 MiB each of total logical payload and encoded WAL per group, then uses one WAL barrier and one final-sequence CONTROL publication; every member remains independently framed for unacknowledged-prefix recovery. Its single background sequencer prepares hashes/envelopes on submitting threads, waits no more than 1 ms and never groups more than 64 requests or 8 MiB. The queue derives from 1/128 of launch-probed memory headroom (16 MiB lean fallback), hard-caps it at 1,024 requests/256 MiB, applies count and byte backpressure, and exports queue/group/commit/failure metrics. Explicit incremental backup copies only missing reachable blocks, publishes a checksummed snapshot manifest last, and uses bounded capacity preflight plus durable restore pins to resume verified restore; plan-backed retention GC preserves pinned generations, spills up to 33.4 million marks within 1 GiB, and deletes blocks in shard batches.
The multiplexed native sink routes up to 64 frozen source scopes into one Engine authority. An explicit administrative scope owns its COW exact-record index, per-owner sequence witnesses and aggregate capacity counter in the same transaction; records above 4 KiB reuse content-addressed blobs, and reopen uses at most 64 bounded point reads instead of a history scan. A shared deterministic VFS drives end-to-end commit faults. The explicit `certify-filesystem` operator command requires an empty persistent target, exercises the real VFS through lock, immutable-block publication, group commit, checkpoint rotation, a destructor-free child exit, and two cross-process reopens, and retains the resulting store as evidence without touching normal startup. Its contract-owned 1 MiB payload is packed by the no-destructor child through segment fsync/rename, manifest and double CONTROL publication, and loose reclamation; the parent and final child must both recover the catalog and read the exact payload through segment random I/O. Its machine-readable result records each process/reopen wall-clock observation and a 4,096-entry-bounded retained file count/length for controlled same-volume comparisons; these observations are neither release certification nor a performance claim. `contracts/storage_v2.json` is the machine authority for protocol v2 field IDs and bounds. The Rust codec implements canonical flat MessagePack with a big-endian length prefix and CRC32C suffix, explicit nonzero correlation/deadline/owner/schema identities, nullable tenant/command fields, Hello version/schema negotiation, mutually exclusive success/error responses, and command-bound 1 MiB blob chunks. It rejects oversized declared frames before allocation and exposes guard-based read/write byte admission. Its mandatory default-deny semantic admission binds decoded requests to generated metadata for all frozen operations, matches request identity against authenticated owner/tenant scope, validates negotiated schema and deadlines, and requires command IDs for every command and maintenance request independently of receipt policy; the pre-authority loopback listener accepts one identity-bound streamed artifact or task-result checkpoint request at a time, requires every chunk to repeat a positive total-length witness, reserves that total once before an exact-capacity allocation, and admits at most 64 ordered chunks/64 MiB under the shared frame budget through dispatch; successful responses above 1 MiB use at most 64 identity-bound chunks/64 MiB followed by one empty success terminator, and the Python harness validates and transparently reassembles that sequence without constructing a second chunk list or final response copy.

Immutable payload compaction packs at most 65,536 same-hash-shard blocks/256 MiB payload behind a sorted index of at most 3 MiB. Its canonical CONTROL-referenced catalog admits 4,096 segments and at most 16 point-lookup candidates per shard. Compaction durably writes the segment and a forced-loose manifest root, publishes that root, installs the bounded reader, republishes the same root into the fallback CONTROL slot, and only then removes loose victims; any error before stabilization leaves those victims intact. The explicit authority planner reuses its reachability marks, loads indexes for only one selected shard, excludes already packed IDs, requires 128 live loose files before acting, and releases mark-spill files before allocating the replacement segment. Normal open reads one at-most-4-MiB catalog block, loads indexes only for a matching point lookup, and never scans the segment directory. Payload catalogs use CONTROL layout v2 while current readers accept zero-extended v1; older binaries reject v2 rather than ignore a catalog whose loose blocks may already be reclaimed. Incremental backup reads through the installed catalog and restores portable loose blocks. Index, payload, syscall, short-write, lost-sync, fallback-slot and cross-reopen faults fail closed while preserving committed references.
The generated baseline `contracts/storage_operations_v1.json` freezes all 331 operations at
schema 58 / registry 38 without copying SQL or handlers.  Existing operations
retain handler-enforced ownership until their owner keys and constraints move
into the future Schema/Transaction IR. The catalog generator also owns the sorted Rust metadata projection used by semantic admission, so catalog drift fails before dispatch code can compile against stale classifications.

This slice tolerates any one silently lost file/directory sync, but lacks correlated multi-sync-loss and extended rotation fault certification and block encryption. Each v2 transaction envelope carries a tri-state entity-root update and every successful authority commit publishes the resulting current root in checksummed `CONTROL`; normal entity open reads and verifies only that content-addressed root page, descendants verify lazily on accessed paths, and locally generated commit pages no longer trigger a whole-tree scan. Legacy slots require only one exact bounded transaction lookup. Lost-ACK recovery, group commit, checkpoint rotation and incremental backup/restore preserve the same root witness, and legacy CONTROL and backup manifests decode with an explicit unknown-root state. Stream appends atomically persist their cursor and immutable segment references inside that root; normal Authority open performs no historical stream rebuild, while bounded predecessor lookup and forward traversal load at most 1,000 events and 8 MiB on demand from the caller's MVCC snapshot. The generated Schema/Transaction IR contract currently compiles or routes two hundred eighty-one operations: both `browser.site_observation` operations, all six `compaction_archive` operations, `system.schema_version`, `system.reclaim`, `rate_limit.record_and_check`, all five `daily_cost` operations, both `log_aggregate` operations, all ten `optimizer.proposal` and `optimizer.action` operations, all eleven scheduler task and poll operations, all twelve timer definition, active-feed, progress, and poll-ledger operations, all sixteen queue item, lease, reap, and autopilot-marker operations, all nineteen orchestration definition, run, event, and Goal-run operations, all eight durable swarm session and agent-checkpoint operations, all sixteen integration workspace, event, status, and global worker operations, all nineteen `project_brain` projection, active-work, work, narrative, checker, decision, watch, cursor, rebuild, owner-scoped recovery, and format-native cutover operations, `conversation.activity_dates/clone/create/count/delete/get/list/metadata.update/purge/restore/search/settings.update/trash.prune`, `turn.append_settled/create_pair/attempt.bind/attempt.claim/attempt.create/attempt.dispatch_worker/attempt.dispatchable.list/attempt.get/attempt.start/branch.create/branch.delete/compact/delete/event.record/events.list/events.prune/exists/get/image.get/list/list_delta/perception.record/projection.update/queue.activate/queue.cancel/recover/related.announce/revision/steer.commit/sync.changes/sync.page/sync.prune/sync.snapshot/timing_trace.get/timing_trace.list`, three `desktop.egress_agent` operations, `record.get/list/put/delete`, and `event.append/append_batch/list/latest/bounds/inspector_summary/prune`, plus `project.recent.list/touch/touch_many/clear` and all six `provider` operations, all seven `worker_job` operations, and all nine `model_routing` operations, and all seven `task_results` operations, plus all seven `tenant.user` account operations, all eleven `credential` operations, and all fifteen `billing` operations, with version CAS, prefix range witnesses, bounded blob overflow and atomic receipt/outbox effects. The schema-version query returns generated frozen metadata rather than inspecting a physical backend. Activity-date projection scans at most 10,000 compact owner-scoped candidate records and 100,000 aggregate 24-byte main-lane timestamp records under the same snapshot, retaining only distinct interval ordinals per conversation; a completeness marker is established only from an empty active-and-trash owner scope, while markerless legacy candidates and completely absent legacy Turn timestamps use bounded compatibility reads and partial marked indexes fail closed. Seven wallet/ledger operations atomically maintain immutable entries, exact global ID/reference claims, wallet balances, O(1) user sum/count aggregates, descending bounded indexes, and active-reserve projections; checked signed arithmetic and exhaustive fault injection preserve one complete money prefix. All eleven credential operations use exact-verified tenant-global ID and secret-hash indexes, an exact 1,000-live-row owner/tenant count, a descending created-time index, blob-capable settings, and compact mutable state; authentication/touch witness bound account state without rewriting settings, public projections redact hashes, and revocation retains identify-only hash tombstones. Settled Turn ingestion uses OCC-protected lane head and exact-count records to allocate monotonic ordinals and commits the Turn/blob document, lane and update indexes, non-human attempt, conversation revision/main-lane count/timestamp/index, search invalidation, sync event, receipt, outbox, and tenant-global Turn/attempt ID claims as one authority transaction; those claims serialize cross-owner collisions without widening ordinary owner reads. `turn.delete` resolves at most 2,000 requested and branch-lane descendants before writing, rejects pending/running attempts, then atomically removes Turn/attempt/index state, repairs exact lane and conversation counts, and writes revision- and age-ordered tombstones plus one sync event. Lane heads remain monotonic across deletion gaps, new Turns select the latest surviving predecessor, and tenant-global identity claims remain fenced through tombstone retention to prevent delete/recreate ABA. Each delete prunes at most 256 owner-scoped tombstones older than seven days through the age-leading index and releases only claims whose stored owner matches. Deterministic syscall-error and short-write injection verifies recovery to one complete append or delete transcript/index/count/tombstone prefix. Settled projection replacement uses projection-revision CAS, preserves typed stale/in-progress failures, and atomically rekeys the blob-capable document, lane/time indexes, conversation revision, search marker, outbox and a deterministic compact `turn.patch` replay event; exhaustive commit fault injection admits only the complete pre-update or post-update state. Branch create/delete uses the same projection-CAS path, derives a command-stable UUID-shaped lane identity, bounds nested deletion to 2,000 Turns across 256 lanes, and atomically publishes descendant tombstones with the parent update under one conversation revision and one compact patch event; empty branches remain deletable. `turn.get/list/exists/revision/attempt.get` remain owner scoped; `turn.list_delta` scans a bounded owner/conversation/time revision index, suppresses unchanged overlap rows before blob materialization, and refuses more than 2,000 candidates or 8 MiB instead of returning an incomplete view. Sync snapshot/page/changes use bounded tail scans, exact replay heads, stale-cursor fencing, and top-level Turn/Attempt routing identities on specific changes, and `conversation.get` plus transcript-bearing `conversation.list` derive their legacy messages from that same authority with bounded response and message-window semantics. Conversation creation atomically publishes the owner header, exact O(1) count, search-dirty marker, receipt/outbox, and a narrowly scoped tenant-global ID claim that preserves active/trash uniqueness without granting generic cross-owner entity access; deterministic syscall and short-write injection verifies replay convergence. Owner-scoped conversation get reads large settings and Turn projections through bounded blob paths and projects the storage.v1 full-transcript and message-window shapes without exposing physical document envelopes. Settings snapshot-CAS and scalar metadata updates retain transcript `rev`, use the hidden physical document version to prevent lost updates, and commit search invalidation, receipt, and outbox atomically; the shared update commit path also passes exhaustive syscall-error and short-write recovery. Create and updates maintain an owner-local covering `(updated_at DESC,id DESC)` index in the same transaction using a prefix-safe descending UTF-8 encoding; normal list and cursor-based catalog pages consume bounded range scans without N+1 entity reads, preserve snapshot-consistent counts, filters and settings projection, and reject responses above 8 MiB. Event batches accept up to 500 items even when every item targets a distinct physical stream; the entity write set, family-record count and inline transaction envelope remain independently capped at 14,336, 512 and 256 KiB. Sparse event application sequences use atomic natural-key, event-type, age-leading retention and physical-position indexes over the continuous physical stream cursor; unfiltered list resolves up to 1,000 positions in one segment pass, while exact types and literal prefixes use a bounded type catalog plus ordered index merge without scanning high-volume unmatched events. Retention deletes at most 1,000 oldest owner-scoped rows per transaction, treats blank types conservatively as structural, and atomically repairs bounds, filters and inspector aggregates. The same commit advances a durable owner-scoped retirement queue for one stream and at most 128 complete immutable segments; stream metadata v2 records the retained base while decoding v1 as base one, retired cursors fail explicitly, and monotonic append continues after fully retired history. Separate bounded authority GC then reclaims blocks no longer referenced by the current catalog, retained history, snapshots or capsules. Inspector summaries fold structural counts and first timestamps directly from the same type metadata for at most 100 roots and their bounded `#agent:` children without reading event bodies. Prefix or child expansion reaching 1,000 entries returns resource exhaustion instead of an incomplete result. Private `_wire_*` projection and payloads above 256 KiB remain explicitly rejected. Browser site observations use digest-keyed, exact-identity-verified blob-capable documents plus an atomic owner-local LRU index; passive hint validation, 30-day expiry, 64-row expiry pruning, deterministic oldest-first eviction and the 200-document owner budget require no unbounded namespace scan; deterministic syscall-error and short-write injection preserves atomic document, LRU and outbox recovery. BYO providers use blob-capable exact-identity documents, a 32-row owner/tenant-label quota, a newest-first covering index, and a tenant-global ID claim; list excludes ciphertext while mutation, receipt, outbox, count, index, and claim publish atomically. Durable worker jobs use tenant-global owner-bearing blob-capable documents, exact task and per-user idempotency identities, 1001-priority per-kind availability summaries, and ordered queue/lease indexes; enqueue, bounded multi-kind claim, monotonic heartbeat, cancellation, completion, fencing, receipt, and outbox transitions publish atomically. Model-routing revision CAS atomically publishes separate blob-capable current, backup, and migration-receipt records; sealed secrets use an exact 1,024-row owner/boundary count, reference and updated-time indexes, ciphertext-redacted lists, and 256-row pruning. `task_results` checkpoints store up to 64 MiB through an immutable tenant-global blob graph while a compact owner-bearing header owns semantic version CAS and abort fencing. Default replay reads only a separate metadata/error projection, summary scans consume owner-local covering rows, terminal replay alone materializes content/thinking, and abort updates no payload blob. Owner-local live and hashed experiment indexes make restart settlement and cost scans proportional to live or matching tasks rather than payload bytes; recovery atomically advances header, summary, and index state without rewriting the full blob, while cost results fail closed above an 8 MiB aggregate. Guarded-v1 checkpoint atomically witnesses its active parent and merges bounded cache HWM/LWW facts, using an exact response echo to retire rolling-upgrade compatibility reads; identical stale-version lost-ACK replay remains stable. Payment records additionally use exact provider/ID claims, a blob-capable 8 MiB document, a bounded created-time index, and atomic wallet/ledger settlement; duplicate provider delivery returns before unrelated payload validation, matching storage.v1. Redemption codes retain the 10,000-code mint contract through 4,096 bounded blob-capable batch, locator, and mutable-state shards: maximum mint and list workloads remain below point-witness and write-set ceilings, while one-code consumption rewrites only one compact state shard. Stale-reserve queries consume the atomically maintained age projection without ledger scans. A compact lane covering index lets `turn.compact` validate up to 100,000 structural rows without hydrating retained Turn blobs; one CAS transaction admits at most 2,000 combined deletion/reparent/projection mutations under a 64 MiB materialization ceiling, with at most 512 reparented Turns and 512 projection updates totaling 8 MiB, repairs ancestry/counts/indexes, inserts the summary, and emits one snapshot-required replay invalidation. Queued pair activation now atomically removes the exact queue binding, installs recovery, dispatchable, and lane-live indexes, and emits one replay revision; cancellation instead removes only the never-started pair and all of its queue, Attempt, event, and index state. Live steering fences the exact unqueued, task-bound Attempt and current running Turn, retires any materialized projection head, appends one command-keyed injection, and publishes its revision, compact sync patch, receipt, and outbox atomically under exhaustive commit-fault certification. Related-Turn announcement reads at most 2,000 explicit identities and advances the live root through a head-consistent no-op patch, preserving externalized projection checkpoints while publishing one Attempt/conversation replay event and revision atomically. Visible-run synchronization admits at most 2,000 messages/8 MiB, removes only provably empty virtual-user ghost pairs before authority access, inherits full live tool fields by stable call identity, and atomically publishes deterministic child Turns/Attempts, stable render segments, root projection/head state, indexes, replay, and revision. Optimizer proposals and reversible action audit rows use blob-capable owner-scoped documents, exact 4,096-proposal and 2,048-action quotas, prefix-safe compact time/status/proposal/active-expiry indexes, 500-row lists, and an 8 MiB response ceiling; proposal existence, compact status, action indexes, receipts, and outbox records publish atomically. Research Foundry reports and optimistic workspaces use bounded owner-scoped blob documents; compact prefix-safe state and a descending-created index make direction discovery independent of report size, while artifact quota, workspace revision CAS, receipts, and outbox records publish atomically. Paper notes use bounded owner-scoped blob documents and a prefix-safe chronological paper/language index; exact counts, documents, indexes, receipts, and outbox records publish atomically without list-time body scans. Raw provider archives use owner-bound blob-capable documents, compact task/round/Attempt/conversation indexes, exact tenant/Attempt/owner/conversation accounting, and strict bounded zlib reads; parent fencing, quota scrubbing, lifecycle deletion, indexes, usage, identity claims, and outbox records publish atomically without body scans. The remaining 50 operations stay default-denied. Missing release work includes
The artifact Schema IR owns bounded chat and tool-result physical layouts: 8/16 MiB owner-bound blobs, atomic dedupe/version/library/expiry indexes, UTF-8-safe 64 KiB range reads, backup/GC reachability edges, and exhaustive single-fault commit recovery. Eleven storage.v1 operations compile through typed Transaction IR with owner checks, receipts, compact content-digest outbox evidence, and exact CPython 15.0 casefold search generated from a checked machine source. Authenticated request chunks admit the large write bounds without relaxing the 8 MiB single-frame limit. The single low-priority maintenance worker prunes at most 128 expired owner-scoped tool results per transaction with content-free progress metrics; the explicit maintenance RPC uses that same owner-scoped path with a 5,000-row hard ceiling. `system.reclaim` executes one explicit launch-budgeted physical GC round, preserves the legacy request bounds, and reports content-free block/segment progress without manufacturing a business transaction. `rate_limit.record_and_check` uses exact owner-scoped event identities, a 256-row expiry index, and a radix-256 timestamp count tree so arbitrary seven-day sliding windows require at most 2,040 counter reads rather than an event scan. The five `daily_cost` operations use chronological date keys, an exact owner count, blob-capable documents, bounded 100-row month scans, one-row reverse latest reads, and at most 366 point probes; whole-cache deletion is one range retirement, and responses fail before exceeding 8 MiB. Scheduler Schema IR fixes tenant-global owner-bearing blob-capable task documents and exact task-ID claims, owner-local exact system-key claims and counts, owner-local plus narrow tenant-global created/enabled covering indexes, a global poll sequence, and a 200-row owner/task poll index. All eleven task and poll operations now execute through the shared OCC layout: adoption preserves the oldest legacy identity while retiring duplicates, the internal cross-owner feed verifies embedded owners, due claims and result accounting update compact task state, and task deletion range-retires poll partitions. Receipt replay, SQLite differential coverage, focused owner/timestamp tests, and deterministic syscall-error/short-write create recovery preserve the frozen semantics. Timer Schema IR adds tenant-global owner-bearing blob-capable documents and exact ID claims, owner-local status/conversation indexes and exact counts, a narrow oldest-first global active index, and conversation-prefixed poll partitions. A one-shot launch probe bounds active timers at 8–16 by default with a 64 hard ceiling; historical rows are capped at 512 per owner so conversation deletion atomically retires all timer state within transaction bounds. Poll append/progress, receipt, and outbox effects share one OCC commit, and repeated poll IDs never double-advance. Differential, page-boundary, lifecycle, and exhaustive syscall-error/short-write tests preserve these semantics. Queue Schema IR separates immutable blob-capable payload cores from compact mutable position, lease, and binding state; exact tenant-global ID claims, owner-local order indexes, and narrow owner-bearing conversation, lease, and autopilot indexes keep worker feeds bounded without widening public scope. Conversations admit at most 512 items, reap releases at most 128 leases, internal indexes page through the 1,000-row Entity boundary, and responses stay below 8 MiB. All sixteen queue operations execute through one OCC transaction: real messages supersede synthetic continuation work atomically, autopilot rows never dequeue or reap, idle reap creates neither receipt nor outbox churn, and permanent conversation deletion retires queue and marker state. SQLite differential coverage and deterministic syscall-error/short-write injection preserve legacy ordering, lease, dedupe, receipt, and recovery semantics. Orchestration Schema IR separates immutable blob-capable run inputs from compact mutable state, uses exact tenant-global identities and owner-bearing bounded startup indexes, and stores versioned event documents. All nineteen orchestration and Goal-run operations share one OCC authority; exact active claims make Goal supersession O(1), public reads re-verify owner scope, and startup recovery retires at most 1,000 active runs across owners. Definition CAS, event projection, terminal fencing, receipts, and outbox effects are covered by SQLite differential and exhaustive commit-fault tests. Swarm Schema IR separates blob-capable session specifications and agent message/result cores from compact lifecycle, rounds, delivery, and exact resumability state. Exact tenant-global owner-bearing keys prevent aliasing; a 512-row owner-local resumable index makes startup proportional to useful work, 1,000-agent session deletion stays within one transaction, and delivery acknowledgement never rewrites transcripts. SQLite differential, owner-isolation, page-boundary, and exhaustive checkpoint commit-fault tests cover all eight operations. Integration Schema IR adds tenant-global owner-bearing workspace documents, exact natural and row identities, oldest-first ready/integrating indexes, one exact project-active claim across owners, bounded owner/project status indexes, and a globally sequenced 300-event retention window. All sixteen integration operations preserve worker CAS, stale-claim recovery, metadata projection, receipts, and outbox effects; SQLite differential, cross-owner serialization, retention, and exhaustive commit-fault tests lock the semantics. Attempt creation adds exact command replay, per-Turn counts, projection/input CAS, bounded checkpoint/regenerate rewrites, and atomic attempt/event/sync publication under exhaustive commit-fault testing. A tenant-global owner-bearing created-time index exposes at most 32 canonical conversation-executor attempts and revalidates exact owner Turn, attempt, and config records under the same snapshot. Worker dispatch validates a canonical explicit principal and atomically enqueues the deterministic job, binds the attempt, removes discovery eligibility, and publishes sync/outbox state; replay fails closed unless binding and durable job agree. Compact owner-scoped task/effective-time and conversation/created-time indexes make timing-trace lookup and discovery proportional to at most 100 small records; list never reads projection blobs, while detail validates the exact indexed Attempt and Turn before using the permanent trace or legacy projection fallback. `turn.perception.record` now appends the closed, content-free browser receipt lane idempotently inside the Attempt document. It enforces the shared 64-row/96-KiB bounds, derives render and transport clocks, adopts only task-matched legacy receipts, and commits without Turn/revision/sync/receipt/outbox amplification under exhaustive crash and short-write testing. Owner-scoped sync-age markers and exact Attempt-event transport references now make `turn.sync.prune` proportional to expired replay data; each transaction processes at most 999 events plus one continuation witness and 64 MiB while preserving the monotonic sync head. Terminal Attempts maintain settled-time cursors so `turn.events.prune` retires at most 64 clustered ranges and 200,000 rows under a 64 MiB hydration ceiling only after sync references and projection heads clear; permanent Turn projections are never retention data. Both operations are owner-isolated, resumable, and crash-atomic. Existing authorities will acquire these indexes only through the explicit migration required before cutover; ordinary startup never scans or backfills historical data. This leaves 281/331 operations executable and 50 unavailable.
remaining operation payload compilers, TofuSQL, migration, GC spill scale and representative native/network/FUSE certification matrices, signed release packaging, and Supervisor selection. Normal Tofu
startup therefore cannot discover it or store authoritative user data. Selection and packaging remain prohibited until semantic differential tests,
deterministic syscall faults, explicit resumable migration/rollback, native durability
tests, a 30-day shadow run, and performance/resource gates pass.

## Identity and dependency rules

- Auth resolves identity once at the request boundary. Every durable
  user-owned operation below it receives a positive `owner_user_id` (some
  established repository method parameters remain named `user_id`; their
  value is always the numeric owner, never an account subject).
- Routes contain no SQL and do not derive storage paths.
- Application services speak domain vocabulary; operation handlers translate
  that vocabulary into backend-neutral SQL authored inside the Sidecar.
- SQLite and PostgreSQL expose the same operation/result/error behavior.
- Resource IDs are never treated as proof of ownership. Reads and writes join
  or filter by the explicit owner.
- Personal mode may currently compose owner `1`, but repositories and schemas
  must not encode “one user exists” as a core invariant.

Account and credential storage is defined by
[`contracts/identity_v1.yaml`](../contracts/identity_v1.yaml). `tenant_users.id`
is the opaque account subject; its unique `owner_user_id` is allocated by
`storage_identity_sequences`. `auth_credentials` is the only bearer authority
and stores a one-way token digest. Credential administration filters by exact
owner and tenant, while authentication atomically updates `last_used_at` and
rejects credentials bound to inactive accounts.

## Commands, queries, and idempotency

`query(operation, payload)` is read-only. `command(operation, payload,
command_id)` is one transaction. Operations with non-reconstructible effects
require a command receipt written in that same transaction.

- same command ID + same operation and canonical payload returns the committed
  response;
- same command ID + different operation or payload is `database_conflict`;
- a rollback writes neither receipt nor business mutation;
- a read-only refusal or empty lease claim is not receipt-cached;
- maintenance commands use the low-priority writer lane and bounded batches.

Command receipts remain capped at 64 KiB on both backends. Responses above
that legacy JSON size get one private zlib level-1 attempt only when their
decoded form is at most 4 MiB; incompressible or larger responses still reject
and roll back atomically. The versioned decoder bounds output before parsing,
fails corrupt/unknown envelopes as ``database_integrity``, and continues to
read every legacy plain-JSON receipt byte-for-byte.

RPC accepts named operations and JSON-compatible payloads only. SQL, paths,
connections, and transaction handles have no wire representation.

Recent-project history uses the owner-scoped `project.recent.touch_many`
command when a project selection carries more than one root. One receipted
transaction accepts at most 32 non-empty paths of at most 4,096 characters,
deduplicates them, locks/updates them in stable path order for PostgreSQL, and
returns only `{touched}`. A lost-ACK replay therefore cannot increment usage
counts twice, owner scopes cannot mix, and a multi-root selection pays one RPC
and one receipt instead of one of each per root.

Declarative plugin manifests may expose `get`, bounded `list`, `put`,
`delete`, and same-physical-table `batch` operations. A batch is one Sidecar
command transaction, rejects duplicate keys, and carries per-row version
witnesses for optimistic concurrency. Prefix/cursor traversal remains bounded
by the manifest's `limit_max`; plugin code never supplies an expression or SQL
fragment. The startup-only `legacy_scan` action is further restricted to the
exact table, column, and ordering identifiers validated from the immutable
manifest. It exists solely for verified cutovers from tables already owned by
the authority; it is read-only and cannot delete or mutate the legacy source.

## Conversation data

`storage_conversations` is the active header/settings index.
`storage_conversation_turns` is the sole active transcript authority.
For Turn-native conversations, `messages_json` and aggregate `search_text` on
the header are frozen empty placeholders. A conversation imported before the
Turn cutover instead keeps its frozen, potentially non-empty `messages_json`
as the compatibility transcript until an explicit verified migration; runtime
writes never replace either shape with a conversation-sized message document.

Owner-scoped `conversation.get` exposes mutually explicit full, metadata-only,
and bounded message projections. A `message_window` is limited to 1..500 and
may carry a non-negative exclusive `before_sequence`; a cursor without a
window is invalid. Active Turn transcripts page through the backend-neutral
adapter contract. Frozen pre-Turn JSON archives scan only a requested first or
last page when its estimated and observed Python work stays within 128 KiB of
code units; middle pages, count/shape ambiguity, or a larger suffix/prefix use
the authoritative full decoder and then slice. Windowed reads never select or
return the unrelated aggregate `search_text` corpus.

The metadata-only `conversation.list` query accepts optional bounded
`project_path` and `title_contains` filters. The Sidecar combines them with the
required owner predicate before ordering and limiting, then applies
`settings_keys` projection before the result crosses the RPC boundary. Title
matching preserves Python `str.lower()` substring semantics across SQLite and
PostgreSQL: the authority scans keyset pages of at most 512 lightweight
`id/title/updated_at` rows, stops once the requested result bound is met, and
projects full metadata only for matches. Thus Unicode casing and literal `%`
or `_` retain the caller's established behavior without returning the whole
catalog. An explicit empty `settings_keys` whitelist projects SQL constant `{}`
instead of reading or decoding the stored settings document. Project consumers
retain only the small `projectPath` witness needed to fail closed during a
mixed-version rollout.

`conversation.activity_dates` accepts an owner, bounded candidate filters, and
an explicit strictly increasing millisecond-boundary vector. It counts each
conversation at most once per `[start,end)` interval. Turn-native rows select
only the typed JSON `timestamp` scalar plus their row-time fallback in batches
of 64; PostgreSQL keeps JSON scalar types rather than coercing numeric zero to
text. Frozen pre-Turn archives load at most four stored documents per query and
decode one at a time, preserving malformed-data failure and missing-timestamp
fallback. Only the interval counts cross the storage frame.

At rest, a versioned private projection codec interns only tool-segment
``input`` / ``result.content`` values that are exactly equal to the uniquely
identified ``toolRounds`` copy. Every semantic read hydrates those references
before mutation, search extraction, or return, so the public turn/API contract
is unchanged; divergent, ambiguous, partial, and future shapes remain verbatim.
Malformed or unknown codec references fail closed as ``database_integrity``.
Turn-native rows are converted only by their next ordinary write—there is no
startup scan or rewrite competing for the authority writer. Frozen pre-Turn
conversation archives remain readable in their plain form. An explicit
physical offline deep-clean may apply the sequence form of the same codec
message by message, but only after an exact canonical round-trip and only when
the stored document becomes smaller; repeated passes are write-free.

Conversation search is an eventually consistent, rebuildable projection. A
turn or lifecycle mutation writes only one small version-token dirty marker to
`storage_projection_outbox` in the authority transaction. Search-text
extraction, historical scans, and projection I/O never run inside the user
transaction or the authority writer. An acknowledgement deletes only the
exact token the worker read; a concurrent mutation replaces that token and
therefore cannot be lost behind a stale acknowledgement.

SQLite materializes `storage_search_conversations` and
`storage_search_turns` in a private host-local `turn-search-v1.sqlite3`, keyed
by a hash of the authority data root. The file is disposable, quick-checked on
open, discarded and rebuilt on corruption, and bounded by
`TOFU_TURN_SEARCH_PROJECTION_MAX_MIB` (personal default: 2% of observed free
space, clamped to 128 MiB..4 GiB). Search failure degrades only search; it does
not revoke authority readiness or roll back a user write. PostgreSQL uses the
same tables in the shared database but writes them through independent,
separately committed maintenance transactions.

The Sidecar owns the projection worker. It drains current dirty entities first,
then begins historical backfill after the default 60-second quiet period using
bounded 8-row/2-MiB authority pages; oversized sources are omitted explicitly.
Global cursors include owner, conversation, and turn identity. Generation tokens
keep old rows searchable until rebuild completion and fence concurrent mutation.
The pre-authority Rust target tightens publication: eight-Turn pages build an
invisible generation in a separate Tofu-DB directory, then one transaction swaps
its ordered header and retires old ranges. Fixed per-Turn n-gram Bloom summaries
reject negatives before chunked text reads; exact verification preserves legacy
phrase-first, cross-Turn word, snippet, owner, and ordering behavior. Owner bytes
are bounded and syscall-error/short-write crash tests expose one complete generation.
The Rust daemon can opt into a dedicated absolute persistent projection directory outside the authority. Its sole maintenance worker drains at most 16 epoch-valued dirty conversations per round, releases authority between 8-row/2-MiB source pages, and acknowledges only exact observed epochs. `conversation.search` uses a separate deadline-bounded projection mutex, returns a retryable capability error when unavailable, and never acquires or fails the foreground authority. Capacity is 2% of launch-observed free space clamped to 128 MiB..4 GiB per owner. `turn.search.backfill` remains a scheduler and legacy aggregate search fields remain non-authoritative. The separate `backfill-activity-index` operator command is explicit existing-authority maintenance: it holds the normal authority lease, persists a building cursor, materializes at most 256 source headers/16 MiB per transaction, and requires repeated invocations until `complete=true`; foreground mutations maintain membership while building, and neither daemon open nor startup performs this retrofit scan.

This split follows the same durable-authority/projection boundary visible in
the vendored Codex state runtime: Codex keeps state, logs, queues, and
paginated thread history in separate SQLite databases, and explicitly refuses
to retrofit a full `VACUUM` during startup because maintenance would contend
with foreground writers (`codex/codex-rs/state/src/sqlite.rs`). Tofu goes one
step further for a network-mounted authority by placing rebuildable search on
a host-local database and rejecting automatic page relocation before it
reaches the authority writer.

Header deletion/restore/clone is defined by
[`contracts/conversation_lifecycle_v1.yaml`](../contracts/conversation_lifecycle_v1.yaml):

- delete atomically moves the header and normalized turn graph to `storage_conversation_trash*`, removes executable state, and makes every
  active query/write observe “missing” immediately;
- restore atomically rebuilds active rows without attempts or live latches;
- clone atomically creates a new terminal graph and remaps executable
  identities; the browser never uploads a message array;
- maintenance permanently purges trash after 30 days in bounded oldest-first batches.

Tofu-DB compiles delete, restore, purge, and clone through bounded lifecycle primitives. Delete pins recoverable header/Turn/tombstone ranges and detaches active indexes, attempts, and sync replay without walking transcript rows. Restore mounts those ranges, overlays a normalized header, advances revision plus a conversation execution epoch, repairs the exact owner count, and removes trash metadata/pin in one receipt-backed commit. Turns from older epochs project as inert: attempt/run latches disappear, live status becomes interrupted, and runtime/tool presentation is terminalized; post-restore Turns inherit the new epoch. Clone reads one OCC snapshot capped at 2,000 Turns, 8 MiB of projection, and 1,000 archives, atomically creates a new header/lane graph/index/identity-claim set, remaps Turn/task/archive/parent identities from the command, and copies no attempts or executable latches. Archive transcript, summary, and receipt documents are separately blob-capable: summary updates never rewrite transcript payload, and same-owner clone reuses immutable content-addressed blocks. Live source projections are terminalized while source execution remains unchanged. Identity claims remain fenced until bounded tombstone pruning or purge, and legacy randomly keyed attempts also require an active header. Explicit backup/GC traversal follows persisted stream segments and owner-bound versioned-document, command-receipt, and logical-outbox blob graphs from active or pinned Entity leaves without transaction history; normal open and foreground delete/restore operations never scan values. Payload-segment compaction and certification remain authority gates; any lifecycle action whose foreground cost or temporary bytes scale with a 90 GB transcript fails closed.

Conversation Sync v3 owns turn commands, snapshots, and replay; see
[CONVERSATION_SYNC_V3.md](CONVERSATION_SYNC_V3.md).

## Transaction model

### SQLite

- One physical read-write connection serializes all writes; read-only pooled
  connections cannot upgrade to writers.
- Active RPC handlers stay hard-capped. The accept loop may wait at most 100 ms
  for one slot so a short scheduling burst drains through the bounded kernel
  backlog; sustained saturation receives a classified retryable rejection.
- The writer uses weighted user/event/maintenance lanes.
- The three lanes share one launch-probed waiting-job ceiling
  (`TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY`; personal 8..64, distributed
  128, hard ceiling 1,024). Saturation fails before retaining the operation
  with retryable `database_busy`. An acquisition timeout removes a job that is
  still queued, releasing its decoded request/closure immediately; a job
  already drained into a batch remains fenced by its cancellation bit.
- Backlogged compatible writes share group commits, while each logical job
  keeps its own deadline, savepoint, result, and rollback behavior. The sole
  cross-domain writer receives one launch-probed, bounded 8..64 MiB personal
  page cache (`TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB`; explicit ceiling
  256 MiB); readers retain SQLite's lean cache.
- Each backend owns one reconstructible, revision-keyed live Turn projection
  cache (`TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB`). Probe failure uses 16 MiB,
  the 8 GiB reference host uses 32 MiB, adaptive personal mode stops at
  128 MiB, distributed mode uses 256 MiB, and explicit values stop at 1 GiB.
  Entries are additionally capped at 256 and expire after ten idle minutes;
  charged bytes are three times canonical stored bytes to cover hydrated
  Python containers. Revision/owner/conversation/Turn/attempt mismatch,
  terminal settlement, backend close, or LRU pressure discards an entry and
  safely reloads durable authority.
- Durable task-event producers derive both waiting-object and serialized-byte
  ceilings from the same launch-probed writer budget. Probe failure resolves
  to 256 objects / 64 MiB, the 8 GiB reference profile to 512 / 64 MiB, and
  distributed mode to 4,096 / 512 MiB; explicit overrides remain hard-capped
  at 8,192 objects / 1,024 MiB. One claimed batch may exist beyond that waiting
  queue, but retains at most 500 events and 60 MiB, leaving protocol headroom
  below the 64 MiB `storage.v1` frame.
- Event serialization and frame size are validated before queue admission;
  item or byte saturation fails immediately with retryable `database_busy`.
  A confirmation/flush timeout identity-removes work not yet claimed and
  releases its payload before the natural-key direct fallback, preventing a
  later redundant batch request. Already-claimed work keeps the existing
  ambiguous-commit contract and is safely deduplicated by `(task_id,
  sequence)`. The 1 ms idle gather window still waits for commit before
  visibility and preserves concurrent-stream amortization.
- Commands use `BEGIN IMMEDIATE`, bounded acquisition, and progress-handler
  deadlines. Rollback precedes error classification. Diagnostics publish the
  current `begin` / `execute` / `commit` / `rollback` / `post_commit` phase.
- Writer commit latency and application event-persist latency retain only a
  recent sample window derived from that same waiting-job budget: 2,048 on
  probe failure, 4,096 on the 8 GiB reference profile, and 32,768 in
  distributed mode/hard maximum. Metric snapshots copy under their short
  observation lock and sort outside it, so a scrape cannot stall commits.
- A statement stalled 15 seconds past its deadline is interrupted. Because
  SQLite cannot interrupt a kernel-blocked commit/fsync, a writer still stuck
  60 seconds past its deadline hard-exits only the Sidecar; the supervisor
  restarts it and WAL recovery preserves the committed prefix. Both bounds are
  explicit and fail configuration if the hard bound is not greater.
- WAL, `synchronous=FULL`, foreign keys, and bounded checkpoints remain on.
- Personal startup probes the filesystem that actually contains `data/` and
  reserves 1% of its capacity, clamped to 256 MiB..2 GiB. The Sidecar refuses
  startup below that floor; `TOFU_STORAGE_MIN_FREE_BYTES` is the explicit
  operator override.
- FUSE is accepted only after lock, fsync, atomic-replace, WAL-recovery, and
  latency preflight succeeds; durability is never weakened to accommodate a
  mount.
- Before reclamation reaches the writer, the adapter checks the observed
  authority topology. Network, generic userspace, and unknown filesystems
  return `offline_required` without executing any SQLite statement: even one
  `incremental_vacuum(1)` can be an uninterruptible kernel/filesystem call on
  BeeGFS/NFS/FUSE. Known host-local block, memory, and container-overlay
  authorities may enter the bounded online path.
- Eligible reclamation checks its wall budget between
  `incremental_vacuum(1)` units.
  A freelist of at least 1,048,576 pages that also occupies 25% of the file is
  classified as bulk compaction: online page moves stop for that process and
  the response points to the explicit offline deep-clean workflow.
  Transactional retention operations are registered once with kind
  `maintenance` and dispatched through the same backend-neutral operation
  catalog as commands; they run at maintenance writer priority and never
  accumulate command receipts. Task-event streaming and structural tiers use
  six-hour and 30-day horizons respectively. By default, each tier deletes at
  most 16 separately committed pages of 25 rows; a remaining backlog is
  revisited in 30 seconds, while a drained backlog is probed only every five
  minutes. The
  mutually exclusive, age-only partial indexes make both an empty probe and
  `ORDER BY created_at_ms LIMIT` range-bounded without scanning the other tier.
  Streaming backlog drains before the longer-lived structural
  tier, so one maintenance cycle cannot stack both workloads. An established
  SQLite authority missing the v2 age-leading partial index does **not** use
  the v1 `(stream_kind, event_type, created_at_ms)` compatibility index while
  serving. A deleted-row limit cannot bound how long that legacy search holds
  SQLite's sole writer on a multi-GiB event table. The semantic prune therefore
  returns `legacy_index_offline_required` before any scan, the maintenance
  owner disables that tier for the process, and the explicit offline
  deep-clean/index workflow below owns legacy classification and transition.
  An authority with neither index similarly returns `missing_index`. All
  online retention and reclamation work shares a process-lifetime circuit:
  if any bounded maintenance unit trips the writer watchdog, optional
  housekeeping stops instead of repeatedly starving user/event writes;
  restart performs one fresh probe.
- Best-effort log aggregates persist counts separately from their TTL sweep.
  Failed flushes back off exponentially to five minutes; TTL deletion runs in
  the maintenance lane and removes at most 500 indexed rows per transaction,
  so observability cannot form a retry storm behind user writes.

### PostgreSQL

- The adapter connects to an externally managed primary with verified TLS.
  The application never runs database-server binaries or stores PG data.
- Read/write pools are isolated and capped below the server connection budget.
- `fsync`, synchronous commit, full-page writes, primary status, schema version,
  and transaction deadlines are verified.
- Application startup validates only. `python -m
  lib.storage_sidecar.migrate` is the one schema migration entry point and
  publishes a new version only after the migration transaction commits.
- Distributed worker claims use adapter-owned `FOR UPDATE SKIP LOCKED` row
  selection. SQLite executes the same operation inside its serialized writer
  transaction; backend-specific lock syntax never enters application code.
- Backup/PITR, HA, and autovacuum belong to the platform.

### Adaptive topology and transactional logical shadow

`lib/storage_sidecar/storage_capabilities.py` is the single vocabulary for
storage topology and primitive capabilities. One bounded probe reports the
mount/filesystem class, persistence confidence, free bytes, private-file
creation, file and directory fsync, atomic replace, exclusive locking, and a
SQLite WAL recovery round trip. Startup preflight exposes the topology fields
in health alongside its existing durability results. Known network and generic
FUSE filesystems never become a local SQLite authority merely because a
single-process WAL round trip or latency benchmark succeeds; unknown evidence
falls back instead of being guessed. Paths under the system temporary or
`XDG_RUNTIME_DIR` lifecycle remain ephemeral even when their backing device is
a persistent local filesystem.

The pure adaptive planner may automatically retain direct SQLite or select an
external client/server adapter when that preserves the declared durability
contract. A verified, decisively faster local SQLite front is only a
recommendation until bounded-RPO consent is already present: directory
permissions, free space, or an apparent local disk are not consent. Every plan
names the selected and recommended strategies, durability contract, reason
code, evidence, and whether one user decision is required.

Schema 39 adds `storage_logical_outbox`. When explicitly enabled, the RPC
dispatcher runs the semantic handler and inserts its canonical logical record
inside the same SQLite/PostgreSQL transaction; a command receipt is inserted
after that callback in the same transaction. The encrypted body carries the
validated request/response plus the exact bounded DML mutation program that
the handler executed, including row-count witnesses. A clean refusal or a
natural-idempotency retry that changes no row emits nothing.
Oversize records roll back the domain mutation, and a full pending-byte budget
returns retryable `database_busy` rather than acknowledging an unrecorded
change. The sequence counter and pending-byte counter are reconciled against
the table before serving. Request and response documents are sealed before the
outbox insert with the deployment's authenticated secret envelope, bound to
event ID, tenant, and numeric owner. The table and segment contain ciphertext,
codec/key identity, and non-secret routing metadata—not a second plaintext copy
of credential/provider inputs.

After commit, `lib/storage_sidecar/logical_outbox.py` wakes one bounded
publisher. It reads a small page, releases the database connection, appends and
fsyncs through the `LogicalCommitSink` boundary, then performs a tiny ordered
ack transaction. Network/FUSE file I/O therefore never runs while the SQLite
writer or a PostgreSQL transaction is held. A crash after file fsync but before
ack is safe: retrying the same sequence and event ID compares the canonical
record and acknowledges it as a duplicate; a divergent reuse, gap, checksum,
or lineage fault poisons the publisher and fails closed.

The built-in sink is `lib/storage_sidecar/logical_shadow.py`. Its private,
single-writer segments contain a checksummed lineage header and length-prefixed
canonical semantic records with explicit tenant/user identity, command
identity, schema/operation-registry contract, and sequence. Append fsyncs before
returning; rotation uses atomic rename plus directory fsync; recovery truncates
only an incomplete tail of the sole open segment. It supports local block and
POSIX network/FUSE mounts whose permission, locking, fsync, and atomic-replace
contracts hold. `LogicalCommitSink` is the adapter boundary for an immutable
object-store implementation; no native cloud SDK or credential policy is
silently selected by this repository. An object store mounted without POSIX
semantics must use such an adapter, not masquerade as a directory.

Activation is intentionally low-mind but never implicit durability consent:

- `TOFU_STORAGE_LOGICAL_SHADOW=off` is the default and changes no write path;
- `auto` activates SQLite only with its measured/consented local write front,
  or when an absolute sink directory is explicitly supplied. PostgreSQL auto
  requires that explicit shared directory;
- `required` refuses startup when the configured topology cannot meet the
  contract. Distributed PostgreSQL requires an explicit shared directory;
- every API/worker replica captures into the shared PostgreSQL outbox, while
  only `scheduler`/`all` publishes. The sink writer lock makes another
  publisher a healthy standby rather than a second writer;
- `TOFU_STORAGE_LOGICAL_SHADOW_DIR` selects an absolute sink. Otherwise an
  eligible personal SQLite deployment uses `data/logical-commits`;
- `TOFU_STORAGE_LOGICAL_ACCESS=owner` creates mode-0700/0600 state. An
  explicitly shared service group may select `group`, which requires a
  world-inaccessible group-rwx directory and group-rw files; existing modes
  are validated and never broadened implicitly. Payloads remain encrypted in
  either mode;
- `TOFU_STORAGE_LOGICAL_OUTBOX_MAX_MIB`,
  `TOFU_STORAGE_LOGICAL_SHADOW_MAX_MIB`,
  `TOFU_STORAGE_LOGICAL_SEGMENT_MIB`,
  `TOFU_STORAGE_LOGICAL_RECORD_MAX_MIB`, and
  `TOFU_STORAGE_LOGICAL_PUBLISH_BATCH` are hard bounded. Defaults derive the
  outbox from the launch-time log budget and keep every batch/record/segment
  finite. History is never deleted merely to regain capacity.

Logical payload encryption reuses `TOFU_SECRET_ENCRYPTION_KEY`; distributed
replicas must receive the same key. Personal mode uses the existing private
mode-0600 deployment key. Back up that key independently with the same controls
as other encrypted provider state: losing it does not corrupt the authority
database, but makes logical replay impossible; replacing it while unpublished
outbox/history exists fails key-identity validation rather than guessing.

The database remains authoritative in all three modes. `logical_replay.py`
provides a concrete SQLite/PostgreSQL `BackendReplayTarget`: it authenticates
the encrypted record, requires the exact schema contract, applies its bounded
DML program, verifies every affected-row witness, and advances
`storage_logical_replay_checkpoints` in that same target transaction. Offline
shadow reads resume from that cursor in bounded pages rather than loading the
history into memory. The module also provides constant-memory ordered
projection digests, stable canary selection, and a fail-closed cutover
decision. Promotion must
advance database → shadow → canary reads → logical authority one stage at a
time and requires an explicit operator request, zero outbox backlog, equal
source/sink/replay cursors, identical source/target projection digests, a
minimum verified sample, a ready publisher, and a verified rollback
checkpoint. No runtime code performs that promotion yet. This is deliberate:
zero remote-wait commit latency and zero loss on destruction of the local
device are mutually incompatible; the existing fast-path bounded-RPO consent
continues to own that choice.

## Durable turn-source queue

`storage_queue_items` is the owner-scoped authority for human, Goal,
peer, and workflow turn sources waiting behind a conversation task. Dequeue
leases rather than deletes a source; only accepted Turn/attempt creation may
finalize it. Normal 60-second maintenance calls
`queue.conversations.list_all` with the additive
`tofu.queue.reap-probe/v1` selector. One read-pool grouped query returns the
existing oldest-first conversation list plus whether any non-autopilot lease
is strictly expired. The application enters the `queue.reap` writer
transaction only after that exact capability echo reports useful repair, then
uses the same list for bounded dispatch. A clean tick is therefore one read
RPC and no writer admission, mutation, or receipt. Old Sidecars return the
legacy bare list and retain the former writer repair; process-start recovery
always force-reclaims predecessor leases before dispatch. Lease taking and the
live-task guard remain the race authority, so read-first maintenance cannot
create a second consumer.

## Durable worker jobs

`storage_worker_jobs` is the shared claim authority for accepted distributed
work. `worker_job.enqueue` is owner-scoped and semantically idempotent;
`worker_job.claim_next` increments both the attempt and fencing token while
granting a 60-second lease. A claim must list the task kinds for which that
worker has a complete handler; omission never means "claim everything". A live
worker heartbeats every 20 seconds and may advance its replay cursor
monotonically.

`turn.attempt.dispatch_worker` is the only distributed conversation dispatch
boundary. In one database transaction it binds the current accepted attempt to
a deterministic task ID, enqueues one `conversation-attempt` job, and captures
the running-state sync event. Its payload is limited to durable
conversation/turn/attempt references and the explicit `PrincipalContext`; it
does not copy a conversation projection or request-local provider handles.
Lost-ACK retries resolve the same job, while a legacy `@dispatching` or other
executor binding fails closed instead of creating a second authority.

Every heartbeat, cancellation poll, and terminal settlement proves
`task_id + claim_owner + fencing_token` against an unexpired lease. Once a new
worker reclaims an expired row, the former token cannot write a terminal state.
Cancellation is durable: queued work becomes terminal immediately, while
running work must observe and acknowledge the command. Redis may wake claimers
but is never consulted for job state or correctness.

`lib/durable_worker.py` supplies the synchronous claim/heartbeat/cancel/terminal
runner ports, but registers no production task kinds. A kind is excluded from
the claim filter until its registration names durable event replay, terminal
billing/admission settlement, cooperative cancellation, and fencing for every
externally visible side effect. These declarations are an executable startup
gate, not proof by themselves.

The storage/runner foundation is intentionally not yet selected by the chat
composition root. Worker autoscaling stays disabled until a conversation
handler reconstructs the executor from durable references, all four safety
properties pass crash-injection E2E, and API admission/cancellation use this
authority end to end. The current in-process path continues to use the
one-shot attempt dispatch claim and must not be advertised as cross-Pod
takeover.

## Durable swarm recovery

Swarm startup recovery reads only sessions in the ordinary `running` or
`terminated` lifecycle states. A legacy resumable row without a positive
owner is default-denied and moved by the dedicated
`swarm.session.quarantine_ownerless` command to
`quarantined:ownerless`. The command re-checks the durable owner inside its
transaction, so a concurrent repair wins. Quarantine preserves the session and
all child checkpoints as evidence while excluding their potentially large
message histories at the recovery query boundary; it neither invents personal
owner `1` nor repeats the same invalid work and startup error on every boot.

## Layout and secrets

Personal SQLite state lives under the project data root:

```text
data/
  tofu.db
  backups/
  logical-commits/       # only when logical shadow is enabled and eligible
  .storage-sidecar.lock
  .storage-sidecar-lease.json
  storage-handoff-audit.jsonl
```

The SQLite search projection is intentionally outside this durable tree under
the configured host-local temporary projection root. Backups, handoff, and
logical replay never copy it; the transactional dirty set and authority turns
are sufficient to reconstruct it.

The child Sidecar receives its random loopback endpoint and token through the
owned startup control channel. Its existing parent watcher also owns one
close-on-exec pipe from the application process; EOF releases the Sidecar and
project lease on parent death or in-place image replacement, including when the
numeric PID is unchanged. The pipe is bounded to one per child and adds no
watcher thread. Distributed Pods exchange startup metadata in a mode-0600
Pod-local memory file and remain independently managed. DSN/Redis credentials
come from mounted secret files and never appear in argv, repr, logs, or RPC
payloads.

## Retention and size bounds

- Streaming task frames: 6 hours.
- Structural request-inspector events: 30 days.
- Settled attempt replay events: 1 day in personal mode, 7 days in distributed
  mode; the permanent turn projection is unaffected.
- Per-generation-attempt timing traces are durable user state, not replay
  transport. Schema 46 owns the bounded document in
  `storage_generation_attempts.timing_trace_json`; settlement freezes server
  spans and user-visible phase history there and mirrors the terminal snapshot
  into the current `projection.timingTrace`. Owner-scoped
  `turn.perception.record` updates only the small attempt document under the terminal-event
  lock. Tofu-DB's sole effectless command IR step still commits its owner-scoped OCC mutation
  without Turn/revision/sync/receipt/outbox growth or a generic exemption. Event pruning never deletes
  that snapshot, and a later attempt cannot overwrite an older task's evidence.
  One trace is capped at 256 spans, 128 gaps, 128 prompt rows, 64 client receipts,
  and 96 KiB with explicit dropped counters.
- Schema 47 adds the partial
  `(conversation_id, created_at DESC, attempt_id DESC)` discovery index only for
  attempts with a non-empty task ID. `turn.timing_trace.list` selects no trace
  JSON or content, filters by the joined Turn owner before `LIMIT`, and returns
  at most 100 compact rows plus `has_more`. Thus Request Inspector paging is
  proportional to the requested conversation page rather than a global
  `task_results` scan; index growth is exactly one derived entry per
  externally addressable generation attempt. Native Turn authority also keeps one compact deletion directory per Turn: at most 64 Attempts and 64 KiB, with a 16 KiB versioned tombstone containing every retained Attempt identity. Creation and lifecycle changes update it in the same OCC transaction. Turn and branch deletion use the directory to retire all historical Attempt/event and timing/dispatch state without reading blob-capable Attempt payloads; claims remain fenced for seven days. Trash capsules retain and restore only the inert directory/count identity evidence, not Attempt documents or events, so later deletion/purge releases every claim without reviving work.
- Conversation sync replay rows: 7 days; expired cursors require a snapshot.

Generic cold chat replay uses two compact semantic reads rather than exposing
raw storage records: `task_results.replay_get` filters the requested key by
positive `user_id` before returning status/clocks and withholds cumulative
content/thinking unless the caller explicitly reaches the terminal page;
`event.bounds` returns exact count/min/max cursors without materializing event
bodies. Ordered bodies still come from bounded `event.list` pages. Routes own
neither JSON decoding nor SQL, and missing/foreign task results are
indistinguishable.

Task-result writes remain receipt-free, version-witnessed snapshots. The
additive `tofu.task-results.checkpoint.guard/v1` mode first takes the same
owner-qualified conversation lifecycle lock used by delete/purge, validates
the parent in that transaction, then takes the task-result key lock and
enforces task ownership, terminal regression, abort-tombstone preservation,
identical replay, and CAS. A missing parent, foreign key collision, or proven
status fence returns one indistinguishable `owned:false` result without
mutating authority. Only an exact guard echo lets a new manager cache the
returned version and collapse later checkpoints from three RPCs to one; old
managers retain the unguarded operation, and a new manager talking to an old
Sidecar retains both compatibility reads. Reconstructible `running` snapshots
get one 500 ms maintenance-priority writer admission. Pending birth and
terminal snapshots keep user priority and five bounded attempts; admission or
CAS pressure is never reported as an ownership fence.

The independent additive
`tofu.task-results.checkpoint.cache-settings/v1` capability accepts at most the
two positive bounded facts carried in the task snapshot and commits them to
the owner-qualified conversation settings in that same transaction. The
prefix HWM is a monotonic maximum; last-turn cache read is LWW for a new task
write. On identical replay after an ambiguous acknowledgement, the operation
may repair/increase HWM but preserves a different last-read value because a
newer task may already own it. The response echoes authoritative values and a
commit bit. A manager clears its staged candidates only after the exact guard
and cache capability echoes; old Sidecars fall back to the serialized per-fact
settings operations. This fusion adds no schema or command receipt.

- Sidecar rate-limit events: every row carries its originating 1-second..7-day
  window as an exact expiry; each later check removes at most 256 globally
  expired rows through the age index, so one-shot identities are reclaimed
  without a table scan. Schema v43 resets only legacy rows that had no TTL.
- Recoverable conversation trash: 30 days.
- Tool-result artifacts: owner-scoped, content-addressed reconstructible data;
  each write declares an expiry, reads/searches reject the wrong owner and
  expired rows, and maintenance prunes expired rows in bounded batches.
- Non-terminal attempt-event payload: maximum 4 MiB.
- Storage RPC frame: maximum 64 MiB; personal-mode active RPCs adapt from 2..12
  using effective CPU and memory, while distributed mode defaults to 64. A
  separate serialized-frame budget is resolved independently per Sidecar and
  client process: 128..512 MiB in personal mode (128 MiB at the 8 GiB
  reference and on probe failure) and 1 GiB in distributed mode, with an
  explicit 128 MiB..8 GiB range.
- Transcript analytics use the owner-scoped conversation repository's
  metadata-first scan and hydrate at most four conversations per RPC. A frame
  limit response recursively splits that batch; one oversize conversation
  fails loudly instead of producing an incomplete report.
- Local conversation-search projection: launch-probed personal budget of
  128 MiB..4 GiB; two read connections, one independent writer, 8-row/2-MiB
  authority scan pages, and no authority-writer backfill work.

Retention runs in separately committed pages and reports whether backlog
remains. Permanent turn projections and active headers are never retention
targets. Command receipts and backups follow their dedicated operational
policies.

Task-event payloads use a private at-rest codec on both backends. Canonical
JSON below 64 KiB stays byte-identical; a larger event gets one deterministic
zlib level-1 attempt and stores the envelope only when it is smaller. Reads
restore the original semantic event before replay, and natural-key duplicate
checks compare the canonical decoded bytes. Stored and decoded forms are each
bounded by the 64 MiB RPC ceiling. Unsupported, truncated, corrupt, trailing,
or length-mismatched envelopes fail as `database_integrity`; existing plain
JSON rows remain readable without migration.

`tool_result_artifacts` stores only bounded tool overflow, not durable user
state or transcript authority. Application code uses the artifact repository
or `tool_result_artifact.put/read/search/prune` semantic operations with an
explicit `user_id`; references expose a CAS digest, cursor, media type, size,
and expiry but never a SQLite row or disk path. Rewriting the same content is
idempotent and cannot shorten its existing lifetime.

## Failure contract

Storage errors expose a stable code, retryability, retry delay, and operation
ID without SQL or sensitive parameters. A capacity response produced before
the server reads or dispatches a request may additionally carry the literal
boolean `request_not_dispatched=true`; absence, false, strings, transport EOF,
timeouts, and backend errors all mean execution state is unknown. Core codes
are:

- `database_not_found`, `database_conflict`, `database_forbidden`,
  `database_busy`;
- `database_unavailable`, `database_timeout`;
- `database_integrity`, `database_protocol_error`, `database_internal`;
- `conversation_authority_conflict`, `storage_payload_too_large`;
- `plugin_storage_incompatible`.

Busy/unavailable/timeout may be retried only where the operation contract says
the write is idempotent. The synchronous client grants commands its existing
three-attempt ceiling only when every failed attempt carries the literal
pre-dispatch proof; it assigns a new request ID, preserves the command ID and
payload, obeys `retry_after_ms`, and otherwise raises after the first ambiguous
failure. This lets a short Sidecar handler-capacity burst settle without ever
replaying a possibly executed mutation. Local client counters and Prometheus
expose retries and retry-bound exhaustion. Integrity/protocol faults fail
closed. A legacy message-array write receives
`conversation_authority_conflict`; clients must use turn commands rather than
GET/rebase/retry.

## Operations and verification

`paper.library.identity` is an owner-scoped query with an additive
`max_text_chars` projection. Omission preserves the rolling full-text result;
zero returns title/arXiv identity plus the authoritative text length without
copying the text, and positive values use a backend-portable `substr` before
the result crosses storage.v1. The hard maximum is the Q&A request-source
budget of 1,000,000 characters. Report title/Insight consumers request zero,
Podcast fallback requests 40,000, a hash-only report start requests 120,000
only after live/cache fast paths miss, and a cold hash-only Q&A start requests
at most 1,000,000. A caller may set `include_text_length=false` only beside
`max_text_chars=0`; this existence projection selects no source and does not
evaluate SQL `length(parsed_text)`. Repeat Q&A starts retain a launch-probed
600-second TTL/LRU working set keyed by explicit `(user_id, paper_hash)`; each
hit repeats that owner-existence lookup. Deletion or another tenant's matching
hash therefore cannot revive cached text, while the content hash itself is the
source revision. The owner predicate precedes ordering and limit; ordinary
bounded projections still report `parsed_text_length` so truncation remains
observable.

Use `scripts/storagectl.py` for preflight, status, baseline, backup, restore,
handoff, and integrity checks. Classic SQLite backup is page-wise. When the
measured-local fastpath is active, backup instead asks the sole checkpoint
owner for a deadline-bounded stable shadow generation: acknowledged commits at
the checkpoint become one standalone database image, same-filesystem targets
pin it with a hard link (on same-directory-link filesystems such as BeeGFS, by
linking beside the source and atomically renaming the second name across
directories), and a separately mounted target receives one sequential copy. Commits accepted during that image copy remain in the next WAL/backup.
Both paths remain capacity-checked, fully integrity-verified, checksummed,
fsynced, and atomically published; checksum reads release their scanned page
cache best-effort. PostgreSQL backup/PITR is requested through the deployment
platform. The distributed scheduler neither
registers nor executes the legacy application-managed database backup tasks.
Online and offline SQLite backup paths share one artifact policy: a job
manifest identifies each temporary copy, expired artifacts are reclaimed only
after their recorded process is dead, and publication retains two verified
recovery points by default. `TOFU_STORAGE_SQLITE_BACKUP_RETENTION`,
`TOFU_STORAGE_SQLITE_BACKUP_TEMP_TTL_SECONDS`, and
`TOFU_STORAGE_SQLITE_BACKUP_RESERVE_BYTES` may tune those bounded personal-mode
budgets. Before allocating a temporary copy, admission also requires the
estimated copy plus retained same-volume verified backups and deep-clean
rollbacks to fit `TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB`. The personal default
is half of the launch-probed data-volume capacity, clamped to 4..512 GiB; an
unknown probe falls back to 64 GiB, distributed mode defaults to 1 TiB, and an
explicit override remains hard-capped at 8 TiB. A backup directory mounted on
a different device owns a separate footprint and does not charge local
rollback points. Successful backup results expose retained, projected, limit,
and same-volume rollback bytes. Budget refusal creates or deletes nothing; the
operator can use an independent backup volume or an explicit bounded override.
One Sidecar accepts at most one full backup at a time; a concurrent request
receives a retryable `database_busy` result.

Fastpath may refresh a verified recovery point when the pre-publication peak
exceeds that copy budget but the post-rotation set fits. This exception is
zero-copy only: capacity planning selects older verified backups, the shipper
must hard-link the checkpointed shadow on the same filesystem, and the new
image must pass integrity, checksum, fsync, atomic publication, and manifest
durability before any selected old backup is retired. Cross-device copy
fallback is refused in this mode. The newest point is always preserved,
deep-clean rollback artifacts are never rotation candidates, and even the
newest point plus retained rollback must fit the hard budget. Successful
results expose both peak/projected bytes and whether budget rotation occurred;
configured backup retention remains the target when the total recovery
footprint allows it.

Fastpath replacement images fsync a resumable prefix every 256 MiB. Its private
state binds the authority UUID, prior shadow generation, and bounded source
fingerprint; timeout, shutdown, or process loss therefore redoes less than one
checkpoint instead of the whole database. A changed source invalidates the
prefix and restarts from zero. The concurrent WAL prefix is always recopied,
and the previously published snapshot/WAL pair remains untouched until the new
image and WAL prefix are both ready for publication. A resumed image retains
the first checkpoint's `recovery_point_at`: canonical backup manifests carry
that boundary, and `serverctl.py doctor` measures freshness from it rather than
the later artifact mtime. `snapshot_progress_bytes`,
`snapshot_resume_count`, and `snapshot_resumed_bytes` expose this lifecycle. The
backup deadline used by the scheduler and online/offline maintenance CLI is
derived once from the same recovery-copy budget (30 minutes..6 hours); an
explicit `TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS` remains bounded to 24
hours. Before checkpointing, the shadow volume must fit the complete image plus
the current WAL and reserve; publication rechecks the pair after concurrent WAL
growth. Only the validated, actually allocated durable resume prefix counts as
reusable capacity, and a short WAL read refuses publication.

`serverctl.py doctor` derives freshness only from canonical
`storage-sqlite-*.sqlite3` artifacts whose Sidecar checksum manifest is present
and structurally complete. The retired `data/db_snapshots/` owner is never
treated as current recovery health; its published and interrupted logical byte
and filesystem-allocated totals are reported separately for operator-controlled
retirement after an independent canonical backup. Diagnostics never delete
those legacy artifacts.

Serialized frame allocation has two independent bounds. The four-byte fixed
header remains bounded by active RPC count; the weighted budget below counts
JSON body bytes. The decoder reserves the declared request bytes before
allocating and receives into one exact `bytearray`; the old fragmented
chunk-list plus `join` retained a second full
copy at the join boundary. In a frozen local benchmark of a 46 MiB frame split
into 64 KiB receives (seven untraced timing trials), the retained path reduced
traced Python peak allocation from 92.09 to 46.00 MiB and median receive time
from 30.31 to 22.27 ms. Before response encoding, each handler reserves the
maximum 64 MiB frame, then releases it after the loopback send. Both directions
share `TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB`; completed responses use their own
FIFO ahead of new request bodies so resource pressure does not preferentially
discard already-committed command results. Waits are bounded to five seconds.
Every application/worker process independently resolves the same numeric
profile for its shared `StorageClient` response decoder. After reading the
four-byte header, it reserves the declared body through receive and JSON decode,
then releases before returning the semantic result to its caller. Command
responses use the priority FIFO; if the client-side wait expires, a read may use
the existing retry loop, while a command remains a single ambiguous attempt.
The budget is per process rather than a host-wide aggregate.
On the 8 GiB reference profile this changes the serialized-buffer envelope
from the slot-only 8 × 64 = 512 MiB to 128 MiB; the measured high-capacity
launch profile resolves from 12 × 64 = 768 MiB to 512 MiB, and distributed mode
from 4 GiB to 1 GiB. These are enforced admitted frame-body envelopes; one
response serialization attempt can still exceed 64 MiB before `encode_frame`
rejects it, so operation-level result limits remain authoritative. They are not
claims about the decoded Python result graph, SQLite pages, kernel socket
buffers, or a deployed RSS saving. `system.metrics.rpc` and Prometheus expose
current/capacity/peak reservations, wait/rejection/admitted totals, and
observed request/response byte totals and maxima. Client-process Prometheus
series expose the same response fields. The shared admission hot path (acquire, observe,
release) measured 1.969 µs best / 2.016 µs median over five 500,000-iteration
loops; that is about 0.14% of the previously measured 1.41 ms median physical
commit acknowledgement, not an end-to-end query-latency claim. It adds one
condition object and integer counters per process, with no worker thread.

Large bounded RPC frames can leave freed allocator arenas resident after the
handler thread exits. At the transition to zero active RPCs, the Sidecar checks
its process RSS no more than once per cooldown and calls `malloc_trim(0)` only
above `TOFU_STORAGE_IDLE_TRIM_RSS_MIB`. The personal default derives from the
same launch-time memory probe as the SQLite writer cache and is capped at
384 MiB so a high-memory host does not retain hundreds of MiB of already-free
arenas; distributed mode has a separate 1 GiB default. Personal mode checks at
most once per 60 seconds, while distributed mode retains 300 seconds. A frozen
13-trim busy-generation window (2026-08-29 00:04..01:04) had a 826.6 MiB median
pre-trim RSS, 304.6 MiB median post-trim RSS, 439.5 MiB median reclaimed, and a
920.6 MiB maximum. At equal allocation rate, the shorter policy projects about
409 MiB median pre-trim RSS; this is a pre-deployment projection, not a measured
saving. `system.metrics.rpc` and Prometheus expose attempts, successes,
cumulative reclaimed bytes, last before/after RSS, cumulative/last trim
duration, RPC active/waiting/capacity/rejections, and process RSS. The trim runs
while new handler admission is briefly fenced, so it cannot race an active
storage transaction; unsupported allocators fail open without changing storage
semantics. Explicit cooldowns remain bounded to 30..3,600 seconds.

`turn.event.record` compares the decoded canonical projection with the current
turn row inside its existing transaction. When a full-fold frame is equal, its
UPDATE omits `projection_json`; when a slim frame leaves both cumulative text
fields equal, it likewise omits the SQLite `json_set` / PostgreSQL `jsonb_set`
assignment. The projection-revision CAS, status and settlement changes,
attempt-event append, optional carried task event, conversation revision,
ordered sync change, and terminal search projection remain mandatory. Thus an
empty patch still advances every replay cursor exactly as before, but does not
dirty an unchanged large value. `system.metrics.attempt_events.by_type` exposes
`projection_blob_write_skips` and
`projection_blob_write_skipped_bytes`; the byte counter is the canonical
stored value excluded from the assignment, not a claim about physical WAL
bytes. In a read-only active-authority audit, 6,887 of 13,878 boot-window
frames had empty patches and carried 3,923,713,036 bytes of conservative
projection evidence; 6,111 known full-fold frames accounted for
3,893,916,098 bytes. Actual WAL reduction must be measured after deployment.

Full-fold application commands use that same versioned projection-patch
primitive instead of transporting the cumulative Turn twice (once as the
command projection and again inside its event envelope). The Sidecar locks the
attempt/Turn, requires `baseRevision` to equal the stored revision and
`targetRevision = base + 1`, normalizes the public stable-segment base, applies
the patch copy-on-write, and then owns the full-row encode plus canonical replay
patch. A malformed path/version fails closed; a stale base refreshes and
rebuilds the application event once, while repeated contention is rejected.
Each live task serializes this lane with one local lock and retains exactly one
last-applied public projection/revision, so after the first authority read a
long turn does not re-download its whole projection for every structural event.
Pure coalesced progress still performs the small attempt-status read because it
does not enter a write transaction; stale executors therefore keep the exact
visibility fence. The baseline is reconstructible and joins terminal heavy
state release. On the read-only 955-tool Turn from `mtdx825fjmhmx5`, the stable
projection is 6,843,583 bytes; an appended `tool_start` patch is 301 bytes, and
the old duplicated command shape is 13,687,938 bytes versus 695 bytes for the
new command (-99.99492%). Patch construction measured 0.338 ms median locally.
These are serialization/transport proxies, not deployed WAL, RSS, API-billing,
or end-to-end latency measurements; the Sidecar still rewrites a changed full
projection row.

The Sidecar write lane also reuses the exact-revision public projection after
the first event. A cache hit selects only Turn metadata, applies the validated
incoming patch copy-on-write, and reuses that patch for replay instead of
reading/decoding the projection BLOB, stabilizing every segment again, and
diffing the two full documents. The authenticated application marks a target as
stable only after the shared normalizer; missing private evidence conservatively
marks it for one repair at the next structural patch. Slim text or status
changes do the same. Rollback or an external revision advance produces a miss,
never a stale read. On the same read-only
955-tool Turn, the encoded BLOB is 5,235,567 bytes and its measured retained
Python baseline is 10,583,620 bytes (2.021x), so the cache charges 15,706,701
bytes (3x). A local old-vs-hit simulation that kept the still-required full
encode changed median processing from 51.985 to 26.407 ms (-49.20%, 1.97x)
and incremental traced peak from 70.269 to 8.635 MiB (-87.71%), while avoiding
one 5,235,567-byte database value read per hit. These are local CPU/allocation
proxies; they do not include the durable-write change below.

Changed live structural frames use a vertically isolated checkpoint plus a
bounded durable patch head. `storage_turn_projection_checkpoints` owns one full,
owner/conversation/attempt/revision-fenced baseline; while it is active, the hot
`storage_conversation_turns` row holds `{}` plus checkpoint/materialized
revisions and exact patch count/bytes. The patches are not duplicated: they are
the already-atomic `storage_attempt_events` replay payloads. A head is limited to
64 patches and 1 MiB of encoded patches. Crossing either limit, or requiring
stable-segment repair, writes one new checkpoint and clears the head. A live
inline projection above 64 KiB is externalized once on its next event. Terminal
settlement, restart recovery, direct edit/branch/compaction writes, and
recoverable deletion materialize the complete projection, clear all head
metadata, and delete the checkpoint. Activity-date reads join the checkpoint
and fold a head only when present; clone and public Turn reads use the same
fail-closed projection loader. Event retention excludes any attempt still
referenced by a checkpoint or head. Restart recovery budgets each checkpoint
from its declared encoded bytes with a conservative hydration multiplier,
without selecting every candidate checkpoint BLOB before choosing the bounded
transaction chunk.

Every revision after externalization remains reconstructible even when its
projection is byte-identical: the first such revision starts a head with the
already-carried empty patch instead of advancing beyond the revision-fenced
checkpoint. This keeps unchanged status/heartbeat frames on the same exact
chain invariant as structural changes without rewriting the checkpoint BLOB. Tofu-DB restart recovery is proportional to unfinished work: pending/running Attempts maintain a compact owner-scoped created-time index, and `turn.recover` reads at most 10,000 index rows while settling at most 500 Turns and 8 MiB of hydrated projections per transaction, always allowing one oversized Turn to progress. Creation-time and live-task guards apply before the byte budget. Attempt/Turn settlement, live-index removal, projection-head retirement, search invalidation, conversation revision, terminal event, sync replay, and outbox publication are one OCC commit; malformed or over-budget indexes fail closed without an authority-wide scan.

Schema 48 introduced the bounded head counters; schema 49 added the checkpoint
revision discriminator, while the latest table catalogue creates the checkpoint
table empty. Neither migration decodes or backfills historical projection
values. Fresh authorities fold the bounded event range through the existing
`(attempt_id, sequence)` primary key. Authorities that already applied the
published schema-48 projection-chain index may retain it; schema 49 does not
rebuild or require that index. Metrics expose checkpoint materializations and
bytes, one-time inline bytes released, BLOB skips, and deferred BLOB bytes.
Schema 51 repairs the exact schema-49 one-revision/no-head cohort by advancing
the checkpoint row and Turn discriminator together. It matches owner,
conversation, attempt, and revision metadata and neither selects nor rewrites
the checkpoint JSON; any other malformed shape remains a fail-closed integrity
error.
Replay `projectionBytes` remains event evidence: it is exact when a checkpoint
or materialized target is encoded, text-only for a slim frame, and conservative
baseline-plus-patch evidence for a deferred structural target. It is never
physical-row or WAL byte accounting.

On the same read-only 955-tool projection, a local temporary-SQLite
per-transaction proxy compared a 5,235,877-byte full target write with a
414-byte exact patch / 519-byte event. Median transaction time was
33.300 ms versus 0.284 ms (-99.15%, 117.35x); WAL was 5,294,232 bytes / 1,285
frames versus 12,392 bytes / 3 frames (-99.77%); incremental full-target encode
peak was 9,037,773 bytes versus 2,706 bytes (-99.97%). The bounded rollover and
terminal paths still pay one full materialization. These figures are local
storage/encoding proxies, not deployed RSS, disk, API-billing, or end-to-end
latency savings.

`turn.image.get` is a read-only compatibility query for historical inline Turn
images. It requires explicit owner, conversation, Turn, positive projection
revision, and an image index below the 20-item ceiling. Missing/foreign or
non-terminal rows return no result; a revision mismatch returns only the
current revision, and a match returns one bounded encoded image plus declared
MIME instead of exporting the full projection over RPC. The conversation-sync
application boundary then strictly decodes at most 8 MiB and sniffs the true
PNG/JPEG/GIF/WebP MIME before HTTP delivery. The operation never rewrites the
Turn, advances a cursor, or makes a compatibility URL into storage authority.
Reference snapshots prefer an existing canonical `/api/images/` upload URL and
replace any stale revision-fenced preview request-locally; only inline-only
historical images use `turn.image.get`.

Verified-copy deep clean has a separate rollback lifecycle. After a new
authority passes integrity, row-parity and publication gates, the newest
`tofu.db.pre-compact-<stamp>` is preserved and older copies are reduced to one
by default (`TOFU_STORAGE_SQLITE_ROLLBACK_RETENTION`, hard ceiling four). The
tool proves its second-stamped candidate, rollback, WAL and SHM names absent
before the first mutation; cleanup is enabled only after that ownership gate,
so an existing file or dangling link is never overwritten or unlinked. The
read-only `--analyze` report includes logical and allocated bytes, age, excess
count, and an exact retirement command for every retained copy. The same
bounded shallow report totals verified SQLite backups and root-level files of
at least 1 GiB whose lifecycle belongs to migration/cutover owner sign-off;
those operator-managed files receive no automatic deletion command. Removing the
last local rollback is never automatic: after observing the replacement
healthy, stop Tofu and run
`python3 scripts/storage_deep_clean.py --retire-rollback <basename> --confirm`.
The command accepts no path or glob, holds the project lease, rejects links or
WAL/SHM companions, quick-checks the current authority, fsyncs the deletion,
and verifies the postcondition.

For an authority created before bounded attempt frames/retention, first run the
read-only `python3 scripts/storage_deep_clean.py --analyze`. Its
`compaction_plan` reports live/source bytes, required reserve, available space,
the bulk-compaction verdict, and missing deferred indexes. The `tables` section
measures exact encoded payload bytes and real row counts rather than multiplying
a sparse `max(rowid)` by a sample average; it also exposes rowid holes and a
64-group exact `storage_events` stream/type breakdown. The same canonical
selectors used by offline deletion report exact settled Sidecar
`storage_attempt_events`, legacy `attempt_events`, and streaming/structural
`task_events` rows plus encoded bytes for the requested `--ttl-days`; invalid
horizons fail before opening the authority. These logical
payload totals exclude SQLite page/index overhead. All measurements share one
60-second SQL progress deadline: a slow authority returns explicit
`analysis_budget_exhausted` partial results instead of continuing an unbounded
diagnostic scan. `offline_compaction_recommended` describes existing freelist
pressure only; `offline_maintenance` combines that signal with exact expired
transport, conditional legacy-mirror payload, deferred indexes, copy capacity,
and one stopped-server command. A capacity-blocked verified copy returns no
command rather than silently choosing the no-rollback low-space mode. Physical
compaction is intentionally offline and explicit:
`python3 scripts/storage_deep_clean.py --offline --confirm`. It acquires the
project lease, deletes only expired transport rows, verifies a compact copy,
and retains the pre-clean authority for operator-controlled rollback. Never run
the offline mode while Tofu is serving, and verify free space before the
maintenance window. While that lease is held, the lifecycle manager reports
`maintenance` with the safe owner label/PID, queues an explicit start request
without consuming the crash-loop budget, and starts Tofu automatically after
the OS lock is released. The lease stamp is diagnostic only; the OS lock stays
authoritative, so a stale `running` stamp never blocks startup. The verified
compact copy also receives every missing deferred performance index and
retires superseded indexes before building their replacement, so their pages
can be reused; established authorities never build such indexes implicitly
during startup. When `--analyze` reports only missing/obsolete deferred indexes
and no physical compaction need, use
`python3 scripts/storage_deep_clean.py --offline --no-vacuum --confirm`: it
performs current transport retention and the in-place index transition under
the same lease without allocating a second authority copy. It deliberately
skips task-event type recovery/codec backfill and does not delete frozen legacy
transport rows because either would create a large freelist without returning
physical space. If a personal disk cannot hold both the compact copy and the
old authority, first make and verify an independent backup, then use
`python3 scripts/storage_deep_clean.py --offline --low-space --confirm`. That
mode returns free pages in bounded in-place batches and verifies integrity plus
authority row counts, but intentionally cannot retain an on-volume rollback
copy.

The verified-copy and low-space modes make one rowid-keyset pass over current
`storage_events` task streams before reclaim. Explicit event types receive the
canonical 6-hour streaming or 30-day structural TTL. Blank v21 migration rows
recover `event_type` and `event_kind` from a valid top-level JSON object, after
which the same TTL applies; malformed or type-less rows remain in the longer
structural class. Retained plain payloads of at least 64 KiB receive the private
task-event codec, including already-typed historical rows. Project feed/status
streams never enter this pass. Selection is capped at 4,096 rows and 64 MiB of
stored payload, writes commit separately with a WAL checkpoint, and a repeat
pass is write-free once the cohort is classified/compressed. The report exposes
scan/write pages, TTL counts and bytes, recovered/opaque/invalid rows,
compression savings, batch maxima and retained blank rows.

New durable `messages_snapshot` rows use private `snapshotDeltaVersion=2`.
The persistence-only projector keeps one chronological message baseline per
`(task, turn)`, so a request identical to the preceding post-tool state stores
an empty tail instead of repeating it in a separate kind chain. Live SSE and
Request Inspector payloads remain full; server-side rebuild removes the marker
and restores kind-specific rows exactly. Unmarked v1 deltas keep their frozen
`(task, turn, kind)` interpretation, exact v1 rows may seed a following v2
baseline, and unsupported versions report degradation. Projector state remains
bounded to 64 active tasks and is released at terminal events. On 1,564 frames
read from the active fastpath authority, exact replay plus the production codec
projected 32,728,847 stored bytes to 22,281,348 (31.92%) and 56 resident v1
baseline chains to 32 v2 chains. This bounds future growth only; historical
rows retain the structural TTL/offline reclaim lifecycle above.

Those physical modes also make one metadata-first keyset pass over non-empty
frozen `storage_conversations.messages_json` archives. Each selected message
uses the lossless projection-sequence codec described above; a canonical
decode/re-encode/decode comparison gates every update, and an equal or larger
encoding leaves the original bytes untouched. Selection is capped at 64 rows,
one page carries at most 64 MiB of source payload, and an individual document
above 64 MiB is counted without fetching its body. Each non-empty write page
commits and checkpoints its WAL independently. The pass never changes message
count or public transcript semantics, and `--no-vacuum` skips it because that
mode cannot return the freed pages.

The verified-copy and low-space physical modes also retire expired rows from
the frozen pre-Sidecar `attempt_events` and `task_events` tables. Legacy
attempt cleanup requires both age eligibility and a newer sequence, so the
newest diagnostic frame for every attempt always survives. Legacy task frames
use the same canonical 6-hour streaming and 30-day structural lifetimes as the
Sidecar event authority. `task_results` remains untouched. Deletes commit in
pages of at most 900 rows and 128 MiB of encoded payload, and truncate the WAL
between pages; an individual over-budget row fails closed. The report includes
deleted rows/payload bytes, batch maxima and retained-row counts for operator
verification.

The read-only report also inventories the frozen pre-Sidecar conversation
mirror family: `conversations`, `conversation_messages`, and
`conversation_turns`. These tables are not runtime authorities, but they are
never removed by the ordinary command above. When the report offers the exact
command, an operator may add `--retire-legacy-conversation-mirrors` to an
offline physical-reclaim pass. Retirement is owner-scoped and per
conversation: the frozen message array, the ordered row mirror reconstructed
from its lossless `meta` plus versioned translation overlay, and the current
`storage_conversations.messages_json` archive must have identical canonical
JSON. A missing current header, ambiguous legacy global ID, malformed or
unequal witness, legacy Turn foreign key, document above 64 MiB, or deletion
cohort above the 128 MiB payload budget retains that conversation unchanged.
Selection is capped at 64
conversations and every successful batch commits and truncates the WAL
separately. The current Sidecar archive is never modified, and integrity plus
active-authority row parity still gate publication. Here, "never modified"
describes the mirror-retirement proof/deletion itself; the independent
lossless archive-codec pass above may change only its private physical
representation. `--no-vacuum` rejects the
flag because deleting mirrors without returning disk space has no user-visible
value; low-space mode requires the same explicit independent-backup contract as
its other in-place mutations.

For SQLite on a high-tail-latency FUSE/network volume, see
[`TRB-fastpath.md`](TRB-fastpath.md). The local-front mode preserves
`synchronous=FULL` on the serving database and activates only after filesystem,
WAL-recovery, capacity, lineage, and measured-speedup checks. It deliberately
has a bounded RPO if the local disk itself is lost; enabling it is an operator
durability decision, not an automatic response to a slow mount. New activation
never creates or restores an absent implicit `/tmp` front; it requires an
explicit persistent-local directory. A surviving legacy temporary front stays
readable so it can be retired without losing an unshipped crash tail. If a
shadow exists while no verified front is selectable, startup fails closed
rather than opening the stale classic file. The explicit offline command
`python3 scripts/storagectl.py retire-fastpath --confirm` is the exclusive
transition back to classic SQLite. It prefers a uniquely verified surviving
front, WAL-checkpoints and
integrity-checks a private candidate, publishes only after authority-UUID
validation, retains rollback authorities, and reclaims only exact incomplete
shipper artifacts after success. First activation uses source-fingerprinted,
fsynced copy checkpoints and resumes only
owned partial bytes whose classic database/WAL identity is unchanged. The
Sidecar reports bounded startup progress to a renewable stall watchdog inside
one immutable topology-aware hard deadline; the ASGI lifespan shares that
deadline plus a fixed reserve, so no outer layer repeatedly kills valid work.
Completed 256 MiB copy ranges are fsynced before their page-cache pages are
released best-effort. The first verified shadow reuses the immutable classic
database through a same-filesystem hard link; later generations copy the stable
post-checkpoint database image sequentially and capture concurrent commits in
the new WAL prefix. Thus a large authority neither needs a second durable
database-sized allocation on first activation nor restarts page-wise backup
work whenever the live front receives a write. Full-copy rebases are triggered
by an adaptive WAL budget instead of a fixed 64 MiB: one quarter of the current
authority (at least 64 MiB), capped per WAL by two percent of launch-time free
disk and 16 GiB. Shipper construction rechecks both the local-front and durable
shadow filesystems and takes the smaller two-percent ceiling; probe failure
keeps the already-bounded launch value. The two WAL copies therefore remain
inside a four-percent hard envelope while halving database-sized rewrite
frequency versus the prior one-eighth/eight-GiB policy. A rebase starts at
15/16 of that budget, reserving one sixteenth for commits that race the raw
checkpoint. Each physical commit observes the local WAL. Only once it reaches
the hard watermark does the fair writer reject later jobs before `BEGIN` with
retryable `database_busy`; the shipper's raw checkpoint bypasses the fence,
truncates the WAL, and reopens admission. One
already-started, deadline- and request-bounded commit segment remains atomic,
so this is an admission watermark rather than a byte-exact filesystem quota.
The fence also remains active when pre-checkpoint or publication capacity is
unavailable, preventing an unbounded local recovery tail while reads and the
previous durable recovery point remain available. Publication still repeats
the complete image-plus-tail capacity check. Sidecar and Prometheus metrics
expose complete generations, physical database/WAL copy bytes, current copy
progress, proactive rebase trigger, hard WAL budget/headroom, pressure
state/activations/rejections, and the resolved limits so write amplification
and write pressure stay measurable.
A failed runtime WAL-size observation fails closed and has its own counter; it
cannot silently report zero bytes and reopen admission.

After backend startup, the process-held project lease publishes a
credential-free `tofu.storage-locator/v1` record: backend identity, the active
SQLite authority path, configured path, fastpath flag, and shadow directory.
It never contains the private RPC token, DSN, or password. Read-only offline
diagnostics resolve this locator first, retain a bounded `/proc` open-file
compatibility path for pre-locator processes, then require matching local and
shadow authority UUIDs. If a shadow exists but no unique front is provable,
they refuse the classic file instead of presenting stale state as current.

The storage metrics surface exports rolling commit p50/p95/max, queue depth by
lane, current writer phase, stall interrupts, group-commit counts, writer-cache
bytes, per-attempt-event accepted/rejected payload budgets and unchanged
projection-write skips, linked SQLite runtime version, fast-path activation,
durable ship lag, last-ship age, and logical capture/publisher state, pending
records/bytes, cursor, duplicate retries, and failures. Fastpath also exports
rebase/pressure state, local WAL bytes, remaining admission headroom, and both
writer- and shipper-side pressure refusals. Required mode revokes
readiness on a degraded, blocked, or poisoned publisher. A healthy
deployment should alert on nonzero stall-interrupt growth, persistent user-lane
queueing, p95 commit latency approaching the command deadline, or advancing
ship lag/age. It should also alert before the logical outbox or sink reaches its
byte ceiling; a full sink intentionally backpressures rather than discarding
durable history.

For a storage change, verify in this order:

1. operation unit/contract test on SQLite;
2. wrong-owner, rollback, idempotency, and timeout behavior;
3. PostgreSQL adapter contract when enabled;
4. schema migration from the previous version;
5. affected HTTP/application contract;
6. storage boundary and documentation gates;
7. representative FUSE fault/load certification for transaction changes.

Never preserve an old runtime path as rollback. Recovery restores verified data
and restarts the single authority; it does not activate a second repository.
