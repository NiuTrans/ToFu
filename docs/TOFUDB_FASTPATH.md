# Native whole-authority fastpath

Automatic **reconstructible index** placement is described in
[INDEX_FASTPATH.md](INDEX_FASTPATH.md). It accelerates eligible network/FUSE
deployments without changing the selected authority or its durability. This
document governs the separate, explicitly configured whole-authority feature.

**Problem:** native batching reduces network fsync waits but leaves data reads,
block publication and checkpoints on the slow volume. Search-index relocation
accelerates only search. **Decision:** `serve --fastpath-dir <existing-local-base>`
seeds a complete native authority on the explicitly selected, measured local
volume, with Strict local commit acknowledgement. Memory filesystems
(tmpfs/ramfs) are refused because their commits cannot be durable. The retained durable seed is
fenced by an exclusive process lease and an atomically synced `FASTPATH` marker;
classic opens fail closed even when fastpath configuration is removed. A
missing source with retained shadow/front artifacts refuses fresh initialization. The front
is namespaced by SHA-256 of the canonical durable authority path. Existing unknown
fronts/shadows and changed path choices fail closed; no automatic deletion occurs.

First activation records a durable preparation receipt binding the source
UUID/commit hash and chosen paths. An interrupted initialized shadow copy resumes
only while that source is unchanged; changed sources and unknown layouts fail
closed. First activation checks observed capacity on both volumes and a 3x median fsync
win, creates a complete encrypted-if-configured native shadow, then publishes the
relocation fence before restoring the front. A lost recorded base may be recreated only after matching path and local-volume
admission. A lost or interrupted front uses
same-lineage native restore, including its resumable `RESTORE` marker. A surviving
front retains its unshipped WAL tail; validation proves its shadow's chain ancestry
and rejects foreign/stale/divergent fronts rather than overwriting them. Search
projections remain separate and reconstructible; they are not recovery replicas.

The existing online-backup worker copies immutable, GC-pinned blocks off the
foreground mutex and publishes the complete snapshot manifest last, retaining two
verified generations through native prune. No second worker or unbounded queue is
added. Default cadence derives from twice the observed initial snapshot cost
(1 s..1 h); `--fastpath-sync-interval-ms` explicitly overrides it. Later expensive
epochs pace automatic replication, failures wait before retry, and unchanged
sequence ticks skip the corpus walk. An already complete same-sequence/hash
shadow is reused after physical checkpoint renumbering, rather than replacing
its immutable manifest. Incremental physical writes still require
reachable-graph traversal after changes: this is snapshot replication, not WAL
streaming. Large-corpus copy/traversal and temporary prune space are operational
costs; no production speedup or time-bounded RPO is claimed.

**Durability:** acknowledged commits are durable on the local volume. Local-volume
loss loses the tail after the latest complete shadow snapshot; the interval is a
scheduling target, not a guaranteed maximum loss window. A planned stop preserves
the local tail and does not promise a final remote snapshot. Shadow failures remain
observable through `system.metrics.onlineBackup` (last success, error, copied bytes
and durable sequence, including the seed `replicated_sequence` and actual
`replication_interval_ms`). Readiness reports effective `nativeFastpath` paths/cadence.

Application opt-in is `TOFU_DB_FASTPATH_DIR`; optional
`TOFU_DB_FASTPATH_SYNC_INTERVAL_MS` uses the same range. Auto transport selects the
daemon; explicit embedded mode fails. SQLite's `TOFU_STORAGE_FASTPATH` remains
independent. Startup uses the bounded fastpath seed budget and outer lifespan
reserve. Existing settings continue to configure the independent search index;
this native capability is an explicit deployment option, not that index picker.
To exit, stop the application and run `tofu-db fastpath-export --data-dir <seed>
--backup-dir <NEW-authority-target>`. It exports the surviving front including its
WAL tail (or the verified shadow if the front is lost), and leaves seed/front/shadow
intact. Explicitly select the verified exported authority for the next deployment;
never simply disable the option and serve the old seed.

**Alternatives:** copying only WAL omits referenced block/checkpoint data; mirroring
raw filesystem operations duplicates publication/recovery authority; synchronous
remote replication restores slow-volume latency to commits. Snapshot replication
reuses the certified native backup, encryption, restore and retention boundaries.
Verification: `fastpath::tests`, `online_backup::tests`, native backup fault matrix,
transport/startup contract tests and the full Rust suite. This option stays off by
default and does not relocate a live production authority automatically.


The database panel exposes this opt-in independently of the required search
index. A save validates an explicit persistent local base and updates only the
next-launch configuration; actual activation is reported from daemon readiness,
not from the saved checkbox. Activation and the durable `FASTPATH` fence lock
disable/base changes in the panel. Exiting requires the stopped-server verified
export described above; the UI must not silently reselect the retained seed.
Path admission can still fail during launch (measured fsync/capacity/lineage), so
saved configuration and pending restart are not proof that a local authority is
serving.

