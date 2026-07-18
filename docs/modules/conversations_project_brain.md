# Module Design Doc — Unit 6: Conversations & Project Brain (`lib/conversations/`)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md`,
> `docs/PROJECT_BRAIN.md`, `docs/PROJECT_BRAIN_STATUS_LANE.md`). This unit is the
> cross-conversation coordination substrate — the "Project Brain" — plus the
> conversation-domain helpers (title/meta/search/reconcile) that used to live in
> `routes/`.
>
> **⚠️ DOCUMENT, DO NOT REFACTOR.** This is the most contested package in the
> tree: `git status` shows 9 of 22 modules dirty right now and 3 live sibling
> conversations are advancing epics here (the seven-pillar brain, the
> status/watch lane, dispatch). This doc describes the CURRENT on-disk
> segmentation. Where a file is mid-migration, it is FLAGGED with what it waits
> on — NOT given a split verdict that would collide with in-flight work.
>
> **Grounding:** every line count is `wc -l` on disk 2026-07-11. Contested-state
> from `git status --short` at read time.

---

## 1. The analytical payload: coordination-mechanism integrity

The load-bearing question: are the seven pillars actually separable modules with
clean seams, or has the "brain" accreted into a tangle where one aggregation is
hand-rolled in five places? The charter names the invariant: `claims_by_conv`
(the presence⋈task⋈board join) is supposed to be the SINGLE aggregation every
surface reuses. Verified on disk.

### 1a. `claims_by_conv` — the single join IS single-sourced (verified)

Defined ONCE at `project_board.py:266`, a pure side-effect-free function
(`owner_conv_id → claimed-epic title`, keyed on effective status so an expired
lease never appears). Every consumer IMPORTS it — none re-implements it:
- `project_brain_summary.py:66-67` — `claim_by_conv = claims_by_conv(board_tasks)`
  (the collab-bar `peerEpics` join).
- `project_peer.py:304-305` — `claims_by_conv(read_board(...).get('tasks', []))`
  (the `build_peer_status` peer→epic join). Its docstring: "Reuses
  `claims_by_conv` — the SAME join `build_brain_summary` uses — so the two views
  can never drift."
- No hand-rolled `owner_conv_id`→title join exists anywhere else (grep-confirmed).

**The invariant holds for the claim→conv join.** The board's `read_board` also
does the at-read-time lease reclaim ONCE, so there is exactly one deadlock path
and one claim-join — both single-sourced.

### 1b. The nuance: there are TWO aggregations, and the SECOND is duplicated by design

`claims_by_conv` (the join) is single-sourced. But the WIDER "read all pillars
into one evidence dict" aggregation exists in TWO places, and this is the one
finding worth flagging — it is deliberate, but it is real duplication that could
rot:

- **`build_brain_summary`** (`project_brain_summary.py:37`) — the COLLAB-BAR
  aggregation. Reads board counts + `claims_by_conv` + `pending_proposals` +
  charter existence + presence peers + conflict advisories + `status_line`.
