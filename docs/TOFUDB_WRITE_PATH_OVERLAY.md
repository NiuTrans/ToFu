# Tofu-DB deferred write overlay

Status: default write path. Owner: `packages/tofu-db/src/entity.rs` and
`entity_overlay.rs`. Durable authority remains the engine WAL and its
content-addressed root; the in-memory overlay is reconstructible.

## Problem

A logical commit, a physical tree publication, and a reader's captured view
are different facts. Treating a root change as a complete materialization
loses pending deltas on restart. Pruning shared mutable chains by a reader's
logical sequence changes snapshots whose roots precede a merge. Rounding a
merge target to a whole transaction also fails to bound physical work.

## Decision

The first deferred mutation publishes a root-directory materialization cursor
atomically with its delta. Retiring the WAL never implies that the tree contains
all deltas: an explicit cursor recovers the uncovered suffix from retained
history and the active log, rejecting a missing transaction. Automatic history
pruning retains every segment intersecting that suffix; partial coverage retains
the boundary transaction. Explicit operator pruning first merges all deltas.
An empty overlay starts a new cursor at the current durable head, so old pruned
history is not accidentally required again. Legacy complete roots keep lazy
history reads. Regression tests reproduce an acknowledged value disappearing
when a no-cursor root's WAL is entirely retired, then cover pending/partial/full
merge crashes and pruning. See the [experiment record](TOFUDB_PERFORMANCE_EXPERIMENTS.md).


Each committed delta is indexed twice over shared immutable records: by
`(key, commit_sequence)` for reads and `(commit_sequence, key)` for bounded
materialization. Both indexes are copy-on-write AVL trees. Point lookup seeks
in logarithmic work; transaction and replica capture clone roots in constant
work. Writes and pruning copy only paths still leased by readers. Deterministic
balancing bounds sorted and adversarial keys without a random-priority assumption.
A range seeks once, borrows payloads and resolves versions in either direction.
Persistent snapshot extraction materializes its captured logical view; OCC still
fences publication. The WAL encoding and durable root format are unchanged.

Independent transactions captured before another commit can append their delta
without draining the overlay. Commit validation compares logical point and blind
write versions across tree plus overlay, and rejects range phantoms using newer
pending keys plus immutable tree-leaf witnesses. Deletes retain overlay versions.
Physical merges can conservatively conflict a range, but cannot make a stale
predicate succeed. Structural/oversized operations retain tree validation.
Version validation groups sorted keys into one traversal of each captured tree.
Shared internal/leaf paths load once per group, and version-only probes borrow
payloads instead of cloning values. Base-tree tombstones mask mounted values;
mount fallback applies only to genuinely absent base keys. Point, blind-write and
range conflict checks remain unchanged. Repeated single-key tree reads were
rejected because they multiply cache synchronization and payload copies under
the commit sequencer. The mounted/tombstone oracle also requires a reduction
in page probes without timing-sensitive assertions.

This avoids turning parallel preparation into repeated full-overlay drains;
weakening conflict checks or accepting lost updates is not an alternative.
Fault-injection coverage lives in `entity_optimistic_tests.rs` and private blob
publication/pin lifetime coverage in `authority_preparation.rs`.

Background and idle-maintenance merges capture one bounded immutable overlay
prefix and a writer-class root pin under authority, then read old pages and
encode new pages outside that mutex. New pages stay private and reserve bytes
against the existing aggregate transaction budget. Publication first validates
the database instance and exact physical root; a changed root rejects the
candidate before any shared page staging. Ordinary overlay appends may continue
because they remain a recoverable suffix; physical or structural root changes
require fresh preparation. A prefix's original versions and dominance witnesses
are never replaced by values from its newer suffix.

