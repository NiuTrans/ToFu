# Storage operations and verification


## Native loose-block read cost

Real loose-block reads reuse one observed length instead of querying it twice.
It remains an allocation hint: bounded EOF reading, actual returned size,
CRC/content hash and wrapper read-fault boundaries still apply. The bounded
read owns only its existing call-local buffer; no worker, resident cache or
persistent growth is added. Paired FUSE warm-read component measurement and
limits: [length reuse](STORAGE_PERFORMANCE_CHARTER.md#loose-block-length-reuse).

## Native hot-cache residency

The one launch probe sizes payload/page caches at headroom/16 and /32 with
32/16 MiB lean floors; explicit MiB overrides are bounded per cache by
headroom/4. Fixed 2/1 GiB ceilings are removed. These are demand-driven
resident byte budgets, not reservations or durable state; existing cache
eviction/close releases ownership. Decoded chunk/proof budgets retain their
existing proportional derivation. No worker or persistent growth is added.
For the 128 GiB headroom measurement, decode-cache accounted bytes rose from
133,252,159 to 314,763,240, within the 128/512 MiB budgets, and zero-capacity
release returned both to zero. Method, timing limits and rationale:
[cache scaling](STORAGE_PERFORMANCE_CHARTER.md#hot-cache-scaling).

## Native checkpoint record lifecycle

Explicit native embedding retains one current header and one current snapshot
per owner/task, replacing both through the existing WAL/blob authority. Header
CAS has no permanent per-command receipt, new thread, process or task cache.
Snapshot residency is bounded by caller-supplied bytes and retired blob/history
versions follow existing GC/backup retention; this adapter never deletes durable
user state. The synthetic 32 KiB tool-result test checks blob recovery and proves
its physical document version remains 1 through release, reacquisition and begin,
while header versions advance. This is write-avoidance evidence, not latency/RSS
or whole-system admission. Configured outbox adds one bounded encrypted mutation per header CAS; Keep
borrows metadata only and never reads old history. Payload admission precedes
blob staging and the existing outbox ACK/retention lifecycle owns source growth.
Passive replay keeps one current witness per task (source UUID/version/digest),
committed with state; no per-command receipt history or worker is introduced.
The 32 KiB fixture produces four source mutations; only completion contains
history. Latest redelivery and failed sync recover atomically. This establishes
bounded record shape and write avoidance, not end-to-end timing/RSS. Explicit
publish_pending connects bounded fetch, passive target, the existing immutable
engine archive and ordered source ACK; source writer is released during sink I/O.
One archive UUID joins the latest target witness; no extra receipt log/cache,
worker or background lifecycle is added. Existing archive byte limits and caller
lifecycle own its retained encrypted records. The four-record fixture proves
whole-batch lost-ACK retry leaves target version 4 and archive sequence 4.
Archive replacement, archive-sync and source-ACK-sync cases fence or reopen before
converging. Direct old/gapped records still refuse; cross-domain routing is pending.
## Embedded native checkpoint lifecycle and cost

The opt-in embedded checkpoint binding shares the selected runtime authority and
its existing WAL/maintenance drivers. Generation-bound durability waits release
the writer; lifecycle readers cover entered calls so close joins before driver
retirement. No new worker, timer, history cache or receipt log is introduced.
Owned callbacks/C ABI capabilities retain the runtime allocation and authority
lock until dropped; forget/close reclaim capability metadata without releasing
leases. The lazy registry adds no history cache or durable receipt; ownership
still follows caller lifetime, not TTL, count quota or authority replacement.
Frozen finish status/Suspend adds a per-capability mutex; the debug metadata
probe measured 24 inline bytes per Entry (previously 16), plus an Arc allocation.
This excludes allocator overhead/shared runtime and proves no latency or RSS gain.

A debug disposable local-authority probe (2026-10-10, eight acquire/load/release
cycles) measured 0.289 ms median. Threads stayed 11 to 11; the lifecycle guard
and Store wrapper measured 12/48 inline bytes, excluding shared heap/runtime,
lease metadata and durable records. This is local binding/durability evidence,
not release throughput, complete RSS or production latency. Run
embed::checkpoint::measurements::embedded_checkpoint_binding_metadata_cost with
native-runtime-checkpoints and one test thread; native_store tests verify exact
WAL/close/fault ordering. Header/snapshot retention remains the existing journal
contract; the binding adds no durable state or cleanup policy.
## Model-context semantic version

A settled projection edit still advances projection CAS, conversation/sync
revision and search/archive invalidation. Native stores one private nonnegative
`_modelContextDisplayOffset` integer only after a verified display-only update;
SQL schema 67 stores one default-zero BIGINT (eight logical bytes per Turn).
The derived public `modelContextRevision` is optional additive DTO metadata.
No new entity, worker, resident cache or startup history scan is introduced;
Turn deletion/trash/purge owns the existing metadata lifecycle. Exclusive
SQL-to-native import/export preserves nonzero display offsets and rejects an
offset exceeding its projection revision; zero-offset legacy snapshots retain
their original encoding. Native header
encoding is unchanged without an offset. New compact proofs are v2; legacy
proofs remain readable but rebuild once, never silently upgrade.

Comparison borrows evidence strings without serializing large tool bodies;
only producer-owned display locations are excluded. Native JSON variant
comparison and SQL's type-aware JSON comparator preserve bool/number and
integer/float distinctions. Terminal witnesses include semantic revision and
Attempt identity so edit/revert and Continue cannot resurrect an obsolete
completed prefix. Page/header/continuation byte limits and ownership/CAS,
current-attempt, settlement, cancellation and replay fences are unchanged.
The behavioral/measurement lane is `tests/tofudb/test_context_semantic_revision.py`
plus `tests/compaction/test_dispatch_context_recovery.py`; semantic authority and
recovery rationale live in [context compaction](CONTEXT_COMPACTION.md).
Restore and publication eligibility are separate: validated Continue may publish
only when its final model message exactly represents delivered content.

Synthetic comparison: 32 rounds / 8,388,918 evidence characters / 30 calls measured 1.621 ms median and 16,656 temporary bytes on Python 3.12.13. At value one, private offset/header fields add 30/24 compact JSON bytes excluding braces/separators; no payload copy or new worker. These are comparison/storage measurements, not production cache-hit, latency or billed-savings claims.

Search projection failures use independent per-scope retry feedback in
`packages/tofu-db/src/maintenance.rs`. Problem: other maintenance work resets
the shared idle cadence, so a persistently failing derived index can repeatedly
rescan its source while foreground storage is under pressure. Decision: failed
search batches wait the launch-derived maintenance interval, doubling on each
consecutive failure up to the existing 60-second maintenance ceiling. Success
resets only that scope. Dirty tokens remain durable until the existing exact-token
acknowledgement; retention and overlay draining keep their original scheduling.
`searchRetryDeferrals` and `searchMaximumFailureBackoffMilliseconds` expose the
feedback. Warnings at failure counts 1, 2, 4, … contain only I/O kind and retry
spacing. Alternative rejected: backing off the whole maintenance worker delays
unrelated retention/draining and healthy owners; dropping failed dirty tokens
would silently leave search incomplete. One in-memory retry record per admitted
scope is owned and reclaimed by the existing worker; no thread, queue, persistent
state, startup scan, or performance cap is added to healthy work. Tests inject a
derived-index failure, verify unrelated maintenance proceeds, repair the index,
and verify exact-token acknowledgement and source reopen. A zero-cost, persistent
failure at a 250-ms scheduling cadence makes eight attempts in 60 seconds versus
240 without feedback; this is a scheduling bound, not a production latency claim.
Linux retry records measure 40 bytes each (2,560 bytes at 64 admitted scopes,
excluding the Vec header/allocation rounding). Counters, limits and verification:
[search retry feedback](evidence/tofudb_search_retry_feedback_20261008.json).

Host-keyed build outputs participate in the same reuse checks. Restart builds
keep release opt-level 3, use Thin LTO and incremental code generation, and derive
LLVM codegen units from the launch CPU/memory worker budget; explicit Cargo
profile overrides win. Problem: full LTO plus one codegen
unit serialized the large engine despite many available CPUs. Alternative
rejected: lowering optimization to debug builds would tax storage operations.
Measured on the same 63-CPU host: a cold optimized build took 90.9s; the preceding
production full-LTO build took 528.5s (different concurrent load, not a fixed SLA).
Preparation reports actual stages via the existing admin approval GET's bounded
`preparation` projection, defined in `contracts/restart_preparation_v1.json`.
The browser observes at 2s intervals with one request in flight, stops on every
terminal/retry transition, and shows stage plus elapsed time without invented
compilation percentages. Compiler logs remain local. Fifty retained approval
records bound progress storage; preparation never changes approval authority.

An unchanged build on that host took 0.785s; disposable engine/library validation
took 2.974s. Incremental intermediates live in the existing host Cargo cache,
retain reconstructible state only and follow Cargo session reclamation.

Automation execution uses launch-probed `TOFU_AGENT_WORKERS` capacity with lazy
growth and one idle claim per 250 ms; empty claims retain no command receipt.
On native source `5412f267…`, a synthetic capacity-97 service added two idle
Python threads (1→3), four active actions grew to eleven including heartbeats,
and shutdown returned to one. Two warm idle seconds made eight claims with zero
receipt/file-length growth; Python/native RSS stayed 49,252/23,584 KiB. Four active
actions measured 49,520/26,848 KiB; native threads grew 11→16 through shutdown.
Executor threads retain their bounded peak until service stop. These are one
isolated daemon's logical file/RSS observations, not physical disk or throughput
guarantees. Shutdown joins effects and heartbeats before fenced deferral/storage
close; native/SQLite/Postgres tests prove immediate takeover and no unknown-write
redispatch. A lost defer acknowledgement retains recovery authority, never success.

Timer polling uses the shared bounded process runner for both predicates and
model evidence. Its 16 MiB output-flood probe retains <2 MiB parent heap and at
most 4,010 diagnostic characters per check. Timer readiness metadata is committed
before continuation dispatch; a ready row survives final-budget and restart
retries without adding ledger rows or paid checks. Default watchers have 120
polls; explicitly unlimited watchers retain durable audit rows and can grow.
No extra resident thread, cache or storage backend is introduced. SQL/native
admission both accept code-only definitions; native zero poll budgets remain zero.
Executable checks live in `tests/scheduler/test_timer_readiness_reliability.py`,
`tests/storage/test_storage_sidecar_contract.py`, and native semantic timer tests.

Execution event metadata retains the producer's epoch-anchored monotonic
timestamp before asynchronous observation. This adds 30 serialized bytes per
delta in `benchmarks/execution_clock.py` (Python 3.12.13; 100,000 events × five
samples, 503.7 ns median including event copying); structural boundaries also
carry the task's fixed clock identity and origin. Existing event/replay byte
budgets count those fields. No new resident worker, queue or durable entity is
allocated. Older events without producer clock evidence retain write-time
metadata. The [Turn Trace contract](contracts/TURN_TRACE_CONTRACT.md) owns clock-domain and missing-live-clock evidence, separate from browser receipts.
Managed root event delivery uses the bounded coordinator and measurements in [TASK_EVENT_DELIVERY.md](TASK_EVENT_DELIVERY.md).

TofuDB is the primary storage acceptance target. Worker lease authority retains
one backend clock (UTC-offset high-water plus monotonic elapsed and an atomic
observation high-water), shared with read replicas through the existing authority
epoch. Running native lease index keys add 33 bytes; epoch metadata is bounded to
32 lowercase hex characters, empty for legacy claims. Claim recovery probes at
most four indexed lease ranges per kind, one pointer each, without a scanner,
extra timer or thread. SQL schema v64 stores the same private epoch with a
kind/epoch/deadline index. Restart and migration revoke previous live authority
while preserving durable jobs and advancing their existing fencing tokens.

Problem: caller clocks can revive expired leases, and independent PostgreSQL
Sidecar clocks can disagree after a UTC adjustment. Decision: native TofuDB owns
its clock; SQLite shares its backend clock; PostgreSQL worker operations require
an optional PostgreSQL 17+ server module under
`lib/storage_sidecar/native_worker_clock/`. One lazy named dynamic shared-memory
state owns the random server epoch and atomic clock floors. The adapter scopes
that epoch by database OID. Connection/pool turnover preserves authority; server
restart revokes it. Alternatives rejected: caller time, wall time alone,
Sidecar-local epochs/clocks, and a durable clock row do not provide the same
cross-replica progression and restart guarantees. Each native/SQL snapshot
separates current UTC eligibility from advancing lease time; PostgreSQL returns
both from the same server function without another query. Using the lease
high-water for absolute UTC availability would claim future slots and spin
suspensions after rollback. UTC-first sampling also prevents scheduling pauses
between probes from inflating the lease offset.

The deployment operator builds the module with PGXS against the actual server
headers (`make`, then the operator's normal extension packaging/install process)
and runs `CREATE EXTENSION tofu_worker_clock` in each database using SQL worker
operations. Application startup never installs it. Missing or incompatible output
fails closed before proving a lease; there is no local-clock or backend fallback.
Other storage operations do not load the module. Private tests compile/load it
only inside fixture-owned clusters; no production clock setter is exported.
The clock structure is 48 logical bytes on the verified host (contract maximum
128), plus PostgreSQL's DSM allocation/registry overhead; it creates no clock row,
scanner or worker thread and remains bounded by the server lifetime.

Published native query configurations and their read personas share the writer's
same clock and epoch, including advancing lease time during UTC rollback. Each
holds one additional Arc reference (eight pointer bytes on this 64-bit target),
with no separate clock, resident thread or persistent record.

Eight isolated native approval samples on source `a413e786…` measured 7.475 ms
median approval (7.422–7.789 ms range). One grant occupied 421–422 compact JSON
bytes; run growth was 411–412 bytes. wake_sequence adds eight logical SQL bytes
or 18 compact native JSON bytes at sequence one. Grants are limited to one per
step, at most 64 steps, within the existing 524,288-byte run bound. Python stayed
at one thread; native threads 11→12 and RSS 21,524→28,724 KiB across eight runs.
These local daemon/service samples ran under concurrent test load and exclude
physical disk allocation; they do not guarantee throughput or steady-state RSS.
Approval adds no queue, resident waiter, ledger or pool; schema 64→65 defaults
legacy wake observations to zero and migration preserves nonzero observations.

Eight isolated native time-publication samples on source `95807fab…` measured
4.511 ms median output/reference commit and 1.295 ms median one-result read.
Each identity-only reference occupied 367 compact JSON bytes; content stays in
the accepted run. One-result reads used three owned queries (attempt, index,
run); full pages hydrate at most eight references after owner/tenant/target
filtering. No backend pool, timer, waiter or queue is added. Python stayed at
one thread; native threads 11→12 and RSS 22,812→30,548 KiB across eight samples.
These local daemon measurements include concurrent host load, exclude physical
allocation, and do not establish throughput or steady-state RSS. Browser reads
are demand-scoped to mounted attempts, coalesced, and cancelled on navigation;
only the first eight references are shown inline, with no resident polling timer.

Ten isolated automation management samples on native source `5412f267…`
measured 4.793 ms median manual admission and 3.107 ms median eight-run history.
Each history page used ten owned queries (definition, index, eight run reads)
and returned 4,319 compact JSON bytes. Python remained at one thread; native
threads grew 11→12 and RSS 21,320→25,128 KiB while admitting ten durable runs.
These local service/daemon observations exclude physical disk allocation and
are not throughput or steady-state memory guarantees. Management adds no pool,
timer, polling queue or persistent record beyond existing publication/admission.

Ten isolated native task-time executions on source `4f22825a…` measured a
25.498 ms median for claim, intent, output and terminal settlement on the shared
FUSE host. Compact run JSON grew from 2,205–2,215 to 2,607–2,617 bytes; accepted
runs and outputs are durable user state, within the existing record bound.
Native RSS grew 21,336→27,744 KiB and runtime threads 11→12; Python stayed at one
thread with zero retained heartbeat threads after each completed claim. These
local observations include RPC/durable writes and are not throughput or speedup
claims. The execution handler creates no new pool, queue, model loop or idle
worker; canonical suspension releases the existing claim/monitor while waiting.

An owned private native authority ran 20 enqueue/claim/heartbeat/complete cycles
against source `75b9930e…`. Median whole-operation latencies were
1.216/1.198/0.934/1.174 ms on this shared FUSE host, including durable writes
and RPC; these are observations, not a speedup claim. RSS grew 22,024→25,068 KiB
and existing runtime thread counts 11→12 during activity. The clock creates zero
threads; native replicas share its existing authority lifetime. Each new running
job adds 33 lease-key bytes plus 59 encoded epoch bytes in each job/index value
(151 logical bytes before allocator/engine overhead); terminal settlement removes
the lease index while preserving the durable job.

Problem: native trace reads ignored payload selection and decoded every token.
Decision: native `event.list(payload_types=...)` retains all selected rows and
producer timestamps, but projects unrequested payloads as `{type}` from the
physical stream header. `null`/absent selection preserves full replay; an empty
list omits every payload. Latest metadata reads use the same header seam.
Large omitted payloads do not materialize blobs; full replay still verifies
payload digests. Owner scope, cursor, filters and existing 1,000-row / 8 MiB
page budgets are unchanged. No new resident cache or durable field is added.
Alternative rejected: discarding payloads after fetch/JSON decoding pays the
CPU, allocation and transport costs on every inspector refresh.
Measurement: the native `event_time_spine_read_budget` test (debug build, local
synthetic authority, five samples, 200 × 2,048-byte tokens) returned 425,891
bytes with full payloads versus 12,491 bytes for the spine; median read plus
response decoding was 19,765 µs versus 2,513 µs. This is a bounded local query
measurement, not whole-conversation capacity or release latency evidence.
Fault tests prove omitted blob content is not fetched and full replay still
rejects unavailable content; normal/reopen, owner and filtered reads retain
their contracts.

Operational contract for the storage authority described in [Storage](STORAGE.md).
Native backend selection and migration live in [Tofu-DB storage](STORAGE_TOFUDB.md).

Problem: a fixed `record.list` window hid later exploration programs. Decision:
optional exclusive `after_key`/`before_key` seek within the literal prefix; both
cursors together, wrong-prefix cursors and invalid types fail before reading.
Forward pages ascend, backward pages descend; existing row/8 MiB bounds remain.
No new store/index or resident cache is added. Evolution emits compact summaries
and retains one page plus one lookahead row; native resource refusal halves page
size at the same cursor, keeping transport backoff and complete originals.
The wire may omit the specific limit reason; this is bounded adaptation, not
proof that pressure came from bytes alone. Unreadable records fail explicitly. Live pages do not promise snapshot
enumeration under insertion/deletion. Alternative rejected: offsets or client
accumulation repeatedly scan/retain growing catalogs and lose stable key seeks.

Native engine updates are prepared before lifecycle shutdown. The preparation
process has one 1,200-second total deadline (1,230-second caller ceiling), a
600-second maximum lock wait, 8 MiB subprocess logs, and bounded HTTPS downloads.
Work uses the remaining total time after lock acquisition; compilation retains
30 seconds for identity, semantic probes and publication. A shorter caller
deadline always wins. Problem: independent 600-second work/compiler caps discarded
unused queue time and timed out healthy compilation. Decision: share the fixed
total clock, not extend it. Alternatives rejected: increasing the total timeout
blindly or renewing a full work budget after a contested lock would weaken bounds. Source compilation
uses an owned input snapshot and the smaller of launch-probed effective CPUs
and available GiB of memory as its worker count (minimum one; explicit Cargo
worker overrides remain). One reconstructible `target/update-build` cache is
reused; staging sources/downloads and logs are released on every outcome.
The existing deployment owner retains rollback binaries and updates content
pins. Candidate semantic probes use disposable authorities with live connection
locators cleared. In a local debug validation, hashing the source took 37 ms
and the three semantic probes completed in 3.5 s; these are preparation
measurements, not production latency claims. Compiler/process groups are
terminated and reaped on deadline or cancellation. A failed preparation does
not stop a live worker or change backend selection.
Source attestation uses SHA-256 over `tofu-db-source-v2\0` followed by sorted
logical UTF-8 path keys, NUL, an unsigned 64-bit little-endian byte length, and
exact file bytes. The root key prefix is always `tofu-db/`; dependency prefixes
are the normalized Cargo-relative snapshot locations. Inputs include each
transitive path crate's manifest, existing lockfile/build script, `src/**/*.rs`
and `.cargo` files, including optional, target, build and dev dependencies.
Existing explicit package include selection is retained (for publishable crates):
literal included files plus Cargo manifests/lockfiles replace the default source
selection; `.cargo` configuration remains included. This selection is not a
complete Cargo glob implementation.
Malformed manifests and logical path collisions fail preparation. Rust
`packages/tofu-db/build.rs` and Python `serverctl_pkg/native_engine.py` share
this contract; `tests/server/test_native_engine_identity.py` compiles the real
build script and checks relocation, configuration and transitive edits.

Problem: root-only compiled identity omitted runtime/vendor edits, while
physical-path closure hashing changed when `tofu-db` became `update-source`.
Decision: retain closure attestation with relocation-independent logical keys;
stage unchanged runtime manifests (including feature/default-feature choices)
and exclude the engine from the synthetic inheritance workspace. Alternatives
rejected: accepting a preparation receipt instead of compiled identity, ignoring
path dependencies, or hashing the synthetic workspace would weaken attestation
or attest staging machinery rather than retained source. Existing incompatible
cached artifacts must rebuild; source or allocator admission is never bypassed.
The drift script remains the independent semantic contract probe, not an
alternative source-identity authority.

An old process may carry the prior dotenv content pin through re-exec. It is
advanced only when the deployment receipt links that exact prior pin to the
current project pin and the installed bytes verify; unrelated overrides fail.

Background-command completion is a durable passive queue receipt until a turn
confirms model consumption or the user cancels delivery. A collective workflow
row is added only after all known commands finish and publish; startup and the
existing maintenance tick rebuild missed wakes without a new worker or timer.

The complete JSON-safe request freezes before the first enqueue: owner,
conversation, command identity, message, timestamp and configuration. The
bounded command registry retains it for maintenance retries; explicit POSIX
background commands fsync `delivery.json` in their existing record directory
before enqueue, then delete it only after acknowledgement. Restart recovery
loads the identical envelope. Foreground handoffs retain their process-local
recovery limit. Freezing only the command ID fails when a lost ACK is retried
with changed configuration or time; reusing just the result is insufficient.
Completion configuration retains execution preferences/policy and excludes the
previous attempt's `checkpoint*`, prefill, content prefix, exclusion and private
buffers. Problem: legacy frozen envelopes copied multi-MiB checkpoints into a
1 MiB native queue core. Decision: replay the original first; only a permanent
size/protocol refusal permits a deterministic config projection under the same
queue/command identity. The original `delivery.json`, result, owner and timestamp
are never overwritten; authority fingerprint conflicts fail closed. Transient
failures/lost ACKs continue replaying identical original bytes. Alternatives
rejected: minting a new identity duplicates completion; discarding the original
or truncating results destroys evidence. Maintenance retries claim one publisher
per result, back off from 2 seconds to 5 minutes and start no further publication
after a one-second tick budget; an already-started storage call retains its
ordinary deadline. Failed controls/records remain available for later recovery. Once the queue ACK
arrives, frozen retry bytes are released from completed-history memory; the
ordinary delivered/headless result retention policy remains.


`queue.list` retains legacy array modes. Background application reads use
`mode=background_page`: complete items, a requested row limit up to 32, an
encoded response byte budget up to 8 MiB, and an explicit `nextCursor`.
The stable `(priority, createdAt, queueId)` cursor survives queue-position
renumbering and deletion. A single item that cannot fit fails explicitly.
Callers follow continuation up to the model's 32-item batch bound; remaining
receipts stay durable for later consumption. The canonical shape lives in
`contracts/tofudb_ir_v1.json` under `queueBackgroundPages`.

Native capacity remains 512 queue rows per conversation. Row count alone is
not a byte bound: valid Unicode/escaped output and configuration can exceed
the response cap in aggregate. Tests traverse large native and Sidecar pages
without truncation or duplicate IDs, including deletion between pages.
Unacknowledged rows are durable user state, never expired to satisfy a budget.
One collective wake adds one short-lived workflow row per delivery batch.

Durable Flow execution receipts use the existing owner-scoped orchestration
run/event operations on both authorities. Each admitted effect adds a start and
full-result receipt; recovery copies committed receipts once before new work.
A 200-effect, 4 KiB-result sample added 896,938 JSON bytes (including seed
markers); only 68,938 bytes were retained on the live event projection. These
are durable user history, removed with explicit run deletion, not TTL cache.
Ordinary Flow agent/iteration limits and storage frame limits still apply;
oversize/unacknowledged receipt writes fail closed before successors dispatch.
Recovery adds no daemon, queue or timer. A complete copied seed survives parent
history deletion; an interrupted seed copy requires its parent's history and
refuses recovery if that history is absent. Source launch claims prevent
recreating a deleted successor. Detailed semantics live in
[execution modes](TASK_EXECUTION_MODES.md#durable-flow-restart-recovery).

Human approval/guidance waits reuse their existing two-second cross-replica
poll for cancellation and heartbeat; they add no resident thread, timer, or
queue. Cancellation and exceptions release the exact registry entry. A local
16-waiter measurement with 3,600-second approval timeouts released all threads
and registrations within one poll interval (approximately 2 seconds). Ephemeral/chat tool waits remain blocking. Durable Flow human nodes instead
release their whole execution generation and park in the existing run log.
Same-run continuation writes a header and seed hash/count, reusing prior receipt
bodies. For 200 committed 4 KiB outputs, a copied seed measured 916,291 JSON
bytes/202 events; a continuation seed measured 400 bytes/2 events (20 resumes:
8,000 seed bytes). Median in-process construction was 8.561 ms versus 6.256 ms
in five samples, excluding storage I/O. Graph progress events and new effects
still append ordinary durable history; no result bodies are copied per gate.

Paused continuation and committed expiry-intent rows survive startup retirement.
Both authorities use the existing native maintenance transaction bound of 1,000
active rows and fail before mutation above it. Approval expiry is enforced on
observation using two bounded tail reads, an event-sequence decision fence and
an idempotent explicit terminal transition. There is no per-wait timer/thread.
A crash after the expiry fence cannot admit an answer; the next read completes
the transition. Parallel-wait tests verify that all branch threads have joined
before the durable pause is exposed.

Goal context recovery uses the existing replace-in-place lane continuation cache
(8 MiB, 16,384 messages, 4,096 legacy Turn witnesses), with at most one row per
owner/conversation/lane and the canonical epoch proof where eligible. It adds no
per-iteration snapshot archive. Its write follows the visible phase commit;
cache failure preserves that authoritative transcript and causes cold recovery.
The native/SQLite application contract suite verifies actual authority restart,
concurrent answers, owner rejection, and recovered model/checklist state.

Native online checkpointing may keep two live WAL generations while the existing
rotation driver persists the sealed predecessor. Each log retains the current
64 MiB ceiling, so combined live WAL residency is bounded by 128 MiB; a second
handover is refused until prefix installation and prior retirement complete.
The previous one-generation write fence is replaced by successor writes during
encoding. Existing CONTROL publication conflicts and a full successor still
apply backpressure. No new worker or queue is allocated. Reopen verifies both
logs and completes the sealed prefix before admitting requests; it cannot delete
an uncheckpointed predecessor to reduce startup cost. Matched measurements and
fault evidence live in [rotation ownership](TOFUDB_ROTATION_WINDOW.md).

Embedded response encoding adds 512 bytes of fixed diagnostic stage atomics and no resident workers/queues or persistent artifacts; ownership and measurements: [FFI response boundary](TOFUDB_EMBEDDED_RESPONSE.md).
Embedded FIFO job delivery retains resource-derived workers/queue/byte caps, bounds idle registration by workers, and adds 256 fixed diagnostic-counter bytes; measured thread counts, allocator retention and lifecycle: [worker delivery](TOFUDB_WORKER_DELIVERY.md).
Encoded semantic validation removes temporary decoded objects, retains parser scratch/recursion bounds, and adds 256 fixed counter bytes plus two bounded sampled-profile stage slots; no worker, cache or persistent-file growth: [JSON ownership](TOFUDB_JSON_MATERIALIZATION.md#encoded-semantic-response-validation).
Canonical document projection borrows strictly recognized body bytes and builds one encoded envelope; four opt-in stages add 512 fixed counter bytes and 96 bytes per active/retained profile sample and operation aggregate (measured retained profile growth 3,264 bytes), without format/cache/worker growth: [projection ownership](TOFUDB_JSON_MATERIALIZATION.md#canonical-versioned-document-projections).

Borrowed-string canonical recognition adds two opt-in work dimensions (16 bytes per active/retained sample and operation aggregate), aggregates once per recognition attempt, and introduces no cache, workers or persistent state: [recognition proof](TOFUDB_JSON_MATERIALIZATION.md#borrowed-string-canonical-recognition).

Immutable document-response proof reuse adds two opt-in work dimensions (16 bytes per active/retained sample and operation aggregate) and 64 proof booleans per transaction's bounded slot array. Proofs have the producing response's lifetime and never persist; depth includes all envelopes and slot mutation invalidates before exposure: [response provenance](TOFUDB_JSON_MATERIALIZATION.md#validated-response-provenance).

Canonical blob certificates share one volatile cache across an authority's read replicas. Capacity is the launch probe's configured hot block-cache budget divided by 64, allocated lazily; direct/offline authorities default to disabled metadata. Conservative accounting uses 4 KiB for index roots plus 512 bytes per retained certificate. No body copies, workers or persistent state are retained. Every lookup follows a complete scoped blob read/hash check; legacy conversion is never cached. Three opt-in work dimensions add 24 bytes per active/retained sample and operation aggregate; anonymous live counters and capacity are exposed through `system.metrics.canonicalJsonProofCache`: [certificate ownership](TOFUDB_JSON_MATERIALIZATION.md#canonical-blob-certificates).
Verified decoded compressed chunks share a second lazy cache across authority replicas, sized to the existing hot block-cache budget divided by 16. Accounting includes encoded/decoded Vec capacities, 512 bytes per entry and 4 KiB index base; eviction releases pins after in-flight readers/observers finish. Current source reads and complete scoped BlobId verification remain mandatory; whole-blob dependency proofs may replace only the repeated full hash. Seven opt-in stages and five work dimensions add 208 bytes per sampled request/retained sample/operation aggregate and 896 fixed counter bytes. No workers, persisted state or format changes. Measured five-body cache accounting is 1,031,784 bytes; SDK RSS cohort means grow by 2,282 KiB with unchanged 130-thread counts. Active sampled request bytes are 6,176 and the same two-operation retained collector is 157,488. Anonymous `decodedBlobChunkCache` metrics expose occupancy and availability: [decode ownership](TOFUDB_JSON_MATERIALIZATION.md#verified-decoded-blob-chunks).

Immutable canonical dispatch results keep one inline optional certificate beside the existing bounded response; public extraction discards it before exposing mutable bytes. No new body copies, workers, caches or persistent data are retained. Five opt-in transport stages add 640 fixed counter bytes and 120 bytes per sampled request/retained sample/operation aggregate. Measured sampler active/retained bytes are 6,296/161,568 (same two-label collector); end-of-corpus SDK RSS means grow by 588 KiB with unchanged 130 threads. Transport spans are global and remain outside sampled dispatch; unproven/notice outputs retain original numeric/parser behavior: [transport evidence ownership](TOFUDB_EMBEDDED_RESPONSE.md#immutable-canonical-dispatch-results).

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

Scheduled backup success requires literal `ok: true`, nonempty string artifact
and manifest references, and an integer nonnegative byte count. Malformed or
unsupported responses record failure; they must not produce a success message
with missing artifacts. This receipt check does not itself verify disk bytes.
Native (TofuDB) deployments satisfy the same receipt contract through the
daemon's online backup driver: `system.backup` returns
`artifact_kind: "directory"`, the backup directory (default
`<data>/backups/tofudb`), the snapshot manifest path, copied/manifest byte
counts, sha256 and retention fields; the artifact is a directory of
immutable content-addressed blocks plus `snapshots/*.manifest`, retained at
two verified generations, restorable with `tofu-db restore`. Offline
integrity operators: run `tofu-db verify --data-dir <authority>` for a
read-only full-structure verdict (`tofu-db.verify.v1`: ok/degraded/fatal,
naming corrupt blocks, failed segments and torn WAL tail bytes), and
`tofu-db salvage --data-dir <authority> --backup-dir <empty-target>` to
extract everything verifiably reachable into backup layout with an
explicit loss list, then `tofu-db restore --backup-dir <target>
--data-dir <fresh>`; a fatally damaged CONTROL pair fails closed instead
of guessing.

SQL deployments use `scripts/storagectl.py` for preflight, status, baseline, backup, restore,
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
the bulk-compaction verdict, and missing deferred indexes. The deferred
catalogue includes the attempt-retention trio
(`idx_storage_generation_attempts_settled`,
`idx_storage_conversation_turns_current_attempt`,
`idx_storage_conversation_changes_attempt`) that covers the
`turn.events.prune` / `turn.cleanup` candidate queries; without them one
maintenance call full-scans three large tables inside the single SQLite
writer and starves user-priority writes past their acquisition cap. The
`tables` section
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
[`TRB-fastpath.md`](contracts/TRB-fastpath.md). The local-front mode preserves
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
by an adaptive WAL maintenance watermark: one quarter of the current
authority (at least 64 MiB), capped as a trigger by two percent of launch-time
free disk and 16 GiB. Shipper construction considers both local-front and
durable-shadow volumes; probe failure retains the launch hint. Rebase starts
at 15/16 of the watermark to create an early maintenance opportunity. This
sizes copy frequency and reports pressure; it never rejects new transactions
or guarantees a four-percent disk envelope. Every accepted transaction retains
SQLite FULL fsync and caller deadlines. The existing ship-to-end, checkpoint,
stable image, concurrent WAL prefix and manifest-generation owners catch up;
physical SQLite I/O failures keep their typed failure and rollback behavior.
Publication still verifies image-plus-tail capacity and preserves the previous
recovery generation on failure. WAL-stat failure reports untrusted size and zero watermark headroom without
blocking writes. Retired refusal counters remain zero for dashboards.

WAL demand ownership — Problem: maintenance estimates and stat failures fenced
writes. Decision: remove resource-admission hooks; observe watermarks.
Alternatives: larger watermarks still refuse valid writes; dropping WAL tails
or weakening fsync loses durable state; shadow publication failure cannot
silently select an older authority. Verify `test_storage_fastpath.py` and
`test_sqlite_writer_watchdog.py`: 32 real FULL-fsync commits create 2,554,432 WAL
bytes above the former 1 MiB refusal threshold in 9.47 ms, then all 32 payloads
recover after local loss. This measures local commits, not volume throughput. A physical SQLITE_FULL before
BEGIN still fails without executing the handler or retrying blindly. Closing
an unstarted shipper preserves startup failures; closed shippers cannot restart.

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
rebase/pressure state, local WAL bytes, observation trust and remaining
maintenance-watermark headroom; retired refusal counters remain zero. Required mode revokes
readiness on a degraded, blocked, or poisoned publisher. A healthy
deployment should alert on nonzero stall-interrupt growth, persistent user-lane
queueing, p95 commit latency approaching the command deadline, or advancing
ship lag/age. It should also alert before the logical outbox or sink reaches its
byte ceiling; a full sink intentionally backpressures rather than discards
durable history.

The native TofuDB backend exports its own observation surface through the
authenticated `system.health`/`system.metrics` control operations (contract
owner: `contracts/storage_v2.json` `controlOperations`; behavior:
`STORAGE_TOFUDB.md`): ready flag with per-component configured/healthy,
listener connection/frame counters, maintenance and background-packer
counters, fixed durability counters, nonzero commit-stage totals, and the
launch `resourceBudget`. They answer from observer handles without the
authority mutex, so they stay responsive under commit load. Client-side, both
backends feed the same `tofu_storage_rpc_seconds` Prometheus histogram. A
healthy native deployment should alert on `ready` flipping false, growth in
listener rejected/failed connections, failed maintenance or packer rounds,
and checkpoint counters stalling while commits advance.

The native client uses a dedicated, single-attempt health lane. Problem: a
frame-boundary peer close escaped classification and retained a dead session.
Decision: classify it as unavailable and retire the session; the next probe
opens a new one. Unhealthy transport probes retain sanitized `probe_error`
codes without raw error text. Alternatives rejected: reusing the closed lane
or hiding a failure through immediate probe retries. Readiness remains false.

## Shared-host verification

The host-overload gate that withholds a storage recycle
([Storage](STORAGE.md#managed-child-liveness)) reads the *process cgroup's*
memory ratio and the host load average. On a shared cgroup — siblings such as
the IDE server or a peer agent whose own database fills page cache and kernel
memory — the memory signal can sit permanently at or above the 92% watermark
even while the storage authority itself is small and healthy. Two consequences
for verification and evaluation:

- Do not read the `cgroup` CRITICAL warnings as proof of an authority fault.
  Confirm the actual kill path against the kernel counters: `memory.oom_control`
  `oom_kill 0` / `failcnt 0` and per-process `oom_score_adj 0` mean the kernel
  never OOM-killed anything. Compare the supervisor's own RSS with the top
  `cgroup_pressure.log` entries to see whose memory is actually resident.
- Run resilience/throughput comparisons in a dedicated cgroup (or with sibling
  agents and IDE servers quiesced). A benchmark whose baseline and candidate
  share one memory ceiling measures the neighbour's footprint and page-cache
  attribution, not the storage engine; a steady-state microbenchmark can look
  far better than an embedded engine while the shared host starves the sidecar
  under load.

Because a shared cgroup's overload never clears, the liveness suppression is
bounded (a wedged authority is recovered within a bounded delay) and the worker
boot grace is heartbeat-phase aware; the knobs are listed in `STORAGE.md` and
`docs/INSTALL.md`.

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


## Overlay correctness and background pressure

The [overlay contract](TOFUDB_WRITE_PATH_OVERLAY.md) owns durable prefix
coverage, immutable reader views and bounded merge cursors. Mounted tombstone
masks and root-directory preservation apply to both repacks and vacuum.

Problem: a sampling-window decision was discarded between samples, and a
maximum of unrelated cumulative counters hid activity. Decision: independent
request, foreground-commit and admission-wait deltas retain the sampled hot
state for the full 250 ms window. Historical counts initialize the baseline.
Background gates permit bounded progress at least every two seconds under
continuous pressure; existing debt thresholds and explicit drains still
force work. The pack controller uses foreground WAL samples. Alternative:
raising busy thresholds cannot fix lost state or guarantee maintenance progress.

Packed-block routing uses verified 16-bit hash prefixes and publishes with
its payload catalog under the same lock boundary. The route memory budget
is one eighth of the probe-derived open-segment cache budget; unknown or
budget-excluded segments remain explicit fallback candidates. Catalog changes
retire old routes, cache-budget changes rebuild within the new bound, and
lookup retains its fresh-catalog retry. Alternative: scanning every segment's
membership filter avoids I/O but still performs linear directory work.


The retained mixed-load measurement (`audit_mixed_load_measurement`, Linux
local temporary volume, debug build) ran 8 readers / 1,024 short reads with
128 writes and bounded merges in 52 ms: read p50/p99 33/372 µs, writer-plus-
merge p50/p99 317/626 µs. Peak accounted immutable overlay bytes were 163,280;
pending and resident delta bytes both returned to zero after readers exited
and the overlay drained. This is one correctness-checked host measurement,
not a production speedup claim. The 64-segment routing fixture reduces its
candidate list from more than 50 to at most 2; a zero budget safely restores
the conservative directory path.

Whole-blob positive certificates share the decoded-cache lifecycle and retain exact current immutable manifest/chunk dependencies only after a complete scoped hash. Their separate launch capacity is the hot block-cache budget /64; encoded capacities, vector/Arc metadata and bounded index records are charged. Oversize admission drops attempted pins; zero/resize/eviction releases metadata after bounded in-flight readers finish. Small bodies below 4 KiB retain the hash path. Three opt-in stages/four work dimensions add 384 fixed bytes and 104 bytes per sampled request/sample/operation aggregate. No workers, persisted state or format growth: [whole-blob ownership](TOFUDB_JSON_MATERIALIZATION.md#verified-whole-blob-dependencies).

Measured whole-proof capacity is 33,554,432 bytes; six corpus entries account for 27,152 bytes, alongside 1,294,583 decoded-cache bytes. Both SDK cohorts retain 130 threads; end-of-corpus RSS means are 74,486→74,394 KiB (−92 KiB, including Python/runtime/allocator). This does not prove free metadata. Sampled active/retained bytes are 6,400/165,104 with the same two labels. Native scalar eight-caller materialized followup remains 5.0% slower; concurrency/queue costs and all adverse cohorts are retained in the [whole-blob evidence](evidence/tofudb_whole_proof_20261003.json).

Published read views retain one current immutable descriptor with no permanent
MVCC pin, worker or persisted bytes. `publishedReadView` exposes availability,
metadata bytes, publication/reuse counts and zero owned pin handles without
taking authority. Four optional stages and three work dimensions add 512 fixed
counter bytes and 120 bytes per sampled request/sample/operation aggregate;
measurements and adverse cohorts live in the
[published-view evidence](evidence/tofudb_published_read_20261003.json).

Measured view-owned metadata is 1,472 bytes with zero permanent pin handles;
SDK end-of-corpus RSS means are 76,088→74,414 KiB, with 130 threads in both
cohorts. Whole-process RSS includes Python/runtime/allocator and does not prove
free metadata. Active sampled/retained collector bytes are 6,520/169,184 for
the same two labels. The bounded collector drops 20/7,763 native scalar samples
and 1/1,663 array samples; retained spans/errors are valid. Low-load overlay and
SDK adverse cohorts remain explicit in the published-view evidence above.

Embedded dispatch bindings are constructed once per runtime, then borrowed
while the worker owns that runtime. Launch budget JSON and observer/coordinator
handles are shared; no per-request default admission allocation or budget copy
remains. Framed builders keep owned bindings and isolated overrides. No added
worker, persisted format or retained root belongs to this change. Two opt-in
stages (binding preparation, pin release) and one work dimension add 256 fixed
counter bytes and 56 bytes per sampled request/sample/operation aggregate.
[Binding evidence](evidence/tofudb_borrowed_bindings_20261003.json) retains paired results and rejected attempts.

Measured binding reuse preserves all launch data/coordinator references without
per-request allocation; sampled preparation is 0.175 µs in mixed scalar reads.
Same-label active/retained profile bytes are 6,576/171,088. Both SDK cohorts keep
130 threads; end-of-corpus RSS means are 75,694→75,010 KiB (includes Python and
allocator; not a proof of free metadata). Long native single-caller reads are
17.365→14.453 µs overlay and 17.314→15.520 µs materialized. SDK main small reads
are 22→19 µs; main ASCII 256 KiB is 11.1% slower and numeric boundary 4.4% slower.
All adverse main/followup cohorts remain in the linked evidence. Sampled scalar
pin release is 9.419 µs per request across two releases; this is retained-sample
wall time, not CPU attribution. Alternative: cache copied launch JSON per
request or copy only selected fields; both retain needless setup/ownership
work. Immutable runtime borrowing removes the whole reconstruction boundary.

Eight balanced 5,000-read SDK followups for the adverse main cases give ASCII
256 KiB 197.50→194.25 µs and numeric boundary 23.00→19.75 µs. The original
11.1%/4.4% regressions remain alongside these followups; shared-host timing
variation and overlapping ASCII ranges prevent an all-scenario claim.

Snapshot pin accounting keeps exact aggregate byte admission in one database-owned
atomic counter; root registration, GC reachability and reader capacity remain
under the original registry mutex. Leases repay bytes without that mutex.
Reader waiters enroll and leave under the predicate mutex, including timeout
and poison paths; registered root release unlocks before notifying enrolled
waiters. No new Arc, worker, persisted bytes or payload retention is introduced.
Three optional stages (`pin_registry_wait`, `pin_byte_reserve`,
`pin_byte_release`) and four work dimensions add 384 fixed counter bytes and
104 bytes per sampled request/sample/operation aggregate. Diagnostic nested
spans overlap; registry wait includes acquisition only. Results, regressions
and observer costs belong to [pin evidence](evidence/tofudb_pin_accounting_20261003.json).

Problem: pin release consumed 9.419 µs per mixed scalar request in the prior
sampled cohort; byte reservations and two releases shared the GC registry lock,
and every registered release called notify_all. Decision: separate exact byte
accounting from topology, while keeping waiter enrollment atomic with the
capacity predicate. Relaxed byte-counter atomics linearize only resource
accounting; immutable data publication and GC retain their original locks.
Alternatives: skip byte charging (breaks hard budgets), hold permanent pins
(retains old roots), add a broad lock-free GC registry (unnecessary lifetime
complexity), or batch quota estimates (weakens exact admission). All are
rejected; final default-off measurements remain separate from diagnostic spans.

Layout isolation with 128-byte alignment was rejected after direct balanced
comparison: scalar eight-caller overlay/tree were 5.0%/1.4% slower, while array
reads improved 3.5%/2.3% with overlapping ranges. This does not confirm uniform
cache-line benefit. Aligned registry payload was 256 bytes; retained unpadded
accounting uses 64 bytes versus 56 originally on this target, excluding Arc
and allocator overhead. Both full suites, exact builds and every adverse
cohort are retained. Initial unpadded long scalar tree +10.5% and array overlay
+7.0% regressions remain; final repeat measurements never replace them.

Pin-accounting code matches the first certified candidate; the final combined
source also preserves parallel automation inputs. Its full native suite passes
1,568 tests with two existing ignores. Matching baseline/candidate builds
include those same parallel inputs. Final scalar overlay 8/32-caller means are
25.729→23.806 µs and 119.040→111.668 µs; scalar eight-caller tree long
reads are 24.020→25.139 µs (4.7% slower), while array eight-caller tree long
reads are 47.950→45.140 µs. All original and followup regressions remain.
Sampled pin release is 5.453→1.218 µs per request with two releases; queue
time is 43.501→50.365 µs. These overlapping diagnostic spans do not prove an
equal default-off gain. Sampled active/retained bytes are 6,680/174,624.
Registry acquisition, budget operations and notifications are observable;
exact quota/error/GC boundaries remain unchanged. Overall results are mixed,
so thread handoff/reply ownership and semantic preparation remain next root
investigations. The queue already reuses recently idle workers.
Cross-worktree Cargo cache reuse produced an incorrect candidate identity;
the guard rejected it before timing. Only the temporary package release cache
was cleaned, and rebuilt CLI/DSO identities were checked before measuring.

Embedded reply diagnostics retain fresh standard channels and original runtime
lifecycles. `embedded_reply_acquire` measures channel allocation;
`embedded_reply_recycle` measures receiver retirement, including explicit drop.
Both are optional global caller-edge spans outside worker dispatch; nested
waits overlap. Sampled work marks creation, with reuse zero in the accepted
implementation. Two stages/two work fields add 256 fixed bytes and 64 bytes per
sampled request/sample/operation aggregate. No cache, worker, handle field or
queued-job growth is added. Three rejected reuse/mailbox implementations,
exact correctness/build receipts, long/short/tail regressions and observer
costs remain in [reply evidence](evidence/tofudb_reply_reuse_20261003.json).


Embedded delivery preserves its original FIFO/byte limits, worker maximum and
notification under the handle lock. Four opt-in edge probes cover
`embedded_handle_lock`, `embedded_queue_lock`, `embedded_queue_admit` and
`embedded_queue_notify`: +512 fixed counter bytes, +96 per sampled record.
No handle word, worker, reply cache or persistent growth is added. Six rejected
wake strategies, exact default-off/observer measurements and source receipts
live in `TOFUDB_WORKER_DELIVERY.md` and
`evidence/tofudb_admission_lock_20261003.json`.

Final IR extraction transfers the selected result allocation and its immutable
JSON proof after the last step. New optional `ir_result_extract` and
`ir_receipt_response_policy` spans plus moved/copied-byte work fields add
256 fixed counter bytes and 64 bytes per sampled aggregate; measured active
request storage is 6,904 bytes. No worker/cache/persistent growth is added.
Ownership, adverse measurements and SQLite gaps remain in
[JSON materialization](TOFUDB_JSON_MATERIALIZATION.md#final-ir-result-ownership).

Generic semantic payload compilation consumes the parsed root object instead
of deep-cloning it. Optional `semantic_payload_decode` and decoded-byte work
accounting add 128 fixed counter bytes and 32 bytes per sampled record; measured
active storage is 6,936 bytes, and the same two-label collector is 183,328 bytes.
No worker/cache/queue/persistent growth is added. Allocation probe, strict write
controls, SDK regressions and unshipped allocator experiment live in
[JSON materialization](TOFUDB_JSON_MATERIALIZATION.md#owned-semantic-payload-decoding).

C ABI request envelopes collect four recognized borrowed raw fields instead of
allocating a full owned-key map. Every unknown value still passes the original
RawValue parser; field types are checked after last-key-wins selection. The
existing envelope-decode anchor and all counter/profile resource layouts stay
unchanged. No worker, cache, queue, allocator or persistent growth is added:
[request envelope ownership](TOFUDB_EMBEDDED_RESPONSE.md#borrowed-request-envelope-fields).

Entity point preparation, source resolution and witness/value retention have
opt-in diagnostic spans plus successful retained-copy byte counts. Their fixed
counter growth is 384 bytes; sampled record growth is 80 bytes. Existing OCC,
reuse, quota, source and worker boundaries remain. The noncommittable query
prototype was rejected after mixed/adverse timing; measurements and limits:
[point attribution](TOFUDB_JSON_MATERIALIZATION.md#entity-point-read-attribution-and-rejected-query-witnesses).
Profiling remains disabled by default. Paired off/16/16/off SDK controls found
about 20%/29% total profiling CPU overhead on small/numeric scalar cases;
this includes existing probes. Use diagnostic spans for attribution and
separate disabled-profiling runs for performance claims.

Decoded-page timer specialization remains rejected: paired native gains did
not generalize to SDK float-text CPU (regression replicated near 8%). Both
cache-timer candidates remain absent. Bounded
process-wide CPU/context-switch diagnostics identified embedded handoff as the
next investigation target; optimization must preserve the worker watchdog and
late-result ownership:
[rejected probes and handoff evidence](TOFUDB_WAL_BARRIER_SCHEDULING.md#rejected-decoded-page-probe-specialization).

Embedded worker/handoff diagnostics add six opt-in global anchors (768 fixed
counter bytes; 144 bytes per sampled record). Reply stamps allocate only in
profiling mode and retire with the owned reply; default-off packet/allocation
policy stays unchanged. Single-slot replies were rejected despite fewer
allocation bytes because native and independent SDK CPU regressed:
[handoff bounds and evidence](TOFUDB_WORKER_DELIVERY.md#current-handoff-diagnostics-and-rejected-single-slot-channel).

The serving native CLI/library pair currently contains the integrated handoff
diagnostics, with compiled source identity
`9847e57a73ca8cda3b212172a77a27ee3143e82f76f4c2b459c1884837ab7044`.
Readiness reports native TofuDB and the running executable digest matches the
selected release. The six anchors remain opt-in; rejected single-slot replies
and page-cache timer experiments remain absent. The exact selected release
passes three disposable contract probes. The source-matched unit/process gate passes
1,582 tests with two ignored, including process tests against the exact selected
release. Later concurrent shared-source changes are outside that gate; historical
tests and timing controls retain their separate source identities. Measurement and gate coverage, installed
hashes, source ownership and live activation are retained in
[publication evidence](evidence/tofudb_handoff_20261004.json).

### Owned automation project bindings

`automation.project.update` retains one bounded durable root binding in the
existing owner/tenant target namespace; root identity is immutable, active state
and label use CAS, and one exact command replaces prior retry metadata. There is
no root index, queue, pool, timer or permanent command receipt. Retirement keeps
user metadata/history; generic target mutation is forbidden. First scoped rule
publication/admission proves an active binding in its owning transaction.
Eight isolated daemon samples measured 554 compact JSON bytes per binding,
1.246 ms median registration and 0.319 ms registered-root resolution. One root
lookup and an eight-row owned page each used one query. Physical task dispatch
also reads its accepted owned execution config once. Python threads stayed 1→1;
native threads were 11→12 and RSS 22,482,944→23,793,664 bytes. Shared engine
background work is included; local JSON/RSS samples under concurrent test load
are not physical allocation, steady-state attribution or throughput guarantees.

### Sub-agent report injection retention

Swarm completion event previews and the Turn's `_inboxInjects` reload sidecar
retain the full model-facing XML report; the previous 1,200-character slice
lost closing tags and prevented display. These are Turn-owned projections,
with no separate cache or cleanup policy. The 8,123-character multilingual
regression report measures 14,125 UTF-8 JSON bytes per preview, versus 1,476
bytes with the old slice (12,649 bytes additional per copy). Model injection
bytes are unchanged. Browser scanning and Markdown remain bounded by
`docs/RENDER_CONTRACT.md`; durable user reports are not deleted to meet display
limits. Test: `tests/tasks_pkg/test_swarm_inbox_reports.py`.

### Original result retention for text report views

A producer JSON report reformatted for the model is captured canonically before
formatting. With an explicit owner, that original source is published once via
the existing artifact repository and referenced by `toolResultEvidence`; a
complete text view stays complete. Repository content addressing, launch-probed
quota, existing expiry/reclamation and unavailable-retention behavior remain in
force. No second debug store is created. Inline outcomes whose format does not
change incur no new write. Ownerless/headless calls retain the runtime canonical
value and do not claim an artifact. The inspector adds no resident cache or
background work. A 300-line multilingual report measures 7,659 source bytes and
7,329 model-view bytes: approximately 7.5 KiB of source plus the existing artifact
record overhead is retained per distinct formatted report. On this host, 100
formatting passes average 0.067 ms with 43,683 peak traced working bytes. JSON
inspection visits at most 2,048 nodes through depth 16 and skips JSON parsing
beyond 262,144 characters; output expansion is separately bounded. Verify with
`tests/tools/test_result_surfaces.py` and the canonical artifact/reuse neighbors.

## Native fastpath operations

The optional whole-authority local front and verified asynchronous shadow are
defined in [native fastpath](TOFUDB_FASTPATH.md).
Activation is offline and requires an explicit existing local base, measured 3x
fsync win and observed copy capacity on both volumes. The initial copy is corpus
proportional; choose the bounded seed startup budget accordingly. Retain all
artifacts after an interrupted first shadow build for explicit verification.
Once the relocation fence exists, missing fronts restore only from same-lineage
complete shadow manifests, and interrupted restores resume natively.

No replication worker/queue is added: the existing online-backup worker serves
the recovery shadow with two verified generations, skips unchanged native
sequences, and paces attempts by requested cadence plus measured epoch cost.
Changed snapshots still traverse reachable data and pruning needs its native
spill/temporary capacity. Monitor completed/failed epochs, last-success age,
replicated durable sequence, copied bytes and local free space. These are
reconstructible recovery-transfer artifacts; retention never deletes the local
authority or retained source seed. Measurements must use disposable authorities
and report foreground latency separately from copy/restore work.

## Health session establishment failures

Problem: the dedicated health lane created/authenticated its session outside
its transport error boundary. Connection refusal and malformed handshake
exceptions escaped health(), instead of producing classified false readiness.
Generic malformed-response and response-admission errors also escaped.

Decision: session establishment and the RPC share the existing classification
boundary. Availability, protocol and frame-budget failures remain false health
results with sanitized probe_error; malformed live sessions retire before the
next probe. Wire constructors keep ownership of partial-socket cleanup. The
current probe makes no reconnect/replay attempt and its deadline is unchanged.
Healthy sessions and application error responses retain their existing rules.

Alternatives considered: retrying inside a probe multiplies the supervisor
window; marking a failed handshake ready hides lack of authenticated access;
catching arbitrary exceptions masks programming errors. Catch only the
existing transport/codec classifications. A real bound, non-listening loopback
socket reproduces refusal; handshake/frame tests assert classification,
privacy, retirement and successful reconnection on the next probe. This fix
does not establish the cause of the historical startup database_timeout or
any productive latency improvement.

## Archive worker pressure attribution

Problem: the archive-seal thread inherited the default foreground component,
so its Blob staging and commits appeared as user work and entered the
foreground-pressure sampler's commit rate. Decision: register that thread as
maintenance at its entry point, like the other background maintenance worker.
The thread-local marker also classifies work performed by its preparation
replicas. Alternatives considered: correcting only the displayed metrics would
leave pressure sampling contaminated; changing archive pacing at the same time
would prevent attributing the effect of this ownership fix. The worker retains
its existing transaction witnesses, commit boundary and shutdown lifecycle.

Translation activity and latency — Problem: boot translation backfills changed old conversation recency and a two-worker ceiling serialized 133 pending translations on a large host. Decision: `turn.projection.update` accepts the compatible boolean `preserve_activity` (default false). Translation success, failure, segment, and no-op commits opt in; the storage transaction retains its current conversation activity timestamp while advancing projection/conversation revisions and publishing sync changes. Ordinary user edits still advance activity. Personal translation workers use the already-probed root capacity, bounded by CPU and RSS headroom, with explicit overrides preserved. Fresh terminal translations enter ahead of older recovery work for the same owner; cross-owner fairness and the shared provider gate remain unchanged. Automatic whole-turn work has a 90-second end-to-end dispatch/retry allowance; failure remains durable and eligible for recovery. Explicit document translation retains its existing allowance. Alternatives rejected: restoring a timestamp from the client can rewind concurrent user activity; reducing revision updates hides translation changes; unbounded threads or provider retries multiply overload.


## Event batcher payload ownership

Problem: queued event counts and aggregate bytes refused independent validated
append/flush calls with database_busy. Decision: the batcher accepts immutable
canonical payloads without those quotas; the shared FrameByteAdmission ledger
chooses RAM or caller-owned anonymous files immediately. The launch byte hint
sizes RAM, independently of the 500-event / 60-MiB RPC composition boundary.
FIFO acceptance, natural-key deduplication, attempt fences, per-event outcomes
and durability-prefix failures retain their authority. Timeout cancellation
releases only unstarted payloads; close drains or fails queued work while an
ambiguous active RPC retains its files until settlement. Close also waits for
unpublished file preparation before reporting quiescence. Real file failures
are undispatched availability/protocol errors, never fabricated saturation.
Alternatives: larger queues retain refusal; unbounded Python graphs inflate
RAM; parallel FIFO barriers can falsely acknowledge an unfinished prefix.
Measurement: 32 accepted events / 2 MiB with count hint 1 and RAM threshold
1 byte staged in 25.79 ms before the blocked writer released; every ordered
frozen payload persisted and every temporary descriptor retired. Transient
file growth equals outstanding spilled canonical bodies, reclaimed after
settlement/cancellation; this adds no durable store or persistent cache.


## SQLite demand and connection ownership

Problem: writer job slots, fixed lane waits and read-pool slots refused or
serialized otherwise eligible requests. Decision: accept ordered writer jobs
without count admission and use only caller deadlines; independent query-only
readers connect immediately, retaining only the configured idle reuse count.
Read leases include connection preparation. Close fences new leases and waits
for all borrowers/preparers before cache/anchor teardown; healthy returns reuse
an idle handle or retire excess, and rollback/progress cleanup failures retire
poisoned handles. Backup source handles use the same joined lease lifecycle,
even when destination cleanup fails. Physical connection failures remain typed;
backend selection, FULL-sync, savepoints, receipts and the writer watchdog stay.
Alternatives: larger pools retain waiting; parallel physical writers invite
SQLite lock contention; closing borrowed handles lets late readers use invalid
connections. Measurement: 32 real SQLite readers rendezvoused with idle hint 1
in 121.52 ms and every opened connection closed after drain; 32 queued writes
above hint 1 retained exact order, and a user write waited beyond the retired
2-second cap within its explicit deadline. Read cache residency follows active
leases plus idle reuse; queue callback residency follows unfinished ordered
writes, with no new durable store, resident worker or second host probe.


## Local SQL search demand

Problem: a two-connection queue delayed independent searches and a corpus-byte
quota refused both new index materialization and existing queries. Decision:
authority and local projection reads use one DemandSQLiteReadPool owner with
immediate connection creation, launch-derived idle reuse and joined close.
Rollback/progress failures retire poisoned handles; preserved protocol/cancel
errors never become successful searches. Retire the local projection byte
admission field from the canonical launch policy and generated defaults.
Indexed rows/chunks follow source deletion, generation replacement, reset and
corruption rebuild; SQLite WAL auto-checkpoint and the one projection writer
remain, so growth serves the retained authorized corpus rather than an
unbounded append cache. True disk/connection errors remain diagnosed and cannot
change durable source authority. Alternatives: larger pools preserve waiting;
closing borrowers invalidates snapshots; evicting indexed source rows silently
weakens search completeness. Measurement: 32 real projection readers rendezvoused
with idle hint 1 in 26.60 ms; every borrower, source connection and idle handle
retired. Tests also verify a one-byte legacy quota cannot suppress a valid indexed
Turn, exact owner filtering, failed rollback quarantine and connecting-reader close.


## PostgreSQL demand connection ownership

Problem: fixed Psycopg pools and an 80%-of-server startup allocation made local
permits delay unrelated read/write transactions. Decision: direct Psycopg
factories create connections concurrently under one shared DemandConnectionPool
lease owner, also used by SQLite through its reset adapter. Read/write owners
remain distinct; configured sizes govern idle reuse. Remaining caller time
supplies libpq connect_timeout at its integer-second precision; expired prepared
connections retire before any semantic transaction starts. Codec/session setup, SQLSTATE errors, transaction
isolation, commit receipts and rollback/replay rules retain their authority.
Close fences both owners and joins all borrowers/preparers before cache disposal.
Reset/return failures retire the handle without turning an already-committed
transaction into a new failed write. Alternatives: larger pool limits retain
waiting; reconnecting every healthy borrower loses reuse; retrying unknown
COMMIT outcomes duplicates receipt-free mutations. Measurement: 32 controlled
Psycopg factories and borrowers rendezvoused with idle hint 1 in 7.06 ms;
all callbacks completed and every handle retired on close. This proves local
concurrency/lifecycle, not external-server throughput. Idle handle retention is
bounded by the launch hints; active handle lifetime follows its owned transaction.
No background pool workers, persistent data or second resource probe are added.


## Anonymous native resource-exhaustion reasons

Problem: `io::OutOfMemory` becomes the same `database_resource_exhausted` wire
error for domain quotas, reservation failure and OS errors. It is not evidence
of host OOM. Decision: authenticated `system.metrics.resourceExhaustionReasons`
exports eight process-local fixed counters under the storage.v2 contract; the
existing opt-in exit stats print the same fixed snapshot. No request log I/O,
worker, cache or durable state is added. Unknown error text is never exported.
Exact library constants classify archive owner/conversation/restore limits,
known domain bounds and request accounting overflow. Typed `TryReserveError`
means reservation failure, which can include capacity overflow; OS ENOMEM is
separate and still does not prove kernel OOM or an OOM kill. All other causes
remain `other`, including unlisted quotas. Direct semantic resource errors that
do not pass through the I/O mapper are outside these counters. Wire errors,
retry flags, ownership, source checks, rollback and ACK stay unchanged.

The eight u64 atomics cost 64 fixed resident bytes. Classification formats at
most 256 bytes on the stack, with no added heap copy of an unknown message;
metric/exit output contains only generated labels and counts. Independent
atomic snapshot loads are not one transaction. Counters saturate and reset on
process restart; SIGKILL before a metrics read loses the volatile diagnosis.
Alternatives rejected: printing raw error strings can expose user content;
per-request stderr may block the writer; fake timing stages confuse durations
with counts; labelling every quota as allocator failure invents a root cause.

### Compaction archive body admission

Compaction archive pre/post message bodies have a dedicated 64 MiB aggregate transaction budget in `contracts/tofudb_ir_v1.json`; metadata, receipts and ordinary IR literals retain their existing bounds. Native writes use owner-bound blob documents and publish content hashes/byte counts through the compact logical outbox. Identical command IDs still validate the complete original request digest. SQL keeps the same semantic repository entry points. Suspended working snapshots reuse the existing compaction archive ring and pruning policy, with no added worker or independent retention store. The regression writes a 9 MiB UTF-8 pre-view, restarts the real authority, reads it losslessly and restores a bounded post-view; this measures storage/recovery, not provider latency.