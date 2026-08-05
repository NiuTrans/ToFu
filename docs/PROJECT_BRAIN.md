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

## 2. The Architecture: A Blackboard, in Seven Pillars

The design is a **blackboard architecture** at its core — a shared, evolving
workspace that all agents read from and write to rather than messaging each
other point-to-point — later extended with a **direct peer channel** (#6) and a
**human↔brain status lane** (#7). Concretely it is a set of per-project durable
artifacts plus live delivery, all keyed strictly on `projectPath`, realized as
**seven** independently-shippable pillars:

| Pillar | Name | What it provides | The "brain" faculty |
|---|---|---|---|
| #1 | **Activity Feed** | live pulse of what every conversation is doing right now | **Perception** |
| #2 | **Charter** | the shared north-star doc + committed decisions | **Shared intent** |
| #3 | **Board** | coordination board of claimable epics | **Coordination (anti-collision)** |
| #5 | **Dispatch + Heartbeat** | auto-selects & starts pickable work | **Autonomy (无需人手)** |
| #6 | **Peer Comms + Intervention** | targeted agent↔agent messaging + advisory nudge / audit-gated abort | **Direct coordination** |
| #7 | **Status Lane** | synthesized "where are we / are we drifting" snapshots for the human | **Human situational awareness** |
| — | **Panel (frontend)** | makes all of the above visible & correctable | **Observability / human control** |

> **A note on the pillar numbering.** The numbers are *historical* — they are
> the order in which the pieces were built and recorded in the project journal,
> which jumped #1 → #2 → #3 → #5. **There is no Pillar #4 and none is missing.**
> What would have been "#4" was the soft-lease *leasing* mechanism, and it was
> folded into the Board (Pillar #3) rather than shipped as a separate component.
> A reader reproducing from scratch should build exactly the seven items in the
> table above (pillars #1–#3 and #5–#7); do not go hunting for a nonexistent
> fourth component. Pillars #6 (peer comms) and #7 (status lane) were added
> after the original five, so the doc's earlier sections still speak of "five"
> in places — the authoritative count is seven.

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
| `decided` | Charter commit (agent self-commit or human) | a decision was committed |
| `proposed_decision` | Charter propose | a decision was suggested (optional, non-binding) |
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

### The discipline: read → propose → commit (DECISION-commit de-gated 2026-07-12)
This is the heart of "shared intent that advances without a human in the loop":

- **read** — any project-mode conversation may read the charter (tool
  `project_charter_read`). Read-only.
- **propose** — an agent may *propose* an amendment (tool
  `project_charter_propose`). A proposal writes **exactly one
  `proposed_decision` event into the Activity Feed** and **never touches the
  `project_charter` table**. Now OPTIONAL — a suggestion the agent is not yet
  ready to make binding.
- **commit** — an agent may now **self-COMMIT a DECISION** (tool
  `project_charter_commit` → `commit_charter(add_decision=…)`), so shared
  intent advances **without a human gate**. It is **optimistic-locked** and, on
  success, emits one `decided` event. The agent path is **`add_decision`-only**
  — it can NEVER edit the north-star `content` (the project goal/direction).

> **Owner directive (2026-07-12): "further reduce human involvement — humans
> no longer participate in charter decision-making or set task statuses; they
> only receive information, set things about themselves, and define
> problems/goals."** So decision-commit and every task status are
> agent-autonomous. The **human retains only optional corrective levers** (NOT
> required for normal progress): editing the north-star `content`,
> `update_decision` / `delete_decision` / `delete_charter`, and board
> `reopen_task` (override a stuck/wrong claim) / `delete_task` (remove a junk
> epic outright) — all reachable only through the REST routes. The human defines the goal and can veto/correct a decision; it
> need not approve each one.
>
> This does **not** widen the write surface: `commit_charter` already existed
> (previously reached only via the human route); the agent tool exposes the
> **same** function, one decision + one `decided` event per call — the
> no-broadcast / anti-N²-storm invariant stands unchanged. The
> `_MAX_DECISIONS`-cap rolling truncation applies to agent commits exactly as
> before (no pagination).

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
(a signal, not a status change). `delete_task` is the **human-only** outright
removal (no agent tool, REST-only): allowed from any status — the advisory
lease never outranks a deleting human — but REFUSED (`has_dependents`) while
another ACTIVE epic depends on it, because a deleted dep can never reach
`done` and the dependent would be stranded invisibly. The removal emits a
`note` feed event + audit record, never a silent yank.

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

### The event channel + the heartbeat safety net
- **Completion trigger** — wired into `complete_task`: finishing one epic may
  unblock its dependents, so `on_epic_completed` dispatches each newly-pickable
  epic. This *propagates* motion already underway.
- **Post trigger (event channel, 2026-07-27)** — wired into `post_task`:
  `on_epic_posted` starts a freshly-posted epic IMMEDIATELY when it can
  genuinely start (deps done, routing-target conversation EXISTS and is IDLE).
  A busy target (the common case — an agent posts mid-turn) is deliberately
  NOT claimed; a missing target conv is never claim-stranded (both keep the
  legacy heartbeat behaviour). The human's revive lever `reopen_task` rides
  the SAME seam (done/claimed → open starts at reopen time, not the next
  sweep).
- **Completion nudge (event channel)** — wired into the post-task queue-drain
  hook (`_dispatch_queued_message` in `lib/tasks_pkg/manager/_sync.py`): when a
  task completes with an EMPTY queue, `on_conv_idle` dispatches + drains ONE
  open epic routed to that conversation — so work posted behind a busy turn
  starts the moment the turn ends, and a backlog advances one epic per
  completed turn (the same chain shape the queue drain uses).
- **Peer send-time drain (event channel)** — a peer message into an IDLE
  conversation is drained at SEND time (`send_peer_message`), rendered as a
  fresh turn immediately instead of waiting for the tick.
- **Heartbeat sweep — now the SAFETY NET, not the starter.**
  `sweep_all_active_projects()` still runs on the **existing scheduler
  30-second tick** (no new thread, no new global): for each recent project it
  runs `select_dispatchable` and dispatches each pickable epic. What remains on
  it by design: the crash / lease-expiry / stranded-kickoff recovery paths
  (inherently time-based, 30 s granularity is noise next to a 30-min lease
  TTL). It stays:
  - **Bounded** — capped per sweep (default 3), so one tick can't flood.
  - **Idempotent under repetition** — two guards, independently proven load-
    bearing: (1) the claim-on-dispatch (excludes the epic next sweep); (2) a
    **busy-guard** that skips an epic whose target conversation already has a
    live task *or* already has a queued `workflow_step` kickoff for that epic —
    so a busy target never gets a stacked duplicate. Both busy-probes fail
    *safe* (on uncertainty, assume busy → never double-dispatch).
  - **Best-effort** — a sweep failure can never break the scheduler loop.

The common flows never touch the 30 s tick: epics start at post time (idle
target) or at the poster's turn end (busy target), dependents start at
completion, answers at answer time, peer notes at send time.

---

## 7. Pillar #6 — Peer Communication & Intervention (Direct Coordination)

The first five pillars coordinate **through the blackboard** — a conversation
writes an epic/decision/feed event, and siblings read it later. That is
deliberately indirect (no point-to-point messaging, so no storm). But two needs
don't fit a blackboard: (a) a conversation wants to say something to **one
specific sibling** ("I'm taking the parser refactor, stand down"), and (b) a
conversation needs to **nudge or stop** a sibling that is duplicating or
mis-heading work. Pillar #6 adds exactly that, while preserving the board's
"advisory, never a hard lock" ethos.

### The three agent tools (gated to project mode)
- **`project_peer_status`** — LIVE introspection: which sibling conversations
  (and their sub-agents) are active right now, their phase, the file being
  edited, and the board epic they're advancing. This is the *live* complement
  to reading past messages. It prints each peer's short id (`[convId[:8]]`) that
  the other two tools consume verbatim.
- **`project_message`** — agent-initiated messaging to ONE named sibling. The
  message is enqueued as a `KIND_PEER_MSG` turn source into the target and seen
  on the target's **next** turn.
- **`project_intervene`** — an advisory nudge asking a peer to pause and re-check
  the board; its optional **hard abort** actually cancels the peer's running
  task.

(`project_feed_read` — read the recent cross-conversation activity feed — rides
in the same tool group.)

### Isolation from the workflow lane
Peer messaging uses a **dedicated** message-queue kind, `KIND_PEER_MSG`,
distinct from `KIND_WORKFLOW` (brain dispatch). Its priority is **40** — *after*
a human `real` turn (10, so a human always wins) but *before* a brain-dispatch
`workflow_step` kickoff (50). A peer message is dispatchable and is seen on the
target's next turn; it **NEVER interrupts a live turn mid-stream.**

### The anti-storm invariants (why this can't melt down)
A point-to-point channel is exactly the storm-class hazard the blackboard was
built to avoid, so two structural guards are load-bearing:

1. **Per-`(sender, target)` sliding-window rate limit** —
   `_PEER_MSG_MAX_PER_WINDOW = 3` messages per `_PEER_MSG_WINDOW_S = 120.0`
   seconds, computed by a pure, testable window check (`_prune_and_check`).
2. **No auto-relay.** A received peer message is *plain turn content* that
   triggers **no send path** — so an A→B→A auto-amplification (the N²-fan-out
   failure) is structurally impossible. Do NOT add any code path that
   auto-sends a peer message in response to receiving one.

### Advisory-first, with one audit-gated exception
Everything here is advisory except the `project_intervene` **hard abort**, which
is the only coercive verb. It is **refused unless an explicit, non-empty
`approved_by` token is present** (an audit-logged human approval:
`audit_log('intervention', approved_by=…)`) — never a silent kill. Even then it
aborts the peer's **TASK only**, never the host process. The presence⋈task⋈board
join these tools read reuses the **same `claims_by_conv`** aggregation
`build_brain_summary` uses — never a second hand-rolled join.

*Code: `lib/conversations/project_peer.py`; tools in `lib/tools/conversation.py`;
human-facing REST under `/api/v1/project/brain/`.*

---

## 8. Pillar #7 — The Status Lane (Human Situational Awareness)

The first six pillars are **agent-facing** blackboards the human can read and
poke per-cell. But as automation runs ahead of the human, two gaps open that no
per-cell read closes: (a) there is no way to **ask the project** "where are we /
are we drifting from the charter?" and get a synthesized answer, and (b) there
is no persistent **memory of project STATUS** — the Charter is durable memory of
*intent*, the Feed is a raw ephemeral pulse, and the per-conversation summaries
were never aggregated. Pillar #7 is the human's window back in.

### Data model — an append-only trail, not one overwritten narrative
Table `project_status_snapshots`, composite primary key `(project_path, seq)`
with a per-project **monotonic `seq`** minted under one module lock (mirrors
`project_events` exactly). Each snapshot row carries a **`narrative`** (the
synthesized "where-are-we + drift read" prose), a **`pillar_state`** evidence
JSON (epic open/claimed/done/blocked counts, in-flight epics + owners, pending
decisions, charter version + north-star, active-peer count, sibling digest), a
**`trigger`** (`epic_completed` / `decision_committed` / `blocked` / `on_open` /
`manual`), and **`ts`**. **Retention** is bounded (`_SNAPSHOTS_KEEP = 200`
most-recent per project, pruned on insert): the human needs the **trail** — how
the project got here — not a single clobbered line.

### The synthesis generator, with a laziness gate
`build_status_snapshot(project_path)` reads LIVE pillar state via the SAME
cross-pillar `claims_by_conv` join `build_brain_summary` uses (never a second
aggregation), then gates on a **material-change fingerprint** (`_fingerprint`
over the coarse signals — epic counts, pending decisions, blocked count, charter
version, in-flight titles). Only when that fingerprint moved does it call the
**cheap model** for the narrative + an explicit alignment-to-north-star read. A
quiescent project is never re-synthesized (no LLM, so repeated tab-opens are
free). It is **best-effort** — it never raises into a caller and **keeps the
previous snapshot** rather than writing an empty one on LLM failure.

### Regeneration — both warm-keeping and on-demand
- **Event-driven** — `epic_completed` / `decision_committed` / `blocked` warm a
  fresh snapshot in a non-blocking daemon thread (like `ensure_summary`), so a
  settled action never blocks on an LLM call.
- **On tab-open** — the view path returns the CACHED latest snapshot + history
  immediately, checks the staleness gate cheaply (a fingerprint compare, no
  LLM), and if the project moved, warms a fresh snapshot in the background.

### Surface
A roomy **Project Brain status tab** shows the latest narrative + the snapshot
trail; the always-visible **collab-bar** shows the one-line headline
(`status_line`, read-only). A read-only Q&A answers a specific human question
against LIVE pillar state and writes **nothing** (no snapshot appended).

### The hard invariant — human-facing ONLY
This memory is **NEVER** injected into `lib/tasks_pkg/system_context.py` or any
sibling-agent prompt — doing so would blur the human↔brain lane into the
agent↔agent lane and re-raise the coupling/storm concerns. The lane is strictly
1:1 human↔synthesis: there is **no fan-out/broadcast verb** and **no new
inter-conversation write path** — the only engine write remains dispatch.

*Code: `lib/conversations/project_status.py`; table in
`lib/database/_core_schema.py` (`project_status_snapshots`); REST at
`/api/v1/project/brain/status`; design in
[`PROJECT_BRAIN_STATUS_LANE.md`](PROJECT_BRAIN_STATUS_LANE.md).*

---

## 9. The Panel (Observability & Human Control)

A brain whose coordination state is invisible can't be trusted or corrected. A
slide-in **Project Brain panel** (opened from the project bar) presents three
columns, all keyed on the *displayed conversation's* project path (resolved via
the same accessor the rest of the UI uses — never a global):

- **Activity** — the live feed (backfill + PushHub stream, deduped as in §3).
- **Charter** — north-star text + committed decisions. Agents self-commit
  decisions (they land here directly), so the panel is primarily the human's
  window in + the **corrective levers**: edit the north-star `content`, and
  edit/remove a committed decision (`update_decision` / `delete_decision` /
  `delete_charter`), all optimistic-locked. Any residual `proposed_decision`
  events still expose a **Commit / Reject** control for the human.
- **Board** — a kanban (open / claimed / done) of epics; claimed cards show the
  owner-conversation chip (clicking it opens that conversation).

All frontend↔backend traffic goes through the **unified API client**
(`window.Api.project.feed/charter/board/commitCharter`) — no raw `fetch`; the
cross-side hash algorithm for the push key is byte-identical to the backend's.

---

## 10. End-to-End: The Brain in Motion

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
4. B reaches a project-wide decision → `project_charter_commit` → the
   optimistic-locked commit writes it into the Charter (`version` bumps), emits
   a `decided` event → from now on every conversation's prompt carries that
   committed decision — **with no human in the loop**. (B may instead
   `project_charter_propose` when it wants to leave a non-binding suggestion.)
5. The **human** watches the decision land in the panel and, if it needs
   correcting, uses a corrective lever (edit/remove the decision) — an optional
   veto, not a required approval.
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

## 11. Reproduction Checklist (the minimal set of moving parts)

To rebuild this from scratch you need exactly:

1. **Four tables**, all per-`projectPath`: `project_events` (append log,
   monotonic `seq`, bounded), `project_charter` (single row, `version`
   optimistic lock), `project_tasks` (epics, `status`, soft-lease
   `lease_expires_at`, `depends_on`), and `project_status_snapshots` (Pillar
   #7's append-only status trail, monotonic `seq`, bounded to 200).
2. **A pure `_effective_status(stored, lease_expires_at, now)`** that treats an
   expired claim as `open` — the single, read-time, reaper-free anti-deadlock
   rule, reused by both the board read and dispatch selection.
3. **A push channel** `project` routed by `sha1(projectPath)[:16]` computed
   identically on both sides — the isolation boundary.
4. **Prompt injection** of `[PROJECT CHARTER]` and `[PROJECT BOARD]` blocks
   (with the per-epic avoid-duplication hint) — ambient, existence-gated,
   path-keyed.
5. **Agent tools** gated to project mode: charter read/propose/**commit**
   (commit appends a DECISION only, never the north-star `content`),
   board read/post/claim/complete/block.
6. **A charter commit path** (optimistic-locked, emits `decided`) reachable by
   BOTH the agent tool (decision-append only) and the human REST route (which
   additionally owns the north-star `content` + the corrective edit/delete
   levers).
7. **A dispatch engine**: pure `select_dispatchable` + idempotent `dispatch_epic`
   (claim-then-enqueue a `workflow_step` kickoff, reusing the existing message
   queue) + a completion trigger + a **heartbeat sweep on the existing
   scheduler tick** (bounded, busy-guarded, best-effort).
8. **A three-column panel** reading feed/charter/board and exposing the human
   Commit/Reject gate, all through the unified API client.
9. **A peer channel** (Pillar #6): a dedicated `KIND_PEER_MSG` turn source
   (priority between `real` and `workflow_step`), the three project-gated tools
   (`project_peer_status` / `project_message` / `project_intervene`), a
   per-`(sender,target)` sliding-window rate limit (3 / 120 s), no auto-relay,
   and an `approved_by`-gated hard abort.
10. **A status lane** (Pillar #7): the append-only `project_status_snapshots`
    trail + a synthesis generator (`build_status_snapshot`) gated on a
    material-change fingerprint (cheap-model, best-effort), warmed event-driven
    + on tab-open, surfaced HUMAN-FACING ONLY (never injected into an agent
    prompt).

Hold to the two invariants — **key on `projectPath` (never a process-global)**
and **best-effort (never block the triggering path)** — and the whole thing
composes without deadlocks, cross-project leaks, or thrash.

---

## 12. Where the Code Lives (implementation map)

| Component | File(s) |
|---|---|
| Table definitions | `lib/database/_core_schema.py` (`project_events`, `project_charter`, `project_tasks`, `project_status_snapshots`); bootstrap in `_schema_sqlite.py` / `_schema_pg.py` |
| Activity Feed engine | `lib/conversations/project_feed.py` (`VALID_KINDS`, `emit_event`, `sha1(path)[:16]` key, retention) |
| Charter engine | `lib/conversations/project_charter.py` (`read`/`propose`/`commit`, optimistic lock) |
| Board engine | `lib/conversations/project_board.py` (`_effective_status`, `claim`/`complete`/`block`, `[PROJECT BOARD]` render) |
| Dispatch + heartbeat | `lib/conversations/project_dispatch.py` (`select_dispatchable`, `dispatch_epic`, `on_epic_posted`, `on_conv_idle`, `on_epic_completed`, `on_epic_answered`, `sweep_all_active_projects`); completion-nudge wiring in `lib/tasks_pkg/manager/_sync.py`; peer send-time drain in `lib/conversations/project_peer.py` |
| Peer comms + intervention (Pillar #6) | `lib/conversations/project_peer.py` (`project_peer_status`, `project_message`, `project_intervene`, `_prune_and_check` rate gate, `_authorize_hard_abort`) |
| Status lane (Pillar #7) | `lib/conversations/project_status.py` (`build_status_snapshot`, `_fingerprint`, `status_line`) |
| Heartbeat wiring | `lib/scheduler/manager.py` (call on the 30s tick) |
| Prompt injection | `lib/tasks_pkg/system_context.py` (`[PROJECT CHARTER]` + `[PROJECT BOARD]` blocks) |
| Agent tools | `lib/tools/conversation.py`, gated in `lib/tools/registry.py`, dispatched in `lib/tasks_pkg/handlers/misc.py` |
| Turn source | `lib/message_queue.py` (`enqueue_message`, `workflow_step` kind) |
| REST surface | `routes/api_v1/project.py` (`/project/feed`, `/project/charter`, `/project/charter/commit`, `/project/board`, `/project/brain/peers`, `/project/brain/status`) |
| Frontend panel | `static/js/project-brain.js`, markup in `index.html`, client methods in `static/js/api.js` (`Api.project.*`) |
| Tests | `tests/test_project_feed.py`, `test_project_charter.py`, `test_project_board.py`, `test_project_dispatch.py`, `test_project_peer.py`, `test_project_status*.py`, `test_frontend_project_brain.py`, `test_core_schema_parity.py` |