Preparation also binds each selected page's content identity to its existing
immutable `Arc<Vec<u8>>` through `PreparedBlockSource::capture_shared`. Payload
reference metadata is charged to the same reservation before allocation, and
the private page reservation remains alive through publication or discard.
After the instance/root checks, fresh prepared staging keeps that allocation
and avoids hashing or copying page bytes while holding authority. Fresh staging
still bypasses durable deduplication; pending rotation, relink accounting and
WAL confirmation follow the original fresh-block path.

Problem: rehashing and copying already encoded private pages extends serialized
publication. Decision: mint the immutable payload proof during preparation and
reuse the allocation at publication. Alternatives: ordinary prepared-blob
staging changes fresh-page deduplication semantics; removing identity checks
would weaken the private-page boundary. Neither is used.

The source-pinned measurement
separates private observer-on attribution from default-off active-stream runs.
Page publication fell from 2.599 to 0.417 ms per call in the diagnostic pair;
final combined 1,000-active stream P99 remained 1.036/1.242 s, above its 1 s
budget. Client peak RSS was adverse in several legs; no memory or all-scenario
win is established. Remaining commit/queue tails require further optimization.

Exhausting that private reservation budget returns `WouldBlock` and defers the
slice; it never terminates maintenance or publishes partially prepared pages.
Reservations release on both batch and later page-encoding failures. This
classification applies only to the private budget, not physical I/O failures.

Preparation encodes both a cursor-bearing and complete-root outcome. Under the
publication hold, the current overlay determines whether a suffix remains; this
prevents clearing recovery coverage based on an obsolete captured empty tail.
Engine staging, relink fencing, WAL confirmation and root publication retain
their existing order. Private memory and the root registration release on every
success, error or stale candidate. Page/reference and carried-byte ceilings
remain; the indivisible spill path and foreground hard backstop stay available.
The 25-ms adaptive target measures serialized publication rather than lock-free
encoding or authority admission delay. Idle rounds prepare one slice instead of
draining an unbounded overlay under authority. Operator full-drain contracts
remain at their existing exclusive boundary.

Replay coverage is `(commit_sequence, inclusive_key_prefix)`, with no key
meaning the whole commit. A chunk consumes at most 2,048 oldest delta positions
and targets an adaptive number of physical keys. For each consumed position,
the key index proves whether a newer committed position dominates it. Obsolete
positions advance coverage without rewriting tree pages. A latest version is
materialized with its original version before coverage consumes it. Selection
never copies a newer suffix value forward or skips over a prefix position.

The root publication follows every dominance proof's commit in WAL order. If
that root is durable, each dominating value is either materialized in this same
root or remains in the replayable suffix. Root plus suffix therefore reconstructs
the exact latest state, including tombstones. Old readers retain their original
root/index pair. No acknowledged value depends on an uncommitted replacement.
A chunk containing only obsolete positions still publishes coverage atomically;
it does not confuse zero physical keys with no progress. The existing version-2
root/WAL format remains sufficient and older readers replay the same suffix.

Both the historical-position bound and the 2-MiB carried-byte bound remain.
Indexed dominance checks cost logarithmic work per visited position; they do
not scan the remaining tail. Adaptive retries halve physical key work when page
fanout exceeds the block-reference budget. The original 25-ms adaptation remains.
Sizing only by historical positions was rejected because repeated keys caused
underfilled tree rewrites. Applying the latest suffix values early was rejected:
that would require additional per-key materialization/version invariants. This
change omits obsolete writes and keeps exact prefix retirement instead.

A partial merge publishes a version-2 root directory containing its base,
mounts, and exact coverage, together with referenced blocks in one WAL
transaction. Recovery replays the uncovered suffix even across other root
publications or an operator checkpoint. A complete drain can restore a
direct root. Version-2 root records explicitly fence all earlier deltas when
no partial directory remains; root equality alone is not such a fence.
Version-1 roots remain readable through the legacy recovery path. These
changes prevent new losses; they do not infer or repair data already omitted
by an older engine. An older binary cannot read an outstanding version-2
partial directory: drain using the new engine before considering downgrade.

