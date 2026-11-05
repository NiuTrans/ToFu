# Automatic index and project-read acceleration on slow storage

**Problem.** A network/FUSE checkout charges remote metadata and publication
latency even for reconstructible indexes. The project file index historically
used the sessions directory inside that checkout. Placing the database authority
or unfinished task files on a disposable local disk would change durability.

**Decision.** Reuse `serverctl_pkg/local_placement.py` for automatic file-index
and native search-index placement. `TOFU_INDEX_FASTPATH=auto` is the default.
Only a network/userspace source (or an unclassified source measured above
50 microseconds) and a local target with at least a 3x median metadata win
qualify. Candidate roots are `$XDG_CACHE_HOME/tofu/indexes`, then
`~/.cache/tofu/indexes`; an explicit `TOFU_LOCAL_DIR` selects only its `indexes`
leaf. The target must be private, writable, local, non-memory and outside
temporary/age-cleaned directories. Container overlays are admitted for these
reconstructible bytes. A noexec mount is sufficient for indexes.

Selection reads mounts, free space and a fixed 24-lookup sample. It creates no
directories, walks no corpus and performs no fsync/engine probe; up to 64
source/environment decisions are memoized. Writers/launch owners create private
directories. Choices are stable during normal process use; native launch pins
its exact effective path. Capacity is checked again by writers. `off` retains
the data-volume paths; `on` skips only the speed comparison, not safety checks.
Apply environment changes at the next normal restart.

File-index namespaces hash the canonical sessions root. Their atomic blob
publication, freshness limits, write invalidation and complete result behavior
remain owned by `tree_index.py`. This reduces persistence/reload and synchronous
invalidation I/O used by file tools and index-backed command paths. The optional
content cache below owns file bytes separately. Neither redirects arbitrary
shell working directories.

The native search index is namespaced by the canonical authority path. Automatic
placement applies when the search-directory preference is absent or empty.
Nonempty `TOFUDB_SEARCH_PROJECTION_DIR` and saved custom directories retain
priority. An empty reset selects automatic placement; `TOFU_INDEX_FASTPATH=off`
selects `data/projections/tofudb-search` as the default instead.
`defaultDirectory` describes the selected automatic reset target,
`pendingDirectory` the selected next-launch target, and readiness
reports the actual active directory. Daemon and embedded launches share the same
preparation. A new automatic cache that cannot be prepared, or that the
[capacity governance](#search-projection-capacity-governance-2026-10-29) does
not admit, falls back to the data-volume projection; an existing index under
capacity pressure remains openable for native GC, and one whose volume is
critical yields. Indexes are reconstructed through native authoritative
queries, never copied from unchecked files. Explicitly configured and
data-volume projection directories are preserved.

The configured database authority, Strict acknowledgements, undo history and
unfinished task files remain in their existing owners/locations. Whole-authority
Fast Path keeps its own selection, lineage, capacity and replication admission
([native contract](TOFUDB_FASTPATH.md)); its automatic mode relocates only onto
proven-persistent local volumes.
Local-volume loss therefore does not introduce an acknowledged-data loss window
through this automatic index feature. Runtime replicas and Python bytecode
caches already auto-activate through their existing lifecycle owners.

## Space and inode pressure

The common reserve is `min(total / 4, 8 GiB, max(2 GiB, total * 5%))`.
It leaves useful room on a 1 GiB volume (256 MiB reserve) without reserving
hundreds of GiB on a large local disk. Known inode capacity has an independent
reserve of `min(total / 4, max(128, min(8192, total / 100)))`; unknown accounting
is distinct from known zero availability. A placed tree must fit its measured
inode demand above that reserve, not only its bytes: a runtime replica
generation counts its directories, links and copied files, and a build refused
for either dimension retries only once both fit. Runtime replicas, Python
bytecode caches and automatic placement share these checks. Existing namespace leases
continue to protect running processes from cache collection.

Project index blobs have a `TOFU_TREE_INDEX_DISK_MAX_MIB` retention ceiling per
installation: 256 MiB in personal mode, 1024 MiB distributed, explicit range
1..4096 MiB. Before publication, reclaim complete `.tfix` files oldest-publication
first when over budget, past seven days, or below the byte/inode reserve.
Only owned regular files with the exact blob name are eligible; symlinks,
hardlinks, live staged writes and unrelated data are untouched. An orphaned
staged blob is reclaimed only after one day and a POSIX probe proving its
writer PID no longer exists; reused or inaccessible PIDs remain protected.
Replacement needs
physical headroom for the full new blob while its old version remains visible.
Concurrent publishers may briefly exceed this best-effort retention target;
subsequent publication reclaims it. There is no periodic whole-project walk.

If a blob is larger than the ceiling or capacity cannot be recovered, skip disk
persistence. The complete in-memory index and live scans remain available.
Native search writes use the existing independent byte/inode guard and expedite
native GC under pressure; they report incomplete/unavailable coverage honestly.
This does not stop database authority writes or remove a live index as generic
cache cleanup. No capacity probe caps task counts or refuses commands.

**Alternatives considered.** Whole-checkout mirroring creates a second write
authority and can lose concurrent edits. Automatically enabling the database
local-authority front changes the local-volume-loss window. Using `/tmp` for
native live index files invites per-file age cleanup. Deleting every directory
under a fast root could destroy workspaces, runtime leases or a database front.
Unbounded blob retention and a flat 2 GiB minimum reserve both misfit small disks.

## Search projection capacity governance (2026-10-29)

**Problem.** Placement checked the target only before the projection existed,
against a fixed reserve, and never re-evaluated it. Native reconstruction then
rebuilt this deployment's projection on the container overlay (the root
filesystem, 16,777,216 inodes) into 16,456,347 loose block files (78 GB) and
257 segments, leaving 15,791 free inodes. Every other writer on that volume
fails with `ENOSPC`, and the free count is below the engine's own
16,777-inode search-volume reserve, so projection writes pause as well.

