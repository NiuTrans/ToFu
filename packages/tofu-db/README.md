# Tofu-DB

This is the pre-authority Rust engine for Tofu's personal storage workload.
The real VFS explicitly unlocks an exclusive lease when its owning file guard
ends, matching deterministic recovery tests; duplicated/inherited descriptors
do not prolong that authority lifetime or unlock a subsequently acquired lease.
It currently implements the crash-recovery envelope: exclusive ownership,
alternating checksummed control generations, immutable 4 MiB content-addressed
blocks, deterministic transaction envelopes, and a bounded hash-chained active
commit log with generation names and checkpoint-chain recovery bases. CONTROL
now records checkpoint witnesses, manifest roots, and the active generation;
explicit Engine checkpoint packs active transactions into immutable history
segments and atomically switches to a new WAL generation; commits trigger it at
a 60 MiB soft limit below the 64 MiB recovery bound. Above the soft limit,
commits materialize toward an 8 MiB foreground target and defer checkpoint while
any staged payload remains, including background reservations awaiting manifest
publication. A single or group append that would exceed 64 MiB instead finishes
the full checkpoint before writing; it cannot fail merely because a bounded
materialization step left a backlog. Checkpoint waits recheck the create state
under the condition mutex, with at most 240 rounds of 250 ms before synchronous
fallback. These bounds derive from `activeWalRotation` in the native IR contract.
Release linking retains symbols: the build host's link-time symbol stripping
reproducibly zeroed 12 KiB of initialized data in an otherwise identical ELF,
crashing at the first allocator call. Linking without stripping and then
stripping a separate copy preserved all initialized data. Retained non-loaded
symbols add about 3 MiB to this build; release/process gates validate the exact
binary that is measured. The allocator dependency and disk format are unchanged.

After durable checkpoint publication, the existing background packer removes
the previous WAL outside the authority mutex. Its fixed slot holds at most one
retired generation (64 MiB) alongside the active WAL. The next checkpoint first
drains that slot; delete or directory-sync failures retain it and block another
rotation, while the already published checkpoint keeps its original result.
Without that worker, cleanup remains synchronous. Worker shutdown drains and
unregisters under the same private mutex and surfaces cleanup failures. Reopen
derives only the immediate predecessor from CONTROL, with no directory scan;
pre-existing older orphan generations are outside this bounded handoff.
Single and group commits prepare history from the already committed WAL prefix.
With a live background packer, preparation starts once a full segment is available
and hashes/copies toward 256 KiB per call. A larger indivisible record may exceed
that target within the unchanged 4 MiB segment bound. One partial encoder or
pending encoded segment shares the existing ≤4 MiB allowance. Checkpoint finishes
any partial encoder and materializes every pending block before retiring its WAL.
Soft rotation lets encoding and its live worker handoff finish incrementally;
the 64 MiB hard limit still forces the complete checkpoint before admission.
Without the worker, synchronous preparation scales its start with arriving WAL
bytes and finishes any partial encoder. Prefixes below one full segment do no
speculative work. Segment bytes and hash validation are unchanged; only checkpoint
publishes the history references. Unpublished history occupies at most one active
WAL window plus segment headers on disk, at most 33 references and one ≤4 MiB
buffer in memory, stays reachable to GC, and is reconstructed from WAL after a
crash without a startup directory scan or additional worker.
The existing maintenance worker trims checkpointed history to the launch-probed
16/64-segment suffix even while the active WAL contains committed transactions.
It first catches CONTROL up to that durable tail, then publishes the smaller
history manifest. Active WAL bytes, its generation, the checkpoint anchor and
staged history prefixes stay unchanged; retirement does not force a checkpoint
or rewrite transaction payloads. Physical GC subsequently reclaims unreferenced
history and COW blocks.
Background payload packing keeps its sole reservation through foreground
manifest publication and staged-payload retirement. A scoped publication guard
releases it on success or error, preventing duplicate concurrent packing
without retaining a second payload or block-ID set.
A failed CONTROL
publication makes the live engine restart-required before any further authority
work. The entity family adds
owner-scoped MVCC snapshots, immutable COW B+Tree pages, batched page rebuilding,
and point/range OCC witnesses. The blob family stages owner-scoped 1 MiB chunks
with bounded zstd compression, deduplication, logical hashes, and atomic
transaction manifests. The stream family adds owner/domain keys, immutable 2 MiB
segments, atomic append fencing, and bounded cursor pages. A durability group
admits at most 64 transactions and 8 MiB each of logical payload and encoded WAL, writes and syncs their
individually framed hash chain once, then publishes the final sequence through
one CONTROL generation. A versioned family-transaction container can carry an
entity root, multiple stream commits, one command receipt, and one logical
outbox record in the same WAL transaction. It canonicalizes records, deduplicates
at most 2,048 referenced blocks, and keeps inline metadata within 256 KiB;
entity and stream recovery accept both this form and the original single-family
records. `authority.rs` stages entity OCC changes, bounded appends and blob
blocks, publishes them through one family transaction, and advances every
in-memory family witness only after durability. Transaction handles are bound
to the originating authority UUID. `receipt.rs` matches the existing v2 SHA-256
command-key domain and receipt limits, stores small responses inline and larger
responses through atomically referenced blobs, and rejects mismatched
operation/request digests. Concurrent first delivery is serialized by the same
entity point witness as the business mutation. `logical_outbox.rs` defines the
native sealed-record codec: AES-256-GCM with a random 96-bit nonce, an
eight-byte key fingerprint, and owner/sequence/schema/request metadata bound as
AAD. Clear payloads are capped at 4 MiB and never appear in encoded records.
`authority.rs` now captures one record with each configured business commit,
uses owner-scoped OCC metadata for continuous logical sequences and pending-byte
backpressure, spills records above 4 KiB to atomically referenced blobs, and
deletes them only through ordered, idempotent ACK transactions. Semantic execution uses the authenticated owner/tenant boundary described below;
application authority selection remains prohibited.
`outbox_publisher.rs` validates complete contiguous batches before external I/O,
caps each batch at 64 records/8 MiB, releases the database boundary before sink
calls, accepts only identity-matched durable receipts, and records content-free
success/failure counters. `outbox_sink.rs` provides an owner-bound sink on the
certified Engine log: explicit absolute paths, exclusive leases, startup and
per-append capacity preflight, bounded checkpoint recovery, exact historical
idempotency checks, and fail-closed scope/sequence enforcement. Its fault suite
covers arbitrary I/O failure, ENOSPC, short writes, lost syncs, and cross-process
reopen. `outbox_worker.rs` owns one isolated sink thread; queued plus in-flight
memory is capped at 16–64 MiB from launch headroom, every request has an absolute
deadline, shutdown drains only until its caller deadline, and terminal sink
faults close admission and fan out to queued requests. `outbox_relay.rs`
round-robins up to 64 unique explicit tenant/owner scopes through that one
aggregate queue, releases the authority mutex during sink I/O, ACKs only
identity-matched durable receipts in order, supports commit notifications plus
bounded fallback polling, and stops on ambiguous authority state. A retrying
owner cannot starve another scope. `outbox_multi_sink.rs` provides native
routing for up to 64 frozen source scopes in one Engine authority. An explicit
administrative scope owns its COW exact-record index, source sequences and
aggregate byte witness; large sealed records use content-addressed blobs.
Reopen performs bounded point reads rather than scanning WAL history.
Event retention also maintains a physical-position index and a durable
owner-scoped retirement queue. Each prune transaction advances one stream by
at most 128 complete immutable segments, retired cursors fail explicitly, and
later appends retain the monotonic physical cursor; bounded authority GC can
then reclaim the unreferenced blocks.
Application wiring is still absent.
Explicit incremental
backup checkpoints the source, copies only missing reachable immutable blocks,
publishes a checksummed generation manifest last, and can rebuild an empty
offline target after a bounded capacity preflight, using a durable generation-pinned resumable marker. Retention GC uses restartable plans and at most 1 GiB of on-volume sorted mark runs instead of a database-sized in-memory set. Its 65,536-reference admission buffer stays bounded after repeated budget or I/O failures: a retained full run must flush successfully before another reference is admitted. The generated `contracts/tofudb_ir_v1.json` currently declares all 351 registered operations executable. This is semantic
operation coverage; complete migration, logical replay and release certification
remain separate requirements.
The explicit `migration::restore_document` library entry point preserves positive snapshot versions/timestamps and exact JSON bytes through the existing owner-bound inline/Blob codec. It inserts or verifies an identical record under an expected authority UUID, and joins its caller's transaction so domain indexes and progress can publish atomically. `lib/storage_sidecar/tofudb_migration_import.py` is the operator offline importer for tenant-user, conversation-header, credential, provider, billing, and record current-state replay: the explicit `migration-session --data-dir <existing-absolute-target> --tenant-id <id> --owner-id <id> --execute` child owns an exclusive authority lease and only private stdin/stdout. Migration-only `tenant.user` restore claims the source `owner_user_id` and advances the sequence; live `tenant.user.create` still allocates. Conversation headers reuse live create/get/list/count with preserved timestamps; turns stay a later domain. Its bounded requests carry the expected UUID; the allowlist and limits derive from `offlineMigration.operatorSession`. Generic records preserve raw JSON, version and timestamp, enumerate all owner keys in pages of 128, and verify point reads before incremental checkpoints. Restart tolerates matching commits ahead of the checkpoint. The task-result domain adapter (`migration.task.result.restore/get/scan` in `task_result/migration.rs`) replays the exact stored `task_results` document with its version/timestamp and atomically rebuilds its header, replay projection, summary, live and cost-experiment indexes in the same authority transaction, with independent catalog enumeration; abort tombstones must arrive merged into the source document exactly as the checkpoint semantic writes them. Managed continuation and paper record namespaces still require dedicated adapters. Wallet metadata differences fail verification. The Python CLI now takes `--binary` and `--target` instead of a live TCP endpoint/token; old plan checkpoints fail binding. Each imported document is bounded to 8 MiB and each pipe frame to 16 MiB + 8 KiB. Complete domain migration, rollback and application selection remain uncertified.

