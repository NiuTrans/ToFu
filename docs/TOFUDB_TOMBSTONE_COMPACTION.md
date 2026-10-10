# Tombstone physical compaction (2026-09-22)

Status: **L1 + L2 implemented, deployed and production-verified**; L3
(offline backlog vacuum via range machinery) remains the operator runbook
below. Feature-gated by `TOFU_DB_TOMBSTONE_RECLAIM` (default off; enabled
in the personal deployment after OCC-matrix and differential validation;
rollback is data-safe — reclaimed entries stay absent, a valid state on
every build).
Owner surface: `packages/tofu-db/src/entity.rs` (tree/merge), `entity_overlay.rs`
(delta overlay), `maintenance.rs` (background scopes).

Production verification (2026-09-23, ~25 min after enablement on the FUSE
authority): `tombstoneCompactionRounds: 40`,
`tombstoneEntriesReclaimed: 22,053`, `tombstonePagesRewritten: 1,961`,
`conflictDeferrals: 0`, `failedRounds: 0` — the legacy tombstone backlog is
being physically reclaimed in bounded rounds with zero wedge recycles,
zero retention failures and zero `database_resource_exhausted` in the
window.

## Mounted-range correctness

Problem: a base tombstone can be the only mask over a live mounted value;
age alone does not make it obsolete. Decision: both repack and vacuum retain
all tombstones covered by a mount until that mount is consolidated or retired.
Vacuum replaces `base_root` inside the complete directory, preserving mounts
and partial-overlay coverage even when the base becomes empty. Alternatives:
dropping age-eligible masks resurrects values; publishing just the base loses
mounted ranges. The retained `audit_*` entity tests cover both after reopen.

## Problem

Entity deletes never leave the B+ tree. `EntityDatabase::delete` stages a
`(key, None)` write; the overlay merge (`merge_overlay_chunk` →
`Tree::apply_batch` → `append_merged_leaf_entries`) writes it into leaves as
`EntityValue { version, value: None }` — a **tombstone** — and every later
page repack "preserves tombstones and their OCC versions"
(`entity.rs:2040`). Tombstones are dropped only when a page happens to be
rewritten for another reason; pages that receive no further writes keep them
forever.

Scans skip tombstones when collecting rows but still pay traversal and decoded
page costs. Range witnesses retain every visited leaf without a leaf-count
admission quota. Before quota removal, a 25-row `event.prune` scan on
2026-09-22 walked 1,165 nodes collecting zero live rows and failed at the old
1,024-leaf cap. That historical resource refusal is retired. Churn-heavy
time-ordered indexes still delete at the old end and append at the new end,
leaving a tombstone field that no later write touches; L1/L2 reclaim its real
space and scan cost. Masked base-row scans now advance bounded cursor windows
until their requested visible result or actual range exhaustion, retaining each
window's OCC witness and rolling back new reservations if a later window fails.

Three distinct costs, all growing with churn and never reclaimed:

1. **Scan amplification** — bounded scans cross dead regions at full leaf
   cost; dense fields exhaust the witness budget and fail closed.
2. **Space** — tombstone entries occupy live pages (key + 13 B each) and
   their old versions pin no data but are never collected (block GC only
   reclaims *unreachable* blocks; tombstoned pages stay reachable).
3. **Commit/scan latency on FUSE** — every dead leaf in a walk is a ~7 ms
   uncached read on the production volume.

## Why tombstones exist (the contract that constrains reclamation)

`EntityValue.version` is the per-entry commit sequence and feeds exactly
three OCC surfaces (all inside `entity.rs`; semantic modules never see
versions):

