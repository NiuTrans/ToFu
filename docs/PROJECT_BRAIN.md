# The Project Brain: How Conversations Within a Project Become a Unified Mind

This is a complete, self-contained explanation of the mechanism that lets many
independent conversations belonging to the same project stop behaving like N
amnesiac strangers and start behaving like one coordinated intelligence —
perceiving each other, sharing intent, avoiding collisions, and even starting
work autonomously without a human hand-dispatching it.

Everything below is defined from first principles. Where a term is specific to
this system, it is defined at first use. This document is intended to stand
alone as a reproduction spec.

> **Companion doc:** [`CROSS_CONV_AWARENESS.md`](CROSS_CONV_AWARENESS.md) covers
> the earlier *read-direction* awareness layers (`@`-reference, conv-ref tools,
> the ambient project digest). The Project Brain builds on top of that
> perception foundation with shared working state, coordination, and autonomy.

---

## 1. The Problem, Precisely Stated

**Conversation** — a single chat thread between a user and the assistant.
Internally it has a `conv_id` (a string), a stored message history, and a
`settings` JSON blob (which may contain a `projectPath`).

**Project** — a working directory on disk that one or more conversations are
attached to. Identified by `projectPath` (an absolute filesystem path string).
A conversation is "in project mode" when its config carries a non-empty
`projectPath`.

**Task / turn** — one unit of assistant work inside a conversation: the model
reads the context, calls tools, produces a reply. Tasks are tracked in an
in-memory registry (`tasks` dict) with a `status` (`running`/`done`) and an
`aborted` flag.

**Autopilot / autonomous mode** — a loop where the assistant keeps taking turns
in one conversation without waiting for a human, driven by a synthetic "virtual
user." A single autopilot run can go many tasks deep.

The problem: a large project accumulates many conversations. Each one runs deep
and blind. Three concrete failures result:

1. **No shared progress picture** — you can't tell what the project as a whole
   is doing or what's left.
2. **Collision & redundancy** — two conversations chase the same goal or edit
   the same thing, wasting work or conflicting.
3. **Parallelism unused** — there's no way to hand independent work to idle
   conversations.

The Project Brain closes all three, plus a fourth requirement: it must be
**observable and correctable by the human**, and ultimately run **without a
human hand-dispatching** (无需人手).

---

## 2. The Architecture: A Blackboard, in Five Pillars

The design is a classic **blackboard architecture** — a shared, evolving
workspace that all agents read from and write to, rather than messaging each
other point-to-point. Concretely it is **four durable artifacts + one live
channel**, all keyed strictly on `projectPath`, realized as five
independently-shippable pillars:

| Pillar | Name | What it provides | The "brain" faculty |
|---|---|---|---|
| #1 | **Activity Feed** | live pulse of what every conversation is doing right now | **Perception** |
| #2 | **Charter** | the shared north-star doc + committed decisions | **Shared intent** |
| #3 | **Board** | coordination board of claimable epics | **Coordination (anti-collision)** |
| #5 | **Dispatch + Heartbeat** | auto-selects & starts pickable work | **Autonomy (无需人手)** |
| — | **Panel (frontend)** | makes all of the above visible & correctable | **Observability / human control** |