`paper_build_artifact.rs` implements retained build uploads, exact chunk retries,
streaming digest checks, read leases and bounded reclamation. Append admission
constructs a private immutable chunk with canonical Base64, decoded length and
SHA-256; subsequent IR checks charge its bounded fields without decoding or
serializing the payload again. Replay metadata hashes the same canonical JSON
bytes incrementally. Its receipt schema
and hard limits are generated from `paper_build_v1.schema.json`. Artifact bytes
use the generic owner-bound versioned document/blob codec; a tenant-wide compact
reservation index counts payload bytes plus receipts across owners. Workspace
CAS and the verified build pin publish in one authority transaction. No upload
request receipt duplicates the chunks, and discard cannot undo a committed pin.
The existing launch probe sets the personal retained-byte budget; readiness
reports `paperBuildRetainedMaximumBytes`. This does not change pre-authority
startup restrictions or certify the pending domain migration adapters.

`paper_document.rs` implements saved source bindings, build generations and reading
position CAS. Its eight operations consume the generated public build/document
schema. Owner-bound generic records hold at most 64 KiB each; a compact shared
tenant index refuses admission beyond 128 documents and 512 conversation
associations without deleting durable state. Only transactions containing saved
document IR admit this new shared namespace; unrelated semantic requests retain
their previous scope set. Source identity uses the same
ASCII canonical JSON digest as the reference authority and never reads a path.
The requested generation's first completion, its retry digest and the retained
PDF pin commit atomically. Expired/abandoned generations cannot publish, and an
unverified replacement cannot displace a verified PDF. Reading updates use an
independent revision plus writer/base-revision/sequence ordering. Point reads
and explicit counts avoid catalog scans; no resident worker or cache is added.

Block-at-rest encryption and signed release packaging are implemented
(see below); Supervisor selection and general query execution remain release
work. The certification gates in `docs/STORAGE.md` still prohibit production
selection; there is no mandatory calendar-duration shadow run. Passing local
tests or selected benchmarks does not waive correctness, durability,
migration/rollback, or resource checks.

As of 2026-09-13 the semantic differential gate passes 167/167 against the
current tree (the `tool_result_artifact.search` folded-stream offset fix changed
the release binary to
`9a2a0755eeea02a970c98b9822863fcdfc69d6d40947d512755e58f674242612`; only the
differential gate has been re-run against it, so the certifications below still
refer to the prior operator-certified build).

