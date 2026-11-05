# TofuDB online garbage collection

Status: authoritative design for the online physical-reclaim lane.
Owner: `packages/tofu-db/src/authority_gc_online.rs` (design), mounted by the
daemon; the offline exclusive collector in `authority_gc.rs` stays unchanged
and remains the certified heavy hammer.

## Problem

Physical reclamation needs a foreground-safe owner: the exclusive offline
collector cannot run with live MVCC pins. The serving source authority now
mounts the two-mark collector, but the independent search projection was left
without that owner and could retain unreachable blocks indefinitely. Because
search and credential state can share a filesystem, exhausting projection
inodes also prevents unrelated applications from creating state and locks.

## Decision

Run a **two-mark epoch collector** on a dedicated daemon thread
(`tofu-db-online-gc`). Each epoch is a sequence of bounded steps; only
short holds of the authority mutex ever happen, and every hold is O(in-memory
clone), O(one bounded publish) or one loose-file unlink (see "One file per
deletion hold"). Long work (graph traversal, directory scans, segment
rebuild, fsync chains) happens off-mutex.

### Search projection mounting

Daemon and embedded transports also mount the same collector against the
selected search runtime's existing mutex. `OnlineGcDatabase` exposes the
actual opened authority; there is no second marking/deletion implementation.
Marks, active pins, WAL references, relink fences and scratch directories belong
to that index, never the source database. A deferred index installs its GC
handle when background maintenance successfully opens it. The derived collector
stops before the index checkpoint; its failure does not change source liveness.
`system.metrics.searchGarbageCollector` reports the existing bounded counters.

Each projection epoch clamps temporary bytes to at most one percent of the
index volume's currently available bytes and the launch-derived GC budget.
Capacity rejection rings the collector doorbell while retaining the common
120-second start floor and 50-percent duty bound. Index writes independently
check the reserve specified by `conversation_search_v2.json::nativeProjection`;
GC remains allowed to reclaim under that reserve. A completely full volume may
still need external space released before a safe mark can be staged.

Alternatives rejected: a recursive file sweep lacks root/pin/relink proof;
restarting the source collector does not cover another authority; periodically
rebuilding the index adds downtime and another publication lifecycle.

### Epoch state machine

```
Idle → Snapshot → Segment lane → Mark → Scan → (first epoch: rotate marks, stop)
     → Confirm → Delete(batches…) → rotate marks → Idle
```

1. **Snapshot** (short hold): clone the mark inputs out of engine state —
   current `authority_state_root`, every active transient snapshot-pin root
   (reader + writer class), the history-manifest segment reference list,
   staged-history references, the committed-envelope block-id lists, and the
   current payload manifest id. All of these are in-memory structures; the
   hold never performs file I/O. The same hold copies the segment IDs the
   installed catalog, unpublished background packs and parked retirements
   name, plus every loose block an open publication window wrote and the
   covered WAL prefix of an in-flight rotation (rule S9).
