# TofuDB online garbage collection

Status: authoritative design for the online physical-reclaim lane.
Owner: `packages/tofu-db/src/authority_gc_online.rs` (design), mounted by the
daemon; the offline exclusive collector in `authority_gc.rs` stays unchanged
and remains the certified heavy hammer.

## Problem

Physical reclamation (dead blocks, dead segment contents, orphan files)
today runs only in the offline `collect-garbage` CLI, which holds an
exclusive engine borrow for the whole session and refuses to start while any
MVCC pin is active. The serving daemon therefore never reclaims: disk grows
until an operator schedules downtime. `system.reclaim` is a documented
no-op returning `offline_required: true` because "a full graph mark has no
resumable foreground-safe cursor yet" (docs/STORAGE_TOFUDB.md, "Online
reclaim boundary"). A personal-assistant authority must reclaim online, in
bounded slices, without starving foreground commits or the snapshot read
lane, and without any operator knob.

## Decision

Run a **two-mark epoch collector** on a dedicated daemon thread
(`tofu-db-online-gc`). Each epoch is a sequence of bounded steps; only
short holds of the authority mutex ever happen, and every hold is O(in-memory
clone) or O(one bounded batch publish). Long work (graph traversal, directory
scans, segment rebuild, fsync chains) happens off-mutex.

### Epoch state machine

```
Idle → Snapshot → Mark → Scan → (first epoch: rotate marks, stop)
     → Confirm → Delete(batches…) → rotate marks → Idle
```

1. **Snapshot** (short hold): clone the mark inputs out of engine state —
   current `authority_state_root`, every active transient snapshot-pin root
   (reader + writer class), the history-manifest segment reference list,
   staged-history references, the committed-envelope block-id lists, and the
   current payload manifest id. All of these are in-memory structures; the
   hold never performs file I/O.
2. **Mark** (off-mutex): union into one `SpillMarkSet`:
   - `visit_entity_page_graph_with_values` over the snapshot root (its pages
     are CoW-immutable; only this lane deletes, so traversal is race-free),
     discovering persistent root pins exactly like the offline walk;
   - the same walk over every transient pin root captured in step 1;
   - semantic value blocks for every visited leaf value;
   - the captured history/staged/committed projection: history segments are
     decoded off-mutex through `BlockStore` reads (segment files are
     immutable and are deleted only by this lane, so a captured reference
     list stays readable).
   The mark spills under `authority-gc/online/` with the existing
   `SpillMarkSet`; marks never carry authority across process boundaries
   (spill files are namespaced per daemon run and wiped at thread start).
3. **Scan** (off-mutex): walk loose block shards and the payload-segment
   directory with the same per-round bounds as the offline collector
   (`MAX_AUTHORITY_GC_PHYSICAL_BLOCKS`, 16-segment catalog windows, …).
   A block/segment content unmarked in the current mark is a *suspect*.
4. **Confirm**: suspects from the previous epoch's scan that are *still*
   unmarked in this epoch's mark **and** whose file mtime is older than the
   configured grace (`ONLINE_GC_CANDIDATE_MIN_AGE`, default 120 s, ≥
   2× the worst transaction/replica lifetime) become *confirmed garbage*.
   First epoch after startup confirms nothing; it only establishes the
   first mark. This is the two-mark rule.
5. **Delete** (bounded batches, short holds): per maintenance round delete
   at most `MAX_AUTHORITY_GC_BLOCKS_PER_ROUND` blocks /
   `MAX_AUTHORITY_GC_VICTIM_BYTES`, one orphan segment, one segment rewrite,
   reusing the offline collector's hard caps. Every batch that mutates
   catalog/CONTROL takes the authority mutex only for the publish
   (`publish_payload_manifest` / `stabilize_control_slots`), after the
   off-mutex file work + fsync chain completed.
6. Rotate marks (current becomes previous), persist epoch metrics, back off.