1. **Point witnesses** — `get` records `Option<version>` per key
   (`entity.rs:3466`); commit verification re-reads the CURRENT root and
   compares (`entity.rs:4100`). `Some(V)` (live *or* tombstone) and `None`
   (absent) are **distinct**: `entity_tests.rs::
   coalesced_leaf_deletes_preserve_absence_witnesses_and_untouched_references`
   pins that an insert/delete ABA must conflict a transaction that witnessed
   the earlier absence ("Insert/delete ABA remains visible to both point and
   range OCC").
2. **Write witnesses** — every written key's snapshot-vs-current version is
   compared (`entity.rs:4108`), same distinction.
3. **Range witnesses** — the leaf BlockId set (`entity.rs:4053`); tombstones
   affect it only through which pages exist.

Pinned snapshots (reader replicas, persistent root pins) hold COW roots, so
compaction of the *current* tree can never change what any pin observes.
The only affected parties are **in-flight transactions** whose recorded
witnesses compare against the current root: dropping tombstone `(K, V)`
flips their comparison `Some(V) → None` and yields a conservative
`database_busy` conflict → retry → re-read `K` as absent → proceed. Final
states stay serializable (a transaction that read `K` absent and commits
after `K`'s create+delete+reclaim sees exactly the absent state it assumed);
the cost is a bounded false-conflict rate, not correctness.

## Decision (proposed)

Reclaim tombstones physically, in three cooperating layers, with an
explicitly documented semantic boundary change:

**L1 — write-path hygiene (free reclamation).** When
`Tree::apply_batch`/`append_merged_leaf_entries` repacks a leaf run *for any
reason*, drop entries that are tombstones with `version < reclaim_below`,
where `reclaim_below = oldest_live_pin_sequence()` (the existing pin
registry minimum, `entity.rs:4283`; `u64::MAX` when no pins are open)
further clamped by a fixed age margin. Threading: `apply_batch` gains one
`reclaim_below: u64` parameter; the overlay merge computes the watermark
beside its existing `note_merged` call. Every repack then vacuums the run it
already pays to rewrite — tombstones entering the tree stop accumulating
*from that moment on*, at zero added I/O.

**L2 — scan-driven compaction hints (targeted, self-medicating).** A scan
that crosses more than `T` dead leaves in one window (the exact signature
that today causes witness overflow) enqueues an in-memory hint
`(start_key, end_key, dead_leaves)` into a small bounded registry
(cap ~64, coalesced by overlap). The maintenance worker owns a new bounded
scope that, per round, pops one hint and rewrites the covered pages in
slices (same COW commit path as L1, budgeted like the overlay drain:
bounded rows/bytes/wall-time, doorbell + idle scheduling, never on the
foreground path). The regions that slow scans are precisely the regions
that get vacuumed; no persistent state, no full-tree walks, hints are
advisory and loss-free on restart (the next slow scan re-issues them).

**L3 — one-time backlog crossing (migration).** The existing production
backlog (weeks of accumulated tombstones) is crossed once by L2 hints alone
only where scans go; for a complete vacuum, an operator-driven offline pass
reuses the existing, certified range machinery: `retire_range` →
`stage_persistent_range_snapshot_restore` → mount consolidation already
materializes **live rows only** (`consolidate_one_range_mount` unwraps live
values), i.e. it is a tested physical tombstone vacuum scoped to a range.
Run per churn-heavy namespace prefix, bounded, resumable, and *offline or
quiesced* (it moves live data; the online L1/L2 layers keep the tree clean
afterwards).

**Semantic boundary (stronger than first drafted, verified in
`entity_tombstone_tests.rs`).** The reclamation watermark is the oldest
live pin sequence, and *every* open transaction registers a snapshot pin —
so any tombstone younger than the oldest open transaction is never
reclaimed underneath it. Two consequences:

* The strict insert/delete ABA-visibility contract holds unchanged for
  every live reader: a transaction that witnessed `K`'s absence holds a
  pin older than any subsequent create+delete of `K`, which therefore
  stays visible to its OCC check (`absence_aba_stays_strict_while_the
  _witness_holds_a_pin`). The named legacy test needs no change.
* The only observable effect of reclamation is a **conservative false
  conflict**: a transaction that witnessed `K` *as a tombstone* (version
  `V`) commits after its own snapshot admitted `V` and a later watermark
  reclaimed it — `Some(V)` vs `None` fires a `database_busy` retry, the
  retry re-reads `K` as absent and proceeds
  (`occ_conflict_on_reclaimed_witness_is_conservative_and_recovers`).
  Final states stay serializable; the cost is a bounded retry rate,
  mitigated by reclaiming only sub-watermark entries in bounded slices.

The opt-in `synchronous_tree_reclaim_defers_to_a_live_snapshot_pin_and_resumes`
regression disables the write overlay and checks commit-path deferral, pinned
pre-delete reads, and reclamation after pin release; overlay-merge lifecycle
coverage remains separate. The reclamation default remains off.

## Alternatives considered

1. **Raise the retired leaf-count cap.** A larger cap would only postpone valid
   query refusal and would leave tombstone storage/traversal costs unchanged.
   The cap is removed; L1/L2 remain responsible for physical reclamation.
2. **Tombstone≡absent in OCC comparisons.** Makes compaction invisible to
   OCC but silently breaks the pinned ABA contract in *both* directions
   (absence witnesses and tombstone witnesses), including for *hot* keys.
   Rejected: the regression is unbounded; L1–L3's bounded weakening names
   its exact boundary instead.
3. **Internal-node tombstone counters** (per-subtree dead-entry counts in
   the page format) for density-driven compaction. Precise targeting but a
   disk-format change with migration of every existing tree; L2's hints get
   90% of the value from already-paid scan work with zero format change.
4. **Retire+consolidate as the *online* mechanism.** The range machinery
   vacuums tombstones but moves all live rows of the range through
   capsules/mounts — heavy under load and disruptive to witnesses; kept as
   the offline L3 migration tool only.
5. **Do nothing beyond the retention sweep.** The sweep fixed the prune
   deadlock, but scan amplification on read paths and space growth remain,
   and every churn-heavy index re-approaches the witness cliff over time.

## Interaction with existing mechanisms

- **Overlay merge** (`merge_overlay_chunk`): preserves per-key original
  commit versions, so merges today never flip OCC witnesses; L1 keeps that
  property (reclaimed entries are only tombstones below the pin watermark).
- **Range witnesses**: compaction rewrites pages → new BlockIds → covered
  range witnesses conflict (conservative retry), identical to any write in
  the region; L2's slices + duty cycle keep the induced conflict rate
  negligible.
- **Block GC**: reclaimed entries make old page versions unreachable; the
  existing collector reclaims them through the normal boundary — that is
  where the space win materializes.
- **Range mount consolidation**: unchanged; it is already a live-rows-only
  materialization and composes with L1.
- **Aggregate transaction byte budget / witness caps**: unchanged; L2 slices
  respect all existing bounds.

## Implementation record (phases 1–3 landed)

| Phase | Content | Verification (landed) |
|---|---|---|
| 1 | L1 `reclaim_below` through `apply_batch`/`apply_page`/`append_merged_leaf_entries` on both production write paths (commit + overlay merge), batch-tombstone delete-as-absence, empty-subtree collapse, Option root | `entity_tombstone_tests.rs`: watermark respected, no-resurrect, collapse, OCC conflict+recover, pin protection; `leaf_repack_reclaims_tombstones_below_the_watermark`; full `cargo test` |
| 2 | L2 dead-leaf counting in scans → bounded advisory hint registry (replica-shared) → `reclaim_tombstones_in_range` (bounded, resumable, crash-atomic) → maintenance `TombstoneCompacted` round + metrics | hint channel/coalesce tests, bounded+resumable sweep tests, `compaction_round_vacuums_a_reported_dense_window`, foreign-scope requeue test |
| 3 | Feature gate (`TOFU_DB_TOMBSTONE_RECLAIM`, `_DAEMON_ENV_PASSTHROUGH` registered) + release | `tests/tofudb/test_tofudb_differential.py` 181/181; deployment passthrough test; production metrics above |
| 4 | L3 offline backlog vacuum (operator runbook) | pending (certified backup first; post-vacuum integrity `verify`; retention/search differential) |

Rollback: L1/L2 are ordinary COW commits (no format change) and the flag
flips off per daemon; already-reclaimed entries stay absent, which every
engine build reads as a valid state — no data migration either way.

## Open questions for the implementation phase

1. The age margin below `oldest_live_pin` (seconds vs sequences): pick from
   measured transaction lifetimes (p99.9) on the production host.
2. Hint threshold `T` (dead leaves per scan window) and L2 slice budgets —
   calibrate against the FUSE volume's leaf-read cost so one round stays
   well under the authority wedge threshold (10 s), like the retention
   sweep's 5 s call budget.
3. Whether `turn.rs`'s churn indexes (sync replay, attempt events) need
   their own sweep-cursor pruning *in addition to* L1/L2, or whether
   physical compaction alone restores their scan envelopes (decide from
   phase-2 metrics).
