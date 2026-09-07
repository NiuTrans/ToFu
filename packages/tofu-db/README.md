# Tofu-DB

This is the pre-authority Rust engine for Tofu's personal storage workload.
It currently implements the crash-recovery envelope: exclusive ownership,
alternating checksummed control generations, immutable 4 MiB content-addressed
blocks, deterministic transaction envelopes, and a bounded hash-chained active
commit log with generation names and checkpoint-chain recovery bases. CONTROL
now records checkpoint witnesses, manifest roots, and the active generation;
explicit Engine checkpoint packs active transactions into immutable history
segments and atomically switches to a new WAL generation; commits trigger it at
a 60 MiB soft limit below the 64 MiB recovery bound. A failed CONTROL
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
deletes them only through ordered, idempotent ACK transactions. Aggregate
multi-owner admission and the semantic executor are still absent.
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
offline target after a bounded capacity preflight, using a durable generation-pinned resumable marker. Retention GC uses restartable plans and at most 1 GiB of on-volume sorted mark runs instead of a database-sized in-memory set. It does **not** implement the remaining 112 Tofu semantic operations, general query
execution, migration, GC spill scale certification, signed release packaging,
or Supervisor selection path.

`protocol.rs` implements the transport-only `storage.v2` machine contract in
`contracts/storage_v2.json`: canonical flat MessagePack, fixed numeric fields,
length plus CRC32C framing, Hello negotiation, correlation/deadline/owner/
tenant/command/schema identity, guarded allocation admission, bounded error
envelopes, a redacted and zeroized 256-bit Hello capability, and 1 MiB blob
chunks. `semantic.rs` is the mandatory default-deny
boundary after decoding: it binds requests to generated metadata for all 331
frozen operations, matches envelope identity against an authenticated owner and
tenant scope, enforces negotiated schema/deadline limits, and requires a
command ID for every command or maintenance operation independently of legacy
receipt policy. Authenticated connections may assemble one identity-bound
artifact request from at most 20 ordered 1 MiB chunks and retain every chunk's
share of the process-wide frame budget through dispatch; single-frame requests
remain capped at 8 MiB. Successful responses above 1 MiB use at most 20
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
`backend=tofudb` and `preAuthority=true` envelope cannot pass the current
Supervisor backend allowlist.

`resource_probe.rs` performs one bounded launch observation before authority
open. It combines process affinity with cgroup v1/v2 CPU quota, combines Linux
`MemAvailable` with cgroup capacity/current usage, and reads free bytes from the
actual authority volume. Missing core evidence selects four connections and a
16 MiB aggregate frame budget; observed pressure can reduce admission to one,
while hard ceilings remain 64 connections, 128 MiB of frames, and 2 MiB per
connection stack. A writable daemon refuses observed free space below two full
64 MiB WAL windows plus 16 MiB instead of beginning work it cannot safely
rotate. Readiness exposes only these aggregate observations and selected bounds.

`contracts/tofudb_ir_v1.json` is the generated machine authority for Schema IR
and Transaction IR bounds. `transaction_ir.rs` interprets owner-scoped entity
reads, conditional writes, result projections, receipts, and logical outbox
capture in one OCC transaction. `semantic_executor.rs` compiles or routes 86
artifact, compaction-archive, conversation, Turn, model-routing, provider,
worker-job, desktop, record, system, and indexed-event operations into that IR with
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
ceiling without N+1 entity reads.

Browser site observations now compile get and record through owner-scoped,
digest-keyed, exact-identity-verified documents. A covering LRU index carries
expiry and deterministic tie-break fields, so the 30-day/200-document policy
needs one bounded index page and materializes only documents actually returned
or deleted. Passive-only hint validation rejects query-bearing paths and
sensitive shape keys; deterministic syscall-error and short-write replay keeps
document, LRU entry, and outbox state atomic.

`turn_search_projection.rs` now provides the Rust-owned disposable search
target without SQLite/PostgreSQL dependencies. Bounded eight-Turn pages build
an invisible generation; one authority transaction swaps the active header
and retires the prior generation, so search observes no partial rebuild. Each
Turn carries a fixed n-gram Bloom summary and chunked 10-KiB text, allowing
negative candidates to avoid text materialization while exact phrase and
cross-Turn word checks preserve the storage.v1 result order and snippets.
Logical bytes are owner-bounded, and exhaustive publish syscall-error and
short-write recovery admits exactly one complete generation. Epoch-valued
dirty-set consumers rebuild at most 16 conversations per round on the sole
maintenance worker and release authority between bounded source pages.
`conversation.search` uses an independent projection mutex, so it cannot queue
behind or hold the foreground authority lock. The daemon enables it only for
an explicit absolute persistent directory outside authority; readiness reports
configured/available separately and projection failure degrades only search.

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
startup.

