# Project Brain — wait-on-path (commit-dependency) primitive

> Status: DESIGN + MECHANISM built (RED-first), dispatch wiring PAUSED pending
> owner review. Sibling of the block-cooldown backoff (board `pt_4137ae062bfd49ed`).

## The problem it closes

Three of the four epics that were "stuck forever" on this project share ONE
failure mode: **"conversation X must commit file Y before my epic can
proceed."** (Decoupling A part 2/2 waits on a charter-owner commit of 6 files;
`_now_ms` consolidation waits for `utils.py`/`log.py` to quiesce; Paper Review
#3 waits on the Epic E `paper/report.js` cut.) This very session is a *fourth*
instance — the block-cooldown feature is fused with a sibling's uncommitted
park-removal and cannot land until that commits.

The board today has NO way to express this. `depends_on` only chains epic→epic
(intra-board), so the agent's ONLY recourse when it discovers a cross-conversation
commit dependency is `project_board_block` — which now (post block-cooldown)
puts the epic to sleep on an escalating backoff. That is *cheap* but *dumb*: the
brain re-attempts on a fixed schedule and re-discovers the unmet dep every time.
The cooldown stopped the bleeding; it did not make the brain *aware* of what it
is waiting for.

## The insight: it is the INVERSE READ of the path-lease

A **path lease** (`claim_lease`, `kind='lease'` row) already encodes:

> "conversation X is actively touching path Y — hold off." (`title=Y`,
> `owner_conv_id=X`, soft TTL + at-read-time expiry.)

A **commit-dependency** is the same fact read from the other side:

> "hold my epic while path Y is actively held by someone else."

So we add **NO third dependency namespace**. The three stay orthogonal and
composed:

| primitive        | shape          | who sets it        | resolves when                          |
|------------------|----------------|--------------------|----------------------------------------|
| `depends_on`     | epic → epic    | poster             | the dep epic is `done`                 |
| path lease       | conv → path    | the editor         | the editor releases / TTL expires      |
| **wait-on-path** | **epic → path**| the blocked worker | **no live lease holds the path** (by another conv) |

wait-on-path is stored as a new `project_tasks.wait_paths` JSON array on the
epic row. `select_dispatchable` skips the epic while ANY listed path is under a
LIVE lease held by a *different* conversation — resolved by reading the SAME
lease rows the board already maintains (`_effective_status` at read time).

## Why lease-based, NOT git-clean-based (the load-bearing decision)

The naive reading is "wait until path Y is committed to HEAD." **Rejected** — it
has no self-expiry. If a sibling abandons Y dirty forever (crash, context loss,
a human walking away), a git-clean wait blocks the epic *forever* with no
automatic release. That is precisely the human-gated, cannot-self-expire hold
the owner forbade when we built the cooldown ("if it needs a human to clear,
it's park and I reject it").

Keying the wait on the **live lease** gives self-expiry for free, from the
lease's OWN TTL invariant:

- A sibling holding path Y keeps its lease alive by re-claiming each turn (zero
  cost — the every-turn board re-read). While it actively works, the epic
  correctly holds.
- The sibling crashes / abandons / finishes → its lease expires within ONE TTL
  (`DEFAULT_LEASE_TTL_MS`, 30 min) or is released explicitly → `_effective_status`
  reads the path unheld → the wait clears → the epic is dispatchable again.
- A path that NOBODY holds a lease on NEVER strands the epic (empty resolver
  set → not held → dispatchable). So a stale `wait_paths` entry can never
  deadlock.

The two mechanisms compose cleanly: when the lease clears, the epic retries; if
the awaited commit still hasn't actually landed, the worker re-discovers that
and `block`s → the cooldown cheaply absorbs the interim. wait-on-path makes the
brain *hold precisely as long as a sibling is actively editing the path*, and no
longer.

## Data + API

- **Schema (v40):** `project_tasks.wait_paths TEXT NOT NULL DEFAULT '[]'` — JSON
  array of path/resource strings. Nullable-safe read (pre-migration row → `[]`).
- **`set_wait_paths(project_path, conv_id, task_id, paths)`** — set/replace the
  epic's wait list (empty list clears it). Records a feed note. Reset to `[]` on
  `complete_task` and `reopen_task` (same as block state — a human reopen forces
  a fresh evaluation).
- **`_paths_waited_but_held(task, board_tasks, now_ms)`** — pure resolver: given
  an epic's `wait_paths` and the board's live lease rows, return the subset of
  paths currently held by a DIFFERENT conversation's live lease. Empty → not
  waiting.
- **`select_dispatchable`** — skip an epic when `_paths_waited_but_held(...)` is
  non-empty (AFTER the existing status/cooldown/dep filters; same at-read-time,
  no-reaper discipline).
- **`render_board_block`** — an epic waiting on a live-held path shows in a
  "Waiting on paths" annotation (who holds what), so the human sees WHY.
- **Tool surface (deferred to wiring step):** `project_wait_on_path(task_id,
  paths)` for the worker; the dispatch kickoff prompt teaches the worker to use
  it instead of a bare `block` when the blocker is "sibling editing path Y".

## Invariants this must uphold (same bar as the cooldown)

1. **Self-expiring, no human un-block gate.** The wait resolves purely from
   lease TTL expiry at read time. NO reaper, NO required human action. (This is
   what keeps it out of park-2.0 territory.)
2. **No third namespace.** Reuse the lease rows; `wait_paths` is a read-time
   join against them, not a new lock table.
3. **Fail-open.** A missing column, an empty list, an unparseable value, or a
   path nobody leases → the epic is DISPATCHABLE. wait-on-path can only ever
   ADD a hold that a live lease justifies; it can never strand.
4. **Reset on complete/reopen.** Terminal + human-revive clear the wait.

## Build order

1. schema v40 column (+ migrations) — done RED-first
2. `set_wait_paths` + `_paths_waited_but_held` + nullable-safe `_row_to_task`
   field — the MECHANISM (this doc's deliverable)
3. **PAUSE — show the mechanism + tests to the owner**
4. wire `select_dispatchable` skip + `render_board_block` annotation
5. `project_wait_on_path` tool + kickoff-prompt guidance
6. fresh-HEAD-worktree acceptance; queue on board (contested tree — same
   non-committable disposition as the cooldown).