**Decision.** The projection reuses the census and headroom rules of an
automatic native front (`lib/storage_authority_sql/native_fastpath_auto.py`);
no new threshold or setting is introduced.

- A new local copy is admitted only with room for the measured data-volume
  projection plus equal growth room, in bytes and in inodes, above the
  reserves: a rebuild materializes its blocks as loose files first. Until the
  supervisor monitor has measured that projection the launch stays on it
  (`census-pending`); with no projection anywhere the local copy starts empty.
- An existing copy is classified like a front from its latest background
  census. Under pressure it keeps serving while native GC and runtime-replica
  relief reclaim space; when critical, the next launch serves the data-volume
  projection. The launch reads persisted measurements plus one
  `statvfs`/`disk_usage` and never walks a projection.
- The monitor reclaims a yielded copy off the launch path: only this
  authority's exact namespace, only when no engine holds its `LOCK` flock, by
  renaming it out of the namespace and removing it in the background. An
  interrupted removal is found again by its exact retired name; foreign
  namespaces, symlinks and the serving directory are never touched. A launch's
  decision and the reclaim's check-and-rename are serialized.
- Switching directories never serves stale hits: owner activation fences an
  index whose activation the authority no longer names and rebuilds it by
  backfill, reporting incomplete coverage meanwhile.
- The panel's `pendingDirectory` follows the predicted decision, so a pending
  yield shows as a required restart. Decisions, censuses and reclaim outcomes
  are persisted under the `searchProjection*` keys of
  `data/native-fastpath-auto.json`.