Recovery with an explicit coverage cursor visits a contiguous transaction suffix
in commit order. It authenticates and decodes each required immutable history
segment once, then borrows active-WAL transactions. The scan holds one bounded
segment at a time and does not fill the foreground block cache. Partial-key
coverage still retains the boundary transaction; missing/pruned required history
or corrupt source/hash witnesses fail closed. Recovery publishes no partial
overlay if the scan fails. Legacy roots without a cursor retain their existing
backward fence search. Per-transaction exact historical lookups were rejected
for suffix replay because they re-decoded whole segments for every transaction.

Merged positions leave both writer indexes immediately. Their records and
leased index nodes live until the last reader releases them.
`AuthorityDatabase::overlay_memory_bytes` separates pending bytes from live
resident bytes. Both include payload capacity, record/index structures and Arc
headers; allocator rounding is excluded. Two nodes per pending record enter the
existing resource-derived overlay budget, and copied paths remain charged until
drop. Reader pin bounds and the overlay budget bound retained views; durable data
is never evicted for memory. Earlier measurements counted key/value bytes only;
compare process RSS as well as this expanded accounting.

Small scans merge ordered overlay, staged-write, and tree cursors. They
clone returned values, not the matching overlay range. The tree window grows
only when masks consume it, and its final exact scan remains the OCC range
witness. Exhausting a scan budget fails explicitly; it never returns a
silently incomplete page. The overlay publication lock is released before
point or range traversal.

## Scheduling and safety

Rotation's final install fences new writes until its finite payload backlog is
published. At that boundary the packer switches from background optimization to
foreground dependency: adaptive pacing drops outstanding sleep debt and wakes
through the block store's revision/condition variable. Early WAL draining keeps
normal pacing, and an explicit operator rate remains binding. Stop wakes either
mode. Publication, fsync, WAL retirement, and batch bounds are unchanged.
Continuing a background sleep after fencing writers was rejected because it
adds deliberate idle time to every blocked request; disabling all pacing was
rejected because running foreground I/O still needs protection. Packer metrics
retain pacing wait count/time and foreground drain unpaces, verified by
`background_pack::tests::rotation_drain_interrupts_only_adaptive_pacing_and_stop_interrupts_both`.

The maintenance soft threshold remains 4,096 pending entries or half the
launch-probed byte budget. The foreground hard backstop performs one chunk.
Targets start at 64 distinct keys and adapt between 1 and 2,048 based on
the measured chunk latency; historical scanning separately stops at 2,048 positions. The active WAL ceiling and
transaction reference limits still apply. Checkpoint/backup paths drain the
overlay before discarding its recovery window.

Tombstone reclamation preserves masks over mounted ranges regardless of
age. Vacuum replaces only the directory's base and retains mounts even if
the base becomes empty. See [tombstone compaction](TOFUDB_TOMBSTONE_COMPACTION.md).

