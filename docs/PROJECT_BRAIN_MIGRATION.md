# Project Brain — idle-sibling epic migration (`dispatch_target`)

> Status: DESIGN + MECHANISM (RED-first); sweep wiring PAUSED pending owner
> review. Third improvement after block-cooldown (`pt_4137ae06`) and
> wait-on-path (`pt_05b18689`).

## The gap it closes

Dispatch always routes an epic to `created_by_conv` (the conversation that
posted it) — `sweep_dispatch`, `on_epic_completed`, and the reconcile pass all
target the originator. If that originator is **genuinely unable to run the
epic** — its conversation row was deleted, its kickoff repeatedly fails to
spawn, it's abandoned — the epic is claimed by it, a kickoff sits undrained in
its queue, and `_reconcile_stranded_kickoffs` re-attempts the SAME dead conv on
every sweep, forever. The work can never move to an idle sibling that *could*
do it.

## The invariant: provenance is immutable, routing is mutable

Owner ruling: **do NOT overwrite `created_by_conv`.** Add a separate
`dispatch_target` column. Dispatch routes to `dispatch_target or
created_by_conv`. The two concepts stay orthogonal:

| field | meaning | mutability |
|---|---|---|
| `created_by_conv` | who POSTED the epic (authorship) | IMMUTABLE — durable audit/feed provenance |
| `dispatch_target` | who should RUN it next | MUTABLE — a runtime routing decision |

Migration only ever sets `dispatch_target`; the feed/board history of who
authored the epic is never corrupted, and a future "return work to its
originator" policy still has the authorship to key on.

## Detecting "originator stuck" WITHOUT a new timer

The load-bearing question. Owner ruling: the originator is stuck ONLY if it has
NO live task AND its kickoff has been undrained/unstarted past a threshold —
reuse the lease/heartbeat clock, not a new timer. Do NOT migrate a merely-busy
originator (that's working, not stuck).

The durable clock already exists: **`message_queue.created_at`** (BIGINT ms) —
the timestamp of the queued `KIND_WORKFLOW` kickoff. `_kickoff_age_ms(conv,
epic, now)` reads it. An epic's originator is stuck iff ALL of:

1. **A kickoff for this epic is queued** on the target, and its age >
   `MIGRATION_STUCK_MS` (= `DEFAULT_LEASE_TTL_MS`, 30 min — the SAME lease
   clock). Rationale: a HEALTHY idle conv drains its kickoff within one 30 s
   sweep (`_reconcile_stranded_kickoffs`). A kickoff still sitting after a FULL
   lease window means the drain has failed across ~60 sweeps AND the claim
   itself would have expired and re-dispatched and re-failed — unambiguously
   stuck, never a transient. No new clock: the queue row's own age is the
   heartbeat.
2. **The target has NO live task** (`not _conv_has_live_task`). A busy
   originator is WORKING, not stuck — never migrate it (owner requirement #1).
3. **The epic is NOT on a live cooldown and NOT on a live wait-on-path.** Those
   mean it is correctly HELD, not stuck (owner requirement #3 — compose, don't
   override). `blocked_until <= now` AND `_paths_waited_but_held(...) == []`.

This is entirely at-read-time and self-correcting: if the originator recovers
(drains the kickoff, or starts a task) before the age threshold, it is never
seen as stuck. Migration itself needs no timer to expire — a migrated epic is
just re-routed and re-dispatched.

## Picking the target (owner requirement #2)

`_pick_migration_target(project_path, exclude_conv, now)` returns a sibling conv
that is GENUINELY idle — never move the strand into another dead end:

- belongs to this project (`settings.$.projectPath == project_path`, the same
  query `project_digest_entries` uses),
- is NOT the originator (`!= exclude_conv`),
- has NO live task (`not _conv_has_live_task`),
- has NO queued kickoff of its own (`not _has_queued_kickoff`),
- its conversation row EXISTS (so the drain can actually spawn).

Returns `''` when no idle sibling exists → the epic stays with its originator
(no migration; the reconcile keeps trying). Prefer the most-recently-updated
idle sibling (recency-ordered query) as the likeliest-live candidate.

## The migration act (owner requirement #4 — bounded + audited)

`migrate_epic(project_path, epic, new_target)`:
1. set `dispatch_target = new_target` on the row (provenance untouched),
2. DROP the stale `KIND_WORKFLOW` kickoff for this epic from the originator's
   queue (else `_reconcile_stranded_kickoffs` keeps re-draining the dead conv),
3. reopen the claim (`status=open`, clear owner/lease) so `select_dispatchable`
   re-picks it and routes to the new target,
4. emit a feed `note` + `audit_log('brain_migrate', from, to, reason)` so a
   human sees "epic X migrated convA→convB because A was idle-stranded".

Bounded: the sweep pass migrates at most ONE epic per sweep (like the reconcile
pass). `dispatch_target` resets to `''` on complete/reopen (a human reopen
restores originator-first routing).

## Data + build order

- **Schema (v41):** `project_tasks.dispatch_target TEXT NOT NULL DEFAULT ''`
  (nullable-safe read → `''`).
- **Mechanism (this doc's deliverable, RED-first):** `_dispatch_target`,
  `_kickoff_age_ms`, `_originator_stuck`, `_pick_migration_target`,
  `_drop_epic_kickoffs`, `migrate_epic`; `_row_to_task` field; reset on
  complete/reopen. NOT wired into the sweep yet.
- **PAUSE — show the mechanism + stuck-detection to the owner.**
- **Wiring (deferred):** route `sweep_dispatch` / `on_epic_completed` /
  `_reconcile_stranded_kickoffs` to `_dispatch_target(epic)`; add
  `_migrate_stranded_epics(project_path)` as a bounded pass in `sweep_dispatch`
  (after reconcile, before the dispatch loop).
- Fresh-HEAD-worktree acceptance; queue on the contested tree (depends on the
  cooldown + wait-on-path epics — shares the same files).

## Invariants (same bar as cooldown + wait-on-path)

1. Provenance (`created_by_conv`) is NEVER overwritten.
2. Stuck detection reuses the queue/lease clock — NO new timer.
3. Never migrate a busy originator, and never migrate INTO a busy/queued/absent
   target.
4. Compose with cooldown + wait-on-path — an epic correctly held by either is
   not "stuck" and is not migrated.
5. Bounded (1/sweep) + audited + self-correcting (recovered originator is never
   migrated; no lock, no deadlock).