As of 2026-09-10 the following certification evidence is green against release
binary `c6cda189b8379938f8f275d38d6a16a9d15da6f1fce27c52042fd63962017a3f`
(retained under `data/`): the semantic differential gate passes 160/160
(`tests/tofudb/test_tofudb_differential.py`); `certify-filesystem` and
`certify-resource-gate` both pass with all `*_exceeded` flags false
(`data/fs-cert-evidence/run-2026-09-10-final/`,
`data/resource-gate-evidence/run-2026-09-10-final/`); and the medium-scale
comparison wins all measured workloads against both SQLite and PostgreSQL
(`data/tofudb-authoritative-results-2026-09-10.md`). The explicit resumable
migration importer and its rollback certification are covered by
`tests/tofudb/test_tofudb_migration_import.py` / `test_tofudb_migration_boundaries.py`
(71 passing) and operator-run end-to-end evidence under
`data/migration-cert-evidence/run-2026-09-10/`; remaining source tables without
a domain adapter and application selection are still uncertified.

`protocol.rs` implements the transport-only `storage.v2` machine contract in
`contracts/storage_v2.json`: canonical flat MessagePack, fixed numeric fields,
length plus CRC32C framing, Hello negotiation, correlation/deadline/owner/
tenant/command/schema identity, guarded allocation admission, bounded error
envelopes, a redacted and zeroized 256-bit Hello capability, and 1 MiB blob
chunks. The Python `scripts/tofudb_v2_client.py` harness optionally uses installed
`orjson` for large text payloads after a bounded 4,096-value/32-depth compatibility
walk. Eligible encodings are byte-identical to the stdlib path; floats, custom
objects, unsupported shapes, missing acceleration and encoding failures retain
the original encoder and error behavior. `semantic.rs` is the mandatory default-deny
boundary after decoding: it binds requests to generated metadata for all 351
registered operations, matches envelope identity against an authenticated owner and
tenant scope, enforces negotiated schema/deadline limits, and requires a
command ID for every command or maintenance operation independently of legacy
receipt policy. Authenticated connections may assemble one identity-bound
artifact or task-result checkpoint request from at most 64 ordered 1 MiB chunks and retain every chunk's
share of the process-wide frame budget through dispatch; single-frame requests
remain capped at 8 MiB. Successful responses above 1 MiB use at most 64
identity-bound chunks followed by an empty success terminator, and the Python
differential client validates and transparently reassembles the same sequence.
`server.rs` fixes authenticated owner/tenant authority for one
session, enforces exactly one Hello negotiation, bridges decoded requests only
through admission and the semantic executor, and emits contract-defined bounded
status envelopes with exact correlation and negotiated schema. Authentication
keeps only a SHA-256 token witness, compares it in constant time, and is the
only constructor for sessions. `listener.rs` binds numeric loopback addresses
only, caps connection threads at 64, configures bounded socket I/O and accept
polling, and records content-free lifecycle metrics. The inherited parent lease
is an empty nonblocking pipe: EOF stops admission, asks live connections to
finish at a frame boundary, and joins every admitted thread. The authority
mutex is held for one semantic request, never for the connection lifetime.
`serve_connection` requires authenticated Hello as the first frame, emits a
negotiation ACK, processes sequential requests until clean boundary EOF, and
rejects truncated tails. All connections share an RAII admission budget capped
at 64 in-flight frames and 128 MiB, with content-free current/peak metrics.
`daemon.rs` adds the supervised pre-authority process boundary. The `serve`
command opens only an existing absolute authority path, binds an ephemeral IPv4
loopback port, derives its wire capability from a bounded ASCII environment
secret and clears that source buffer, emits one bounded credential-free JSON
readiness line, and exits when the inherited empty stdin pipe reaches EOF. Its
`backend=tofudb` and `preAuthority=true` envelope is admitted by the Supervisor
backend allowlist, so the engine is selectable as a certified storage backend;
it is not the default backend.

`resource_probe.rs` performs one bounded launch observation before authority
open. Host memory is read per platform: Linux from `/proc/meminfo`
(`MemTotal`/`MemAvailable`), macOS from `sysctl hw.memsize` plus the Mach
`host_statistics64` free+inactive page counts, and Windows from
`GlobalMemoryStatusEx`; other platforms report no observation. Linux
additionally combines process affinity with cgroup v1/v2 CPU quota and
capacity/current usage; cgroup ceilings are a Linux container surface and are
absent elsewhere. All platforms read free bytes from the actual authority
volume. Missing core evidence selects four connections and a 16 MiB aggregate
frame budget; observed pressure can reduce admission to one, while hard
ceilings remain 64 connections, 128 MiB of frames, and 2 MiB per connection
stack. A writable daemon refuses observed free space below two full 64 MiB WAL
windows plus 16 MiB instead of beginning work it cannot safely rotate.
Readiness exposes only these aggregate observations and selected bounds.

`contracts/tofudb_ir_v1.json` is the generated machine authority for Schema IR
and Transaction IR bounds. `transaction_ir.rs` interprets owner-scoped entity
reads, conditional writes, result projections, receipts, and logical outbox
capture in one OCC transaction. `semantic_executor.rs` and its domain modules compile or route the executable
operations declared by that generated contract into the IR with
storage.v1 JSON, Unicode character, owner, timestamp, receipt, retention, and
error semantics. Conversation creation commits its blob-capable header, exact
owner count, search-dirty marker, receipt/outbox, and tenant-global ID claim
atomically; only this IR step receives access to that fixed claim namespace.
Owner-scoped conversation reads project the storage.v1 empty-transcript and
bounded message-window result shapes through the blob-aware header codec.
Settings snapshot-CAS and metadata updates preserve transcript revision while
using the physical document version for lost-update protection. Create and
timestamp updates atomically maintain the prefix-safe owner-local
covering `(updated_at DESC,id DESC)` index used by bounded sidebar paging.
Activity-date queries use a compact updated-order candidate index for at most
10,000 headers and scan at most 100,000 aggregate 24-byte main-lane timestamp
records, retaining only distinct interval ordinals rather than settings or
transcript payloads. An owner completeness marker is created only from a truly
empty active-and-trash scope; old authorities without it keep using bounded
header reads until `backfill-activity-index` explicitly advances a durable
owner cursor in transactions of at most 256 rows/16 MiB of source headers. A building marker makes
foreground create, timestamp movement, delete, and restore maintain candidate
membership between invocations; only an exhausted source scan atomically
publishes completeness, so normal startup performs no retrofit scan. A
completely absent Turn timestamp index
uses bounded one-Turn-at-a-time compatibility reads; a partial index fails closed.
Conversation clone takes one bounded MVCC snapshot of at most 2,000 Turns and
1,000 archives, creates a new header/graph without attempts or live latches,
and deterministically remaps Turn, task, archive, and parent identity. Archive
messages, summaries, and receipts use separate blob-capable documents, so
summary updates never rewrite transcripts and clone reuses content-addressed
payload blocks instead of copying them.
Live source Turns become interrupted static projections while the source keeps
running unchanged; exhaustive commit-fault replay preserves one complete graph.
Normal and catalog list shapes support owner scope, cursors, filters, settings
projection, snapshot count, 10,000-row semantic bounds, and an 8 MiB response
ceiling without N+1 entity reads. Unfiltered lists scan only the remaining
requested rows; catalog pages fetch one additional row to establish `has_more`.
Row filters and folder counts preserve the full bounded scan allowance, while
small cold pages avoid loading 1,000 index entries just to return 50 results.