Query saturation waits outside the authority mutex, wakes on pin release,
and observes cancellation and its original deadline. Writer pin reservations
remain available. See [the read lane](STORAGE_TOFUDB.md#snapshot-read-lane-2026-09-18).

## Parallel decoded-page reads

Problem: pinned transactions shared immutable decoded pages, but each cache hit
took an exclusive mutex to append an LRU record. Parallel preparation therefore
serialized again at every tree level, including periodic recency-list scans.

Decision: readers share the cache lock and set an atomic CLOCK reference bit.
Insertion/resize alone take exclusive ownership; eviction examines at most two
positions per resident page, retains one queue position per page, and enforces
the existing byte budget before inserting a verified miss. Returned immutable
Arcs survive eviction. Capacity probes, explicit overrides, verified decoding,
and durable state are unchanged. Tests cover simultaneous hit progress while a
reader remains active, hot-page second chances, disabled/oversized caches, and
eviction with retained page references.

Alternatives considered: strict LRU requires writing global recency state on
every hit; sharding that LRU still serializes all readers of a hot root page.
An unbounded decoded-page map would hide contention by violating memory bounds.

### Decoded-page admission and benchmark budgets

Problem: freshly encoded immutable nodes can be read and decoded again later,
but eagerly copying every written node into the shared cache adds hashing,
allocation and exclusive-lock work, including for pages never read again.
Decision: retain verified read-miss admission. Eager fresh-page admission is
not enabled without reproducible application gains and bounded-memory evidence.
The rejected alternative checked the returned content ID, skipped contended or
oversized entries and used the existing byte-accounted CLOCK cache; these
correctness properties alone did not establish a performance benefit.

`tofudb_fresh_page_admission_20261015.json` retains
the prototype, exact production builds, all arms and failed fixture attempts.
Under strict durability, the 1,000-conversation A/B/B/A phase cohort averaged
12.0% lower scheduled-stream P99 and 7.2% lower settlement P99. The delta cohort
was 0.4% lower, 5.0% higher and 13.6% higher for scheduled-stream P99, settlement
P99 and productive wall time respectively. All eight arms preserved correctness
and failed latency gates. These are means of per-run statistics, not pooled
percentiles or a universal SQLite comparison. Neighboring default and 16 MiB
page-budget workloads also retain regressions. Sampled profiles suggest fewer
read/decode calls, but dropped profiles and different completed populations
prevent exact causal attribution; profile-on latency is never admission evidence.

The comparison harness reports `resources.tofudb.native_resource_budget` after
all workloads and before shutdown. `requested_cache_environment` contains only
the two explicit cache environment values captured at launch; `effective` comes from the latest
native runtime's `system.metrics.resourceBudget`. Missing or invalid observations
set `available=false` with an error, preserving measured workloads and physical
write counters; they cannot certify a cache-budget comparison. The observation
runs outside latency and write-volume windows and does not describe prior
runtime generations. In particular, `TOFU_DB_PAGE_CACHE_MIB=0` is ignored by
the positive-only override parser and falls back to the launch probe. It is not
a cache-disable switch. Historical zero-override arms are labelled accordingly;
only the native zero-capacity API tests establish disabled-cache behavior.

JSON decoding and compact-head buffer ownership follow the
[materialization contract](TOFUDB_JSON_MATERIALIZATION.md). Byte and decoded
reads share scoped authentication and bounds; patch errors never publish a
partial logical projection. This changes neither overlay nor WAL representation.

## Bounded Turn projection cleanup

Problem: settling a Turn used structural range retirement for its temporary
projection head. That operation drains the entire authority's overlay before
rewriting the tree, making one conversation's completion pay for unrelated
pending writes. Repeating it for many completing conversations defeats deferred
materialization.

Decision: `turn.rs::retire_event_projection_head` scans at most one checkpoint plus
the contract's maximum patch count plus one overflow witness. A normal bounded
head is deleted through exact entity tombstones in the settlement's ordinary
WAL transaction. The range witness preserves concurrent-insert conflicts;
owner scoping, pinned snapshots, and atomic settlement remain unchanged.
An unexpectedly larger range retains structural retirement, including all rows.
Only single-event settlement and head rollover use this path. Bulk recovery
keeps range retirement so hundreds of heads cannot exhaust point-write limits.

Alternatives considered: leaving abandoned heads indefinitely would add
persistent garbage; asynchronous cleanup would require a new durable queue and
its recovery protocol; globally disabling structural drains would violate the
root/overlay coverage contract. Bounded point deletion uses the existing
transaction and tombstone lifecycle without those changes.

## Alternatives considered

- Changing `<` to `<=` in a global prune watermark cannot identify which
  root a reader captured, especially during partial merges.
- Keeping every old delta in the shared writer index confuses retained
  reader memory with pending maintenance and repeats scan work.
- Rounding to transaction boundaries preserves visibility but does not
  bound physical work. A durable prefix cursor preserves both properties.
- Copying commit-reference arrays for every snapshot and searching every commit
  for every point read makes queue debt increase CPU work per request. The two
  persistent indexes share payloads and preserve every pending original version.
- A mutable latest-value hash map alone cannot preserve old reader versions or
  ordered range predicates. Copying its contents per snapshot restores linear work.
- Ignoring WAL records before any root change is invalid for partial merges.

## Physical page representation

Materialization emits the [versioned entity-page codec](TOFUDB_ENTITY_PAGES.md).
Prefix-coded key sizes determine physical packing; an independent expanded-byte
ceiling bounds decoded ownership. Capture, private preparation, exact-root fences,
original versions and WAL replay cursors remain unchanged. Existing legacy pages
are rewritten only on modified paths; old reader roots retain their original IDs.

## Verification

Run `cargo test --lib` from `packages/tofu-db/`. The retained entity tests
cover full/partial merge with current-head pins, transaction-prefix reopen,
partial merge across checkpoints, per-operation I/O errors and torn writes,
stale capsule staging, mounted-mask reclamation, and empty-base vacuum.
`entity_overlay_index.rs` checks deterministic balancing, bounded search/path
copy work at 12,000 pending positions, constant-work snapshot sharing, and full
allocation release. A history oracle compares old/current point and bidirectional
range reads across 240 mixed commits and partial merges.
`entity_overlay_tests.rs` retains the full merge crash/lost-sync matrix,
legacy parity, OCC, structural operations and scattered-key fanout tests.

`coalesced_merge_and_checkpoint_faults_preserve_latest_versions_and_unmerged_suffix`
injects every merge/checkpoint I/O error and short write before destructor
catch-up; selection tests cover the independent key/position bounds, tombstones,
exact partial-commit continuation, and pinned old views.
`superseded_only_prefix_survives_every_merge_checkpoint_fault_with_old_readers`
adds a 2,048-position batch with no physical value writes, pinned old values,
suffix tombstones, every I/O error/short write, partial checkpoint reopen and full
drain. `system.metrics.durability.overlaySupersededPositions` counts omitted
positions only after the coverage root successfully commits.

`turn_projection_retirement_tests.rs` verifies pending unrelated work, owner
isolation, old snapshots, concurrent insert conflicts, oversized-range fallback,
and settlement/deletion atomicity under every commit I/O error and short write.

The opt-in `audit_mixed_load_measurement` test mixes up to eight readers,
short pages, 128 writes and bounded merges, checks final content, reports
latency and resident overlay bytes, and verifies all delta memory is released.
Its timings describe the test host and build, not production p99 improvements.

## Parallel preparation and embedded execution

**Decision (2026-09-30): parallel preparation and bounded embedded execution.**
Problem: one FFI worker serialized snapshots, semantic evaluation, commits and
strict WAL waits, despite those waits already releasing the authority mutex.
Decision: the launch resource probe sets execution workers to twice observed
CPU capacity, bounded by the existing memory-derived connection budget; pending
jobs use that connection budget and queued/running JSON reserves bytes against
the frame-memory budget before parsing. Python teardown drains active calls
before freeing the C handle. Poisoned workers cancel queued work and retain the
existing detached-worker watchdog behavior. Graceful shutdown keeps the WAL
barrier alive through the final checkpoint.

Explicitly certified IR steps prepare against a registered immutable snapshot
outside the authority mutex. Private blob buffers share the aggregate transaction
memory budget and publish only after commit validation. A conflict or rotation
fence returns to the FIFO execution lane; maintenance and uncertified IR remain
exclusive. After validating business predicates, the sequencer allocates the outbox
sequence against its current head. This commit-owned counter cannot invalidate
independent business preparations. The deferred capture is memory-accounted;
structural transactions retain exclusive preparation. The business writes,
receipt and encrypted outbox record still publish in one WAL transaction.
Commit ordering and strict acknowledgement are unchanged. `commitStages` exposes `parallel_prepare`, `parallel_commit`,
and `parallel_fallback` counts/times; a fallback is not a successful parallel
commit. The loaded library exposes its own source SHA, which benchmarks record
alongside the library file SHA. `--native-client embedded` in the conversation
harness measures the production embedded client; in-process `abandon` cannot
satisfy the process-crash coverage gate.

Alternatives: unbounded thread-per-request moves congestion into memory;
publishing prepared blobs into shared storage before validation races GC;
removing the single commit order weakens durability and is not required for
parallel preparation. All three are rejected. This mechanism is not evidence
of meeting 1,000-conversation latency budgets; retained reports decide that.


The payload membership Bloom filter is reconstructible, never an authority.
Its negative answers carry an exact process-local catalog publication witness.
Catalog/routing replacement invalidates negative coverage before publishing the
new view; recovery and geometric rebuilds capture their matching witness.
Positive answers still resolve verified bytes. An incomplete filter uses the
existing exact routing/index path, without rescanning every catalog during a
foreground publication. Batch history promotion may lose unpublished hint bits
when it rebuilds against the old catalog; those bits cannot disprove membership
after the new catalog is installed. The witness uses fixed scalar memory and
changes no durable format, pins, rollback or publication order.

## Borrowed history sources

Problem: history index building and metadata-only suffix recovery copied every
complete logged envelope, including carried pages, although their source segment
remained alive. Decision: one common segment parser returns either owned logged
transactions or views borrowing exact source bytes. Metadata visitors borrow the
parsed envelope. Header, sequence, hash-chain and terminal checks finish before
any view escapes; current source reads and all restore/lookup byte ownership stay.
Alternatives considered: a decoded cache adds retention and source invalidation;
separate parsers duplicate validation; omitting carried-byte verification weakens
integrity. Borrowing removes duplicate bytes within the existing segment lifetime.

Measurements: 16 original workload windows retain exact replay and no errors.
Four-window medians show normalized decoder time about 31% lower and same-process
peak RSS about 3–4% lower; these are inclusive workload observations, not exclusive
CPU or allocator measurements. Settlement and admission have mixed paired results,
including one 4.87 s embedded settlement P99; all 1 s stream gates remain unmet.
Source, tests and full tradeoffs.


## Published preparation admission

Problem: an eligible command waits in the writer FIFO to capture its snapshot,
then waits again to commit. A tested prototype reused the existing published
root and its pin-registration guard for private preparation, with the writer's
single live rotation-install fence. OCC, private-byte ownership, FIFO commit,
strict WAL acknowledgement and error boundaries remained authoritative.

Decision: retain writer admission for preparation. Passing the blocked-writer,
private-byte, exact-conflict and full I/O-fault tests proved feasibility; it did
not establish a latency benefit. The full native suite passed 2,001 cases
(9 ignored), but production-release A/B/B/A measurements rejected this change.
For 1,000 queued conversations, candidate changes in run-median stream P99 /
settlement P99 / productive time were:

| Connection / events | Stream P99 | Settlement P99 | Productive time |
|---|---:|---:|---:|
| application / phase | +64.33% | +0.44% | -1.40% |
| application / delta | +3.16% | +4.76% | +1.95% |
| embedded / phase | +36.58% | +12.28% | +4.61% |
| embedded / delta | +14.79% | +3.37% | +1.74% |

All 16 pipeline arms passed content/recovery checks and missed the full SLO.
The shared pipeline now accepts `--native-client application|embedded`; its
application default, workload, coordinator and correctness gates remain.
Embedded in-process abandonment is not application SIGKILL evidence. Actual
`authority.client_mode`, build identity and recovery scope must match a claim.
Alternatives considered: keeping a universally enabled fast path on the strength
of a lock test ignores observed regressions; inventing another adaptive queue
before isolating the cost adds an unqualified scheduling owner. Neither ships.
All arms, SQLite neighbors, memory costs, source and tests
retain this rejected prototype. Its successful-capture counter is diagnostic
prototype evidence, not a newly shipped runtime metric.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