The disposable 16 MiB payload comparison on this host measured Strict commit
P95 1.286 → 0.127 ms, and read P95 0.674 → 0.174 ms; read medians stayed
approximately 0.164 ms. This is a warm, unoptimized generic-engine workload,
not application latency or a large production corpus. Initial copy/restore
took 2.13 s; a changed snapshot copied 538,408 bytes in 0.592 s, with about
65 MiB peak process RSS. Whole-graph traversal remains a material large-corpus
cost. Exact source digest, sizes and raw distributions: measurement.

## Interrupted prune recovery

Problem: an epoch could commit its prune plan and fail before clearing it;
subsequent backups refused forever before reaching the existing prune resumer.
Decision: resume the persisted plan through the same backup/prune lease owner
before the next copy phase, outside the foreground mutex. Recheck the prune
fence after acquiring the copy lease. The persisted retention set, pins,
encryption, fault rollback and manifest-last publication remain authoritative;
the completion receipt includes both resumed and current prune removals.
Alternatives rejected: deleting the marker loses crash safety; changing a
pending retention set can discard recovery generations; pruning under the copy
lease deadlocks its ownership boundary. An injected interruption test restores
the newest data after the next online epoch; no worker or retained queue is added.

## Prune marks every retained generation's tree (2026-10-31)

Problem: an interrupted restore resumes the generation its marker names, and
its restore pin makes prune retain that manifest. But `write_live_marks`
walked only the newest retained generation's entity tree; older retained
generations contributed their root, history manifest and history references.
Once the source changed and a newer backup was taken during the interruption,
prune deleted pages only the pinned generation's tree reaches, and the
resumed restore failed with a missing block, so it could never finish. The
same held for every older generation that `retain_latest` keeps.
Decision: prune marks the whole tree of every retained generation, pinned or
not, through `entity::visit_entity_page_graphs_once`, so pages the
generations share are read and marked once. A retained generation is
therefore exactly a restorable one.
Alternatives rejected: marking only pinned generations' trees keeps
manifests that cannot be restored; dropping older manifests changes the
retention contract; walking each tree separately rereads every shared page.
`backup::tests::prune_keeps_every_block_an_interrupted_restore_still_copies`
fails before the change with `content block ... is missing` and restores the
pinned generation's values after it.

## Capacity walks sum bytes without a file count (2026-10-31)

Problem: the restore capacity preflight and the fastpath relocation admission
walk the backup or authority tree to sum its bytes, and refused any tree with
more than 20,000,000 files ("capacity preflight file bound exceeded",
fastpath `authority-too-large`). The walk holds one directory's entries and
the pending directory stack, so the count bounded no memory; it only made a
large backup unrestorable. The live authority already holds about 19.2M
loose blocks, so a backup of it would have crossed the bound.
Decision: the walk counts bytes only; the 4,096-directory bound, which bounds
the pending stack and the layout's fixed directory tree, stays.
Alternatives rejected: a higher file bound moves the same refusal; a
volume-derived bound is a hardware limit on work.
`backup::tests::capacity_walk_sums_every_file_with_no_file_count_bound` walks
20,001,000 synthetic files.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

## Automatic selection and capacity governance (2026-10-27)

**Problem:** the capability existed only as an operator-typed
`--fastpath-dir`, and its admission asked one question: is a 4 KiB fsync
median 3x faster? Measured on this deployment's BeeGFS FUSE authority on
2026-10-27, fsync was not the dominant cost. The daemon accumulated 12,492 s
of `authority_wait` behind holders of the authority mutex, whose cost was
uncached block reads (7.5 ms foreground mean); a `/proc/<pid>/task/*/syscall`
sample put 3,323 of 3,379 blocked-I/O samples in `openat`/`statx` on
`payload-segments/*.pseg`, against a 1.94 ms FUSE fsync and a 2.39 ms local
HDD-overlay fsync (metadata lookups 332 us versus 4 us). A commit-barrier-only
gate therefore refuses exactly the metadata-bound volumes this capability
exists for.

**Decision: automatic selection replaces the instruction to pick a volume,
and admission asks the workload's question.** `TOFU_DB_FASTPATH` is
`off | auto | on` with default `auto`; `TOFU_DB_FASTPATH_DIR` keeps its
explicit meaning and always runs required admission. Automatic selection
nominates a base through the shared placement policy
(`serverctl_pkg.local_placement`, purpose `native-authority`): `auto` accepts
only a candidate whose volume the mount table calls persistent local block
storage, while `on` also accepts a container overlay after the operator
consents to its loss window. Before nominating, the supervisor requires a
remote authority; a census no older than 24 h (sampled shards and segments,
never a full walk, which took over 2.5 h here and did not finish); free bytes
and free inodes for the corpus plus equal growth room above the placement
reserve; a search projection outside the front; and a planned first activation
of two whole-corpus copies inside half the bounded seed budget. It then
launches `--fastpath-admission auto`, and the engine admits only on a decisive
win: a 3x fsync median, or a 3x metadata win within a 2x fsync regression
(`contracts/storage_v2.json`: `minimumFsyncSpeedup`, `minimumMetadataSpeedup`,
`maximumFsyncRegression`).

