# Project Brain — Pillar #7: the human↔brain status lane

> Status: DESIGN (owner-approved 2026-07-08). Increment 1 = persistent snapshot
> store + synthesis generator + read-only chat tab. Propose-actions layer is a
> SEPARATE later increment (owner judges synthesis quality first).

## 1. The gap this closes

The first six pillars (Activity Feed, Charter, Board, presence, per-conv
summaries, peer comms) made **N conversations of a project feel like one
brain**. But they are all agent-facing or read-mostly *blackboard* surfaces.
The human can *read* each column and *poke* individual cells (commit a charter
decision, post a board epic, nudge a sibling), but cannot:

  1. **Ask the project a question** — "where are we? are we drifting from what
     I asked for?" — and get a *synthesized* answer. Every existing surface
     forces the human to do the cross-pillar synthesis by eye.
  2. **See where the project HAS BEEN.** The Charter is durable memory of
     *intent* (north star + committed decisions). There is no continuously
     maintained memory of *status* — where the project actually *is*. The Feed
     is a raw, ephemeral event pulse that scrolls away; per-conversation
     summaries exist but are **never aggregated** into a project-level "state
     of the project" narrative.

As automation runs ahead of the human, human perception of the project
diminishes. Pillar #7 is the human's window back in: a **1:1 human↔brain lane**
that synthesizes across all six pillars, backed by a **persistent, append-only
history of project-status snapshots**.

## 2. What it is NOT (invariants inherited from prior pillars)

- **NOT a fan-out / broadcast verb.** The ratified no-broadcast decision
  (charter v8) stands: one call must never fan out to N conversations (N²
  storm). This lane is **1:1: human ↔ synthesis**, read-and-synthesize.
- **NOT a new inter-conversation write path.** The only engine-write stays
  `dispatch_epic`. When the (later) propose-actions layer lands, anything the
  lane does on the human's behalf routes through an EXISTING human-gated path
  (charter commit, board post, path lease, rate-limited nudge) — never a new
  channel.
- **NOT ambient prompt injection.** The status memory is **human-facing only**.
  It is NEVER injected into `lib/tasks_pkg/system_context.py` / the sibling
  agents' prompts. Doing so would blur the human↔brain lane into the
  agent↔agent lane and re-raise the coupling/storm concerns. This is a hard
  guarantee, guarded by a test.

## 3. Persistence — append-only status snapshots

A new table `project_status_snapshots`, keyed on `project_path`, **append-only**
with a monotonic per-project `seq` (mirrors `project_events` exactly). The human
works with facts that drift over time and needs to see *how* the project got
here — so we keep a TRAIL of snapshots, not a single overwritten narrative.

Each snapshot row:

| column | meaning |
|---|---|
| `project_path` | storage key (normalized via `project_feed.normalize_project_path`) |
| `seq` | monotonic per-project counter (composite PK with `project_path`) |
| `snapshot_id` | uuid hex (stable id for the row) |
| `narrative` | the synthesized "where are we / drift read" text (bounded) |
| `pillar_state` | JSON: the pillar-state the narrative was generated FROM — epics {open,claimed,done}, pendingDecisions, blocked count, activePeers, charter version, alignment read. This is the *evidence* behind the narrative, so a future reader can see what the brain saw. |
| `trigger` | what caused this snapshot: `epic_completed` / `decision_committed` / `blocked` / `on_open` / `manual` |
| `ts` | epoch ms |

**Retention:** bounded like the feed — keep at most `_SNAPSHOTS_KEEP` (start at
200) most-recent per project, pruned on insert (cheap seq-window delete). The
trail is kept, not an unbounded archive.

