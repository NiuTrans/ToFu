# Native whole-authority fastpath

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
cost. Exact source digest, sizes and raw distributions: [measurement](evidence/tofudb_native_fastpath_20261006.json).