On this deployment the prediction is `capacity-critical` (inode headroom
-823,069 against the overlay's 5% reserve): the next storage launch serves
`data/projections/tofudb-search` and reclaims the overlay copy. A shard sample
puts that projection at about 18M loose files, so a local rebuild is not
admitted until its blocks are packed or `TOFU_LOCAL_DIR` names a local volume
with room for about twice that.

**Alternatives rejected.** Relying on native GC alone: it reclaims unreachable
blocks, and the live loose blocks are the corpus. Removing the copy on the
launch path: a multi-million-file removal delays readiness, and an unlocked
check can race an engine that still holds the index. Placing the index under
`/tmp`: per-file age cleanup (above). A separate projection threshold: the
front's measured rules already answer the same question.

## Large projects: demand-filled content, not whole-checkout migration

**Problem.** A project may contain terabytes of data, dependencies and Git
history. Copying it before a command can cost more than the work. A second
writable tree also needs explicit publication and recovery authority. The old
`content_shadow.py` was not connected to production search; it tried to fill
an entire index, required completeness and trusted second-resolution metadata.

**Decision.** Indexed `grep` uses the same search engine over a mixture of local
cached files and original files. `content_shadow.candidate_view` owns this
leased view. Only candidates requested three times within ten minutes become
copy candidates. The heat map retains at most 8,192 observations. Each
background round copies at most 2,000 files / 64 MiB and stops between files
after 250 ms. No foreground search waits for the copy. Files smaller than 64 KiB, oversized files and cold
files remain live candidates; they cannot truncate a search or prevent smaller
hot files later in the candidate list from being cached. There is no project
walk, Git clone, data migration, sparse checkout or command-cwd rewrite.

Default placement uses the measured index root's private `content` directory.
Both automatic and explicit placement reject a cache inside its source tree,
including through symlinks, before creating it.
`TOFU_CONTENT_SHADOW=0` disables it. A nonempty, absolute
`TOFU_CONTENT_SHADOW_DIR` selects an explicitly managed private cache directory;
its operator owns that placement. POSIX leases are required. Limits remain
`TOFU_CONTENT_SHADOW_MAX_FILE_BYTES` (8 MiB),
`TOFU_CONTENT_SHADOW_ROOT_MAX_FILES` (200,000),
`TOFU_CONTENT_SHADOW_ROOT_MAX_BYTES` (1 GiB), and
`TOFU_CONTENT_SHADOW_GLOBAL_MAX_BYTES` (4 GiB). Invalid numeric settings retain
defaults; zero disables that retention allowance. Allocation rounding and
conservative manifest/name overhead count against byte retention, including
empty files. Before copying, reclaim owned idle bytes and observe the common
free-byte/inode reserve. Insufficient room skips only copying.

Source identity includes device, inode, size, nanosecond mtime/ctime and mode.
BeeGFS/FUSE may expose whole-second timestamps through the nanosecond API:
copying waits until the source timestamps are at least two seconds old, also
covering coarse kernel clock ticks on nominally nanosecond filesystems.
Descriptor/path identity must agree through copying. Local cache identity and
SHA-256 content are checked, so lost, replaced, corrupted or symlinked entries
miss even when cached file size and timestamps happen to agree. Search
validates source identities before and after reading. A concurrent change
repeats the live search within the original deadline and bypasses that root's
cache for 60 seconds. Validation uses eight physical I/O groups through the
existing project demand executor, with joined lifetime; no hardware-derived
worker/task cap is introduced. All normal partial, timeout, paging, count and
remainder evidence remain with the search owner. Only filename fields are
rewritten; matched user content is never rewritten.

Shared reader and exclusive writer/collector leases protect active cache
bytes across processes. Lock files use 256 fixed shards, so removed projects
do not leak permanent per-project lock inodes. Reclaim least recently used
idle roots after seven days or under aggregate byte/inode pressure; active
readers win over this best-effort retention target. Within a root, requested
hits are retained ahead of older entries when publishing new ones. Interrupted
copy/manifest staging and unreferenced owned blobs are reclaimed under the
exclusive lease on the next fill. A small ownership receipt is fsynced before
payload publication, allowing a lost/corrupt manifest to be rebuilt or reclaimed
without adopting an unrelated nonempty directory. Explicit invalidation also
requires this ownership receipt. Sources, undo history, uncommitted changes,
Git metadata, runtime environments and database authority are never reclaimed
by this cache.

**Alternatives considered.** Whole-project automatic relocation changes the
durability of uncommitted files and makes large one-off tasks pay migration
cost. Whole-index content fill wastes bytes on untouched files and cannot help
an over-budget repository. Blind metadata reuse can serve an old same-size
edit. Serial freshness validation can erase the I/O gain. A global string
replacement corrupts matched text containing a cache-looking path. These are
not acceptable shortcuts. Opted-in managed worktrees keep their separate
placement/publication contract in [project integration](modules/project_integration.md).

For Tofu itself, existing local Python environment replicas and bytecode caches
remain the automatic execution mechanism. An additional source-import replica
was tried and discarded: 901 modules / 0.77 MiB on this host measured
400.3 → 381.3 ms within large variance, then 472.2 → 511.4 ms on repetition.
Scanning Tofu's 2,532 Python files (32,161,326 bytes) alone took 8.3 seconds.
Adding another source tree/import hook had no stable benefit. Do not move the
live shared checkout or its data to enable these caches.

## Measurement and verification

`python3 benchmarks/index_fastpath.py --source <network-directory> --local
<non-temporary-local-directory>` creates only disposable owned directories.
It alternates nine pairs after equal warmup, executes the same production
serializer/loader and verifies all paths, sizes and mtimes after every read.
Cold selection cost is reported separately with raw timings and a source digest.

On this host, 50,000 entries occupied 1,950,133 bytes. Median publish latency
was 15.857 ms on BeeGFS/FUSE and 4.183 ms on the local overlay (3.79x);
load was 9.824 ms and 4.627 ms (2.12x). Cold placement took 16.0 ms.
A second nine-pair run measured publish 18.492 → 4.068 ms (4.55x),
load 20.984 → 15.184 ms (1.38x), and cold placement 18.5 ms. Load includes
the same source ignore-rule checks in both modes, so remote-source variability
remains even with a local blob. Each run verified all 20 roundtrips.
This is an index-storage observation, not end-to-end command or transaction
latency. Automatic selection remains measurement-gated on each deployment.

`benchmarks/project_content_fastpath.py --source-parent <network-directory>
--cache-parent <local-directory>` measures complete indexed search, including
both freshness passes, path rewriting and joined validation carriers. It
creates disposable fixtures, records copy cost separately, alternates nine
pairs after equal warmup, and validates every match record and remainder.
The serial-validation experiment regressed under load (179.7 → 341.1 ms).
Grouped validation on 800 small files / 27 MiB initially improved 125.7 →
75.3 ms, but repetition with source-metadata churn measured 176.0 → 209.3 ms.
That small-file strategy was rejected. The retained policy caches only files
of 64 KiB through 8 MiB and includes checksum verification.

Two alternating nine-pair runs on 800 files / 232,333,810 bytes measured
3162.166 → 632.346 ms (5.00x), then 1946.738 → 934.298 ms (2.08x).
Every pass agreed on all 800 match records, with no incomplete result or live
retry; raw per-run timings remain emitted by the benchmark. Initial cache fill
cost 10.600 and 7.153 seconds respectively. The steady median difference would
amortize those copies after roughly five and eight subsequent equivalent
searches, excluding contention/overlap. Demand heat and bounded background
rounds avoid charging this as foreground migration latency. These are component
observations on a shared host, not a promise of faster first access, uniform
filesystem behavior, or whole-task speedup.

Focused specifications: `test_index_fastpath_placement`,
`test_tree_index_storage`, `test_automatic_search_placement`,
`test_search_projection_capacity`, and
`test_index_fastpath_measure`; neighbors cover bytecode leases, runtime replica
retention, complete tree scans, native daemon/embedded projection reconstruction,
path settings and the hardware-never-limits-work contract.
`test_content_shadow` and `test_project_content_fastpath_measure` cover the
content cache, with indexed grep remainder/timeout/process-retirement neighbors.