- **`collect_pillar_state`** (`project_status.py`, Pillar #7) — the STATUS-LANE
  aggregation. Reads board counts + in-flight epics + charter (north-star +
  decisions + version) + `pending_proposals` + presence peers + recent blocks +
  sibling digest.

`collect_pillar_state`'s docstring explicitly claims it "is the SAME cross-pillar
join `build_brain_summary` performs" — but it does NOT call `build_brain_summary`
or share its body; it independently re-reads `read_board`, `read_charter`,
`pending_proposals`, and `presence.snapshot`. So the two aggregations:
- **DO** share the atomic sub-readers (`read_board`, `pending_proposals`,
  `presence.snapshot`, `claims_by_conv`) — so the *primitive* single-source
  discipline holds; neither re-implements a join.
- **do NOT** share the composite aggregation shape — `build_brain_summary` and
  `collect_pillar_state` are two hand-maintained readers of the same pillars,
  with overlapping-but-different field sets.

**Assessment (document, not prescribe):** this is a SEAM-DUPLICATION risk, not a
correctness bug today, because both compose the SAME single-sourced primitives
(the thing the charter actually mandates — `claims_by_conv`, `pending_proposals`,
`read_board` — is not re-implemented). The divergence is justified: the collab-bar
needs a tiny hot always-on summary (+ conflicts + status_line), while the status
lane needs a richer evidence dict for LLM synthesis (+ decisions + blocks +
siblings) — a single function serving both would over-fetch for the bar. The
watch lane (`project_watch._item_fingerprint`) correctly REUSES
`project_status._fingerprint` + `collect_pillar_state` rather than adding a third
reader — evidence the reuse discipline is being followed for NEW pillars. **The
right note for the eventual refactor: if a THIRD composite reader appears, extract
a shared `collect_pillar_state` core that `build_brain_summary` projects a subset
of — but do NOT force it now (Pillar #7 is actively being built by a sibling).**

### 1c. Pillar separability — the seams ARE clean

Each pillar is its own module with a single concern and a documented "keyed on
`project_path`, never a process-global; best-effort, never raises" contract
(verified in every docstring read):

| Pillar | Module | Concern |
|---|---|---|
| #1 Feed | `project_feed.py` | append-only per-project event log + PushHub mirror |
| #2 Charter | `project_charter.py` | shared intent (north-star + committed decisions), propose/commit gate |
| #3 Board | `project_board.py` | coarse epics, soft TTL leases, `claims_by_conv` (the join home) |
| #5 Dispatch | `project_dispatch.py` | select + kick off dispatchable epics via message_queue |
| #6 Peer | `project_peer.py` | peer status / messaging / intervention |
| #7 Status | `project_status.py` | human↔brain status snapshots (append-only trail) |
| #7 Watch | `project_watch.py` | human standing watch-list, addressed recurringly |
| (agg) | `project_brain_summary.py` | collab-bar summary (consumes #1/#2/#3/presence) |

The pillars depend DOWNWARD on shared primitives (`project_feed.normalize_project_path`
is the single path-canonicalization seam every read/write funnels through;
`project_board.claims_by_conv`; `project_charter.pending_proposals`) and on
`lib/presence/` + `lib/database`. **No pillar reaches sideways into another's
internals** — cross-pillar needs go through the documented public function
(e.g. charter commit → `build_status_snapshot(blocking=False)` +
`address_open_items(blocking=False)` as best-effort warm triggers). This is a
clean pillar architecture, not a tangle.

---

## 2. Contested state (read at 2026-07-11 — the document-not-refactor constraint)

`git status --short lib/conversations/` shows these DIRTY (uncommitted sibling
work in flight):

```
 M __init__.py  meta_cache.py  project_brain_summary.py  project_commit.py
 M project_feed.py  project_peer.py  reconcile.py  search_index.py  settings_store.py
?? turn_initiation.py  vu_translate_backfill.py
```

Plus 3 live sibling conversations (peer status at read time): two advancing
frontend-comms / autopilot-summary + paper-report work, per the board. **The
JOURNAL confirms the Pillar-7 backend (`project_status.py`, `project_watch.py`)
landed at `7416b58` but its FRONTEND tail + charter-CRUD route half are still
in-flight across `project.py` / `api.js` / `project-brain.js`.**

**Mid-migration files — flagged, NOT split-verdicted:**
- `project_peer.py` (dirty) — actively being extended (the human-nudge /
  status-lane peer surface). Do not propose a split.
- `project_brain_summary.py` (dirty) — the `status_line` field (§1b) was just
  wired in; it's mid-change.
- `reconcile.py` (dirty) — the JS→backend reconcile migration (§3.4) is the
  charter's patch-register item #1, in progress.
- `project_commit.py`, `project_feed.py`, `meta_cache.py`, `search_index.py`,
  `settings_store.py`, `__init__.py` (dirty) + `turn_initiation.py`,
  `vu_translate_backfill.py` (untracked) — all carrying uncommitted sibling
  hunks. Documented as-is; no structural verdict.

---

## 3. Module inventory (real `wc -l`, size verdict, status, tests)

### 3.1 The seven pillars + aggregation

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `project_board.py` | 1071 | **BIG** | HOT (contested-adjacent) | `test_project_board`, `_lease`, `_migration`, `_wait_on_path`, `_block_cooldown`, `_class_aware_backoff`, `_no_defer` |
| `project_peer.py` | 914 | **BIG** | HOT (**dirty**) | `test_project_peer`, `test_project_peer_human_nudge` |
| `project_dispatch.py` | 843 | **BIG** | HOT | `test_project_dispatch`, `test_project_brain_integration` |
| `project_charter.py` | 634 | **BIG** | HOT | `test_project_charter` |
| `project_status.py` | 582 | OK | live (Pillar #7, new) | `test_project_status_lane` |
| `project_watch.py` | 470 | OK | live (Pillar #7, new) | `test_project_watch_lane` |
| `project_feed.py` | 283 | OK | HOT (**dirty**) | `test_project_feed`, `test_project_feed_read_tool` |
| `project_brain_summary.py` | 140 | OK | HOT (**dirty**) | `test_project_brain_summary`, `test_project_brain_live_paths` |

`project_board.py` — **BIG.** Bundles: epic CRUD + the soft-lease model
(`claim_task`/`_effective_status`/at-read-time reclaim) + `claims_by_conv` (the
join) + the path-lease primitive (`claim_lease`/`release_lease`, the Pillar-#6
file-reservation from the charter) + `render_board_block` (prompt injection incl.
the Landing lane) + the ready-marker denylist + block-cooldown/backoff. That is
5–6 concerns, and it's the biggest file in the unit — BUT it is board-adjacent to
active dispatch/ready-marker work (the JOURNAL shows `5c81f5a`/`13a9be0`/`a7aa66b`
all touched it recently). **Flagged BIG; a split (e.g. `project_board_lease.py`
for the path-lease + `project_board_render.py` for the block) is plausible but
must wait for the dispatch/ready-marker epics to settle — proposing it now
collides with the exact files siblings are landing.**

`project_peer.py` — **BIG + DIRTY.** Three verbs (status/message/intervene) +
`_join_peers` + rate-limit/storm-guard + human-nudge surface. Cohesive per the
charter's Pillar-#6 invariants, but actively being edited. Document only.

`project_dispatch.py` — **BIG.** select_dispatchable + dispatch_epic +
the stranded-kickoff reconcile + orphan-queue-on-startup + the auto-land-ready
heartbeat hook. Cohesive (all "make the board autonomous"), heavily tested. The
recent ready-marker/landing loop work lives partly here. Flagged BIG; defer.

`project_charter.py` — **BIG but cohesive.** read/propose/commit + decision
CRUD (update/delete) + dismiss + `pending_proposals` (the single pending source)
+ `repair_truncated_decisions` + `render_charter_block` + `execute_charter_tool`.
One concern (the charter contract) with a rich but coherent surface. The
decision-CRUD half is the charter-CRUD route work the JOURNAL flags as in-flight
(`f93b7a4` landed the backend). Flagged BIG; defer.

`project_status.py` / `project_watch.py` — **OK, and freshly landed** (`7416b58`).
Both are clean single-concern Pillar-#7 modules with the documented
append-only-trail + laziness-gate + human-facing-only invariants. `project_watch`
correctly reuses `project_status._fingerprint`/`collect_pillar_state` (§1b).
Correctly bounded — no action (and none possible: frontend tail still in flight).

`project_feed.py` — **OK.** The event-log foundation + `normalize_project_path`
(the single path-canonicalization seam) + `project_channel_key` (path never on
the wire). Well-bounded; dirty but structurally sound.

`project_brain_summary.py` — **OK.** The 140-line collab-bar aggregation (§1b).
Small, cohesive; the second composite reader but justified by the hot-path
constraint.

### 3.2 Autonomous-landing subsystem (newer, brain-adjacent)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `project_commit.py` | 473 | OK | HOT (**dirty**) | `test_project_commit` |
| `project_ready.py` | 349 | OK | live | `test_project_ready_marker` |
| `project_acceptance.py` | 326 | OK | live | `test_project_acceptance_gate` |

These implement the "green work auto-lands" loop (`project_acceptance` = the
fresh-worktree gate; `project_ready` = marker producer/consumer;
`project_commit` = byte-identity clean-commit). All well-bounded single-concern
modules — the JOURNAL shows they were built with the clean-split discipline.
`project_commit.py` is dirty (sibling work). No split needed.

### 3.3 Digest + influence

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `project_summary.py` | 487 | OK | HOT | `test_project_summary` |
| `project_brain_influence.py` | 152 | OK | live | `test_project_brain_influence` |

`project_summary.py` — per-conversation summary digest (ambient "what siblings
accomplished"), consumed by `collect_pillar_state`'s sibling digest. OK.

### 3.4 Conversation-domain helpers (the non-brain half of the package)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `reconcile.py` | 313 | OK | HOT (**dirty**) | `test_reconcile_conversation`, `test_reconcile_js_backend_equivalence`, `test_reconcile_error_husk_collapse`, `test_startup_reconcile` |
| `title_gen.py` | 264 | OK | live | via title e2e |
| `meta_cache.py` | 237 | OK | HOT (**dirty**) | via meta e2e |
| `settings_store.py` | 165 | OK | HOT (**dirty**) | via settings e2e |
| `turn_initiation.py` | 143 | OK | HOT (**untracked**) | via reconcile e2e |
| `project_brain_influence.py` | 152 | OK | live | `test_project_brain_influence` |
| `search_index.py` | 120 | OK | HOT (**dirty**) | via search e2e |
| `segments_backfill.py` | 106 | OK | one-shot migration | via segments e2e |
| `vu_translate_backfill.py` | 71 | OK | one-shot (**untracked**) | — |
| `__init__.py` | 34 | OK (facade, **dirty**) | — | — |

`reconcile.py` — **OK and important.** The backend-authoritative turn-end ghost
reconcile — a PURE function (no DB/network) porting the JS `_classifyGhostTail`/
`_isBuriedEmptyGhost` classifiers byte-for-byte. This is the charter's
patch-register item #1 (move reconcile onto the conversation GET path + delete
the JS classifiers). **Mid-migration: it's ported + tested but the JS-deletion +
GET-path-wiring is the in-flight work** — dirty for exactly that reason.
`turn_initiation.py` (the single `is_auto_initiated` resolver `reconcile`
depends on) is untracked = brand-new sibling work. Document; do not touch.

`__init__.py` — a 34-line facade that re-exports only the CONVERSATION-DOMAIN
helpers (meta/search/settings/title), NOT the brain pillars. Notable: the package
docstring says it was created (2026-06) to break the `lib → routes` circular
import; the brain pillars are imported directly by path, not through this facade.

---

## 4. Dependencies (in / out)

**Inbound:** `routes/api_v1/project.py` (the whole Project Brain REST surface —
board/charter/feed/peer/status/watch/ready endpoints); `lib/tasks_pkg/manager.py`
+ `handlers/misc.py` (the charter/board/peer agent tools — Unit 3);
`lib/tasks_pkg/system_context.py` (injects `render_charter_block` +
`render_board_block` per turn); `lib/scheduler/` (the 30s dispatch heartbeat →
`sweep_dispatch`); `routes/chat.py` + `manager` recovery (→ `reconcile`).

**Outbound / key shared primitives (the single-source seams):**
- `project_feed.normalize_project_path` — THE path-canonicalization seam (every
  read + write funnels through it; matches the frontend normalizer byte-for-byte).
- `project_board.claims_by_conv` — THE claim→conv join (§1a).
- `project_charter.pending_proposals` — THE pending-decision count (both the bar
  and the panel read it).
- `project_status._fingerprint` / `collect_pillar_state` — reused by
  `project_watch` (§1b — the correct reuse pattern).
- `lib/presence/` (`snapshot`, `detect_overlaps`) — the presence pillar (#4),
  which lives in a SEPARATE package `lib/presence/` (not in `conversations/`) —
  worth noting the seven pillars are split across two packages.
- `lib/database` (DOMAIN_CHAT, `get_thread_db`) — every pillar persists to its
  own table (`project_events`, `project_charter`, `project_tasks`,
  `project_status_snapshots`, `project_watch_items`/`_responses`).
- `lib/agent_core/push` — feed live-mirror.

**No back-edges up into `routes`** (the whole reason the package exists — §3.4
`__init__` docstring). Pillars import each other's PUBLIC functions only.

---

## 5. Invariants (must not be broken — many are charter-committed)

1. **`claims_by_conv` is the SINGLE claim→conv join** (charter-committed §1a).
   Never hand-roll a second presence⋈task⋈board aggregation.
2. **`normalize_project_path` is the SINGLE path-canonicalization seam** — write
   side and read side must agree or the board/feed render empty. Matches the JS
   normalizer byte-for-byte.
3. **Every pillar is keyed on `project_path`, never a process-global**, and is
   **best-effort / never-raises** into its caller (audit-logic-must-not-block).
4. **The board lease is advisory + at-read-time-evaluated** — no reaper thread,
   an expired claim reads `open`. Cannot deadlock the board.
5. **Peer messaging: per-(sender,target) rate limit + no-auto-relay** (charter
   Pillar-#6 invariant #2) — an A→B→A storm is impossible by construction. Hard
   abort is audit-gated (`approved_by`).
6. **Charter commit is human-gated + optimistic-locked**; agents may only
   PROPOSE. There is intentionally NO commit tool.
7. **Status + Watch are HUMAN-FACING ONLY** — never injected into sibling agent
   prompts (guarded by `test_project_status_no_ambient_injection`). The ONLY
   bridge to agents is `promote_watch_item` → human-gated `commit_charter`. NO
   fan-out/broadcast verb (charter-committed).
8. **`reconcile` is a PURE function that NEVER auto-fires a turn** — a ghost
   removal is cleanup, never a trigger (kills the Case-D→Case-E auto-fire leak).
   The buried-ghost sweep NEVER removes an in-cache-prefix message.
9. **All snapshot/feed/watch tables use a monotonic per-project `seq` minted
   under a module lock** — two concurrent emitters can't collide on the
   `(project_path, seq)` PK.

---

## 6. Known debt (grounded, document-only)

- **Two composite pillar-readers** (`build_brain_summary` +
  `collect_pillar_state`) with overlapping field sets (§1b). NOT a correctness
  bug (both compose the same single-sourced primitives), but a seam-duplication
  risk if a third reader appears. Refactor gate: extract a shared
  `collect_pillar_state` core ONLY when Pillar #7 settles.
- **`project_board.py` (1071) bundles 5–6 concerns** (§3.1) — the biggest file;
  a split is plausible but blocked on active dispatch/ready-marker work.
- **The seven pillars span two packages** (`conversations/` + `lib/presence/`) —
  a documentation footgun (Pillar #4 isn't where the other six are), not a code
  defect.
- **9/22 modules dirty + 2 untracked** — the package is mid-migration on several
  fronts (Pillar-7 frontend, charter-CRUD routes, reconcile GET-path, autopilot
  summary). Structural verdicts are deferred until these land.

---

## 7. Segmentation verdict (this unit — DOCUMENT-ONLY, no split proposed now)

**Correctly bounded — leave as-is:**
`project_feed`, `project_brain_summary`, `project_status`, `project_watch`,
`project_ready`, `project_acceptance`, `project_commit`, `project_summary`,
`project_brain_influence`, `reconcile`, `title_gen`, `meta_cache`,
`settings_store`, `turn_initiation`, `search_index`, the two backfills, `__init__`.
The Pillar architecture (single-concern module per pillar, downward dependency on
single-sourced primitives) is CLEAN — this package is well-decomposed, not a tangle.

**BIG but explicitly DEFERRED (contested / mid-migration — no split now):**
- `project_board.py` (1071) — 5–6 concerns; split candidate is
  `project_board_lease.py` (path-lease) + a render module, BUT blocked on active
  dispatch/ready-marker/landing epics touching this exact file.
- `project_peer.py` (914) — dirty, actively extended; document only.
- `project_dispatch.py` (843) — brain-adjacent active work; document only.
- `project_charter.py` (634) — charter-CRUD route half in flight.

**Seam-duplication to WATCH (not fix now):** the two composite pillar-readers
(§1b). Extract a shared core only after Pillar #7 lands.

**No structural action taken or proposed for this unit** — per the
document-not-refactor constraint. The verdict is: the coordination-mechanism
integrity is SOUND (single-sourced primitives, clean pillar seams, charter
invariants honored), with one watch-item (composite-reader duplication) and one
deferred BIG file (`project_board`).

---

## 8. Comparison to Units 1–5 (the running thesis)

- **This is the first unit examined for CORRECTNESS-of-coordination, not just
  structure — and it passes.** The charter's central invariant (`claims_by_conv`
  single-sourced) holds on disk; the feared "one aggregation hand-rolled in five
  places" is NOT present at the primitive level. The only duplication is two
  composite readers that both compose those single-sourced primitives — a
  watch-item, not a rot.
- **Well-decomposed despite being the most contested package** — each pillar is a
  clean single-concern module, on par with `swarm/` (Unit 4) and `token_counter/`
  (Unit 5). The size outliers (`project_board`, `project_peer`, `project_dispatch`)
  are BIG-but-cohesive, and crucially are ACTIVELY OWNED — the correct call is
  defer, not split, exactly because a premature split collides with the parallel
  work the brain itself coordinates.
- **The document-not-refactor discipline is itself validated here:** 9/22 files
  are dirty; a Unit-1-style "split `manager.py`" verdict on `project_board.py`
  right now would be the collision the whole Project Brain exists to prevent.

---

*Next unit: Unit 7 (Data tier — `database/` _core_schema, _schema_pg/sqlite,
_wrappers, _bootstrap, _sql_translate).*