Browser site observations now compile get and record through owner-scoped,
digest-keyed, exact-identity-verified documents. A covering LRU index carries
expiry and deterministic tie-break fields, so the 30-day/200-document policy
needs one bounded index page and materializes only documents actually returned
or deleted. Passive-only hint validation rejects query-bearing paths and
sensitive shape keys; deterministic syscall-error and short-write replay keeps
document, LRU entry, and outbox state atomic.

`turn_search_projection.rs` owns the disposable native search store;
`turn_search_query.rs` owns bounded pages and source/owner/query-bound cursors.
Public text fields are kept separate in 4096-character chunks with 512-character
overlap. Headers include titles, and queries return the source role/field with
Unicode-correct snippets. Phrase/title/ID hits precede cross-message word hits;
user original wording wins among equivalent source hits. At most 256 headers
and 512 chunks are visited per page, with empty continuation pages for long
histories. The comparison harness times all continuation calls needed for its
logical search limit. Header reads batch at most 16 rows, and query Bloom bits
are prepared once per request. Exact persisted generation accounting skips
empty chunk ranges; per-chunk Bloom summaries avoid unnecessary text reads.
Neither optimization adds a cache, index, background worker or disk format.

New B+Tree pages target 4 KiB while the format still admits existing pages up
to 16 KiB. Internal splits retain at least two children; a small final group
merges or redistributes within the format ceiling, so maximum-size keys do not
produce empty pages. Adjacent modified leaves repack through one bounded page
buffer, retaining tombstones/OCC versions and untouched block references. The
5,000-row tool-result cleanup fits the existing 2,048-block transaction bound. Packing bounds
derive from `contracts/tofudb_ir_v1.json`.

The sole maintenance worker reads bounded pinned Turn/compaction archive pages
and publishes one complete generation with its coverage counter. Each round
first schedules at most 16 inline backfill headers (128 KiB of source values),
then rebuilds at most 16 dirty conversations through that durable cursor.
This coalesces initial dirty work with backfill; explicit later backfills still
rebuild every source header. Source snapshots are released before the next
round, and mutations on either side of the cursor retain their exact tokens.
Oversized sources stay partial. Turn and archive reads share a 1,024-page budget; reaching
it atomically publishes explicit partial coverage and acknowledges only the
observed dirty tokens. An unchanged oversized source does not retry forever,
and later source mutations can rebuild and clear partial coverage. Owner budgets
include headers/titles as well as chunks.
Fresh persisted invalidation tokens fence ACKs even after marker recreation;
legacy tokens stay readable. Upgrade state binds the source authority UUID and
remains pending until backfill is durably scheduled. Search uses a separate
projection mutex and its failure does not acquire or poison the foreground
authority. The explicit projection directory remains outside authority.

Recent-project navigation now compiles list, single/batched touch, and clear
through one owner-scoped OCC transaction. Digest physical keys admit the full
4,096-character path contract, exact-path verification detects collisions,
blob overflow handles multibyte paths, an exact 1,000-item count bounds list,
and clear retires the full range without walking it. Receipt/outbox replay and
deterministic commit faults preserve either the old collection or the complete
batch. BYO provider create/get/list/update/touch/delete use blob-capable exact-
identity documents, a 32-row owner/tenant-label quota, newest-first covering
index, and tenant-global ID claim. List never exposes the existing envelope
ciphertext; mutation, receipt, outbox, count, index and claim state commit as
one recoverable prefix. Worker jobs use a tenant-global, owner-bearing
blob-capable document plus exact task/idempotency identities. Per-kind priority
summaries and queue/lease
indexes bound claim work while preserving expired-lease precedence, priority,
availability and stable tie ordering; enqueue, claim, heartbeat, cancellation,
completion, fencing and replay state publish atomically. All eleven scheduler
task/poll operations use tenant-global owner-bearing blob-capable task documents,
exact ID claims, owner-local quotas and system-key claims, owner-local and narrow
global created/enabled indexes, plus bounded per-task poll partitions. Adoption,
due claims, result accounting, cross-owner worker feed, and poll lifecycle publish
through the same OCC snapshot. All twelve timer operations use exact owner counts,
status/conversation indexes, a narrow oldest-first global active feed, and
conversation-prefixed poll partitions. The active cap is launch-probed and hard
bounded; poll append/progress, receipt, and outbox effects share one OCC commit,
and conversation deletion permanently retires timer state. Conversation queue
authority stores immutable large payload cores separately from compact
lease/binding state, with exact tenant-global ID claims, owner-local ordering,
narrow owner-bearing worker and lease indexes, and bounded autopilot markers.
All sixteen queue operations now publish item, marker, receipt, and outbox
effects through one OCC transaction; real-message supersession, dequeue/reap,
and conversation deletion preserve legacy ordering and lifecycle behavior. The
unified orchestration authority adds blob-capable definition and run records,
exact tenant-global identities, owner-bearing bounded startup indexes, compact
mutable run state, and versioned events. All nineteen orchestration and Goal
operations execute through one OCC transaction; an exact Goal-active claim
makes supersession O(1), and startup interruption recovery can settle multiple
owners without widening public read scope. Durable swarm persistence separates
blob-capable session/agent checkpoints from compact lifecycle and delivery state.
Exact tenant-global swarm-key claims prevent owner aliasing; a bounded owner-local
resumable index makes startup proportional to pending work, and delivery ACKs do
not rewrite message history. All eight swarm operations are native, leaving 112
catalog operations default-denied before storage access. Model-routing
authority commits use revision CAS and separate blob-capable current, backup,
and migration-receipt records. Envelope ciphertext secrets have an exact
1,024-row owner/boundary quota, reference-order and updated-time indexes, and
bounded 256-row pruning; list projections never expose ciphertext.