1b. **Segment lane** (before and independent of marks): sweep parked
   retirements past their reader floor, observe every orphan segment file
   off-mutex, and remove each one the previous observation saw with the same
   name and size, one short re-validating hold per file (see "Superseded
   payload-segment reclamation independent of block marks" below). A stop
   or deferral abandons the epoch; any other lane failure is recorded and
   the epoch continues with its marks (see "Segment-lane failures never
   stop block marks").
2. **Mark** (off-mutex): union into one `SpillMarkSet`:
   - `visit_entity_page_graphs_once` over the snapshot root (its pages
     are CoW-immutable; only this lane deletes, so traversal is race-free)
     and every transient pin root captured in step 1, discovering
     persistent root pins exactly like the offline walk; each page is read
     and marked once per mark, by the first walk that reaches it (see
     "Pages shared by several roots are walked once per mark" below);
   - semantic value references for every visited leaf value: blob manifests
     are read for their chunk lists, while chunks and stream segments are
     marked by identity (see "Reference-only marks" below);
   - the captured history/staged/committed projection: history segments are
     decoded off-mutex through `BlockStore` reads (segment files are
     immutable and are deleted only by this lane, so a captured reference
     list stays readable).
   The mark spills under `authority-gc/online/` with the existing
   `SpillMarkSet`; marks never carry authority across process boundaries
   (spill files are namespaced per daemon run and wiped at thread start).
   A mark runs until it completes, stops or fails; it has no wall-clock
   budget (see "Marks run to completion" below).
3. **Scan** (off-mutex): walk loose block shards one at a time, with no
   store-wide total and entry kinds from the directory listing (see "Loose
   listing without a store-wide total" below), and the payload-segment
   directory with the same per-round bounds as the offline collector
   (16-segment catalog windows, …).
   A block/segment content unmarked in the current mark is a *suspect*.
   Live loose blocks not yet in the catalog feed loose compaction: when the
   next one would exceed `MAX_SEGMENT_BLOCKS` or the temporary-byte limit,
   the open segment is built off-mutex and published under one short hold,
   then its loose copies go one short hold each, before the scan continues
   (see "Loose compaction rate" and "One file per deletion hold" below).
   A live loose block the installed catalog already holds is a redundant
   copy: once its shard is scanned it goes through the same hold, only while
   both CONTROL slots name a catalog holding it (see "Redundant loose copies
   of catalogued blocks" below).
4. **Confirm**: suspects from the previous completed scan that are *still*
   unmarked in this epoch's mark become *confirmed garbage*, except blocks
   a deletion hold unlinked after that scan (rule S10). Confirmation reads
   no file age (rule S4).
   First epoch after startup confirms nothing; it only establishes the
   first mark. This is the two-mark rule.
5. **Delete** (bounded rounds, short holds): per maintenance round delete
   at most `MAX_AUTHORITY_GC_BLOCKS_PER_ROUND` blocks /
   `MAX_AUTHORITY_GC_VICTIM_BYTES` and one segment rewrite, reusing the
   offline collector's hard caps (orphan segment files belong to step 1b). Every batch that mutates
   catalog/CONTROL takes the authority mutex only for the publish
   (`publish_payload_manifest` / `stabilize_control_slots`), after the
   off-mutex file work + fsync chain completed. Each loose victim is
   validated and unlinked under its own short hold, which first makes both
   CONTROL slots name the current physical roots (rule S8; see "One file
   per deletion hold").
6. Rotate marks (current becomes previous), persist epoch metrics, back off.
   The completed scan's suspects rotate as soon as the deletion holds ran,
   even when one failed, together with the blocks they unlinked (rule S10);
   temporary-file, segment-dead and catalog-window memory rotate only after
   a completed epoch.

The daemon schedules an epoch when the maintenance lane reports idle, with
exponential backoff when no garbage is found (never while the payload catalog
ends an epoch at 90% of its ceiling or more, see "Catalog pressure keeps the
minimum interval"), and expedites an epoch when
the launch probe reports volume pressure (`has_volume_pressure`). Every
trigger — doorbell, pressure, idle timer — respects one pacing floor: epoch
starts are at least `ONLINE_GC_MIN_EPOCH_INTERVAL` apart and the collector
never exceeds a 50% duty cycle, so multi-minute epochs on slow mounts can
never run the collector continuously (2026-09-21 wedge class). There is no
user-facing knob; budgets derive from the launch resource probe via the
existing `AuthorityGarbageCollectionBudget::from_resource_budget`.

At ≥75% of the 4,096-entry payload catalog ceiling the collector enters
consolidation mode: each pressured epoch merges up to 8 small or
two-mark-confirmed dead-heavy segments into one replacement, freeing
catalog slots (the ceiling is a hard data-plane bound; the 120-minute
soak hit it at 7.7M commits and wedged the rotation pack merge). At ≥90%
that work counts as pending: an epoch ending there keeps the minimum
interval instead of backing off. The
packer backs off new-segment production at ≥95% except for checkpoint
drains, and its steady-state batch target is 32 MiB so steady segment
production stays well ahead of the ceiling. Loose compaction is optional
work and never takes a slot that would put the catalog at this 75% floor.
A consolidation parks its victims behind the retirement floor once both
CONTROL slots name the new catalog, like every other catalog publisher
(see "Consolidation victims are parked, not unlinked" below).

### Safety rules (each is load-bearing; tests pin every one)

- **S1 two-mark rule.** Only suspects unmarked in two consecutive marks may
  be deleted. Anything committed, packed, pinned, or staged after epoch N's
  scan is either absent from the suspect list or marked in epoch N+1.
- **S2 pin-inclusive marks.** Every mark unions the current root and all
  transient pin roots active at snapshot time. A pin acquired between
  snapshot and deletion can only reference pages reachable from a root ≥
  the snapshot root or newly written pages; confirmed garbage is neither
  (CoW induction; S7 and S10 cover a new write whose content names a
  suspect), so deletion never touches it.
- **S3 overlay coverage.** Committed-but-unmerged overlay deltas are covered
  by the WAL committed-envelope projection, because checkpoint publishes
  merge the overlay fully first (invariant already enforced by
  `EntityDatabase` checkpoint paths). The collector asserts the active-WAL
  projection is captured at every snapshot.
- **S4 no file-age grace.** Confirmation reads no mtime; the
  `ONLINE_GC_CANDIDATE_MIN_AGE` grace this rule once required never existed
  in code. Writes that straddle the scans are covered by S7 (decisions
  after the capture), S10 (staged copies and recreated names) and the
  `ONLINE_GC_MIN_EPOCH_INTERVAL` floor of 120 s between captures, which a
  staged-but-uncommitted write that a checkpoint already materialized would
  have to outlast across two captures; the code records that interval as
  far longer than any ordinary transaction or upload staging.
- **S5 publish atomicity.** All catalog/CONTROL mutations keep using the
  existing `publish_payload_manifest` + `stabilize_control_slots` pair under
  the authority mutex; a crash anywhere in Delete leaves either the old or
  the new catalog, never a torn one, and leaves at worst orphan files that
  a later epoch re-collects. Unpublished background-pack segments keep
  their existing orphan protection, and no orphan is unlinked while either
  durable CONTROL slot names a catalog other than the installed one.
- **S6 single physical lane.** Online GC takes
  `require_physical_maintenance_available` for every publish, exactly like
  the offline collector, so it can never race checkpoint catalog merges or
  pack adoption; contention defers the GC batch, never the foreground.
- **S7 relink fence.** Dedupe never writes bytes twice: `put_deferred`
  returns `(id, false)` and the transaction *references* existing durable
  content (verified by bytes, never by the bloom filter alone), and the
  payload cache hit path does the same. Between an epoch's mark snapshot and
  its deletion hold, such a reference can silently resurrect a confirmed
  candidate. All reference-without-write decisions run under the authority
  mutex (single-writer model), and a decision must share one authority hold
  with the commit that uses it, so the fence is an in-memory set in
  `BlockStore`: the capture hold opens it empty, every `(id, false)` return
  (and every idempotent loose republish or materialization over an existing
  file) inserts while it is open, and the collector drops it after the
  epoch's last deletion hold or when it abandons the epoch. It is exact and
  has no capacity (see "Exact relink fence per epoch window"). Every
  deletion hold (loose victim, segment rewrite, consolidation) skips or
  defers candidates present in the set, and fails closed with `WouldBlock`
  when no window is open. Loose removal
  already evicts the payload cache, so a deleted id can never be served
  from cache afterwards; a later probe downgrades to `Missing` and
  re-stages, which is always safe. Stale bloom bits for removed segments
  are harmless because `probe_dedupe` claims existence only after reading
  candidate bytes through the *current* manifest.
- **S8 both CONTROL slots.** Recovery selects the older slot when the
  newest is unreadable. Before every loose unlink, decided in its own hold,
  every slot recovery can select must name the current WAL chain, checkpoint,
  history manifest and payload catalog
  (`ControlFile::slots_name_only_physical_roots`, decided in memory);
  otherwise the hold first republishes the current state into the other
  slot. Catalog publications already stabilize both slots before they park
  or remove anything (see "Loose deletion and the fallback CONTROL slot").
- **S9 publication windows.** A WAL rotation (preparation to
  installation), a history compaction and a pack adoption write loose
  blocks outside the authority mutex that engine state names only when
  they install. `BlockStore::put_loose` registers every block it handles
  into each open window before the file can appear; every capture marks
  those IDs and the covered WAL prefix, and a window's set is dropped when
  it installs or aborts (see "Publication windows stay marked").
- **S10 content-addressed rewrites.** A write can produce a suspect's name
  again without any reference-without-write decision: a blob write stages
  complete bytes without probing for a loose duplicate, and a block a hold
  unlinked can be written again as a fresh file. The loose deletion hold
  drops every victim with a staged copy, before it consults the fence
  (materialization records the fence before it retires a staged copy), and
  no block confirms from suspects observed before a hold unlinked it (see
  "Unlinked blocks never confirm from older suspects").

### What changes where

- `entity.rs`: add `pinned_snapshot_roots()` returning the distinct roots of
  the transient pin registry (read-only, under its own mutex).
- `authority_gc_online.rs` (new): snapshot/mark/scan/confirm/delete state
  machine, epoch metrics, spill management under `authority-gc/online/`.
- `engine.rs`: split `collect_payload_segment` into
  `prepare_segment_reclaim` (short hold) / off-mutex rebuild +
  `finish_segment_reclaim` (short hold publish), keeping the old fused path
  for the offline CLI; expose a `&BlockStore`-based loose-block batch delete
  for the online lane (deletion of loose blocks mutates no catalog).
- `daemon.rs`: spawn `tofu-db-online-gc`, wire stop/join, expose epoch
  metrics through `system.health`.
- `semantic_executor.rs`: `system.reclaim` reports live online-collector
  progress (`offline_required: false`, cumulative reclaimed bytes/blocks,
  `statistics_complete` once the first full epoch finished, plus the
  existing bounded fields). The RPC still performs no physical I/O.
- `main.rs` / `authority_gc.rs`: offline CLI unchanged.

## Alternatives considered

- **Keep offline-only reclaim (status quo).** Defeated: disk growth without
  scheduled downtime is the single largest operational gap; the whole point
  is that no operator action is ever required.
- **Synchronous GC rounds inside the maintenance worker holding `&mut
  Engine`.** Defeated: a full mark under the authority mutex stalls
  foreground commits for the whole traversal; segment rebuild (up to a
  segment budget of file copies + fsync) under the mutex violates the
  maintenance lane's never-block-foreground contract.
- **Fully incremental resumable mark cursor persisted in the authority.**
  Defeated for now: per-shard invalidation on every commit and crash-safe
  cursor ownership across processes is a much larger correctness surface;
  the two-mark epoch design reuses the existing certified mark/sweep
  machinery and `SpillMarkSet` unchanged, and epochs give us natural
  resumability (any crash simply restarts the current epoch). The epoch
  mark build is also the exact prerequisite a future incremental cursor
  would validate against.
- **Reference counting on blocks.** Defeated: counts would have to survive
  crashes and history retirement, duplicating what reachability already
  proves; content-addressed dedupe makes count ownership ambiguous across
  independent semantic references.

## Budgets and constants

All bounds come from `generated_tofudb_ir.rs` and the launch probe, exactly
as the offline collector: ≤ 65 536 loose victims per epoch (the offline
round's 256 MiB victim byte cap does not apply online, see "Reclamation
keeps its pace under foreground load"), rewrite and compaction scratch ≤ min(1 GiB, 2 % volume free) (the mark spill has no budget, see "Mark spill sized by its inserts"), catalog window 16
segments, compaction floor 128 blocks (for an epoch's trailing compaction
segment; every segment the format limits close is packed); loose directory
listings hold one shard at a time and have no store-wide total. Loose
compaction takes a catalog slot only while the catalog stays below the
catalog-pressure floor (75 % of 4,096) afterwards. New constants (owned by
`authority_gc_online.rs`):
`ONLINE_GC_MIN_EPOCH_INTERVAL` = 120 s between epoch starts,
`ONLINE_GC_BOOT_GRACE` = 30 s, and an idle
backoff that doubles from the minimum interval up to
`ONLINE_GC_MAX_IDLE_INTERVAL` = 3600 s. There is no file-age grace (rule
S4) and no relink-fence capacity. Loose deletion runs add no constant: one
file per hold is structural, and the pause between holds is the previous
hold's measured duration ("One file per deletion hold").
A mark has no wall-clock budget; its memory is bounded by the
captured catalog (location table) and the distinct pages it walks
(visited-page map), and its spill grows with its inserts (32 bytes each,
plus 52 bytes per 65,536-insert run).

## Observability

Epoch metrics (marks built, suspects, confirmed, reclaimed bytes/blocks per
kind, segment rewrites, compacted blocks and compaction segments
(`total_compacted_blocks`, `total_compaction_segments`), redundant loose
copies removed (`total_redundant_copies_removed`), last-epoch duration,
epochs completed, segment-lane failures and the latest lane error, failed
epochs and the latest epoch error) are exported through `system.metrics`
(`onlineGarbageCollector`, `searchGarbageCollector`) and summarized by
`system.reclaim`; `system.health` reports whether the driver thread is
alive. The application's `/metrics` endpoint republishes the reclamation
progress per collector (`tofu_storage_gc_epochs_{completed,failed,deferred}_total`,
`tofu_storage_gc_epoch_failing`, `tofu_storage_gc_reclaimed_bytes_total`,
`tofu_storage_gc_orphan_segments_removed_total`) and the retained history
(`tofu_storage_history_retained_segments`), so a collector that never
completes an epoch or a history that keeps growing shows on a dashboard
instead of only in a debugger session. No log spam: one line per completed epoch at INFO
(`step=epoch_complete`), deletions at DEBUG, and one line per change of
the segment-lane error or of the epoch error.

## Test plan (test ladder, smallest first)

1. Unit: `pinned_snapshot_roots` registry enumeration; multi-root mark
   union; two-mark confirm transitions; staged-copy and unlinked-name
   exclusion (rule S10).
2. Deterministic fault injection (`DeterministicVfs`): crash between
   rebuild and publish, crash between publish and old-segment delete, torn
   CONTROL during stabilize, fault during spill write — every fault must
   preserve every live block (mirror the offline `every_*_fault` families).
3. Concurrency: staged-block-across-epochs survival; pin-held-across-epoch
   survival; commit storm during an epoch; checkpoint rotation during an
   epoch; catalog merge contention defers GC, never foreground.
4. Process: daemon serves reads/writes continuously while epochs reclaim
   (assert p99 foreground latency budget unchanged); `system.reclaim`
   reports progress; offline CLI still passes its existing suite.
5. Differential: `tests/tofudb/test_tofudb_differential.py` stays green;
   `system.reclaim` response shape stays contract-compatible.

## Compatibility

Marks and suspect lists are spill files under `authority-gc/online/`, never
read across process boundaries and wiped at thread start, so their layout
changes without a migration: initialization removes the current spill file
and the per-run files and temporaries of the earlier layout alike
(`backup_gc::is_spill_entry_name`). The offline CLI keeps its exclusive
session semantics unchanged. CONTROL/history/payload formats are untouched.

## Restart cancellation at source-I/O boundaries (2026-10-05)

**Problem.** The mark walk checked shutdown only every 4,096 visited blocks,
and a loose-shard callback checked only before its entire file list. A visit
can issue a network-volume read; these counts therefore did not bound shutdown
latency. Python's bounded stop can kill a daemon that is still joining its
collector, preventing the final WAL checkpoint and increasing next-boot replay.

**Decision.** Observe the collector's existing stop flag at every marked block
and every loose-file iteration. Keep the elapsed-time probe amortized over
4,096 visits. Cancellation still follows the existing WouldBlock abandonment
path, cleans private spill state, and publishes no incomplete marks or victims.
A source I/O already in progress must finish; this is not a promise to interrupt
a blocked kernel call. The collector thread is still joined before checkpoint.
No new threads, retained state, backend selection, or durability shortcuts are
introduced.

**Alternatives considered.** Shorter process-stop timeouts force more WAL
recovery; longer timeouts preserve the unnecessary scan. Detaching a collector
would let it outlive the authority lease. Neither addresses the cancellation
boundary.

The regression requests shutdown during a cold source read in a 257-record
authority, verifies that no additional source read is issued, no block is reclaimed,
and a subsequent uncancelled epoch preserves the original value. This isolates
the cancellation mechanism; total production restart latency also includes
foreground drains, checkpoint I/O, and source replay.

On that deterministic fixture, the old mark guard issues 20 further source
reads after the stop signal; the corrected guard issues zero. This is a count
of avoided simulated I/O, not a production wall-clock speedup.


## Deferred history metadata source reads (isolated candidate)

Problem: snapshot capture called `history_payload_metadata_ids` inside the
authority hold. That helper can open/stat/read a cold routing-root block before
returning its page IDs. A `try_lock` controls acquisition, not hold duration.

Decision: GC captures the same manifest-selected root ID and index IDs, plus the
original authority/pin/history/staged/committed inputs and relink fence. The storage
owner's shared enumerator verifies and decodes that selected routing-root source
during off-authority marking. It streams all IDs into the existing spill mark set.
Source/decode or partial-mark failures abort the epoch before scan/deletion and
clean only reconstructible marks. Backup, verify and salvage keep eager source
validation and their original failure boundary. GC never substitutes a later
installed root, treats a membership/cache answer as existence, or omits page IDs.

The epoch-owned metadata selection contains one optional root ID and the same
index IDs the original helper used. Allocation is fallible and admitted before
copying; actual Vec capacity plus selection size is charged against the existing
probe-derived GC workspace ceiling at capture.
Selection storage is released during marking or on abort. Tests measure typical
and large 32,768-segment selections; no new pool, cache or persistent data exists.

Availability follows the existing captured-history/CoW rule: only this collector
deletes loose metadata; history compaction promotes carried sources before
retiring references, and does not unlink the old routing metadata. Background
materialization may change a current source, so the original fresh-source reader
and its failure/fallback rules remain required. An Arc to BlockStore alone is
not a source lease. Tests must rotate/retain after capture, keep an old root pin,
and prove the captured metadata remains marked and readable.

Alternatives rejected: cloning every complete manifest segment would add about
5 MiB at the format ceiling; using the new installed root could miss old pages;
suppressing fresh-source errors could delete after a partial mark. The paired
same-volume ignored experiment delays a real cold metadata response, compares
old/new placement in one test binary, and SIGKILLs before any Drop before checking
exact ACK recovery. These diagnostic timings do not certify normal capacity P99.
This candidate moves only capture metadata reads: orphan planning, retirement
sweep and final durable removal still contain separate authority-held I/O.

Final candidate verification: 26 GC neighbors pass (two ignored diagnostics),
including the existing fault campaigns; the full native gate passes 1,666 tests
with six ignores in 114.59 s. The executable test digest matches source
`5dafd7d538f66dd85d70903a79bb50c8087c38983e80751b11ff0f653fe54343`.
Measured selection residency is 96 bytes for one indexed segment, 1,088 bytes
for 32, and 1,048,640 bytes at 32,768; failed admission occurs before allocation.
The normal capacity gate remains pending; the diagnostic results follow.

The separately scheduled CPU-idle same-volume experiment passes in 1.58 s. With
a 200 ms response delay after a real cold metadata call, old-placement authority
waits are 200.713/200.930 ms; off-authority waits are 0.000218/0.000203 ms.
Foreground durable ACKs are 203.922/204.147 ms versus 2.398/2.173 ms. Both arms
execute two actual metadata calls for the selected source, use zero raw cache,
and verify the same complete single-source body. All four children are killed
by signal 9 after ACK and reopen with the exact acknowledged value. This proves
lock-domain placement, not removal of source I/O, natural 200 ms FUSE latency,
or normal 1,000-conversation P99. Orphan/deletion I/O remains a separate boundary.

Cancellation review adds a gate before every selected metadata ID insertion:
reuse the mark visitor's stop check and 4,096-visit elapsed-budget check, then
recheck stop after insertion (which may flush a run). The root-ID visitor is
the gate before starting its fresh source read. Stopping at selection entry,
within index IDs, before root insertion, or during root insertion aborts before
that read or user-source scanning/deletion and releases the epoch inputs. A
logical root read already in progress may finish; its next page visitor observes
stop and aborts. Precise tests also check scratch cleanup, captured Arc release
and physical-lane availability. The earlier 5daf candidate and four diagnostics
remain original evidence; the cancellation-fixed source requires its own gates
and a newly bound paired diagnostic before delivery.

The cancellation-fixed source passes 28 GC neighbors (two ignored diagnostics)
in 96.34 s and the full native gate with 1,668 passes, six ignores and zero
failures in 114.92 s. Actual executable test/source digests match
`12e59b319eaaffb72d3f2eaae2ea362805090c243f3ade558a5a78266caeafee`.
The earlier four source-delay controls belong to 5daf and remain preserved.

The cancellation-fixed 12e59 source separately repeats the same-volume four-arm
control: authority waits 200.752/200.849 ms become 0.000324/0.000197 ms, and
durable ACKs 204.310/204.438 ms become 2.450/2.448 ms. Every arm retains two
actual target metadata calls, zero raw cache and the complete source validator.
All four signal-9 exits recover the exact ACK; the diagnostic finishes in
2.01 s. This newly binds the placement result to the cancellation-fixed source
and retains the same diagnostic-only limits and separate remaining orphan I/O.

### Idle catalog traversal

Problem. Catalog windows selected only by CONTROL generation can repeat forever on an idle authority, leaving other payload segments unverified and unreclaimed.

Decision. The online collector owns a volatile catalog-window cursor. It starts with the captured generation for the original first-window choice, then advances after two successfully completed epochs per window. Deferred, cancelled and failed attempts retain the cursor and confirmation phase. The existing fresh marks, exact dead-set comparison, relink fence, 16-segment scan bound and pacing floors remain mandatory. Catalog changes may invalidate a confirmation; exact segment identities and dead sets remain authoritative. Restart discards the cursor and previous marks together.

Alternatives considered. Waiting for foreground writes to rotate generation fails for idle databases. Rotating after every epoch discards the preceding window's suspects before its second confirmation. Retaining dead sets for every window would grow resident tracking; paired bounded windows keep the existing memory bound.

The `idle_epoch_sweeps_expired_retirement_without_another_catalog_publication` regression also verifies that an idle epoch removes a grace-expired parked predecessor without changing the catalog or losing live payloads. Its test-only clock helper ages existing parks; production retirement grace and durable bytes remain unchanged.

## Reference-only marks and non-admitting traversal reads (2026-10-30)

Problem. The mark read every reachable block's payload, including every
1 MiB blob chunk and every stream segment, only to record identities it
already had from the blob manifest or stream catalog. These reads went
through the shared block cache, so each attempt evicted the foreground
working set, and the collector thread had no stats component, so its I/O
was reported as foreground. The production daemon sampled on 2026-10-30
(a 421 GB payload volume on DolphinFS) reported `epochs_completed` 0 and
`deferred_epochs` 46. Every attempt hit the 900-second mark budget, and
nothing was reclaimed. The same snapshot attributed 41.8M `block_get`
calls (80,021 s, a 93% miss rate against an 11.6 GB cache) to foreground.
The online backup copy and its retention-prune mark walked the store the
same way; its last completed generation was 2026-10-21.

Decision. `backup::visit_semantic_value_references` yields the same block
identities as the payload visitor but reads only blob manifests, with the
same owner-scope and blob-identity checks. The online mark and backup prune
use it. Whole-store passes read without cache admission: segment rewrites
and consolidations and the backup copy through `BlockStore::get_traversal`,
the online mark and backup prune through `BlockStore::traversal_reader`
("Catalog location table for marks" below). Both reuse staged and cached
payloads and verify disk reads exactly as `get`, but neither admits nor
refreshes cache entries. Collector and online-backup threads report their stages as
`online_gc` and `online_backup` components. The offline collector and
`verify` keep their payload walks.

Backup copies are incremental in reads as well as writes.
`BlockStore::retain_held_block` recognizes a published loose file or a
catalogued segment entry without reading it, and records into the relink
fence as an idempotent `put` does. Loose publication renames a synced
temporary file, so a present file is complete. The copy still reads pages,
blob manifests and retained history segments from the source for their
references, but a referenced leaf the destination already holds is neither
read from the source nor read back from the destination. Previously,
`put` re-read and compared every held block, so each generation read the
whole store twice. A held destination block is therefore no longer
re-verified on each generation; restore and `tofu-db verify` remain the
byte-integrity checks.

Alternatives rejected. A longer mark budget or a smaller cache for the
collector would tune the symptom and keep the I/O. A separate cache for
background work adds a second byte budget; one pass over the store gains
nothing from caching. Resumable marks would need a durable partial mark
set with its own safety argument and remain a separate change.

Verification. `reachability_by_reference_reads_only_manifests_and_visits_the_same_blocks`
compares both visitors over a four-chunk document and three stream segments:
the same eight identities, with exactly one read (the manifest).
`online_mark_skips_leaf_payloads_and_admits_nothing_to_the_block_cache`
checks that every reachable value block stays marked and that no traversed
page or value block enters an empty cache. With hot caches disabled, the
mark needed 3 volume reads where a payload walk of the same values needed 9.
The ignored `mark_value_reads_measurement` walks one page graph of 16
four-chunk documents (pseudo-random hex text) and 4 three-segment streams
with each visitor in a debug build, with hot engine caches disabled and
operating-system caches left in place. Value reads fall from 92 blocks
(32.4 MB) to the 16 manifests (3.5 KB). On DolphinFS, three runs took 114.5,
44.6 and 43.9 ms with payloads versus 0.34, 0.32 and 0.30 ms by reference;
local XFS took 42–44 ms versus 0.29–0.30 ms. These are component timings,
not production epoch durations.
`traversal_reads_reuse_cached_payloads_without_admission_or_recency` and two
driver attribution tests cover the read and component contracts.
`unchanged_generation_reads_no_held_blob_chunk_from_either_store` takes a
second online-backup generation of an unchanged four-chunk document: no
chunk is read from either directory, and source payload-segment reads total
398 bytes. Each of these tests fails when its part of the change is
reverted. The production effect still has to
be observed on a rebuilt engine through `epochs_completed`, `online_gc`
`get_unc_ok` and the foreground `block_get` totals.

## Superseded payload-segment reclamation independent of block marks (2026-10-30)

Problem. The main authority's `payload-segments` directory held 49,543 files
(4.64 TB) while the installed catalog referenced at most 4,096. Files arrived
at 750–820 per hour, about 135 MB each (11.4k files on 2026-10-30 by 15:50).
A four-minute `ls` sample showed 48 files created (bursts of eight, one burst
every 30–37 s) and only 4 removed. Three code facts explain it:

- At the 4,096-segment ceiling, automatic history compaction promotes each
  retired history source by rewriting the smallest fitting catalog segment
  plus the batch. The replaced segment was never parked: the comment left it
  to "ordinary CONTROL-aware GC", because the off-authority publication wrote
  one CONTROL generation and the fallback slot still named it. The live
  daemon's 32 MiB checkpoint batch over 4 MiB history sources makes each
  automatic slice retire eight sources: the burst of eight.
- Online orphan reclamation planned one file per epoch and ran only after
  the block mark build. The main authority's mark aborts at its 900 s budget
  on every epoch, so no orphan was ever removed; a healthy collector would
  still have removed at most one file per epoch (at least 120 s apart).
- Restarts drop the in-memory retirement set, and a catalog-full synchronous
  adoption leaves its unused background segment; both also rely on the
  orphan path.

Checkpoint merges and pack adoption park what they replace, and their sweep
runs after every catalog publication, so they were not the leak.

Decision.

- History compaction publishes the new catalog, then republishes it into the
  fallback CONTROL slot (one extra CONTROL write, outside the authority
  mutex), installs it, and parks every catalog segment the slice replaced,
  including promotions superseded within the same slice. This is the parked
  retirement that checkpoint merges and pack adoption already use; a failed
  park leaves an unreferenced file for the orphan lane.
- The collector reclaims superseded segment files in a lane that never reads
  a block mark. After the snapshot hold it sweeps parked retirements past
  their reader floor (without waiting for another publication), observes
  every orphan file outside the mutex, and removes each file that the
  previous observation saw with the same name and size. There is no
  per-epoch file cap. Observations are at least one epoch interval
  (≥120 s) apart; a failed observation clears the memory, so confirmation
  restarts from two fresh observations.
- Each removal takes its own short `try_lock` hold and re-validates: the
  authority is usable, no publication owns the physical lane, the installed
  catalog and the protected set (unpublished packs, parked retirements) do
  not name the file, no pack is active for a temporary file, and, for a
  complete segment file, both durable CONTROL slots name only the installed
  catalog. `ControlFile` now
  remembers the fallback slot's state, so this check needs no I/O; when the
  fallback still names an older catalog the hold republishes both slots
  first, as the offline round does. Holds are released between files; a
  contended or refused hold is retried after the run's backoff ("Reclamation
  keeps its pace under foreground load"). One directory
  barrier after the batch makes the unlinks durable; a crash before it can
  only resurrect unreferenced files.

Why each removal stays safe. A file in the installed catalog, an unpublished
or publishing pack, or the parked set is refused under the hold. Files an
in-flight off-authority publication builds or may still publish (history
promotion, full-catalog pack adoption merges) are refused while
`require_physical_maintenance_available` reports the lane busy, and a
latched restart refuses everything. A durable slot
naming another catalog is stabilized first. Backup anchors and readers
resolve blocks through catalogs, never orphan files; readers of a dropped
catalog keep the existing parked reader floor, and an orphan is at least one
epoch interval old when removed. New catalog members always have fresh
segment IDs, so a file proven unreferenced cannot be re-referenced later.

Alternatives rejected. Running the orphan step before marks but keeping
one file per epoch would need 62 days even at the 120 s floor (about
2.6 years at the live 30-minute cadence) for the existing 45k files, while
the producer adds about 19k files a day. A longer mark budget tunes the
symptom (the mark proof is irrelevant to segment files). Persisting the retirement set would add a durable structure that
the orphan observation already reconstructs. Unlinking replaced sources
directly in the history publication would take away the reader floor that
every other catalog publisher gives stale snapshot readers.

Verification (deterministic, `DeterministicVfs`).
`superseded_segments_stay_bounded_while_every_mark_build_aborts` makes every
mark fail with the 900 s budget error while four superseded files arrive per
epoch: before the change the unreferenced count after epochs 1–6 was
4, 8, 12, 16, 20, 24; now it is 4 in every epoch and 0 one epoch after
production stops (24 files removed, no epoch completed).
`restart_dropped_retirements_are_reclaimed_although_marks_abort` parks eight
retired segments (protected for three epochs), restarts, and removes all
eight in the second epoch; before the change 9 files remained instead of 1.
`full_catalog_history_compaction_parks_replaced_sources_after_both_slots_move`
checks the full-catalog promotion: both slots name the new catalog, the
replaced source is parked and the sweep returns the directory to its prior
file count (before the change the fallback slot still named the old catalog
and nothing was parked, so each promotion left one file behind).
`every_full_catalog_history_compaction_fault_keeps_slot_named_segments`
fails each durable operation from the first CONTROL write on (error, short
write, lost sync) and sweeps immediately: every segment a durable slot names
exists after reopen, with acknowledged payloads intact. The stop flag is
checked before each observed file
(`stop_during_orphan_observation_removes_nothing_and_restarts_confirmation`).
`racing_background_pack_files_survive_the_orphan_lane` creates a pack between
the snapshot hold and the listing, and
`orphan_named_by_the_fallback_control_slot_is_unlinked_only_after_both_slots_move`
crashes between a retirement's publication and its stabilization, then
corrupts the newest slot after reclamation; recovery still resolves every
catalog segment. The existing mark and delete fault campaigns now also
cover the lane's listing, unlink and directory-barrier operations.

Remaining cost. Full-catalog promotion rewrote the smallest fitting segment
for each retired source (about 135 MB per source of at most 4 MiB on the live
volume, roughly 31 MB/s). Since 2026-10-31 a slice's sources share one
replacement (`docs/STORAGE_TOFUDB.md`, "History promotion: one segment write
per slice"), so a live slice writes about 168 MiB instead of 1,120 MiB and
seals one file instead of eight. Only marks that finish can free catalog
slots; until then every slice still rewrites one segment of the full catalog.

## Search mounting verification

The deterministic runtime regression performs 24 generation replacements and
stages one abandoned publication block per generation. Three shared collector
epochs reduce loose files from 27 to 3, reclaiming exactly 24 blocks / 2,054
bytes; current hits survive both collection and reopen. This proves projection
mounting and orphan reclamation, not the original incident's unreachable-byte
ratio or a production throughput claim. Runtime object size is 2,480 bytes
(including the opened index), collector size 376 bytes; the additional selected
index driver reserves the existing 1 MiB stack and creates scratch lazily.
The regression also covers driver stop/join, capacity loss without file writes,
independent source writes, a deferred missing index, source admission and
recovery of capacity with the installed GC handle.

## Loose compaction rate and planning probes (2026-10-31)

Problem. Loose compaction is the only online path that turns loose block
files into payload segments, and every loose file costs a directory entry, an
inode and a metadata operation on every scan. Each epoch used to plan at most
one compaction segment (`MAX_SEGMENT_BLOCKS` blocks and the temporary-byte
limit) in scan order and left every later live loose block for a later epoch.
Epochs start at least `ONLINE_GC_MIN_EPOCH_INTERVAL` apart with a 50% duty
cycle, so a backlog of millions of loose blocks took days to weeks to pack
(see the measurement in
[storage operations](STORAGE_OPERATIONS.md#loose-block-growth-and-online-compaction)).
The planning probes have their own cost: on 2026-10-30 the search-projection
collector (17.5M loose blocks, 338 segments) spent about 7 of every 9 s per
68k-block shard in the per-block loop, because it asked
`has_catalogued_payload_block` and opened every live loose block for its size.
After the second catalog publication of a process the packed filter no longer
covers the installed catalog, so each probe falls through to candidate segment
indexes, and the default 64 MiB open-segment cache held about a fifth of the
projection's ~300 MB of indexes.

Decision. A completed scan packs every live loose block it finds into as many
segments as the format limits require. The scan keeps one open segment. For a
live block (marked, or the captured catalog manifest) it first runs one exact
catalog membership probe: a block the catalog already holds is a redundant
copy, removed once its shard is scanned (see "Redundant loose copies of
catalogued blocks"); any other is sized and admitted when its file fits a
segment under the byte limit `min(temporary budget,
MAX_SEGMENT_PAYLOAD_BYTES)`. When the next admitted block would exceed
`MAX_SEGMENT_BLOCKS` or that byte limit, the open segment is packed before the
block joins the next one: it is built and verified off the authority mutex (the
stop flag is checked before every source read and between segments) and
published under one short `try_lock` hold through `online_gc_finish_compaction`,
which checks the physical publication lane and publishes the catalog with
`publish_payload_manifest` (both CONTROL slots name it on return); only then
are the loose copies removed, one short hold each (rule S8; see "One file per
deletion hold"). The epoch's trailing segment still needs
the 128-block floor and packs in the deletion phase after the loose,
consolidation and rewrite holds. Candidate memory is one segment's IDs (at most
65,536, 2 MiB), bounded by the format rather than by a count or batch setting.
Compaction takes a catalog slot only while the catalog stays below the
catalog-pressure floor afterwards, checked before each build and again under
the hold: it is optional work, so it never consumes the slots history promotion
and pack adoption need, nor creates pressure that consolidation would have to
undo. No block is sized once compaction is closed for the epoch (the
membership probe still runs, for redundant copies). `WouldBlock`
(hold, lane or catalog contended) ends compaction for the epoch and discards
the built file, because it proves nothing was published; any other error ends
compaction only, the scan still completes its suspects, and the error is
returned after the deletion holds. Marks, suspects, victims and the two-mark
rule are unchanged. Over the life of a backlog every live loose block is still
sized once, in the epoch that packs it, and every scan probes its membership.

Alternatives considered. Spilling the whole candidate list to temporary runs
and packing it in the deletion phase reads every run once per shard
(`SpillMarkSet::live_for_shard`) and adds a third temporary spill beside the
marks and suspects. Re-listing the shards in the deletion phase doubles the
listing, the dominant per-scan cost. A per-epoch segment count or batch size is
a tuning knob. Filling the catalog to the 4,096 ceiling would let one epoch take
every remaining slot with full-size segments that consolidation cannot merge,
forcing history promotion into its full-catalog rewrite mode. Caching every
catalogued index grows memory with the catalog and only hides the redundant
probe; stopping the shard scan early would lose suspect tracking, which needs
every loose ID; a persisted compaction cursor adds state for an order the scan
already provides.

Verification.
`one_epoch_packs_a_loose_backlog_into_every_segment_the_limits_require` (640
live 16 KiB loose blocks, 128 per segment) counts per epoch (segments
published, backlog blocks still loose): `[(5, 0)]`, where the single-segment
collector measured `[(1, 512), (1, 384), (1, 257), (1, 129), (1, 2), (0, 2), …]`
(five epochs, and two blocks stranded behind the floor); recovery from either
CONTROL slot reads the whole backlog.
`compaction_failing_between_a_middle_segments_control_publications_keeps_its_loose_copies`
fails the middle segment's stabilizing CONTROL write of a three-segment epoch
(6 CONTROL writes, 2 per segment): exactly that segment's copies stay loose and
both slots recover the backlog.
`scan_packs_the_segment_it_closes_and_sizes_each_later_block_once`,
`scan_probes_catalog_membership_for_every_block_a_segment_can_hold` and
`scan_skips_compaction_probes_while_no_catalog_slot_stays_below_the_pressure_floor`
pin the probe rules (their fixture notes packed-filter bits for its loose
blocks, standing in for retired segments' stale bits and filter false
positives, because the filter now covers every publication): one size probe
per live block, membership proven before admission, and a closed compaction
opens no live block (its membership probes still run, for redundant copies).

## Consolidation victims are parked, not unlinked (2026-10-31)

Problem. `Engine::finish_segment_consolidation` unlinked its victim segments
inside the publishing hold, while the single-segment rewrite, history promotion
and pack adoption park what they replace. Crash recovery never needed the
victims: the unlink followed `publish_payload_manifest`, which returns only
after both CONTROL slots name the new catalog, and a failed stabilizing write
returns before any unlink. Readers off the authority mutex did need them:
`remove_segment` evicts the idle read handles, so a reader holding a victim's
segment index re-opens the unlinked path and fails with `NotFound`, and
`has_catalogued_payload_block` (the online backup's presence check) has no
changed-view retry and reports "CONTROL payload manifest references a missing
segment" for candidates taken before the publication.

Decision. After the publication every victim is parked through
`PayloadSegmentStore::defer_segment_retirement`, exactly like the rewrite path:
parked victims stay in `protected_segment_ids`, the publication and
segment-lane sweeps unlink them once the existing retirement floor expires, and
the size check is unchanged. Parking happens only after both slots name the new
catalog.

Alternatives considered. A changed-view retry in every off-mutex reader leaves
held segment indexes broken and spreads one rule across call sites; a separate
consolidation floor would be a new knob.

Verification. `consolidation_failing_between_its_control_publications_keeps_every_victim`
fails the stabilizing write: the fallback slot names every victim, all of them
exist, and losing the newest slot reopens a catalog that resolves.
`consolidation_victims_stay_readable_through_handles_held_across_the_publication`
holds victim indexes across a pressured consolidation; the unlinking version
failed its first read with `NotFound`. The reads now succeed, both slots name
the new catalog, the victims are parked, the expired floor reclaims them, and
losing the newest slot still selects a catalog that resolves.

## Catalog location table for marks (2026-10-31)

Problem. The main authority's mark build still never completed: every attempt
hit the 900 s budget, so loose reclamation, compaction planning and
catalog-pressure consolidation never ran. Sampled live on 2026-10-30/31, the
mark marked 431-437 blocks per second (one 65,536-ID spill run every ~152 s,
about six runs per attempt) while its thread ran at 87-99% CPU on about
220-450 whole verified segment-index opens per second (1.34 MiB and about 4 ms
each; 637 MB/s and 5,135 read calls/s in one 60 s sample). Each lookup also
walked all ~4,091 catalogued witnesses, because content-hashed IDs make every
segment span the whole ID space. Removing the index re-reads alone would have
left that walk at about 0.3-1 ms of CPU per lookup.

Decision. `build_marks` builds one `BlockStore::traversal_reader` per mark
(docs/TOFUDB_BLOCK_READ_PATH.md, "Catalog location table for whole-store
passes"): every catalogued index is streamed and verified once into an exact
in-memory table, and each page or manifest the mark visits then costs a
binary search, at most one positioned index-entry read and its verified
payload read. The table build checks the stop flag before every segment and
between sort steps, and is logged once per mark (`online_gc` `step=mark_location_table`, with located
blocks, unindexed segments, resident bytes and elapsed time). Backup-prune
marks use the same reader. Blocks the table does not hold go to the loose
file and then the exact catalog path, which alone can report a block missing,
so a mark never skips a reachable block and every integrity failure still
aborts the attempt with its original type. The table
is dropped with the mark: about 949 MB for the live 118.6M catalogued blocks,
at most 2 GiB for a full catalog of full segments.

Alternatives rejected. Breadth-first waves that read each index once per wave
avoid the table's memory but re-read about 5.5 GB per tree level and
restructure the visitor and its witnesses. Positioned searches of each
candidate's on-disk index keep the per-lookup witness walk. A larger index
cache is tuning and keeps the work.

Verification. `located_mark_reads_each_index_once_and_one_candidate_per_block_with_identical_marks`
re-homes an authority's 145 marked blocks into 32 shuffled full-range segments
with filler, gives the verified-index cache a quarter of the index bytes, and
counts every segment read by region. The exact catalog path needed 122
whole-index reads (0.84 per marked block), 588,384 index bytes and 4,053
witness checks for 130 segment payload reads. The located mark read each of
the 23 non-resident indexes once (the 9 resident ones not at all), then 95
index entries, 130 payloads and exactly one table candidate per payload, with
no witness check; marks, payload bytes and loose opens are identical, and no
index or payload is admitted to a cache.

Projected live effect. The table build costs about 21 s (4,091 verified index
reads at the live ~4 ms per open, plus a 4.8 s sort measured at the live
shape). After it, per marked block the mark keeps only the payload or loose read
it already performed: a cold located read cost 0.21 ms on this DolphinFS volume
(`traversal_reader_measurement`, against 6.1-6.3 ms through the exact path),
plus microseconds of CPU, and leaf references need no read at all. That raises
the rate from about 434 to at least about 4,500 marked blocks per second. The
projected duration of a complete live mark is in "Marks run to completion"
below.

## Loose deletion and the fallback CONTROL slot (2026-10-31)

Problem. `ControlFile::read_current` selects the newest valid slot; when that
slot is unreadable after it was durably written, recovery selects the other
slot and replays the WAL chain that slot names. What that path needs depends
on what the older slot names:

- The same WAL chain and checkpoint, differing only in generation and durable
  tip: recovery replays the same retained WAL and `Engine::open` re-derives
  the authority root from the newest root update in that suffix, so it needs
  exactly what the current state needs.
- An older WAL generation (after a checkpoint rotation or a sealed-prefix
  install): recovery from it fails closed ("unreferenced successor WAL
  contains records", or the retired predecessor file is gone) as soon as the
  successor WAL holds a record or the predecessor is retired, which follows
  the publication promptly.
- The same WAL chain with another history manifest or payload catalog: this
  is reachable. History pruning that changes no payload catalog (retired
  segments without carried payloads, or promotions that fall back to loose
  blocks because no catalog segment fits at the 4,096-segment ceiling)
  publishes one CONTROL generation; the older slot names the previous
  manifest, its routing root and the retired segments until the next CONTROL
  publication, and no mark includes them. Catalog rewrites,
  consolidations and compactions publish through `publish_payload_manifest`,
  which stabilizes both slots before parking or removing anything, but loose
  deletion publishes nothing. In a deterministic fixture two epochs unlinked
  the older slot's manifest (seven blocks reclaimed) and reopening from that
  slot failed with NotFound for exactly that manifest block. When the same
  epoch also rewrote a segment, the rewrite's stabilization ran after the
  loose unlinks, so a crash between them left the same state.

Decision. Before each loose unlink, its deletion hold asks
`ControlFile::slots_name_only_physical_roots(state)`, decided from the
in-memory slot states: every selectable slot must name the current active and
sealed WAL generations, checkpoint, history manifest and payload catalog,
with a root witness. Otherwise the hold republishes the current state
(`stabilize_control_slots`) and checks again; unknown slots after a failed
write answer false, and the authority is already latched for restart then.
Garbage only the older slot named becomes unreferenced by both slots and is
reclaimed in the same deletion run, so nothing is retained for it. Since
loose victims take one hold each, a publication between two of them that
leaves the older slot behind is caught by the next hold ("One file per
deletion hold").

Alternatives rejected. Folding the older slot's roots into the mark capture,
like pinned roots, moves the cost into every mark: that slot's history
manifest is only a block ID in memory, so its segments, routing metadata and
retired history would have to be read and decoded off the hold, and its
authority root differs after almost every CONTROL catch-up, adding another
root to every mark. It would also retain pages recovery never reads
(the root is re-derived from the WAL), and keep the older slot's garbage
until some later publication moves that slot, indefinitely on an idle
authority. Stabilizing unconditionally
before every batch, as the offline round does, writes CONTROL on every
deleting epoch even when the slots agree. Publishing every single-publication
physical change twice (rotation, sealed install, history pruning) puts one
more CONTROL write and barrier on the rotation install, which fences writers,
whether or not the collector ever deletes.

Cost. No I/O while the slots agree, which every double publication (catalog
publications, pack adoptions, the stabilization itself) restores. Otherwise
one CONTROL slot write with its two-sync barrier (plus the WAL barrier every
publication performs in Normal mode) per deleting epoch, under the first hold
that unlinks a victim, plus one per publication that leaves the older slot
behind during the deletion run; epoch starts are at least 120 s apart. After a
rotation the next deleting epoch pays it once although recovery from the
retired-WAL slot already failed closed; the republication makes the older
slot a working recovery path again.

Verification. `loose_deletion_keeps_every_block_the_fallback_control_slot_recovers_from`:
before collection the older slot recovers its four-segment manifest and the
value; the deleting epoch now writes CONTROL exactly once, both slots name the
current manifest, the old manifest is reclaimed, and a reopen from the older
slot reads the value and every history segment; a later deleting epoch writes
CONTROL zero times. `crash_before_the_epochs_catalog_publication_keeps_the_fallback_slot_recoverable`
fails every CONTROL write of the deleting epoch in the rewrite shape: nothing
is unlinked and the older slot recovers. Both failed on the previous code:
reopening from the older slot hit NotFound for a block that slot names.
`every_online_epoch_fallback_stabilization_fault_keeps_both_slots_recoverable`
injects an error, short write or dropped sync at each of the deleting epoch's
200 operations (233 cases): whenever the crash image keeps two valid slots the
older one recovers, and reopening plus a fresh collector converge. Moving the
unlink ahead of the stabilization fails both ordering specifications.

## Segment-lane failures never stop block marks (2026-10-31)

Problem. The superseded-segment lane runs before the mark build. Any error it
returned (an unlink or directory-barrier failure, or an integrity error such
as `payload segment directory has an unknown entry`) abandoned the whole epoch
before marking and recurred every epoch, so one foreign file in
`payload-segments` stopped all block reclamation while the only trace was
`deferred_epochs`. The parked-retirement sweep discarded its errors.

Decision. The lane returns `Err` only for a stop or another `WouldBlock`, which
keep their meaning: a stop abandons the epoch before marking, and contention
in a removal hold defers the remaining files. Any other error ends only the
lane's pass: removals before it stay counted, the observation memory clears
(two fresh observations confirm again), and the epoch continues with its
marks and deletions. The collector counts failed passes in
`segment_lane_failures` and keeps the latest message in `segment_lane_error`
until a pass completes again; one `online_gc` warning marks each change of the
reported error and one info line its recovery, so a persistent failure is
logged once. Sweep failures are reported the same way and keep their parks.
Integrity errors still fail the lane: unknown entries are neither skipped nor
removed. `system.health` readiness is unchanged, because a restart cannot
repair a directory integrity error and readiness false recycles the daemon.

Alternatives rejected. Skipping unknown entries hides an integrity violation
and guesses about a file the directory contract does not allow. Flipping
readiness restarts the daemon in a loop without repairing anything. Logging
every failed epoch repeats the same line at the epoch cadence.

Verification. `segment_lane_integrity_failure_stays_visible_while_block_marks_continue`
places an unknown file: both epochs complete their marks and remove a loose
orphan, the failure count reaches 2 with the unknown-entry message, the
directory keeps every file, and after the file is removed the next pass clears
the error and the following one removes both superseded files.
`segment_unlink_failure_is_recorded_and_retried_from_fresh_observations`
fails the second unlink: the epoch completes, one removal counts, and the two
other files wait for two fresh observations. Under the previous rule both
epochs returned their lane error (`InvalidData` / `PermissionDenied`) before
marking. The stop specification also asserts that a stop builds no mark and
records no lane failure.

## Publication windows stay marked (2026-10-31)

Problem. The capture read only `committed` and `staged_history`. A WAL
rotation moves its covered prefix into `rotation_read_batches` and its staged
history into the prepared snapshot, and `encode_rotation_segments` writes the
new history segments, payload indexes and routing pages as loose blocks
outside the authority mutex; history compaction and pack adoption likewise
write routing pages, history manifests and catalog roots before their install.
A mark captured inside such a window included none of them. Two epochs inside
one rotation window reclaimed four blocks, after which the installed manifest
named a missing segment; an epoch whose capture saw a history-compaction
window and whose deletion hold followed the install reclaimed fifteen blocks,
the installed history manifest among them. The windows have no fixed length:
encoding takes as long as the volume needs, and a rotation that finishes
while a pack adoption owns the lane parks as `deferred_rotation` until it
ends. Under write load rotations follow each other every few seconds
(`daemon::run_rotation_driver`), and on the live store history-compaction
publications held the lane back to back, one every 30–37 s.

Decision. `BlockStore` keeps one set per open publication window. The engine
opens the rotation's window at preparation, seeded with the staged history the
snapshot took, and closes it in the same authority hold that installs the new
manifest or restores an aborted snapshot; an off-mutex history or catalog
publication's window spans exactly `async_publication_in_flight`.
`put_loose` registers each block it handles into every open window before the
file can appear, including idempotent republishes, so no scan lists an
unregistered window file. Every capture marks the union of open windows and
the transactions of `rotation_read_batches`. Epochs keep running during
windows.

Alternatives rejected. Deferring the capture while a rotation is in flight
starves the collector whenever windows run back to back and still misses
history-compaction and pack-adoption windows. Deferring only the
deletion when a window was open at capture starves the same way. Registering
inside each encoder threads a registry through every history and routing
writer. Re-encoding the covered prefix during the mark to predict segment IDs
costs a second encode per epoch and still misses routing pages.

Cost. One mutex acquisition per loose write, plus one set insertion per open
window. A set holds the loose blocks written while its window is open: for a
60 MiB rotation about fifteen history segments of at most 4 MiB, their
payload indexes and the routing pages it appends; a full-catalog history
compaction adds the loose fallback copies of one slice. Each set is dropped
when its window closes, and captures copy the union.

Verification. `epochs_inside_a_rotation_window_keep_its_outputs_and_still_reclaim`:
two epochs inside one rotation window complete without deferral and remove an
orphan written before the window, and after the install every segment, the
values and a crash-image reopen are intact.
`epoch_spanning_a_history_compaction_install_keeps_the_installed_history`
installs between an epoch's capture and its deletion hold: the pruned history
is reclaimed and the installed manifest survives.
`pack_adoption_window_marks_its_catalog_root_until_it_installs` and
`aborted_rotation_closes_its_window` pin the window lifecycle. Removing the
fold from the capture fails the first three.

## Unlinked blocks never confirm from older suspects (2026-10-31)

Problem. Blocks are named by their content, and the relink fence (S7)
records only reference-without-write decisions. Two writes produce a
suspect's name again without one:

- A block one epoch unlinked can be written again as a fresh loose file: an
  oversized commit spilling its staged copy, the checkpoint's loose fallback
  at a full catalog, any `put_loose` publisher. `scan_loose_shards` inserts
  every unmarked loose block into the next suspect set before it checks the
  previous one, so the blocks epoch N unlinked stayed in the suspects epoch
  N+1 confirmed from. A commit that wrote such a block again after epoch
  N+1's capture was missing from that mark, the stale entry confirmed the
  new file, and the hold unlinked it while committed state referenced it.
  A failed epoch kept the older suspect set even after its own hold had
  unlinked some of its entries.
- A blob write (`put_deferred_blob`, prepared blob publication) stages
  complete bytes without probing for a loose duplicate, so staging the
  content of a garbage loose block records nothing in the fence. When the
  next epoch confirmed that file, `remove_blocks` dropped the staged copy
  together with it.

Both lost committed data in deterministic fixtures: the block read failed
with `content block … is missing` after the deleting epoch, and after a
crash-image reopen (an oversized commit names its blocks by identifier only;
a blob's carried WAL copy survives a restart but not the checkpoint that
retires its record). The search projection runs the same collector and
completes epochs on the live host.

Decision.

- The loose deletion hold drops every victim with a staged copy (pending
  payloads or the rotation-draining prefix) before it consults the fence.
  The order is load-bearing: materialization records the fence before it
  retires a staged copy, so one of the two checks sees every interleaving.
  A skipped file stays a suspect; once materialized it is reused (fence) or
  packed, and the next mark includes the committed reference.
- The hold reports every victim whose file it unlinked, found missing, or
  may have unlinked (an unlink error after which the file is gone), also
  when the unlink fails (`BlockStore::remove_block_noting_absent`).
  The collector keeps exactly those IDs beside the suspects they invalidate
  and skips them at confirmation, so a recreated block confirms only from a
  scan that listed the new file, plus the mark after it.
- Once a scan completes, its suspects become the previous set even when a
  later hold of that epoch fails, so the exclusion list covers exactly one
  epoch's deletion run: at most one round's victims (65,536 IDs, 2 MiB),
  released at the next rotation.

The other two-observation confirmations need no exclusion. Temporary block
files and orphan segment files are named by fresh random UUIDs, so a removed
name never reappears. Segment dead sets are keyed by catalog segment ID, a
retired segment never returns to the catalog, and a dead block stays
readable through its segment until the publish, so every re-reference is a
probe hit (fence) or a fresh staging that a rewrite never touches.

Alternatives rejected. A file-age grace (the never-implemented
`ONLINE_GC_CANDIDATE_MIN_AGE`) adds a wall-clock threshold and trusts the
volume's mtimes. Leaving confirmed victims out of the next suspect set at
scan time delays every victim the hold did not unlink (contention, fence,
CONTROL stabilization) by a second epoch. Recording the whole batch delays a
victim whose unlink failed by an extra epoch. Accumulating exclusions across
failed epochs has no per-round bound. Recording every staging in the fence
makes it grow with all writes instead of re-references.

Verification (`authority_gc_online/tests_rebirth.rs`).
`block_reborn_after_its_unlink_survives_the_next_epoch`: epoch 2 unlinks
garbage X and a commit writes X again after epoch 3's capture. Before the
change epoch 3 reclaimed it (reclaimed blocks 1 → 2) and the committed
block was missing, live and after a crash-image reopen; now epochs 3 and 4
reclaim nothing and X reads back in both.
`block_reborn_after_a_failed_epochs_unlink_survives_the_retry` fails epoch
2's shard barrier after the unlink; the retry keeps X.
`garbage_block_restaged_by_a_blob_write_survives_its_confirmation` reads X
back live, after a checkpoint and after a crash-image reopen, with nothing
reclaimed. All three failed on the previous code, and
`online_gc_loose_batch_mid_removal_fault_recovers_remaining_victims` still
removes the victim whose unlink failed in the very next epoch.

## Exact relink fence per epoch window (2026-10-31)

Problem. The relink fence held at most 16,384 IDs (`MAX_RELINK_FENCE_IDS`).
The 16,385th distinct re-reference after a capture set an overflow flag,
and every deletion hold of that epoch returned `WouldBlock`, so a store
whose foreground re-references that many blocks while one multi-minute
epoch runs never reclaimed anything. The cap was a memory knob, not a
protocol or correctness bound. The fence also recorded between epochs,
although the next capture cleared those entries before any hold read them,
and every hold cloned the whole set under the authority mutex.

Decision. The capture hold opens an empty set; reference-without-write
decisions insert into it while it is open; the collector drops it right
after the epoch's last deletion hold, or when it abandons the epoch (the
capture runs last in its hold, so every successful capture is closed).
Memory is exactly the distinct blocks re-referenced inside one epoch window
and is released when the window closes; nothing is recorded between
windows. Holds query the set under its lock (`retain_unrelinked`,
`any_relinked`) instead of cloning it, and fail closed with `WouldBlock`
when no window is open. Every hold still consults exactly the decisions
made since its capture.

Alternatives rejected. A larger cap only moves the starvation point.
Filtering insertions by suspect membership needs spill-set lookups on the
write path. A clone per hold costs O(fence) under the authority mutex once
the cap is gone.

Cost. One hash-set entry (a 32-byte ID plus table overhead) per distinct
block re-referenced inside a window, on the existing insert path. Outside a
window `note_relink` takes the fence mutex and records nothing.

Verification. `relink_fence_keeps_every_dedupe_hit_of_an_epoch_window_and_still_filters`
makes 16,385 dedupe hits and re-references one garbage loose block after a
capture: the fence holds exactly 16,386 entries, and the hold unlinks the
other garbage block and keeps the re-referenced one. With the previous
fence the same steps kept 16,384 IDs with the overflow flag set, and the
hold returned `WouldBlock` with nothing reclaimed. After an epoch the
fence is closed and a dedupe hit records nothing.

## Pages shared by several roots are walked once per mark (2026-10-31)

Problem. A mark walked the current root, every transient pin root and every
backup anchor separately, and each of those walks walked every persistent pin
root it discovered again. The roots of one authority share almost every page,
so a mark read and inserted each shared page once per root plus once per walk
of each persistent pin. In the shared-page fixture below a page was read 18
times by one mark. At live scale this multiplied the work of a mark that
already took tens of minutes, and the duplicate inserts alone exceeded the
33.5M-insert spill cap a mark then had (since removed, see "Mark spill sized
by its inserts").

Decision. `build_marks` walks all of its roots with
`entity::visit_entity_page_graphs_once`, which shares one map of walked pages
across them:

- A page ID fixes the page's bytes and therefore its whole subtree, so a page
  already walked is not read, marked or descended into again.
- Every root's graph is walked with persistent-root discovery first. The
  persistent pin roots and range mounts those walks reach need no discovery
  and are deferred until all roots are done, so a page is never first walked
  without discovery and then needed with it.
- The map's key includes the discovery mode anyway: a walk without discovery
  never satisfies one that needs it, and such a page would be read again
  (not marked again) to find the pins it records.
- Each page walked with discovery keeps the pin-catalog tally of its subtree
  (declared count record and pin records), taken when its subtree leaves the
  DFS frontier. A skipped page's tally joins the walk's, so every root still
  compares its declared pin count with all of its pin records, and malformed
  pin records still fail on the first walk that reads them.
- Levels and key ranges are checked on the first path that reaches a page. A
  corrupt page still fails on its first visit. A second, inconsistent edge to
  an already walked page is not checked again; it cannot drop a mark (same ID,
  same subtree), and offline `verify`, which walks every root on its own,
  remains the integrity authority.

The explicit single-root walk used by `verify`, backup copy and the offline
collector is unchanged. Each completed mark logs one `online_gc`
`step=mark_built` line: roots, walked pages, skipped page references,
inserts, spill runs and bytes, elapsed time.

Cost. The map holds one 33-byte entry per walked page (ID and discovery flag)
in a hash table at 44-88% load, about 40-80 bytes per page: about 0.6 GB for
the 10-11M pages projected below. Tallies are kept only for pages above pin or
count records, and the deferred list holds one 32-byte ID per persistent pin
root or mount found. All three are dropped with the mark.

Alternatives rejected. Re-walking only subtrees whose path range can hold pin
records re-reads the right spine and every pin leaf for every root, and
trusts path bounds that a skip never checks. Diffing each root against one
reference tree removes only the sharing with that tree and restructures the
visitor. A visited set of truncated IDs is not exact: a false hit would drop a
reachable page. A visited set of blob manifests would remove only the
manifest reads of distinct pages that reference one blob, which follow churn.

Verification. `mark_reads_and_inserts_each_page_shared_by_pinned_roots_once`
pins 8 roots behind open transactions, each one single-record update apart,
with one persistent pin declared by every root, and compares one mark with
walking each root on its own (the previous mark): catalogued payload reads
fall from 2,287 (8.57 MB) to 139 (0.49 MB) and inserts from 2,502 to 181,
exactly one read per distinct page and per blob reference of a distinct page,
while the marks still cover every root. The entity specifications
`reachability_once_reads_each_shared_page_once_and_checks_every_roots_pin_count`
(six roots declaring 200 and 201 pins; the second root validates only through
kept tallies, and a last root with a wrong count still fails),
`reachability_once_discovers_the_pins_of_a_root_that_another_pin_reaches`,
`page_walked_without_discovery_is_walked_again_by_a_walk_that_discovers`,
`reachability_once_rejects_a_transplant_on_its_first_walk` and
`pin_leaf_shared_with_a_persistent_pin_tree_still_marks_the_root_it_names`
(the only path to a deleted record is a pin recorded in a pin leaf that both a
pin walk and its own root reach) pin the rules above. Letting a walk without
discovery satisfy one that needs it leaves pin `c` undiscovered; walking pin
roots inline instead of deferring them reads 148 instead of 139 blocks;
skipping pages without their tallies fails the second root's pin count.

## Marks run to completion (2026-10-31)

Problem. A mark build that ran longer than 900 s aborted, and the next epoch
started a new mark from scratch. A mark that needs longer, as the main
authority's does (below), could therefore never complete: a livelock that
also stopped loose reclamation, compaction planning and catalog-pressure
consolidation. The budget was described as safe because an aborted epoch
holds nothing, which is also why the budget is not needed.

Decision. Marks have no wall-clock budget. Nothing a mark holds or
accumulates depends on how long it runs:

- The capture copies root IDs; it holds no snapshot lease, so readers and
  backups release their roots as usual.
- Open publication windows are copied at capture; each window drops its own
  set when it installs or aborts.
- History segments are named by block ID, and only this lane deletes blocks.
  A payload segment retired during the mark makes its located reads fall back
  to the loose file and the exact catalog path.
- The location table, the walked-page map and the spill are per mark,
  bounded by the captured catalog, the distinct pages and the mark's inserts,
  and dropped with the mark.
- Deletion safety never depended on mark duration: confirmation needs two
  consecutive marks, publication windows and the relink fence cover what
  happens between a capture and its deletion hold, and both CONTROL slots are
  re-validated under that hold.

The relink fence does grow with the epoch: it records every
reference-without-write decision from the capture to the last deletion hold.
It is exact and has no capacity ("Exact relink fence per epoch window"
above), so a long mark costs memory proportional to those decisions but
never defers or abandons the epoch. The stop flag is checked before every mark read, including
each segment of the location table build, and before every mark insert.
Epoch pacing is unchanged: starts stay at least 120 s apart and the collector
never exceeds a 50% duty cycle, so a 60-minute epoch is followed by at least
60 idle minutes.

Alternatives rejected. A longer or volume-derived budget is a knob that still
livelocks any mark past it. A resumable mark needs a durable partial mark set
with its own crash-safety argument, for a bound nothing requires. Aborting
only when the mark stops progressing needs a progress threshold, another knob.

Projected live mark. Method: (1) count reads: one per distinct entity page
and one per blob reference of a distinct page; chunks and stream segments are
marked without a read. (2) Size the reachable set from the online backup,
which holds the reachable blocks of its two retained generations: shards 00
and a5 hold 91,781 and 91,401 blocks, about 23.4M in total, so one mark
reaches about 20-23M. (3) Split it by file size (shard a5): 48% of the blocks
are 1-4.25 KiB (the entity page range), 44% are below 1 KiB (manifests, small
chunks and stream segments) and 8% are larger, so 50-85% of the blocks need a
read: 10-20M reads. (4) Price a read at the measured 0.21 ms of a cold located
read on DolphinFS (`traversal_reader_measurement`). A complete live mark then
takes about 35-70 minutes of serial reads, plus about 21 s for the location
table, under a second of history decoding and 300-350 spill runs: roughly
40-75 minutes, independent of the number of pinned roots. Walking each root
on its own multiplied that by the roots plus the persistent pin walks and
exceeded the spill cap a mark then had. The estimate rests on the backup sample and a
component timing; a rebuilt engine reports the real figure in its
`step=mark_built` line, `marks_built` and `epochs_completed`.

What still limits an epoch after its mark (measured read-only on the live
volume, owned by the scan and deletion lanes): the loose directory holds about
19.0M files (74,262 and 74,598 in shards 00 and 7f), which the scan now lists
without a store-wide total or a stat per file ("Loose listing without a
store-wide total" below); and rule S4's file-age grace is not implemented by
the deletion hold.

## Loose listing without a store-wide total (2026-10-31)

Problem. Every loose listing counted entries against one store-wide total,
`authorityGarbageCollection.maximumPhysicalBlocks` = 20,000,000: the `.new-`
temporary scan and the block listing of each online scan and offline round,
and `verify`'s loose pass (backup prune used its own 50,000,000). Past it
every scan failed closed ("temporary block scan bound exceeded", then "block
listing bound exceeded"), so the collector would livelock while loose blocks
kept growing; on 2026-10-31 the main authority's shard 7f held 75,155 files,
about 19.2M in all. The total protected nothing. Both scans stream shard by
shard: the temporary scan retains at most one match, and the block listing
hands one shard's IDs to its visitor and drops them before the next shard,
so their memory never depended on the store's size, and no consumer needed
the total for correctness. (The bounded read even reserved capacity for the
remaining total, about 480 MB of address space for the first shard.) The
block listing also statted every listed file, about 19.2M FUSE metadata calls
per scan, only to check that it is a regular file, and the scan sized every
confirmed garbage block even after the round's victim count was full.

Decision.

- The total is retired from the contract, the generated constants and
  `AuthorityGarbageCollectionBudget`. `visit_block_shards` holds one shard's
  entries at a time; that, not a count, bounds its memory. `plan_temporary_file`
  streams every entry. The segment lane's orphan observation, which borrowed
  the same number as a window and silently truncated past it, reads the whole
  payload directory; its two-unchanged-observation confirmation is unchanged.
- Entry kinds come from the listing. `Vfs::read_directory_entries` returns
  `d_type` (std falls back to one `lstat` only for `DT_UNKNOWN`). Names are
  validated first, `.new-` entries are skipped, and only an entry the listing
  could not classify (a symbolic link, another kind, `DT_UNKNOWN`) gets a
  `metadata` call, which refuses it exactly as before; foreign IDs and
  directories or links named like blocks still fail closed with the same
  errors. A VFS wrapper that does not forward the method keeps one stat per
  entry.
- The scan sizes a confirmed victim only while the round can still admit one:
  fewer than `maximumBlocksPerRound` victims and at least `MIN_BLOCK_FILE_BYTES`
  of victim budget left. Every well-formed block file holds at least that, so
  the selected victims are unchanged. Compaction candidates were already sized
  only while the plan can admit a readable block.
- The offline collector, `verify` and backup prune share the listing; their
  per-round bounds (65,536 blocks or 256 MiB) are unchanged.

Alternatives rejected. A larger or volume-derived total is a knob that still
livelocks the store that passes it. A per-shard cap protects nothing the
one-shard listing does not already bound, and fails a shard that grows past
it. Streaming names through a callback instead of one sorted listing per
shard would save the path vector (about 15 MB for a 75,000-entry shard) but
change which of several bad entries is reported and hold the simulator's lock
across callbacks. Statting only `.blk` names still costs one FUSE call per
file.

Measurement (deterministic counts, `authority_gc_online::tests_listing` and
`block::tests`). One online scan over 143 loose files (10 confirmed garbage
blocks under a 4-victim round, 5 first-time suspects, 128 live compaction
candidates) made 143 metadata calls and 138 size reads before, 0 and 132
(4 victims + 128 candidates) after. A 12-file shard listing made 13 metadata
calls before and 1 (the shard root's entry) after. Stores above the total
scaled to 16 entries are now collected online (40 orphans) and offline
(17 orphans); before, every round failed. On the live volume a read-only
`readdir` of shard 7f returned 75,155 entries, all `DT_REG`, in 0.65 s:
DolphinFS (BeeGFS FUSE) reports `d_type`, so a scan of the 19.2M-file store
makes no per-file metadata call instead of about 19.2M.

## Mark spill sized by its inserts (2026-11-04)

Problem. A mark spills its inserts into sorted runs of 65,536 IDs (2 MiB),
which were files of their own, and `SpillMarkSet` refused a run beyond a byte
limit: the temporary budget, `min(1 GiB, 2% of volume free)` or 64 MiB when
the launch probe cannot observe the volume (`contracts/tofudb_ir_v1.json`
`authorityGarbageCollection`; the online mark subtracted its captured history
selection), under a certified 511-run cap. A mark therefore failed with
`garbage-collection spill budget exceeded` past 33,488,896 inserts, whatever
the reachable set needed; inserts, not distinct IDs, count, because runs
deduplicate only internally. The cap is a fixed capacity that a growing store
outgrows. The search-index authority's last successful marks held 30.9M and
then 31.35M inserts; every epoch since 2026-11-03 has failed
(`tofu_storage_gc_epoch_failing{collector="search"}` stays 1), so that
authority reclaims nothing. The main authority's projected 20-23M reachable
blocks sat at 60-70% of the same cap. The limit protected nothing: the spill
is reconstructible scratch that never outlives the daemon run, so it is
retention-class disk use (`docs/RESOURCE_CONTROL.md`, "Hardware never limits
work"), and a write the volume cannot hold already fails with the volume's
own error.

Decision. A mark's spill is sized by its inserts and nothing else.

- A set appends its runs back to back to one file, `gc-runs/spill.mark`: at
  most 32 bytes per insert plus 52 bytes per run (header and footer). No run
  count, byte limit or budget parameter remains; `SpillMarkSet::initialize`
  is the only constructor, used by the offline collector, the online mark
  and suspect sets and backup prune. With shared pages walked once, a mark
  inserts each reachable block about once (one insert per distinct page and
  per value reference of a distinct page, plus about 10^5 for retained
  history and the active WAL), so the spill stays near 32 bytes per
  reachable block. The `step=mark_built` line reports inserts, runs and
  spill bytes.
- The run format is unchanged (16-byte header, sorted unique IDs, CRC32C,
  BLAKE3). A run is written where the last recorded run ends with one
  positioned write and the existing file barrier; only the first run also
  runs the directory barrier, because only it names the file. A failed
  append records nothing and keeps its marks buffered; its retry writes the
  same offset again, and no read reaches past the last recorded run.
- The in-memory index adds each run's offset to its footer digest and
  per-shard offsets and digests, and every read uses them, so a run moved,
  swapped or replaced within the file fails exactly as a swapped run file
  did. A shard read and a segment-window pass open the spill once instead of
  once per run (the two sections below).
- Initialization wipes the spill file and every per-run file and `.run-new-`
  temporary of the earlier layout (`backup_gc::is_spill_entry_name`, shared
  with `wipe_work_directories` at thread start) and still refuses any other
  entry.
- The temporary budget keeps bounding what it budgets: compaction batch
  bytes, segment rewrite candidates, the captured history selection, and the
  offline session's release of cached marks before a rewrite whose space
  would exceed it together with the marks (an eviction, never a refusal).
  `OnlineGcMarkSnapshot::history_metadata_resident_bytes` existed only to
  shrink the spill limit and is removed.

Alternatives rejected. Raising the run cap or `maximumTemporaryBytes`, or
deriving the limit from free space: a tuned value that a growing store passes
again, livelocking its collector. Merging runs when the cap is reached:
removes only duplicates, which shared-page walks already removed, and needs
scratch space beyond the cap while it merges. Keeping one file per run
without a cap: correct, but each run pays an exclusive create, a rename and a
directory barrier, and every shard read opens every run (about 164K opens per
scan at live scale). Truncated or probabilistic marks would be safe (a false
positive keeps garbage) but change the mark format that segment intersection
and compaction planning read as exact IDs.

Measurement (deterministic operation traces, `backup_gc::tests`). Writing a
run costs one open, one write and two file syncs, and the first run adds two
directory syncs; the per-run layout cost an exclusive create, one write, two
file syncs, a rename and two directory syncs for every run. A shard read over
N runs makes one open and at most 2N small reads instead of N opens and 2N
reads, and a segment-window pass one open and N reads instead of N opens and
N reads. At live scale (about 350 mark and 290 suspect runs) a scan makes at
most 512 opens instead of about 164K. On the DolphinFS (BeeGFS FUSE) mount an
open, two reads and a close of a small file measured about 1.07 ms against
0.006 ms for the same reads on an open handle, so the opens of one scan drop
from about 3 minutes to under a second (a component timing, not an epoch
measurement). A fault campaign covers every operation of an append (error
before it, torn write, lost sync) for the first and a later run, with and
without a crash before the retry (`spill_appends_survive_every_fault_and_crash`).
Certification (`spill_set_certifies_marks_past_one_gib`, release build,
ignored by default because it writes more than 1 GiB): 512 full runs, that is
33,554,432 distinct marks and 1,073,768,448 bytes, past the former
33,488,896-insert cap, spill into one file and read back exact per shard in
25.7 s on local disk with a peak RSS of 23.9 MB.

## Staging names the collectors accept (2026-11-05)

Problem. Both loose-block writers (`put_loose`, `write_loose_synced`) stage a
file as `.new-<uuid>` with the UUID's default, hyphenated spelling, while
`validate_temporary_block_path` accepted only the 32-digit simple spelling
that the test fixtures wrote. A staging file left by an interrupted write
therefore failed `plan_temporary_file` with `temporary block filename is
malformed`, and since the temporary scan runs right after the mark, every
online epoch and every offline round failed there. Once marks completed
again ("Mark spill sized by its inserts" above), the search-index collector
logged `step=mark_built` and then this error every epoch and never
`step=epoch_complete`; it was first logged on 2026-11-02 and again after each
restart (a repeated epoch error logs once per process). At-rest encryption's
`rename_reencrypt` stages its own `.new-ren-<uuid>` beside the destination,
a block shard or the payload-segment directory, and neither collector
accepted that name, so an encrypted store failed the same way after any
interrupted publication.

Decision. One grammar mints and recognizes staging names:
`vfs::hyphenated_temporary_name` writes a prefix and the canonical lowercase
hyphenated UUID, and `vfs::is_hyphenated_temporary_name` accepts exactly
that. Both loose writers mint through `block::temporary_block_file_name`;
`at_rest::is_reencrypt_temporary_name` recognizes the at-rest prefix. A block
shard accepts the writer's name and the at-rest name; the payload-segment
directory keeps its writer's simple `.new-<uuid>` and also accepts the
at-rest name. Every other `.new-*` entry, other UUID spellings included,
still fails closed, and the test fixtures mint through the same function.

Alternatives rejected. Switching the writers to the simple spelling: staging
files already on disk keep the hyphenated name, so the validator would need
it anyway. Accepting any UUID spelling: wider than any writer produces.
Skipping an unrecognized `.new-*` entry: collects around a file of unknown
origin, which failing closed exists to prevent. Removing the live leftovers
by hand: repairs one volume and leaves the next interrupted write fatal.

Verification (`block::tests`). `staging_files_left_by_the_loose_writers_are_collected`
and `staging_files_left_by_the_at_rest_rename_are_collected` fail each
writer's rename and then its cleanup unlink, so the real staging file stays,
and require the temporary scan to plan and remove it;
`malformed_temporary_block_names_fail_closed` covers the near-miss spellings;
`payload_segment::tests::orphan_plan_treats_at_rest_rename_staging_files_as_temporaries`
covers the segment directory.

## Shard-ranged spill reads (2026-10-31)

Problem. `SpillMarkSet::live_for_shard` read and decoded every spill run in
full for every shard. The loose scan asks for each non-empty shard of the
mark and of the previous suspects (offline GC and backup prune do the same
for their marks), so a scan read every run 256 times: at live scale, about
350 mark runs and 290 suspect runs of 2,097,204 bytes, about 344 GB per scan
instead of 1.34 GB.

Decision. Runs are sorted and a shard is the range of one first block-ID
byte, so each shard owns one contiguous slice of every run. When a run is
flushed the set records, per shard, the slice's start (a 257-entry offset
array fixed by the 256-shard layout) and the BLAKE3 digest of the slice's
bytes as written. `live_for_shard` opens the spill once per call, checks
every run's 16-byte header (magic, format version, recorded ID count) at the
run's recorded offset, then reads only its slice with one positioned read
and checks that digest. Answers are unchanged: the same IDs per run,
concatenated, sorted and deduplicated. No per-shard bound applies: the former
1,000,000-ID bound was a second fixed ceiling on the reachable set, and a
shard read holds one shard's marks just as the loose listing holds one
shard's entries. A corrupt header fails every shard, a flipped, truncated or
swapped slice fails each shard whose bytes it changed, all with
`InvalidData`, and a missing spill fails with `NotFound`; no shard returns
fewer or other IDs. Other shards' slices and the run footer cannot change a
shard's answer; segment intersection still reads runs whole and checks the
run's CRC32C and BLAKE3. The index lives in memory, not in the spill: only
the set that wrote a run reads it (initialization wipes stale spills, and a
spill never outlives the daemon run), so the digests are out of reach of the
bytes they verify and the run format is unchanged. It costs about 9 KiB per
run, 0.44% of a full run (about 3 MiB for the 350 runs of a live mark). Under at-rest encryption a positioned read decrypts the 4 KiB
regions it covers: the first region for the header and less than 8 KiB
beyond the slice, per run and shard.

Alternatives rejected. A shard table stored in each run: every shard read
would first read and verify the table (about 9 KiB per run and shard, more
than the run itself across 256 shards) or need a verified-table cache; the
in-memory index is that table without the reads. CRC32C per shard instead of
BLAKE3: 7 KiB less per run, but a 32-bit check on the IDs that decide
deletion. Re-partitioning runs into per-shard files when the mark completes:
one more full read and write of the spill, twice the scratch space, and 256
more files per mark. Keeping the spill open across shard calls: saves at most
512 opens per scan (under a second at live scale) for a descriptor whose
lifetime would span the scan. Skipping runs that hold no IDs of the shard (no
header read): a
corrupt run would then pass every shard it holds no IDs for, while the
offline collector requires a corrupt mark run to fail its scan before any
deletion (`authority_gc::tests::corrupted_session_marks_fail_before_deletion_and_poison_further_calls`).

Measurement. `backup_gc::tests::all_shard_reads_together_read_each_spilled_id_once`
spills 8 runs (15,729,056 bytes) and reads all 256 shards: 4,026,638,336
bytes before (each run 256 times), 15,761,408 after (each ID byte once plus
256 headers of 16 bytes per run; footers are not read). At live scale a scan
reads about 1.34 GB instead of 344 GB. Each run and shard costs two small
reads (header and slice) instead of one 2 MiB read; since "Mark spill sized
by its inserts" a shard read opens the spill once, not each run (at most 512
opens per scan instead of about 164K). Segment intersection reads
runs whole, once per scan window ("One mark-run pass per segment window").

## One file per deletion hold (2026-10-31)

Problem. The loose deletion hold unlinked every confirmed victim of a round,
and `online_gc_finish_compaction` unlinked every loose copy of the segment it
had just published, each under one authority hold. On the production
beegfs-fuse mount an unlink costs 0.653 ms (mean; p50 0.622, p99 1.045, max
6.6 ms, measured on 2026-10-31 in a scratch directory), and a round admits up
to 65,536 victims, as a segment holds up to 65,536 blocks: one hold could keep
the mutex for about 43 s. That is four times the 10 s threshold after which
`authority_watch.rs` flips `system.health` ready and the supervisor recycles
the daemon, and every foreground write waits meanwhile. Complete marks and
multi-segment compaction made both holds reachable on the main authority; the
search projection's local disk (about 10–50 µs per unlink) hid them.

Decision.

- Each loose victim gets its own short `try_lock` hold
  (`AuthorityDatabase::online_gc_delete_loose_victim`) with the checks of the
  former batch hold, applied to that one file: a usable authority and an idle
  physical lane; a staged copy keeps the file, then the relink fence does
  (rule S10 before S7); both CONTROL slots must name the current physical
  roots, decided from the in-memory slot states and republished first when
  they do not (rule S8); the unlink is noted for rule S10
  (`BlockStore::remove_block_noting_absent`). `WouldBlock` (busy lane, slots
  that stay apart, closed fence) ends this epoch's loose deletion; the
  remaining victims stay suspects and confirm again.
- Each loose copy of a just-published compaction segment also gets its own
  short hold (`online_gc_remove_redundant_copy`, which also removes every
  redundant copy the scan finds), once the publication made both slots name
  the catalog holding it. The hold checks that the installed
  catalog still names the segment and is not rooted at that block, and that
  both slots name the installed catalog (republished first when the lane is
  idle; `WouldBlock` while a publication owns the lane, which moves both slots
  itself).
- Between holds, off the mutex, a shard's directory barrier runs once the run
  leaves that shard; a crash before it can only bring back a garbage file or a
  redundant copy. Every attempt waits until the collector has stayed off the
  mutex at least as long as its previous hold took: the collector's 50%
  duty-cycle rule applied to authority occupancy, measured, with no constant.
  A contended `try_lock` postpones the hold and retries; the collector never
  blocks in `lock()`. A stop ends the run, and rule S10 keeps exactly the
  files unlinked so far. Copy removal also retries `WouldBlock`, so a run
  finishes in its epoch; a copy that a stop or an error leaves waits for the
  next epoch's scan.
- A retry after a contended or refused attempt waits the larger of that pause
  and the time the run has already spent contended or refused, so a streak of
  retries thins out geometrically from the loop's own cost. Without it, a
  garbage run whose first hold found the authority held had no measured hold
  to pace it and retried 80 ns after each failure: a busy spin for as long as
  the foreground held the mutex. A copy run refused while a publication keeps
  the slots apart re-took the mutex at the pace of its own refused holds
  (65.6 us), for as long as the publication lasted. A retry overshoots the
  end of a contention by at most as long as the contention lasted.
  `a_contended_first_deletion_hold_backs_off_instead_of_spinning` and
  `refused_deletion_holds_back_off_like_a_contended_authority` fail before the
  change. Alternatives rejected: a fixed retry delay or retry cap is a tuning
  knob; seeding the pause with an earlier hold of the epoch (the capture) can
  be a few microseconds, which still polls the mutex; blocking in `lock()`
  gives up stop responsiveness behind a wedged holder.

Why each unlink stays exact.

- Garbage: the unit that needs exclusion is "validate, then unlink". Every
  reference-without-write decision runs under the authority mutex (rule S7),
  so a hold that finds neither a staged copy nor a fence entry unlinks a file
  no committed state can reach before the next mark. Holding across victims
  added nothing: each victim's checks run in its own hold against the staged
  set, the fence and the slot states as they are then. A long or contended run
  keeps the epoch's fence open longer; it stays exact, one entry per distinct
  re-reference.
- Copies: after the publication returned, both slots name a catalog holding
  the block, and only this collector, whose thread runs the copy holds, drops a
  catalogued block. A writer that recreates an identical loose file meanwhile
  loses only that file: its reference resolves through the published segment
  (a loose miss re-checks the installed catalog, `get_loose_after_catalog`;
  see the read order in "Redundant loose copies of catalogued blocks"). The
  copies were live in this epoch's mark, so
  none of them is in the suspects the next epoch confirms from: rule S10 needs
  no record of them, and a recreated file that later dies confirms only from a
  scan that lists it.

Alternatives rejected.

- Unlinking compaction copies off the mutex. Paths that hold the mutex stat a
  loose file and then open it. `put_deferred_fresh` stages a page without any
  probe, so a fresh copy-on-write page can equal a packed block, and
  `flush_pending_selection` (checkpoint, reference flush) then finds the
  loose copy and opens it; an off-mutex unlink between the two becomes a
  NotFound checkpoint or commit error. Loose files vanish only under the
  mutex, which those paths rely on. Its shard workers are scoped to the
  calling thread, so a batch that runs under the mutex finishes all of its
  I/O before the mutex is released.
- Ending a run on contention. Exactness does not need it, since every hold
  re-validates; with the pause a foreground request takes the mutex within one
  or two of its 1 ms polls, so the next attempt would collide and reclamation
  would stop after a few dozen victims per epoch under light load. Abandoned
  compaction copies would never be removed.
- Back-to-back holds without the pause. The foreground polls `try_lock` with a
  1 ms sleep (`server.rs`, `acquire_authority_permit_until`) and the watch
  probes every 250 ms, resetting only on a free probe or foreground progress.
  Holds of about 0.65 ms a few microseconds apart leave a polling request about
  a 1% chance per poll, and an idle authority's watch a likely run of 40 busy
  probes (a false wedge) within a 43 s run. Counting the collector's
  acquisitions as watch progress fixes only the false wedge.
- A smaller batch per hold: a batch size is a tuning knob, and a hold's
  mutations would still scale with it. A shard barrier inside every hold: two
  directory syncs per file instead of per shard, under the mutex.

Cost. The same opens and unlinks as before and one directory barrier per shard
run, now off the mutex; per file one `try_lock`, the in-memory checks and a
pause, so a run takes about twice its unlink time, half of it off the mutex. No
hold keeps a foreground request out for longer than one unlink, and the mutex
is free at least half of a run. CONTROL is republished once more for each
publication that leaves the older slot behind during a run.

Verification (`authority_gc_online/tests_short_holds.rs`: a hold-counting
`OnlineGcDatabase` port and a VFS that checks the authority mutex at every
loose unlink and CONTROL write).
`every_compaction_copy_and_garbage_victim_is_unlinked_in_its_own_hold` packs
two backlog segments (256 copies) and deletes 64 garbage blocks: before the
change 256 copy unlinks ran in 2 holds (128 in one) and 64 victim unlinks in 1;
now every hold unlinks at most one file and none runs off the mutex.
`a_foreground_commit_completes_between_two_deletion_holds`: a commit from
another thread at the barrier between two victims lands and the later victims
go after it (before, the mutex was held there).
`a_control_publication_between_two_victims_is_revalidated_by_the_next_hold`:
history pruning between two victims publishes CONTROL once, the next hold
republishes it before its unlink, both slots name the pruned manifest and the
fallback slot recovers.
`contended_deletion_holds_retry_until_every_file_goes_while_foreground_commits_land`:
before every other attempt a foreground thread holds the authority across the
collector's `try_lock` and commits; every copy and victim still goes in its
epoch, one per hold, and every commit lands.
`stop_between_deletion_holds_records_exactly_the_unlinked_victims`: a stop
raised during the third unlink ends the run before the fourth hold,
`unlinked_loose` holds exactly those three, and the next epoch reclaims the
rest. `an_idle_authority_is_free_between_every_two_deletion_holds`: the watch's
probe, run through the hold seam before every attempt, finds the mutex free,
so no two unlinks are observed without a free probe between them, and every
attempt waited at least as long as the hold before it. All six fail on the
previous code; the existing rebirth, fallback-slot, publication-window,
segment-reclaim, fault, capture, listing and backlog specifications pass
unchanged.

## One mark-run pass per segment window (2026-10-31)

Problem. The payload scan intersects each segment of its catalog window (at
most `MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED`, 16) with the completed
mark, and `SpillMarkSet::retain_live_ids` read, verified (BLAKE3 and CRC32C)
and decoded every mark run in full on every call. A 16-segment window read
the whole spill 16 times: at live scale about 350 runs of up to 2 MiB (about
700 MB), so roughly 12 GB of reads and hashing per epoch where one pass
answers every segment.

Decision. `retain_live_ids_per_segment` takes the window's segment indexes,
each sorted, unique and at most one run's 65,536 IDs (the existing bound),
and reads each run once: one whole read at its recorded offset (one open of
the spill per pass) and `decode_run`, then the existing merge-join against
each segment in turn. A segment keeps an ID if
any run holds it; visiting runs in the outer loop and segments in the inner
one gives every segment the answer of a call of its own, and duplicates
across segments or empty segments need no special case. The answers are
applied only after every run was read, so a failed, corrupt or truncated run
or a missing spill fails the whole window (the I/O error, `InvalidData` or
`NotFound`) with every segment unchanged. `retain_live_ids` is the
one-segment case. The online scan reads and verifies the window's segment
indexes first (the stop flag is checked before each one and before the
pass), makes one call, and then plans dead sets, confirmation, rewrites and
consolidation per segment exactly as before; a scan error discards the epoch,
as it did, so reading every index before planning changes no outcome.

The whole-run read also became stricter. Each `SpillRun` records the run's
footer BLAKE3 digest as written (32 bytes in memory), and a read must match
it. Before, segment intersection accepted a run that is valid on its own but
is not the run the set wrote (a swapped or replaced run), which changes
liveness without any error; shard reads already rejected it.

Cost besides one decoded run (2 MiB): one flag per window ID. The online scan
holds the window's ID lists and their retained copies at once, at most
16 × 65,536 × 32 bytes (32 MiB) each. Merge comparisons are unchanged (each
run is still merged against each segment); reads and hashing drop 16-fold.

Alternatives rejected. One merged, sorted window with back-references to
each segment position, merge-joined once per run: fewer comparisons per run
(run plus window instead of segments times run) but a sorted copy of the
window (up to 36 MiB) and duplicate bookkeeping, for the same reads and
hashing, which are the cost. Reading only the shard slices that hold window
IDs: one segment of packed history or checkpoint batches spans every shard,
so such windows skip nothing and would pay up to 256 reads per run instead of
one; only windows made entirely of loose-compaction segments, each covering
adjacent shards in listing order, would read less. Skipping runs whose ID range misses the
window: a corrupt run would then pass, and the offline collector requires a
corrupt mark run to fail before any deletion. Batching the offline
collector's window in `authority_gc.rs` too: its loop stops at the first
rewrite candidate, so one pass would first read and verify every segment
index of the window, and a corrupt later segment would fail a round that
succeeds today; the certified offline collector keeps its per-segment calls
(at most 16 passes per offline round).

Measurement. `authority_gc_online::tests_mark_cost::segment_window_scan_reads_each_mark_run_once`
scans a 16-segment window against a 3-run mark of 4,577,660 bytes:
73,242,560 run bytes before (16 passes), 4,577,660 after, with the same dead
set per segment. `backup_gc::tests::window_intersection_reads_each_run_once`
(16 segments, 5 runs) sees one open and one whole read per run. Projected at
live scale (about 350 runs, 700 MB), an epoch reads the runs once instead of
about 12 GB. Exactness and
failure behavior: `window_intersection_matches_per_segment_answers_for_random_ids`
(random marks with duplicate inserts or no runs; windows with an empty
segment, the ID extremes, IDs in no run, IDs shared by segments and a
full-size segment), `window_intersection_fails_closed_and_keeps_every_segment_intact`
(every failed operation; flipped magic, ID, CRC32C and BLAKE3 bytes;
truncated, swapped and missing runs) and
`window_intersection_validates_every_segment_before_reading`.

## Redundant loose copies of catalogued blocks (2026-10-31)

Problem. A live block can keep a loose file beside its packed copy in the
installed payload catalog: a crash between a catalog publication and its loose
cleanup (a compaction's copy run, `compact_payload_blocks`, pack adoption); a
stop, error or authority restart latch in the middle of a compaction's copy
run, which leaves the rest of that segment's copies, up to 65,536 files; and a
writer that publishes identical content again (a checkpoint materializing a
fresh copy-on-write page, a rotation's payload index root, history
promotion's loose fallback). Nothing removed such a copy: the garbage lane
skips it as live, and the scan's compaction probe skipped it as catalogued.
That probe also ran only while compaction had a catalog slot below the 75%
floor, so the main authority, at about 4,091 of 4,096 segments, never probed.
Each copy costs an inode and a metadata operation on every scan, and the
container's root overlay is at 100% inode use.

Decision. Every completed scan runs one exact membership probe per live loose
block while copy removal or compaction is open:
`BlockStore::catalogued_payload_segment`, the existing filter, routing and
validated-index probe, now returning the segment that holds the block
(`has_catalogued_payload_block` wraps it). A block the installed catalog holds
is a redundant copy. Once its shard is scanned, each such copy goes through the
hold that removes compaction copies (`remove_loose_copies`,
`AuthorityDatabase::online_gc_remove_redundant_copy`,
`Engine::remove_redundant_loose_copy`), one file per hold, and only while:

- the installed catalog still names that segment;
- both durable CONTROL slots name the installed catalog (republished first
  when the physical lane is idle; `WouldBlock`, retried with the run's
  geometric backoff, while a publication owns the lane);
- the installed catalog root is not that block (a catalog root stays loose,
  because reopen reads it before any catalog exists).

Segments are immutable under fresh IDs, so the probe's membership holds as
long as a catalog names the segment, and the hold reads no index. The copies
of all shards form one run of single-file holds, so the pause before each hold
covers the previous one across shard boundaries too (the 50% duty rule), and
each shard's directory barrier runs once its copies are gone. Memory is one
shard's IDs with their segment IDs. A stop abandons the epoch; any other error
ends copy removal for the epoch, the scan still completes its suspects, and the
error surfaces after the deletion holds, like a compaction error. The garbage
lane, rule S10, the relink fence and what compaction packs are unchanged: the
copies are live in this epoch's mark, so none is a suspect the next epoch
confirms from. `total_redundant_copies_removed` counts these copies (not
compaction's own). There is no new knob: the bounds are one shard and one file
per hold.

Why removing a copy never makes a reader of a committed reference see
NotFound. Unlike a compaction copy, a redundant copy can go while the catalog
that holds its block stays installed, and readers run on or off the authority
mutex.

- `get`, `get_shared`, `get_materialized_shared`, `get_traversal` and the
  exact fallback of `TraversalReader::get` try, in order: a staged payload, the
  payload cache, history locations, then `get_uncached_materialized`. That
  tries a positive read hint; the loose file, only while the packed filter is
  unbuilt (a clean open), where a file that vanished after the stat falls
  through; the packed filter, which never excludes a block of a publication it
  covers; the routing candidates, complete for the installed catalog (every
  segment is prefix-routed or an explicit fallback, read under the manifest
  lock); and the candidate probe, retried once against a changed view. Only a
  block none of these resolves reaches `get_loose_after_catalog`. A reader
  whose view already includes the segment never needs the loose file, and a
  hold removes a copy only while the installed catalog names that segment.
- A reader whose view predates the publication that packed the block (every
  compaction copy goes right after its publication, and any reader can stall
  across one) reaches `get_loose_after_catalog`. Its stat can miss, or the
  file can vanish between the stat and the open. Either miss consults the
  installed catalog once more, unless the filter excluded the block under the
  publication still installed: that proves no catalogued segment holds it, so
  no copy hold can have removed it. Before, the open's NotFound reached the
  reader, and a miss without routing candidates was final even under a newer
  publication. The re-check is exact, so it needs no retry count.
- `TraversalReader::get` ignores a failed loose read and takes the exact path;
  a history source whose loose open misses is read through
  `get_materialized_shared`.
- Dedupe probes (`put_deferred`) try packed candidates first once the filter is
  built, and a missing loose file answers `Missing`, which restages. They run
  under the authority mutex (rule S7), so no hold falls between their stat and
  open.
- Writers: `put_loose` and each shard of a `flush_pending_selection` batch stat
  a loose file and then open it. Under the mutex no hold interleaves: unlinks stay
  under the mutex, as "One file per deletion hold" requires. Rotation encoding,
  pack adoption's publication and history promotion call them off the mutex,
  and an open miss now takes the absent-file path a moment-later stat would
  have taken: publish the file again, materialize the staged payload, or skip
  the fsync.

Alternatives rejected. A second membership lookup or a catalog-wide set of
packed IDs: memory grows with the catalog, and the existing probe already
proves membership. Spilling the copies for the deletion phase: a third spill
per epoch, where the scan already holds the shard. Probing only while
compaction has a slot: it never runs under pressure, where the live main
authority is. Requiring an idle physical lane for every copy hold: it fences
only the off-mutex writers, readers still need the re-check, and every copy
run would stall behind each rotation. A retry count for loose misses: a knob,
where the re-check is exact. Reading the packed payload before each unlink:
one payload read per copy, while every packed read verifies CRC32C and the
content hash and every publisher verifies its segment. Unlinking off the mutex:
rejected in "One file per deletion hold".

Cost. One membership probe per live loose block and scan. For a block the
covering packed filter excludes (every block not catalogued, apart from false
positives below 0.1% and retired segments' stale bits) the probe is an
in-memory filter check. A filter hit consults the routing candidates and their
membership witnesses, which cover the installed catalog, and reads a segment
index only for a candidate the witnesses cannot exclude. Before, a store under
catalog pressure skipped the probe; on the live main authority (about 19.2M
loose files, 4,091 segments) a scan now adds about 19.2M filter checks. Every
copy removed saves an inode and a metadata operation on each later scan. Copy
memory is one shard's IDs at 48 bytes each (about 75,000 loose files per shard
on the live store, at most about 3.6 MB). Holds are paced like every deletion
run.

Verification (`authority_gc_online/tests_redundant_copies.rs` and
`block::tests`; every specification below except the read-order one failed
before the change):

- `the_next_scan_removes_every_crash_leftover_copy_one_unlink_per_hold`: a crash
  after two compaction publications leaves 256 copies while both slots name the
  catalog, and no catalog slot is left for compaction. Before, the next scan
  removed none; now 256 unlinks in 256 holds and no CONTROL write, every hold
  after the first waited off the mutex at least as long as the one before it
  (a fresh run per shard fails this), and every value reads back from either
  slot.
- `a_redundant_copy_goes_only_after_both_control_slots_name_its_catalog`: the
  second publication's stabilizing CONTROL write failed, so the fallback slot
  recovers a one-segment catalog and reaches the second segment's 128 blocks
  only through their copies. The first copy hold writes CONTROL once, before
  its unlink; all 256 copies go, and with the newest slot destroyed recovery
  reads every value.
- `a_copy_recreated_during_the_run_keeps_its_value_and_goes_in_the_next_scan`:
  at a shard barrier, off the mutex, a foreground thread publishes a removed
  copy again and commits a reference. The commit lands, values read
  throughout, and the next scan removes the 1 recreated copy in its own hold.
- `copies_a_stopped_compaction_run_left_go_in_the_next_epoch`: a stop during
  the third unlink of a compaction's copy run leaves 125 copies, which stayed
  for good before; the next epoch removes all 125 (253 unlinks in 253 holds,
  its own compaction's copies included).
- `a_catalogued_block_reads_through_its_segment_whatever_happens_to_its_loose_copy`
  (the read order above, true before the change too): with a covering filter
  no read path opens the loose copy; with an unbuilt filter a copy vanishing
  between the stat and the open falls through to the segment.
- `a_loose_copy_unlinked_between_a_readers_stat_and_open_resolves_through_the_new_catalog`,
  `a_loose_miss_with_no_catalog_candidate_rechecks_a_later_publication`,
  `put_loose_publishes_again_a_copy_unlinked_after_its_stat`,
  `flushing_a_staged_block_materializes_a_copy_unlinked_after_its_stat` and
  `syncing_loose_blocks_skips_a_copy_unlinked_after_its_stat`: each returned
  NotFound before.

## Catalog pressure keeps the minimum interval (2026-10-31)

Problem. `catalog_pressure_expedited` (at least 90% of the 4,096-entry payload
catalog ceiling) had no caller since the 0.18.3 source import, and the
compiler reported it unused, although the comment on `catalog_pressure` said it
expedites consolidation epochs at 90%. Pressure did not prevent the idle
backoff on any path: `found_work` has no catalog term, so an epoch with nothing
to delete, rewrite or consolidate in its 16-segment window, or whose
consolidation publication was refused with `WouldBlock`, doubled the idle
interval up to `ONLINE_GC_MAX_IDLE_INTERVAL` (3,600 s). The live catalog sits
at about 4,091 of 4,096 segments.

Decision. An epoch that ends with the installed catalog at or above the
threshold keeps `min_epoch_interval`: consolidation work is pending, whatever
that epoch found. The threshold is `(MAX_PAYLOAD_SEGMENTS * 90).div_ceil(100)`,
3,687 segments, a collector field like the 75% floor so that a specification
can lower it. The start floor (`ONLINE_GC_MIN_EPOCH_INTERVAL` between starts,
for every trigger) and the 50% duty floor stay unconditional, so pressure
never starts epochs more often than every 120 s.

Alternatives rejected. Deleting the function as dead: pressure does not keep
the interval (see the measurement below). Starting pressured epochs like the
doorbell: both floors stay unconditional (the 2026-09-21 wedge class). A
shorter interval under pressure: a knob.

Verification.
`an_epoch_ending_at_ninety_percent_of_the_catalog_ceiling_keeps_the_minimum_interval`
pins the production threshold (3,687 segments expedite, 3,686 do not), then
runs idle epochs on a two-segment catalog with the threshold lowered to two and
consolidation off: at the threshold the idle intervals are [1 ms, 1 ms], where
the previous code doubled from the first idle epoch ([2 ms, 4 ms]); one segment
below they are [1 ms, 1 ms, 2 ms] (the shrinking publication's orphan segment
and old catalog root are work, then an idle epoch backs off).

## Reclamation keeps its pace under foreground load (2026-11-02)

Problem. On the live main authority (BeeGFS FUSE, daemon transport, catalog
at its 4,096-segment ceiling) reclamation fell behind production by orders
of magnitude:

- `payload-segments` held 86,757 files (10.5 TB) for a 4,096-entry catalog
  (735.5 GiB): 82,813 files (9.1 TB) were orphans, decoded offline from the
  CONTROL catalog. The engine started at 11:18 removed 295 of them in 4.8 h.
  The lane ran once per epoch, before an hour-long mark, and every pass
  ended at its first contended `try_lock` or its first `WouldBlock`; on a
  full catalog an off-mutex publication owns the physical lane for seconds
  every few seconds.
- The same engine reported `epochs_completed: 0`, `marks_built: 0` and
  `deferred_epochs: 8` although engine logs show marks completing every
  30–60 minutes (2.1M pages, 20–24M references, up to 44 min each). The
  driver dropped every epoch error, so nothing said why. One failure is
  certain: a consolidation or rewrite publish made one `try_lock` after its
  off-mutex build, failed the whole epoch with "online GC consolidation hold
  contended" when the foreground held the authority, and left the built
  segment as an unreferenced file. Every epoch at catalog pressure plans a
  consolidation.
- Loose victims were admitted under the offline round's 256 MiB victim byte
  cap. History routing pages (`TDBHLP01`) are 92% of the 10.4 TB of loose
  bytes (a size-weighted sample of one shard) at about 3.5 MB each, so a
  successful epoch could delete about seventy of millions. The cap bounded
  the batch a single deletion hold once unlinked; since every victim gets its
  own hold, no hold depends on the byte total. Admission also stat-ed every
  victim only to apply it.

Decision. Every collector hold that waits for the authority shares one paced
attempt (`SingleFileHolds::attempt`): the duty-cycle pause after a hold, and
geometric backoff from the run's own retry time after a contended or refused
attempt; it never blocks in `lock()`.

- The segment lane retries a contended or refused removal instead of ending
  the pass; only a stop or an error ends it. Each removal still re-validates
  catalog, protected sets, physical lane and both CONTROL slots under its
  hold, and confirmation still needs two observations at least one epoch
  interval apart, so the safety argument above is unchanged; only how many
  confirmed files one pass removes changes.
- Consolidation and segment-rewrite publishes go through `publish_under_hold`:
  contention is waited for; the publication's own `WouldBlock` (lane busy,
  catalog moved, relink fence) and a stop still prove that nothing was
  published, and only then the built segment is discarded. Any other error
  may follow a durable publish and still keeps the file, as before.
- An epoch error other than a stop increments `failed_epochs` and stays in
  `epoch_error` until an epoch completes; each change of the error logs one
  WARN line, and each completed epoch logs `step=epoch_complete` at INFO, as
  this document always claimed. `deferred_epochs` keeps counting failures,
  so existing consumers see the same values.
- Online loose victims are admitted by count alone
  (`maximum_blocks_per_round`, which bounds the in-memory victim list); the
  scan sizes only compaction candidates. The offline collector keeps both
  caps.

Alternatives rejected. A separate lane thread: the paced retries already let
one pass remove every confirmed file, and a second driver would duplicate
lifecycle, stop-join and pacing owners. A larger lane cadence or retry count:
knobs that leave the first contended hold ending the pass. Unbounded online
deletion per epoch by streaming victims per shard: it changes the
confirmation/unlink ordering that rules S1–S10 pin, for a further gain the
count cap does not block yet (65,536 victims per completed epoch).

Verification (`tests_reclaim_throughput`, each failing before the change):
`contended_orphan_removals_retry_until_every_confirmed_file_goes` (a
foreground thread holds the authority across the first removal attempt; all
six confirmed files go in that pass, none before),
`refused_orphan_removals_retry_until_every_confirmed_file_goes` (three
`WouldBlock` refusals; before, the pass ended at the first),
`a_failed_epoch_is_reported_until_an_epoch_completes` (an aborted mark is
reported, a stop is not, a completed epoch clears it),
`a_contended_consolidation_publish_neither_fails_the_epoch_nor_leaks_its_segment`
(before: the epoch failed with the live error text and the built segment
stayed unreferenced), and
`one_epoch_deletes_every_confirmed_loose_victim_beyond_the_victim_byte_cap`
(eight 2 KiB victims under a 4 KiB byte cap; before, one per epoch).
`scan_sizes_only_compaction_candidates_and_stats_no_listed_file` now pins
zero size reads for admitted victims. These are deterministic-VFS
specifications; the live reclamation rate needs a deployed engine and is not
measured here.