The daemon schedules an epoch when the maintenance lane reports idle, with
exponential backoff when no garbage is found, and expedites an epoch when
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
soak hit it at 7.7M commits and wedged the rotation pack merge). The
packer backs off new-segment production at ≥95% except for checkpoint
drains, and its steady-state batch target is 32 MiB so steady segment
production stays well ahead of the ceiling.

### Safety rules (each is load-bearing; tests pin every one)

- **S1 two-mark rule.** Only suspects unmarked in two consecutive marks may
  be deleted. Anything committed, packed, pinned, or staged after epoch N's
  scan is either absent from the suspect list or marked in epoch N+1.
- **S2 pin-inclusive marks.** Every mark unions the current root and all
  transient pin roots active at snapshot time. A pin acquired between
  snapshot and deletion can only reference pages reachable from a root ≥
  the snapshot root or newly written pages; confirmed garbage is neither
  (CoW induction + S1 mtime grace), so deletion never touches it.
- **S3 overlay coverage.** Committed-but-unmerged overlay deltas are covered
  by the WAL committed-envelope projection, because checkpoint publishes
  merge the overlay fully first (invariant already enforced by
  `EntityDatabase` checkpoint paths). The collector asserts the active-WAL
  projection is captured at every snapshot.
- **S4 mtime grace.** Confirmed deletion additionally requires file mtime
  older than `ONLINE_GC_CANDIDATE_MIN_AGE`, so even a pathological
  long-lived staged blob/transaction cannot straddle both scans.
- **S5 publish atomicity.** All catalog/CONTROL mutations keep using the
  existing `publish_payload_manifest` + `stabilize_control_slots` pair under
  the authority mutex; a crash anywhere in Delete leaves either the old or
  the new catalog, never a torn one, and leaves at worst orphan files that
  a later epoch re-collects. Unpublished background-pack segments keep
  their existing orphan protection.
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
  mutex (single-writer model), so the fence is an in-memory
  `recent_relink_ids` set in `BlockStore`: cleared at every epoch snapshot
  hold, appended on every `(id, false)` return, capacity 16 384 with an
  overflow flag. Every deletion hold (loose batch, segment-rewrite finish,
  orphan/temp removal with mtime grace) skips candidates present in the set
  and defers the whole batch when the overflow flag is set. `remove_blocks`
  already evicts the payload cache, so a deleted id can never be served
  from cache afterwards; a later probe downgrades to `Missing` and
  re-stages, which is always safe. Stale bloom bits for removed segments
  are harmless because `probe_dedupe` claims existence only after reading
  candidate bytes through the *current* manifest.

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
as the offline collector: victim ≤ 256 MiB and ≤ 65 536 blocks per delete
batch, temporary spill ≤ min(1 GiB, 2 % volume free), catalog window 16
segments, loose directory scan ≤ 20 000 000 entries, compaction floor 128
blocks. New constants (owned by `authority_gc_online.rs`, documented in
`packages/tofu-db/AGENTS.md`): `ONLINE_GC_CANDIDATE_MIN_AGE` = 120 s,
`ONLINE_GC_MAX_EPOCH_MARK_BUILD_SECONDS` = 900 (epoch aborts and retries,
never holds anything), idle backoff 250 ms → 60 s.

## Observability

Epoch metrics (marks built, suspects, confirmed, reclaimed bytes/blocks per
kind, segment rewrites, last-epoch duration, epochs completed) are exported
through `system.health` and returned by `system.reclaim`. No log spam: one
line per completed epoch at INFO, deletions at DEBUG.

## Test plan (test ladder, smallest first)

1. Unit: `pinned_snapshot_roots` registry enumeration; multi-root mark
   union; two-mark confirm transitions; mtime grace filtering.
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

No on-disk format change: marks and suspect lists are spill files under
`authority-gc/online/`, never read across process boundaries, wiped at
thread start. The offline CLI keeps its exclusive session semantics
unchanged. CONTROL/history/payload formats are untouched.

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
probe-derived GC workspace ceiling and deducted from mark spilling allowance.
Selection storage is released during marking or on abort. Tests measure typical
and maximum 32,768-segment selections; no new pool, cache or persistent data exists.

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