The artifact slice now has a storage.v1 semantic and Transaction IR authority.
Chat artifacts store 8 MiB bodies as immutable owner-bound blobs and atomically
maintain conversation, dedupe, path-version, parent, pin/library, and exact-
count records under a 1,000-version ceiling. Reconstructible tool results admit
16 MiB blobs, UTF-8-safe 64 KiB ranges, monotonic seven-day expiry extension,
and bounded owner-local pruning. Their custom blob edges participate in backup
and GC reachability, and deterministic syscall/short-write injection covers
body-plus-index recovery. Ten artifact operations now enforce owner scope,
receipts, compact content-digest outbox evidence, and CPython 15.0 casefold
search generated from one checked machine source. Maintenance pruning remains
unavailable to user RPC, but the single low-priority scheduler now deletes at
most 128 expired owner-scoped tool results per transaction and reports bounded
content-free progress metrics.

`sequencer.rs` owns the one background writer. Submitters hash blocks and encode
envelopes before admission; the worker waits no more than 1 ms and never groups
more than 64 requests or 8 MiB. The queue derives from 1/128 of launch-probed
memory headroom, falls back to a lean 64 requests/16 MiB, and has hard ceilings
of 1,024 requests/256 MiB, blocking backpressure, terminal-error fanout,
graceful drain, and bounded operational metrics.

Every WAL append path, including commits over already-published block
references, poisons the live authority on an ambiguous write error. No caller
may allocate another sequence until close/reopen recovery selects the durable
prefix.

`certify-filesystem` is an explicit destructive-to-an-empty-target operator
probe. It validates the real VFS across an exclusive lock, immutable block
publication, group commit, checkpoint rotation, a child exit that skips Rust
destructors, and cross-process reopens. The no-destructor child also packs a
contract-owned 1 MiB payload, publishes and stabilizes its segment catalog,
reclaims the loose file, and requires both later processes to read the exact
payload through the segment path. The completed store is retained as auditable
evidence. Its machine-readable result also reports wall-clock observations for
each process/reopen boundary and the bounded retained file count/length; those
observations support controlled same-volume comparisons but are not release
certification or a performance claim. This command is never part of ordinary
startup. `certify-filesystem-matrix` adds an absolute empty primary
path, an optional second path from `TOFU_DB_CERT_MATRIX_MOUNT_DIR`, and bounded
syscall-error, ENOSPC, short-write and lost-sync cells. It preserves acknowledged
transactions and only accepts complete unacknowledged prefixes; skipped mounts
produce `matrix_result=partial`, and any failed cell returns nonzero. The
operator records the actual filesystem identity for each path separately;
path labels do not certify that a filesystem is native or FUSE. The deterministic fixture pre-creates 256 empty block shards so random segment IDs cannot move injection positions; real-path probes and block fault tests cover fresh shard creation.

`certify-resource-gate` is the bounded measurement half of the launch
performance/resource gate. It runs one fixed workload on an explicit absolute
empty directory: 512 sequential inline commits plus 24 1 MiB block-carried
commits with one midpoint checkpoint (crossing the 60 MiB rotation window),
one separate commit+checkpoint stage, and one reopen. Each stage runs in a
spawned child whose wall-clock and Linux `wait4` peak RSS are reaped
together; the commit child exits without Rust destructors, so recovery under
the measured workload is also proven. It emits one
`tofu-db.resource-gate.v1` line with per-stage wall-clock, `max_rss_kib`,
stage-timer attribution (encode/checkpoint-check/WAL/tail for commits;
drain/segments/WAL-swap/control for checkpoint), the retained authority
footprint, the launch-probe snapshot and derived budget, the reference
personal-computer budgets (8 GiB RAM / 500 GB disk: 512 MiB peak RSS, 1 GiB
retained bytes, 5 ms mean commit, 300 s total), and explicit `*_exceeded`
booleans. All budgets, including latency, are observational references for
one operator-recorded volume class: the command exits nonzero only when the
workload misbehaves, never on a measurement. The gate decision belongs to
the runner, which pins the exact build and volume, records them beside the
line, and requires every `*_exceeded` flag to be false; the measured store
is retained as auditable evidence. This command is never part of ordinary
startup.

`collect-garbage` is explicit existing-authority maintenance. Without
`--execute` it reports one bounded candidate plan and removes no blocks. With
`--execute` it republishes the unchanged durable state across both CONTROL
slots before reclaiming at most 65,536 orphan loose blocks or 256 MiB. When no
loose orphan wins the round, it inspects one generation-selected window of
at most 16 catalogued payload segments and rewrites or retires at most one
segment. An executed empty window advances the durable CONTROL cursor, so
later windows remain reachable even when many segments share a catalog bucket.
Liveness uses exact block IDs across every hash partition, retaining only one
bounded segment index and decoding one mark run at a time. Partial rewrites fit the same
temporary-space budget, while fully dead segments need no replacement. Both
modes derive bounded mark-spill space from one launch probe and emit
content-free metrics. Use an explicit bounded batch to prove a full catalog
pass; independent one-round calls conservatively report further windows. It
never runs during startup.

`--execute --max-rounds 4096 --budget-ms 600000` runs a bounded exclusive
session, reusing stable-data reachability marks while protecting the current
payload-manifest root on every round. Session admission rejects active snapshot
handles; its engine borrow excludes commits, checkpoints and history retirement.
At most 4,096 plan/execute calls share marks; finish checks cleanup, abandoned
sessions clean up best effort, and any round error prevents reuse. Retained
marks share the existing temporary-space budget with segment replacement; if
both cannot fit, marks are released before rewriting and rebuilt next round.
No marks survive a session or process boundary. `system.reclaim` walks that
session once and skips below `min_free_pages` without changing its response contract.
Batch flags require `--execute`; rounds default to one, time defaults to
60,000 ms, and time is checked between rounds (the first round always runs).
Each round retains the existing metrics line; a final session line reports
rounds, mark builds, completion, and exhausted budgets. Completion requires an
unchanged full catalog-window pass; manifest changes reset coverage, and plans
do not advance it. Budget exhaustion returns explicit incomplete metrics.

Before reachability marking, it stream-matches at most 20 million loose-block
directory entries while retaining at most two paths, then removes at most one
strictly named, size-bounded `.new-*` block temporary and syncs its shard. This
prevents interrupted block publication from leaking files or forcing a later
directory-sized allocation; malformed temporary names fail closed.