**Typed refusals keep automatic selection from ever failing a launch.** Every
pre-relocation refusal (unobserved local volume, memory volume, insufficient
speedup, capacity, inode or corpus bound, unclaimed artifacts, stale
preparation, selection drift) carries a reason code. Under
`--fastpath-admission auto` the daemon publishes no relocation state, serves
the classic authority, re-derives that volume's automatic durability tier and
reports `nativeFastpathDeclined` in readiness. A `FASTPATH` fence, lineage
divergence, unreadable marker or corrupt seed is never a declinable refusal:
those still fail closed, because the fenced authority must not be served. Each
launch persists its decision, checks and engine outcome in
`data/native-fastpath-auto.json`, which the database panel reads; census and
capacity passes run from the supervisor monitor, never from the launch path.

**Capacity governance answers what happens when the smaller tier fills.** An
active front is classified on both dimensions it can exhaust: bytes above the
placement reserve and free inodes, under pressure below half its measured size
and critical below a fifth. Pressure first reclaims reconstructible
co-tenants on the same volume (runtime-replica generations nothing is using);
critical records `exitRequested`. The next launch then performs the documented
lossless exit automatically, for an automatic front only: `fastpath-export` of
the surviving front with its WAL tail, `inspect` of the target, then a
journaled `authority -> retired seed`, `export -> authority` swap and a durable
directory sync. The retired seed and the shadow stay on the durable volume for
operator review; the exported front's local copy is removed, because relieving
the small tier is the point and every byte of it now lives in the verified
export. A failed export keeps the front serving and is retried after six hours;
a failed swap resumes from its journal instead of guessing. After a capacity
exit the tier is not re-selected for seven days. Setting
`TOFU_DB_FASTPATH=off` on an automatic front requests the same exit at the next
launch instead of leaving a fence the configuration no longer names; explicit
fronts keep the operator-only retirement path below. User work is never
refused, and the live front is never deleted while it is the authority.
The same census and headroom rules govern the automatically placed search
projection, which yields to the data-volume projection instead of exporting
([INDEX_FASTPATH.md](INDEX_FASTPATH.md#search-projection-capacity-governance-2026-10-29)).

**This deployment declines today, and says why.** Its corpus is ~0.96 TB of
logical payload across 20,371 immutable segments and ~15.2M loose block files
(segments are sparse: 47.0 MB logical against 13.7 MB allocated). Native
backup and restore republish every reachable block as one file, so first
activation plans ~68M block publications, roughly 185 h at the measured 5.5 ms
FUSE and 4.3 ms overlay publication costs, against a 450 s budget. The
container overlay also holds 16.5M free inodes against that corpus, and
`/opt/meituan/hadoop` (4.9 TB, 169M inodes) is mounted read-only. Automatic
selection therefore reports `activation-too-costly`, `inode-capacity` or
`placement-insufficient-space` and serves the classic authority. Relocating
this corpus needs its loose blocks packed first, so the reachable set shrinks,
or a persistent local disk large enough for about twice the corpus and its
inode count.

**Alternatives rejected:** narrowing admission to commit barriers (refuses the
metadata-bound volumes this capability exists for); predicting admission in
the supervisor instead of the engine (a second, drifting owner of one probe);
measuring the corpus on the launch path (launch latency, and a partial walk is
not a capacity measurement); treating a container overlay as persistent
because its snapshotter is named `persisting-*` (a filesystem name is not a
lifetime guarantee); deleting a retired seed or shadow automatically (they are
retained evidence); and demoting the live front in place (no second writer to
the authority, so the export runs only before a launch).

## Segment read handles (measured 2026-10-27)

The same profile explains the read cost: the idle-handle LRU held 32
descriptors for a 20k-segment catalog, verification opens never reused one,
and every catalog install closed all of them. The bound now follows the soft
descriptor limit (1/16, clamped 32..4096, so macOS's 256 keeps the historical
32). A point-read index miss hands its verifying handle to the payload fetch;
verification opens reuse an idle handle while still re-checking the path and
re-verifying framing, and never insert; catalog replacement keeps handles of
still-referenced segments; a lease returned after its segment was removed is
closed instead of re-cached. Paired measurement (1,024 segments x 64 blocks,
20,000 random reads, same source tree before and after): FUSE point read
721-2,267 us to 160 us and verification open 659-2,066 us to 74 us; local
overlay 30 us to 12 us and 32-36 us to 13 us. Reproduce with the ignored
`payload_segment::segment_read_bench::segment_point_read_bench` test and
`TOFU_DB_SEGMENT_BENCH_DIR` on the volume under study.