**Cost discipline (reuse `ensure_summary`'s laziness):** a new snapshot is
minted only when the pillar-state has **materially changed** since the last
snapshot. `_is_stale(last_snapshot, current_pillar_state)` compares a cheap
fingerprint (epic counts + pending count + charter version + blocked count); if
unchanged, we return the cached latest snapshot and skip the LLM call. So a
quiescent project is never re-synthesized, and repeated tab-opens on a static
project are free.

## 4. Synthesis generator

`build_status_snapshot(project_path, *, trigger, force=False)` in
`lib/conversations/project_status.py`:

1. **Reads live pillar state** — board (`read_board`), charter
   (`read_charter` + `pending_proposals`), feed (`read_project_feed` for recent
   blocked/decided), presence (`snapshot`), and the per-conv digest
   (`project_digest_entries`). This assembly is the SAME cross-pillar join
   `build_brain_summary` uses; we reuse `claims_by_conv` for the peer→epic join,
   never hand-roll a second aggregation.
2. **Staleness gate** — if not `force` and the fingerprint matches the latest
   stored snapshot, return the cached snapshot (no LLM).
3. **LLM synthesis** — a bounded cheap-model call (`dispatch_chat`,
   `capability='cheap'`, mirroring `project_summary.generate_summary`) that
   produces the narrative + an explicit **alignment-to-north-star read** (is
   current in-flight work tracking the charter goal, or drifting? name the
   drift if any). The prompt is fed the charter north-star + committed
   decisions + the in-flight epics + recent blocks + sibling digest.
4. **Persist** the snapshot row (narrative + pillar_state evidence + trigger).

Best-effort throughout: any pillar read failing degrades that field; the
generator never raises into a caller. On LLM failure it keeps the previous
snapshot rather than writing an empty one.

## 5. Regeneration triggers

- **Event-driven (keep warm):** on `epic_completed`, `decision_committed`,
  `blocked`. These fire a non-blocking `build_status_snapshot(..., trigger=…)`
  (daemon thread, like `ensure_summary(blocking=False)`) so a settled event
  leaves a fresh status behind without blocking the triggering action.
- **On tab-open (never stale):** the tab-open REST read calls
  `build_status_snapshot(..., trigger='on_open')` which, via the staleness
  gate, regenerates only if the pillar-state moved since the last snapshot —
  otherwise returns the cached latest instantly.

## 6. Surfaces

- **Primary — a new tab** in the Project Brain panel (alongside
  charter/board/activity/peers): the roomy home for back-and-forth. Increment 1
  renders the latest narrative + the snapshot history trail (timestamp +
  narrative + expandable pillar-state evidence). The conversational Q&A input
  is wired in increment 1 as read-only synthesis (ask → fresh synthesis answer,
  no writes).
- **Ambient — the collab-bar** surfaces the latest one-line status headline so
  the human gets perception without opening the panel (extends
  `build_brain_summary`'s output with a `statusLine` field; the bar already
  renders that summary).

## 7. REST surface (all read-only in increment 1)

- `GET /api/v1/project/brain/status?path=…` — latest snapshot + short history
  (calls `build_status_snapshot(trigger='on_open')`, so tab-open is fresh).
- `GET /api/v1/project/brain/status/history?path=…&limit=…` — the snapshot
  trail (read-only, no synthesis).
- `POST /api/v1/project/brain/status/ask` — a read-only synthesis Q&A: the
  human's question + live pillar state → a synthesized answer. Writes NOTHING.

## 8. Build order

1. **Increment 1 (this):** table + `project_status.py` generator + the three
   read-only REST routes + the read-only tab + collab-bar status line. Tests
   incl. a NEUTER proving the synthesis reads LIVE pillar state (not a stub) and
   a test proving the status memory is NOT in the system-context injection path.
2. **Increment 2 (later, owner-gated):** propose-actions layer — the lane may
   DRAFT a charter amendment / board epic for the human to confirm, routed
   strictly through the existing human-gated write paths. No new channel, no
   fan-out.

## 8b. Increment 2 — the WATCH lane (SHIPPED 2026-07-08)

A durable counterpart to the ephemeral Ask: a standing list of things the HUMAN
cares about, that the brain addresses on a recurring basis.

- **Two tables** (`lib/database/_core_schema.py`, created in both bootstraps):
  `project_watch_items` (human input — single TEXT PK `item_id`; `kind` ∈
  concern|question|goal; `status` ∈ open|resolved; `promoted` flag;
  `response_fingerprint` for the staleness gate) and `project_watch_responses`
  (append-only, composite PK `(item_id, seq)`, monotonic per-item seq, bounded
  retention `_RESPONSES_KEEP=100`, pruned on insert). The drift of a concern's
  answer over time IS the signal, so the trail is kept, not latest-only.
- **Core** (`lib/conversations/project_watch.py`): human CRUD
  (`add`/`edit`/`set_status`/`delete`/`list`), the `address_watch_item`
  generator (reuses `collect_pillar_state` + the SAME `_fingerprint` staleness
  gate + `_build_synthesis_source` from the status lane — no second
  aggregation), `address_open_items` (recurring cadence, non-blocking daemon),
  and `promote_watch_item`.
- **Cadence** = on-tab-open (`GET /watch?refresh=1` re-addresses open items) +
  event-driven (the SAME `epic_completed`/`decision_committed`/`blocked`
  triggers re-address open items non-blocking). The closed-panel scheduler
  cadence is a DEFERRED follow-up (no background cost surprise).
- **The ONE bridge to agents — `promote_watch_item`.** The only way a watch
  item reaches sibling agents: it routes strictly through the human-gated
  `commit_charter` (adds a committed decision `[Goal/Concern/Question —
  promoted by owner] <text>`), because the charter is already the
  ambient-to-agents surface and already human-gated. No auto-steering, no new
  inter-conv write, no fan-out. A failed/version-skewed commit does NOT flag
  the item promoted.
- **HUMAN-FACING ONLY** — same invariant + source-grep guard as the status
  memory (`test_watch_not_in_system_context_source`).
- **Tests** (`tests/test_project_watch_lane.py`, 10): CRUD + validation; the
  live-state NEUTER (stub `_build_synthesis_source` → live north-star/epic
  vanish from the item prompt); append-only trail + retention + monotonic
  ordering + staleness gate; edit-forces-readdress; promote calls charter
  commit (not the agent path) + propagates version conflict; the source guard.

## 8c. Increment 2 slice 1 — per-response interaction (SHIPPED 2026-08-05)

The owner's ask: *"each brain answer should be a conversation — click in to
keep asking, or request a fix."* Every response on a watch item now carries
two doors, both inside the existing invariants (no new write channel, no
fan-out, lane stays human-facing-only):

- **继续追问 (Follow up)** — an inline composer under every response (latest
  + trail rows). `answer_follow_up(item_id, question, response_seq=…)` in
  `lib/conversations/project_watch.py` answers grounded in LIVE pillar state
  + the item text + the ANCHOR response, and appends the answer to the SAME
  trail with `trigger='follow_up'`; the question rides the evidence JSON
  (`followUpQuestion` / `anchorSeq`) and renders as a labelled line above the
  answer, so the thread reads without expanding anything. It deliberately
  does NOT touch the item's `response_fingerprint` — a Q&A turn is not a
  fresh assessment and must not mark the recurring cadence fresh. Synthesized
  responses still never reach the injection path (the goals renderer ships
  item TEXT only; the §8b source guard is unchanged).
- **请求修复 (Request fix)** — an inline epic-draft editor (multi-line)
  pre-filled with the FULL anchor response (whitespace-normalized, capped at
  the board's 2000-char title limit, human-editable). The whole diagnosis
  rides along because in a status narrative the praise leads and the actual
  problem lives in later sentences — first-sentence drafting posted epics
  that said the opposite of what needed fixing (owner catch 2026-08-05).
  Submitting rides the EXISTING human-gated `POST /api/v1/project/board/post`
  (`created_by_conv` = displayed conv → dispatch target): the brain dispatches
  the fix to a conversation the human opens from the Board tab — the
  propose-actions route §2 reserved for exactly this.
- **REST:** `POST /api/v1/project/brain/watch/follow_up`
  (`{itemId, question, seq?}`) — envelope + rate-limited like the other watch
  routes; 400 on missing item / empty question / synthesis failure.
- **Tests:** `tests/test_project_watch_lane.py` +5 (anchor+grounding+persist,
  explicit-seq anchor, cadence-untouched, validation/no-raise, no-anchor) and
  `tests/test_frontend_watch_follow_up.py` (6 jsdom pins: both doors on every
  response, question line, anchor seq, toggle-close, board-post channel,
  trigger label).

## 9. Schema placement

`project_status_snapshots` is defined ONCE in
`lib/database/_core_schema.py` (Core `define_table`, composite PK
`(project_path, seq)`), and created in both `_schema_sqlite.py` and
`_schema_pg.py` alongside the other project-brain tables, with an index on
`(project_path, seq DESC)`. No ALTER migration needed (new table).