After all reclaimable loose, orphan-file, and manifest-segment victims are
absent, GC may compact one bounded set of reachable loose blocks. It excludes
blocks already present in any catalogued segment using a bounded negative
filter followed by exact index membership, without a catalog-wide block-ID set.
Empty loose-block shards perform no mark-run reads. Packing only acts when at
least 128 reachable loose files fit both the 65,536-block/256 MiB
segment bounds and the launch-derived temporary-space budget. The plan reports
candidate bytes, reclaimed loose bytes, replacement segment bytes,
and catalog pressure; sub-threshold shards converge without rewrite churn.

Before inspecting manifest segments, the same explicit command reads at most
4,097 entries from the payload-segment directory and reclaims at most one
strictly named `.pseg` or `.new-*` file absent from the current manifest. It
stabilizes CONTROL first; unknown names, non-files, changed sizes, and oversized
orphans fail closed. Since a manifest admits at most 4,096 segments, every
overfull bounded window necessarily exposes an orphan without an unbounded
directory allocation.

`payload_segment.rs` and `payload_manifest.rs` own physical payload compaction.
One segment admits at most 65,536 content-addressed blocks across hash partitions and 256 MiB
of payload, keeps its sorted index at or below 3 MiB, and validates index and
random-read payload integrity independently. A canonical catalog admits 4,096
segments; full block-ID ranges select point-lookup candidates. Engine
compaction publishes the catalog block through `CONTROL`, installs its bounded
reader, republishes the same root into the fallback slot, and only then removes
the loose copies. Normal open reads one catalog block and loads segment indexes
only on a matching point lookup; it never scans the directory. Deterministic
syscall, short-write, lost-sync, corruption, fallback-slot and cross-reopen tests
preserve every committed reference. This path remains pre-authority and has no
automatic startup scheduling; reclamation and profitable packing are only the
explicit GC command.
Publishing a payload catalog upgrades CONTROL layout to v2 while the reader
still accepts zero-extended v1 slots. Older binaries reject v2 instead of
silently ignoring a catalog whose loose blocks may already be reclaimed.

Conversation catalogs and updated-order lists for owners with at most 1,000
live headers use bounded primary-header scans. Historical updated-index OCC
tombstones must not determine small-directory query cost. Lists apply filters
before sorting by descending update time and ID, then load transcripts only for
selected rows; response byte limits remain enforced. Larger owners retain the
bounded index plan. Exhausting its 1,024-leaf range witness budget reports a
resource error rather than a malformed request; this is not automatic index
compaction or an unbounded scan fallback.

The tri-state authority root is carried in each transaction envelope and the
resulting current root is published directly in checksummed `CONTROL`.
Asynchronous pack publication updates only its physical catalog and CONTROL
generation; it must preserve every live logical field, including authority roots
committed while publication runs. Open reconciles a stale CONTROL authority root
with the latest authenticated active-WAL root even when its sequence is already
current. This repairs mismatched metadata only while that witness remains:
if later transactions already incorporated missing state, or the relevant history
is no longer available, this is not a general historical-data recovery mechanism.
Normal entity open therefore reads only the current content-addressed root page;
descendants verify lazily along accessed paths and commits validate only newly
generated pages instead of scanning the whole tree. Legacy slots retain a
one-segment exact-lookup compatibility path. Incremental
backup manifests preserve this witness and decode their legacy form explicitly.

`vfs.rs` defines the bounded runtime filesystem interface and a deterministic
durability model for short writes, dropped syncs, injected I/O errors, namespace
publication, and crashes. Block publication, `CONTROL`, the active WAL, and the
authority lease share this seam. End-to-end commits enumerate I/O errors and
short writes; paired durability barriers prove acknowledged recovery when any
one file or directory sync silently reports success without persisting.
Correlated multi-sync loss is certified: every enumerated pair of dropped
commit+checkpoint syncs recovers the complete acknowledged prefix or fails
closed. Barrier-latency optimization remains a certification gate.

The compatibility baseline is generated at
`contracts/storage_operations_v1.json`; the same generator owns the sorted Rust
metadata projection. Promotion requires the semantic,
fault-injection, migration, platform, and performance gates in
`docs/STORAGE.md`; adding an application selection switch before those gates is
a contract violation.

The payload-segment read cache derives a small negative membership filter from each
verified immutable index; filter hits still require an exact block-ID lookup and
payload reads still verify CRC32C and the content hash. Index allocations and
filters share the existing 64 MiB cache ceiling, at most 4,096 segments are
resident, and access records are compacted before exceeding 8,192 allocated
slots while preserving LRU order. One filter is at most 128 KiB. These bounds
come from `payloadSegmentReadCache` in the native IR contract; repeated-read,
false-positive, eviction and maximum-index regressions cover them.
Catalogued point reads reuse open segment handles through a separate,
deliberately tiny LRU of at most 32 handles keyed by segment ID: segments are
sealed and content-addressed, so one handle provably serves the exact bytes
the ID names, and retirement or manifest replacement evicts it. Reopening the
file per block multiplied network-filesystem round trips — WAL recovery
replays thousands of block fetches and each `open` is a remote metadata
operation there, which dominated reopen wall-clock on FUSE stores (a
7-second recovery of a 700-transaction log dropped to sub-second). The bound
is independent of the index cache so desktop descriptor budgets (macOS 256)
absorb it beside the existing working set, and handles never retain payload
bytes. Per-read CRC32C and content-hash verification is unchanged; unit
regressions pin single-handle reuse, LRU eviction at the bound, and eviction
on segment removal.

The verified block-payload cache also bounds recency metadata and admits at most
65,536 entries and 131,072 allocated recency slots under its existing launch-derived
byte budget. Shared vectors are charged by allocated capacity; zero capacity
disables all population and releases payload and metadata allocations. Cache
policy comes from `blockPayloadReadCache` in the native IR contract.

The decoded-page cache retains verified immutable pages only. Repeated hits compact
stale recency records once they exceed twice the resident page count, preserving
LRU eviction order; disabling the cache releases its recency allocation. A
100,000-hit regression covers this metadata bound independently of payload bytes.
Settled-append regression measures staged blocks/bytes across 256 appends and
checks transcript identity after reopen; it is not a wall-clock superiority gate.
Entity pages allocate their exact encoded length within the 16 KiB format bound
and transfer that allocation into the existing pending store. The owned staging
entry point drops spare capacity and preserves the same verified deduplication,
WAL-carried payloads and checkpoint durability. Fixed-byte page fixtures, maximum
page/capacity checks and per-I/O commit faults cover this allocation boundary.
Copy-on-write pages and rebuilt root directories stage through a fresh-content
entry point that skips the durable dedup probe entirely: staging stays idempotent
through the pending-set entry API, the commit's WAL envelope remains the
durability point, and a coincidental durable duplicate costs WAL bandwidth
only. Stage-attribution counters (`get_unc_miss` write-path probes) assert the
probe elimination; turn-append burst measurement attributes the FUSE win to it
(13-32 probes per commit removed).