`collect-garbage` is explicit existing-authority maintenance. Without
`--execute` it reports one bounded candidate plan and removes no blocks. With
`--execute` it republishes the unchanged durable state across both CONTROL
slots before reclaiming at most 65,536 orphan loose blocks or 256 MiB. When no
loose orphan wins the round, it inspects one generation-selected hash shard of
at most 16 catalogued payload segments and rewrites or retires at most one
segment; an executed no-victim shard advances the durable CONTROL cursor, so
repeated rounds cannot starve later shards. Partial rewrites must fit the same
temporary-space budget, while fully dead segments need no replacement. Both
modes derive bounded mark-spill space from one launch probe and emit
content-free metrics. Repeat while `more_candidates=true`; it never runs during
startup.

Before reachability marking, it stream-matches at most 20 million loose-block
directory entries while retaining at most two paths, then removes at most one
strictly named, size-bounded `.new-*` block temporary and syncs its shard. This
prevents interrupted block publication from leaking files or forcing a later
directory-sized allocation; malformed temporary names fail closed.

After all reclaimable loose, orphan-file, and manifest-segment victims are
absent, GC may compact one generation-selected loose hash shard. It excludes
blocks already present in that shard's at-most-16 manifest segments and only
acts when at least 128 reachable loose files fit both the 65,536-block/256 MiB
segment bounds and the launch-derived temporary-space budget. The plan reports
the shard, candidate bytes, reclaimed loose bytes, replacement segment bytes,
and catalog pressure; sub-threshold shards converge without rewrite churn.

Before inspecting manifest segments, the same explicit command reads at most
4,097 entries from the payload-segment directory and reclaims at most one
strictly named `.pseg` or `.new-*` file absent from the current manifest. It
stabilizes CONTROL first; unknown names, non-files, changed sizes, and oversized
orphans fail closed. Since a manifest admits at most 4,096 segments, every
overfull bounded window necessarily exposes an orphan without an unbounded
directory allocation.

`payload_segment.rs` and `payload_manifest.rs` own physical payload compaction.
One segment admits at most 65,536 same-shard content-addressed blocks and 256 MiB
of payload, keeps its sorted index at or below 3 MiB, and validates index and
random-read payload integrity independently. A canonical catalog admits 4,096
segments but at most 16 point-lookup candidates per hash shard. Engine
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

The tri-state authority root is carried in each transaction envelope and the
resulting current root is published directly in checksummed `CONTROL`.
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
Correlated multi-sync loss and barrier-latency optimization remain
certification gates.

The compatibility baseline is generated at
`contracts/storage_operations_v1.json`; the same generator owns the sorted Rust
metadata projection. Promotion requires the semantic,
fault-injection, migration, shadow-run, platform, and performance gates in
`docs/STORAGE.md`; adding an application selection switch before those gates is
a contract violation.

For local engine work:

```bash
cargo test --manifest-path packages/tofu-db/Cargo.toml
cargo run --manifest-path packages/tofu-db/Cargo.toml -- init --data-dir /absolute/persistent/empty/path
cargo run --manifest-path packages/tofu-db/Cargo.toml -- certify-filesystem --data-dir /absolute/persistent/empty/path
cargo run --manifest-path packages/tofu-db/Cargo.toml -- collect-garbage --data-dir /absolute/existing/authority
cargo run --manifest-path packages/tofu-db/Cargo.toml -- collect-garbage --data-dir /absolute/existing/authority --execute
cargo run --manifest-path packages/tofu-db/Cargo.toml -- backfill-activity-index --data-dir /absolute/existing/authority --tenant-id 7 --owner-id 11 --maximum-rows 256 --execute
cargo run --manifest-path packages/tofu-db/Cargo.toml -- backup --data-dir /absolute/source --backup-dir /absolute/backup
cargo run --manifest-path packages/tofu-db/Cargo.toml -- restore --backup-dir /absolute/backup --data-dir /absolute/empty-target
cargo run --manifest-path packages/tofu-db/Cargo.toml -- prune-backup --backup-dir /absolute/backup --retain-generations 3
```