> **A note on the pillar numbering.** The numbers are *historical* — they are
> the order in which the pieces were built and recorded in the project journal,
> which jumped #1 → #2 → #3 → #5. **There is no Pillar #4 and none is missing.**
> What would have been "#4" was the soft-lease *leasing* mechanism, and it was
> folded into the Board (Pillar #3) rather than shipped as a separate component.
> A reader reproducing from scratch should build exactly the five items in the
> table above; do not go hunting for a nonexistent fourth component.

Two invariants hold across every pillar, because violating them previously
caused real bugs:

- **Keyed on `projectPath`, never a process-global.** Every read/write takes
  the project path explicitly. There is no ambient "current project" singleton
  consulted by these mechanisms — reading such a global caused a read/write-
  badge "thrash" bug where concurrent conversations on different projects
  clobbered each other's view.
- **Best-effort, never blocking.** Any Brain operation that fails is logged and
  swallowed; it must never break the task, turn, or scheduler tick that
  triggered it.

---

## 3. Pillar #1 — The Activity Feed (Perception)

**Activity Feed** — an append-only log of events, one log per project,
recording what conversations *did*.

### Data model
Table `project_events`, composite primary key `(project_path, seq)`:

- **`seq`** — a per-project **monotonic counter** (1, 2, 3, …). "Monotonic" =
  strictly increasing, never reused. Computed inside the insert as
  `MAX(seq)+1 WHERE project_path=?`, serialized by one process lock so two
  concurrent writers can't mint the same `(project_path, seq)` pair.
- **`event_id`** — a UUID, for idempotent dedup on the client.
- **`conv_id`, `task_id`** — who/what emitted it.
- **`kind`** — the event type (frozen enum, below).
- **`title`, `summary`** — denormalized display text (so the UI renders without
  a join).
- **`payload`** — kind-specific JSON extra.
- **`ts`** — epoch-ms timestamp.

**Retention** — the log is bounded: on each write, rows older than the most-
recent 500 per project are pruned. No unbounded growth.

### Event kinds (the frozen `VALID_KINDS` set)
An unknown kind is coerced to `note` (never crashes). The nine kinds and their
producers:

| kind | Emitted by | Meaning |
|---|---|---|
| `started` | task creation | a turn began |
| `completed` | task finalize | a turn finished |
| `aborted` | task finalize (abort flag) | a turn was cancelled |
| `run_concluded` | autopilot run close-out | a whole autopilot run finished |
| `claimed` | Board claim | a conversation took ownership of an epic |
| `blocked` | Board block | an epic is stuck |
| `decided` | Charter commit | a decision was committed |
| `proposed_decision` | Charter propose | a decision was proposed for human review |
| `note` | fallback | generic |

**Granularity rule (important):** an autopilot run is dozens of tasks. Emitting
`started`/`completed` per task would flood the feed. So autopilot turns (those
whose config carries an `autopilotRunId`) **suppress** their per-task events and
instead emit exactly **one** `run_concluded` at run close-out. Ordinary (human-
driven) conversations emit per-task. This keeps the feed a *human-meaningful
pulse*, not turn noise.

### Live delivery
Events are mirrored in real time over **PushHub** — the server's WebSocket
multiplexer that broadcasts server-push events to subscribed browser tabs on
named channels.

- **Channel** = `project`.
- **Routing key** — this is the crux of isolation. The key is
  **`sha1(projectPath)[:16]`** (first 16 hex chars of the SHA-1 hash of the
  path), computed **identically** on backend and frontend. A tab subscribes to
  `('project', sha1(itsPath)[:16])`; the server publishes each event on
  `('project', sha1(eventPath)[:16])`. A frame minted for project Y therefore
  routes to a different key than a panel opened on project X — **project X never
  receives project Y's pulse.** The raw absolute path never travels over the
  wire (it's hashed), which also avoids leaking filesystem paths.

### Client rendering & the backfill→live seam
When the panel opens it does two things:

1. **Backfill** — one REST read `GET /api/v1/project/feed?path=…&since=<seq>`
   returns recent events newest-first plus `maxSeq` (the highest seq present).
2. **Subscribe** — then it goes live via PushHub.

The boundary between the two is **deduped** two ways, mirroring an SSE
"Last-Event-ID resume": a live frame is dropped if its `seq ≤ maxSeq` (already
covered by backfill) **or** its `event_id` was already rendered. This prevents
an event double-rendering exactly at the handoff.

---

## 4. Pillar #2 — The Charter (Shared Intent)

**Charter** — the project's living **north star**: the goal/direction plus the
list of **committed key decisions**. It is the thing that makes N conversations
feel like one mind, because every conversation reads the same authoritative
intent.

### Data model
Table `project_charter`, single-row-per-project (primary key `project_path`,
upsert semantics):

- **`content`** — the north-star text.
- **`decisions`** — JSON array of committed decisions.
- **`version`** — an **optimistic-lock** integer (defined below).
- **`updated_by_conv`, `updated_at`**.

### The three-stage discipline: read → propose → commit
This is the heart of "shared intent that doesn't drift autonomously":

- **read** — any project-mode conversation may read the charter (tool
  `project_charter_read`). Read-only.
- **propose** — an agent may only *propose* an amendment (tool
  `project_charter_propose`). A proposal writes **exactly one
  `proposed_decision` event into the Activity Feed** and **never touches the
  `project_charter` table**. This distinction — *proposal ≠ commit* — is
  enforced at the source, not by convention.
- **commit** — the actual charter change is **human-gated**: only a human,
  through the UI, calls the commit route. It is **optimistic-locked** and, on
  success, emits one `decided` event.

**Optimistic lock** — a concurrency-safety scheme where the writer supplies the
`version` it *expects* the row to currently hold (`expected_version`). If the
stored version differs (someone else committed in the meantime), the commit is
**rejected** (`version_conflict`, surfaced as HTTP 409) and the human must
re-read and retry. This prevents two commits silently clobbering each other. On
success `version` is bumped by 1.

### Injection — how the Charter reaches the model
Prompt assembly (in `lib/tasks_pkg/system_context.py`) injects the charter as
its own cache-stable system block, marked `[PROJECT CHARTER]`, **only when a
charter exists** (an empty project adds zero prompt weight), keyed strictly on
the explicit `projectPath`. This injection is *the actual mechanism* by which
every conversation shares one intent — it's ambient, not something an agent has
to remember to fetch.

---

## 5. Pillar #3 — The Board (Coordination / Anti-Collision)

**Board** — a per-project list of coarse, human-meaningful **epics**
(workstream-level units of work — *not* fine agent sub-steps, which stay in the
Activity Feed). Conversations **post**, **claim**, **complete**, and **block**
epics on it.

### Data model
Table `project_tasks`, primary key `id`:

- **`title`**, **`status`** ∈ {`open`, `claimed`, `done`}, **`owner_conv_id`**.
- **`lease_expires_at`** — the soft-lease expiry timestamp (defined below).
- **`created_by_conv`** — the conversation that posted the epic (used as the
  dispatch target).
- **`depends_on`** — JSON array of other epic ids this one depends on (intra-
  board dependencies; deliberately *not* a second namespace or sub-project
  system).

### Soft lease — the anti-deadlock core
**Claim** — a conversation calls `project_board_claim` to signal "I'm working
this epic." This writes `owner_conv_id` and sets `lease_expires_at = now + TTL`
(default 30 min).

**Soft lease** — the claim is **advisory, not a hard lock.** It does not
*prevent* another conversation from acting; it *informs* them (via injection,
below) so they voluntarily step aside. Crucially, the lease **expires**.

**The anti-deadlock rule — evaluated at READ time, with no background reaper:**
the function `_effective_status(stored_status, lease_expires_at, now)` returns
`open` for any epic that is stored as `claimed` but whose
`lease_expires_at ≤ now`. So an abandoned or crashed conversation's claim
silently becomes available again the next time anyone reads the board. There is
**no cleanup thread and no global mutator** — expiry is a pure, stateless read-
time computation. This is deliberate: a background reaper would itself be a new
global and a new failure mode; read-time evaluation cannot deadlock.

`claim_task` succeeds if the epic is open, or its lease has expired, or the
caller already owns it (lease refresh); it gives an *advisory* refusal
(`already_claimed`) only if a **different** conversation holds an **unexpired**
lease. `complete_task` marks `done`; `block_task` emits a `blocked` feed event
(a signal, not a status change).

### The auto-avoidance injection — the actual coordination mechanism
Prompt assembly injects a `[PROJECT BOARD]` block (only when the board is non-
empty) listing open / claimed / recently-done epics. For each epic under an
*unexpired* claim by *another* conversation, it emits an explicit hint:

> *"[epic] — claimed by conversation …; another conversation is advancing this,
> pick a different epic or coordinate, do not redo it."*

(The reader's own claim is marked "(you)" with no warning.) This injected hint
is what a conversation *acts on* to avoid collision and redundancy — it is not a
passive display, it is the coordination signal delivered straight into the
model's context. Because `_effective_status` is applied at render time, the
injected board never shows a deadlocked (expired) claim as claimed.

---

## 6. Pillar #5 — Dispatch + Heartbeat (Autonomy, 无需人手)

Everything above lets conversations coordinate **when a human is driving them**.
But an open epic with nobody working it, and no human typing, would sit forever.
Dispatch is the spine that makes the project *start* work by itself.

### The message queue (the turn-source we reuse)
**Message queue** — an existing per-conversation, DB-backed queue of pending
turns, with a `kind` field: `real` (human turn), `workflow_step` (engine-
injected dispatchable turn), `autopilot`. `enqueue_message(conv_id,
message_data, config, kind)` adds a turn; when a conversation goes idle, the
existing `dispatch_next_queued` machinery drains it and starts the task.
**Dispatch reuses this — it never builds a second turn-source.**

### Selection (pure, testable)
`select_dispatchable(project_path)` returns epics that are **genuinely pickable
right now**: effective status `open` (so live-claimed epics are excluded, and —
via the same `_effective_status` — expired claims are eligible again) **and**
every `depends_on` id is `done`. It is built on the board's read path, so there
is exactly **one** deadlock-safety path, not two.

### Dispatch action (idempotent)
`dispatch_epic(project_path, epic, target_conv_id)`:

1. **Claims the epic first** under the target conversation. This is the
   **idempotency guard** — after dispatch the epic is `claimed`, so the next
   selection pass excludes it → no concurrent double-dispatch. If another
   conversation already holds a live claim, the claim is refused and **no
   kickoff is enqueued.**
2. Enqueues a **brain-dispatched kickoff turn** via
   `enqueue_message(kind=workflow_step)` carrying a `_brainDispatch` marker (it
   is an engine-injected turn, explicitly *not* a human `real` turn). The
   kickoff instructs the conversation to pick up the epic, read the board &
   charter, and complete it when done.

The **target** is the epic's `created_by_conv` (work returns to its
originator). If unknown, the epic is left open rather than inventing a
conversation.

### Two triggers — completion and heartbeat
- **Completion trigger** — wired into `complete_task`: finishing one epic may
  unblock its dependents, so `on_epic_completed` dispatches each newly-pickable
  epic. This *propagates* motion already underway.
- **Heartbeat sweep** — the piece that *starts* motion (including the cold-start
  very-first epic, which no completion could ever trigger).
  `sweep_all_active_projects()` runs on the **existing scheduler 30-second
  tick** (no new thread, no new global): for each recent project it runs
  `select_dispatchable` and dispatches each pickable epic. It is:
  - **Bounded** — capped per sweep (default 3), so one tick can't flood.
  - **Idempotent under repetition** — two guards, independently proven load-
    bearing: (1) the claim-on-dispatch (excludes the epic next sweep); (2) a
    **busy-guard** that skips an epic whose target conversation already has a
    live task *or* already has a queued `workflow_step` kickoff for that epic —
    so a busy target never gets a stacked duplicate. Both busy-probes fail
    *safe* (on uncertainty, assume busy → never double-dispatch).
  - **Best-effort** — a sweep failure can never break the scheduler loop.

With the heartbeat, an idle project's dependency-satisfied open epics self-
start, get claimed so siblings avoid them, and run — with no human involved.

---

## 7. The Panel (Observability & Human Control)

A brain whose coordination state is invisible can't be trusted or corrected. A
slide-in **Project Brain panel** (opened from the project bar) presents three
columns, all keyed on the *displayed conversation's* project path (resolved via
the same accessor the rest of the UI uses — never a global):

- **Activity** — the live feed (backfill + PushHub stream, deduped as in §3).
- **Charter** — north-star text + committed decisions, plus **pending
  `proposed_decision` events** each with a human **Commit / Reject** control.
  Commit calls the human-gated commit route with the proposal text +
  `expected_version`; this is the *only* path that mutates the charter — the
  human gate that was otherwise unreachable.
- **Board** — a kanban (open / claimed / done) of epics; claimed cards show the
  owner-conversation chip (clicking it opens that conversation).

All frontend↔backend traffic goes through the **unified API client**
(`window.Api.project.feed/charter/board/commitCharter`) — no raw `fetch`; the
cross-side hash algorithm for the push key is byte-identical to the backend's.

---

## 8. End-to-End: The Brain in Motion

Putting the definitions together, here is the full loop for a project with
several conversations:

1. Conversation A, working, discovers a workstream → calls `project_board_post`
   → an **epic** appears on the **Board** (status `open`).
2. The **heartbeat sweep** (or a completion trigger) runs `select_dispatchable`,
   finds the epic pickable (deps done, no live claim), **claims** it under its
   poster and **enqueues a `workflow_step` kickoff** → the conversation starts
   working it **with no human** → a `claimed` event pulses on the **Activity
   Feed**.
3. Conversation B's next turn assembles its prompt; the `[PROJECT BOARD]`
   injection tells it *"that epic is being advanced by conversation A — don't
   redo it."* B **auto-avoids** the collision and picks different work.
   Meanwhile B reads the `[PROJECT CHARTER]` block and aligns to the shared
   north star.
4. B reaches a project-wide decision → `project_charter_propose` → a
   `proposed_decision` event appears in the feed and in the panel's Charter
   column.
5. The **human** sees the proposal in the panel, clicks **Commit** → the
   optimistic-locked commit route writes it into the Charter (`version` bumps),
   emits a `decided` event → from now on every conversation's prompt carries
   that committed decision.
6. A finishes → `project_board_complete` → `completed` event + the completion
   trigger dispatches any dependents that just unblocked.
7. Throughout, the human watches the whole pulse — who's doing what, what's
   claimed, what's pending approval — live in the panel, and can correct course
   by committing/rejecting decisions.

Perception (Feed) → shared intent (Charter) → coordination (Board) → autonomy
(Dispatch + Heartbeat) → human oversight (Panel). That is the unified brain:
many conversations, one evolving shared workspace they all read from, write to,
and are steered by — coordinating automatically, and starting work on their
own, while remaining fully visible and correctable by the owner.

---

## 9. Reproduction Checklist (the minimal set of moving parts)

To rebuild this from scratch you need exactly:

1. **Three tables**, all per-`projectPath`: `project_events` (append log,
   monotonic `seq`, bounded), `project_charter` (single row, `version`
   optimistic lock), `project_tasks` (epics, `status`, soft-lease
   `lease_expires_at`, `depends_on`).
2. **A pure `_effective_status(stored, lease_expires_at, now)`** that treats an
   expired claim as `open` — the single, read-time, reaper-free anti-deadlock
   rule, reused by both the board read and dispatch selection.
3. **A push channel** `project` routed by `sha1(projectPath)[:16]` computed
   identically on both sides — the isolation boundary.
4. **Prompt injection** of `[PROJECT CHARTER]` and `[PROJECT BOARD]` blocks
   (with the per-epic avoid-duplication hint) — ambient, existence-gated,
   path-keyed.
5. **Agent tools** gated to project mode: charter read/propose (never commit),
   board read/post/claim/complete/block.
6. **A human-gated commit route** (optimistic-locked, emits `decided`) — the
   only charter mutator.
7. **A dispatch engine**: pure `select_dispatchable` + idempotent `dispatch_epic`
   (claim-then-enqueue a `workflow_step` kickoff, reusing the existing message
   queue) + a completion trigger + a **heartbeat sweep on the existing
   scheduler tick** (bounded, busy-guarded, best-effort).
8. **A three-column panel** reading feed/charter/board and exposing the human
   Commit/Reject gate, all through the unified API client.

Hold to the two invariants — **key on `projectPath` (never a process-global)**
and **best-effort (never block the triggering path)** — and the whole thing
composes without deadlocks, cross-project leaks, or thrash.

---

## 10. Where the Code Lives (implementation map)

| Component | File(s) |
|---|---|
| Table definitions | `lib/database/_core_schema.py` (`project_events`, `project_charter`, `project_tasks`); bootstrap in `_schema_sqlite.py` / `_schema_pg.py` |
| Activity Feed engine | `lib/conversations/project_feed.py` (`VALID_KINDS`, `emit_event`, `sha1(path)[:16]` key, retention) |
| Charter engine | `lib/conversations/project_charter.py` (`read`/`propose`/`commit`, optimistic lock) |
| Board engine | `lib/conversations/project_board.py` (`_effective_status`, `claim`/`complete`/`block`, `[PROJECT BOARD]` render) |
| Dispatch + heartbeat | `lib/conversations/project_dispatch.py` (`select_dispatchable`, `dispatch_epic`, `on_epic_completed`, `sweep_all_active_projects`) |
| Heartbeat wiring | `lib/scheduler/manager.py` (call on the 30s tick) |
| Prompt injection | `lib/tasks_pkg/system_context.py` (`[PROJECT CHARTER]` + `[PROJECT BOARD]` blocks) |
| Agent tools | `lib/tools/conversation.py`, gated in `lib/tools/registry.py`, dispatched in `lib/tasks_pkg/handlers/misc.py` |
| Turn source | `lib/message_queue.py` (`enqueue_message`, `workflow_step` kind) |
| REST surface | `routes/api_v1/project.py` (`/project/feed`, `/project/charter`, `/project/charter/commit`, `/project/board`) |
| Frontend panel | `static/js/project-brain.js`, markup in `index.html`, client methods in `static/js/api.js` (`Api.project.*`) |
| Tests | `tests/test_project_feed.py`, `test_project_charter.py`, `test_project_board.py`, `test_project_dispatch.py`, `test_frontend_project_brain.py`, `test_core_schema_parity.py` |