Reference commits share transaction-local pending page/root payload handles until
WAL encoding instead of deep-copying those buffers. Exact-envelope and injected
commit-fault regressions preserve recovery and handle release; this does not
populate the verified-read cache or remove any durability barrier.

A root already present in the reference list is carried once, not again in the
authority-root slot; an independent root keeps its own payload. Byte-exact and
crash/reopen regressions cover both paths.

Payload segments coalesce small writes through at most 256 KiB of scratch space;
larger blocks bypass that buffer. The 128 × 8 KiB regression reduces writes from
132 to 8 with identical encoded bytes and unchanged sync/rename barriers.
Checkpoint history encodes and stores one ≤4 MiB segment at a time, with exact
capacity for short suffixes instead of retaining the entire checkpoint encoding.
The pending store takes ownership of that encoded allocation. History hash
verification streams the shared 56-byte WAL header, payload and CRC into BLAKE3
without allocating another framed record; persisted hashes and the 8 MiB
transaction ceiling remain identical.

A supervised background packer (`background_pack.rs`) reserves staged payloads
of at least 1 MiB, builds and fsyncs the 8 MiB segment file without holding the
authority mutex, and hands the finished segment back; commits or an otherwise
idle maintenance round publish its manifest reference and retire staged copies. It also accepts the one
pending history segment; the already durable active WAL supplies its recovery
source. Checkpoint materializes all pending blocks before publishing history
and retiring that WAL. An RAII registration admits only one worker per store,
clears availability on exit or spawn failure, and makes stop/drop release the
worker before the authority is discarded. History arrival and publication wake
the same worker through a revision-checked condition variable; notifications
arriving before sleep are retained, while ordinary small payloads keep the
25 ms idle fallback. Every worker failure
leaves payloads staged, so the synchronous bounded checkpoint drain remains the
complete deterministic fallback; an in-flight create that outlives its wait
budget is abandoned and repacked synchronously. Setting
`TOFU_DB_DISABLE_BACKGROUND_PACK` restores the pre-worker behavior (checkpoint
drain only) as an operations escape hatch.

The existing idle maintenance round adopts at most one completed background
pack after history and business maintenance find no work. It uses nonblocking
authority admission, requires spare catalog capacity, and never merges payloads
or checkpoints the active WAL. This gives recovered WAL payloads a durable
catalog reference even when the daemon serves only reads. Normal worker exit
joins after reclaiming its still-completed unpublished segment, before releasing
its worker registration; publication already handed to the authority remains
protected. Staged/WAL source bytes remain intact and cleanup errors surface.
Metrics report published packs and discarded segment count/bytes. Abrupt exits
can still leave bounded orphan files for the explicit GC path.

The internal search-backfill checkpoint persists an owner/tenant-scoped generation
and at most 1,024 cursor bytes, with exact-observation OCC and atomic dirty-token
progress. Explicit Rust `schedule_owner_search_backfill` schedules one supplied
scope; the existing worker consumes at most eight header rows after dirty work
drains, atomically enqueues invalidations and advances the cursor. Completion
means invalidations are enqueued, not that the projection is published. No startup
scan or automatic scheduling occurs. The legacy global RPC remains unavailable
until authorized global scope enumeration is implemented and certified.

Turn event recording reports continuation status in responses and terminal replay
payloads. Candidate validation is bounded to 8 MiB. Native continuation
persistence stores one latest checkpoint per owner/conversation/lane through
`model_continuation.get`/`put`/`delete`: the settled Turn ordinal plus one is
the monotonic CAS frontier, the model-visible witness fences edited or foreign
writers, and equal-frontier retries are read-only so no candidate can
manufacture a stored acknowledgement. Terminal settlement persists a valid
candidate atomically inside the settlement transaction and reports `stored` or
`rejected`; absent-candidate settlement matches the legacy authority. Commit
fault-injection regressions prove every put recovers one atomic prefix and
replays to the same checkpoint.

`scripts/tofudb_perf_compare.py` compares ten client-stack workloads: eight
sequential phases, one concurrent phase (one writer plus four readers), and
one maintenance phase (graceful stop plus reopen walls on the seeded
authority).
Rust/SQLite authorities are disposable; PostgreSQL requires an explicitly created
disposable database supplied through `--postgres-dsn`, never a live authority.
Legacy drivers own separate runtimes/clients and restore startup
environment overrides, so starting PostgreSQL cannot replace SQLite's runtime.
Select a freshly built release binary with `TOFUDB_PERF_BIN`; report mean and tail
latencies separately. Single-run wins do not certify durability, sustained load,
or production cutover. In the current paired medium runs (fresh-write staging
build), conversation creation and settled Turn append lead SQLite on mean
latency (1.21x and 1.47x respectively) and creation/append p99 tails still
trail SQLite's; mean wins do not establish tail-latency parity. `conversation.search` scans the indexed owner corpus in
bounded pages. Measure after the search worker drains the source writes and
require ready coverage as well as visibility of every seeded search document,
and include every continuation needed for the requested logical result count;
incomplete projections or timing only the first page understate search cost.

`--workloads turn_append_burst` (comma list) restricts the comparison to named
workloads for focused iteration. For in-process attribution of the settled Turn
append hot path (no loopback socket), run
`cargo run --release --example turn_append_bench -- [iterations]`: it drives the
same `TransactionIrStep::TurnAppendSettled` IR the semantic executor emits —
command effects included — against a disposable on-disk authority and prints
per-window p50/p95/max so history-dependent growth (entity copy-on-write page
rewrite amplification as namespaces widen) is visible alongside the median.
It starts the same default background packer as the daemon and honors
`TOFU_DB_DISABLE_BACKGROUND_PACK`; iterations must be an integer in 1..=10,000.
With `TOFU_DB_FSYNC_STATS`, daemon exit also reports fixed maintenance counters,
including search failures, rebuilds, source pages, and foreground deferrals.
Checkpoint diagnostics add total and maximum remaining history record bytes
(`checkpoint_history_tail_bytes`, `max_checkpoint_history_tail_bytes`). These
measure the synchronous suffix after partial preparation finishes, exclude segment
headers, and include attempts whose later publication fails. Two fixed 64-bit
atomics retain these observations without storing samples or creating a worker.
Only eight slow samples retain fixed stage snapshots. Nested stage timings
overlap and include concurrent worker activity; they cannot be summed as
independent foreground costs.

Reports include p99 and maximum latency as well as mean/p50/p95. A skipped engine
or failed workload makes the command exit nonzero while retaining the report;
an incomplete comparison must not be counted as a passing benchmark. Reports
record binary SHA-256, engine order, Python version and timing scope. At most ten
chronological latency windows expose growth within a workload. Empty/duplicate
engine selections and invalid explicit binary paths fail rather than silently
changing the comparison. Rust startup bounds its readiness line to 4 KiB/120 s
and cleans disposable resources on failure. Current conversation-open rows have
empty transcripts; the mixed workload is 80% point reads, 10% writes and 10%
sidebar reads. These results do not cover long transcript opens or contention.

For local engine work:

```bash
cargo test --manifest-path packages/tofu-db/Cargo.toml
cargo run --manifest-path packages/tofu-db/Cargo.toml -- init --data-dir /absolute/persistent/empty/path
cargo run --manifest-path packages/tofu-db/Cargo.toml -- certify-filesystem --data-dir /absolute/persistent/empty/path
cargo run --manifest-path packages/tofu-db/Cargo.toml -- certify-resource-gate --data-dir /absolute/persistent/empty/path
cargo run --manifest-path packages/tofu-db/Cargo.toml -- collect-garbage --data-dir /absolute/existing/authority
cargo run --manifest-path packages/tofu-db/Cargo.toml -- collect-garbage --data-dir /absolute/existing/authority --execute
cargo run --manifest-path packages/tofu-db/Cargo.toml -- backfill-activity-index --data-dir /absolute/existing/authority --tenant-id 7 --owner-id 11 --maximum-rows 256 --execute
cargo run --manifest-path packages/tofu-db/Cargo.toml -- backup --data-dir /absolute/source --backup-dir /absolute/backup
cargo run --manifest-path packages/tofu-db/Cargo.toml -- restore --backup-dir /absolute/backup --data-dir /absolute/empty-target
cargo run --manifest-path packages/tofu-db/Cargo.toml -- prune-backup --backup-dir /absolute/backup --retain-generations 3
cargo run --manifest-path packages/tofu-db/Cargo.toml -- certify-release --manifest /absolute/SHA256SUMS --signature /absolute/SHA256SUMS.sig --binary /absolute/packaged/tofu-db
cargo run --manifest-path packages/tofu-db/Cargo.toml -- certify-release-keygen --key-out /absolute/secret-key.json --pubkey-out /absolute/public-key.json
```

## Platform support

The engine's Rust sources are portable across Linux, macOS, and Windows.
Platform-specific launch-time observation is gated behind `cfg`:

| Surface | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Host memory capacity/headroom | `/proc/meminfo` | `sysctl hw.memsize` + Mach `host_statistics64` | `GlobalMemoryStatusEx` |
| cgroup CPU/memory ceiling | v1/v2 quota files | not applicable | not applicable |
| Authority volume free bytes | `fs2::available_space` (all platforms) | | |
| Directory sync | `File::sync_all` | `File::sync_all` | no-op (no dir-handle fsync) |
| Secret-file permissions | `0o600` | `0o600` | profile-dir ACL |
| `certify-resource-gate` peak RSS | `wait4` | refuses to run | refuses to run |

The engine runs on all three desktop platforms; the launch probe falls back
to the lean profile wherever an observation is unavailable, so behavior
stays correct rather than host-specific. `certify-resource-gate` is a
Linux-only certification command by design (it requires `wait4` peak-RSS
accounting) and refuses to run on other platforms; the runtime daemon does
not depend on it.

**Building.** The pure-Rust crates compile for any target, but three
transitive dependencies bundle C sources (`libsqlite3-sys`, `libmimalloc-sys`,
`zstd-sys`) and therefore need a C toolchain for the target. Cross-compiling
from one OS to another requires a matching cross C toolchain (e.g. osxcross
or clang with the target SDK); the simplest supported path is to build
natively on each platform. On any of the three desktop OSes with Rust
installed:

```sh
cargo build --release --manifest-path packages/tofu-db/Cargo.toml
```

The platform `cfg` branches are type-checked for all three targets in CI via
`rustc --emit=metadata` (see `scripts/platform_branch_check.rs`), which
validates the gating and FFI signatures without needing a cross C toolchain.
Android is not yet a supported target; it requires the Android NDK and a
Termux/AOSP packaging step that have not been added.

## Block-at-rest encryption

The block store can encrypt payload-bearing files at rest behind the VFS
syscall boundary with length-preserving AES-256-GCM per 4 KiB region. Content
addressing stays on plaintext (`BlockId = BLAKE3(plaintext)`), so dedup, GC
and backup-index verification are unchanged. `blocks/**`,
`payload-segments/**` and `active*.wal` are encrypted; `CONTROL`, `LOCK` and
the key fingerprint stay plaintext, and the deterministic simulator VFS is
never wrapped. Encryption is opt-in: set the bounded ASCII
`TOFU_DB_AT_REST_KEY` launch secret (read into a bounded buffer, removed from
the environment, zeroized after key derivation). Absent the variable the
store runs plaintext. On authority open a key-identity witness fails closed
with `PermissionDenied` on the wrong or absent key. The backup destination
store receives the same key, so backups of an encrypted source are themselves
encrypted at rest; restoring also re-encrypts the target. A plaintext backup
target of an encrypted source is rejected rather than silently downgraded.

## Signed release packaging

`scripts/release_package.py` builds the deterministic tarball and its SHA-256
checksum manifest. Passing `--signing-key <secret-key.json>` additionally
emits `<base>.SHA256SUMS.sig`, a detached Ed25519 signature over the exact
manifest bytes (the secret half is zeroized after signing). The release
signing public key is the trust root, distributed in
`packages/tofu-db/keys/release-signing-public.json`; the secret half never
enters the repository. `tofu-db certify-release --manifest <SHA256SUMS>
--signature <SHA256SUMS.sig> --binary <packaged-tofu-db> [--pubkey <path>]`
is the explicit operator gate: it verifies the signature over the manifest
and matches the packaged binary's SHA-256 against the manifest's `tofu-db
(stripped, packaged)` line, exiting nonzero on any mismatch. A manifest
without a valid signature blocks certification (fail-closed). The gate is an
operator command only; the daemon never auto-verifies at boot. Key custody is
an operator concern — generate a pair once with `certify-release-keygen`,
keep the secret outside the repository (mode 0600), and commit only the
public half.
