# Task Mode & the Orchestration Run-Instance Model

> Status: **proposal / design** — not yet implemented.
> Scope: introduce a durable *run instance* for orchestration flows and a
> dedicated full-surface **Task Mode** UI, distinct from the conversational
> "flow-in-chat" experience.
>
> The load-bearing decision in this doc is the **run-instance schema** (§3).
> Everything else (UI, API, phasing) hangs off it. Read §2 and §3 first.

---

## 1. Why

The orchestration system today empowers *chat*: you pick a saved flow in the
Mode dropdown (`activeFlow`), and the conversation runs on the `FlowExecutor`
engine instead of the plain chat loop. That is genuinely useful and stays.

But a class of work does not fit a chat thread at all. The motivating example:

> "Generate a stable workflow that has a model call tools to screen valuable
> candidates from hundreds/thousands of résumés, then handle follow-ups and
> outreach."

That is not a conversation — it is a **job with state and a dashboard**:
hundreds of items, per-item status, intermediate artifacts, human approval
gates on outreach, and a lifespan measured in days, surviving page reloads.
Stuffing it into "chat-inner" is the wrong container.

We already have the right precedent in-tree: **Reader / Paper mode** is a
full-surface, non-chat experience driven by its own `TaskRuntime` + push
channel. **Task Mode is that same move applied to orchestration runs.**

### What already exists (reuse, don't rebuild)

| Piece | Where | Reused for Task Mode |
|---|---|---|
| Flow authoring (the graph) | `static/js/orchestration.js` (Studio) | unchanged — produces the *template* |
| AI Composer (NL → graph) | `lib/orchestration_composer.py` | unchanged — authors the *template* |
| Template store (CRUD) | `routes/api_v1/orchestrations.py` → `data/config/orchestrations.json` | unchanged — holds *templates* |
| Execution engine | `lib/orchestration_engine.py::FlowExecutor` | unchanged — runs an *instance* |
| Event streaming | `FlowExecutor.on_event` → `orchestration` push channel | unchanged transport |
| Human gates | `/orchestrations/run/human-approve`, `/human-input` | the outreach-approval seam |

### The one real gap

`FlowExecutor` runs on a `TaskRuntime` task that is **in-memory and
TTL-purged** (`lib/agent_core/task_runtime.py::cleanup_stale` deletes finished
tasks after `ttl`, default 1h). So today:

- A run's events, `final`, and `artifacts` **evaporate** shortly after it ends.
- There is no record you can reopen tomorrow, no per-item state, no resume.

A stateful, long-lived job needs a **durable run instance**. That record is the
load-bearing new thing this doc specifies.

---

## 2. Conceptual model: Template vs Instance

```
  Flow Template  (reusable graph; the "recipe")          Run Instance  (one execution; the "job")
  ─────────────────────────────────────────             ─────────────────────────────────────────
  tofu.orchestration/v1 definition                       a definition SNAPSHOT + inputs + live state
  authored in Studio / AI Composer                       created when you "Run as Task"
  stored in orchestrations.json (today)                  stored durably in the DB (new — §3)
  edited freely, versioned by updatedAt                  immutable definition; mutable run state
  N templates                                            M instances per template (1:N)
```

Two rules fall out of this split:

1. **A run instance pins a snapshot of the definition.** Editing the template
   later must never mutate a run that's already in flight or already finished.
   We copy the definition into the instance at creation time.
2. **The composer's job ends at the template.** "How should AI-generated
   workflows be saved?" → as a **template** in the existing store. Running one
   is a separate verb that mints an **instance**. The composer never creates
   instances.

This also answers the secondary question cleanly: the Studio/Composer stay as
the *authoring* surface; **Task Mode is the *operating* surface** for an
instance.

---

## 3. Run-instance schema (the load-bearing decision)

### 3.1 Where it lives — DB, not JSON

Templates live in a flat JSON array and that's fine (few, small, hand-edited).
Run instances are different: potentially many, each carrying large per-item
state (hundreds of résumés), queried by status, and long-lived. That is a
database's job, and the project already has the **dual-backend DB layer**
(`lib/database/`, PostgreSQL primary / SQLite fallback) for exactly this.

So: **two new tables** (DDL added to both `_schema_pg.py` and
`_schema_sqlite.py`; access via the uniform `_wrappers.py` execute/fetch API).
SQLite-flavored SQL is fine — `_sql_translate.py` adapts it for PG.

### 3.2 Table: `orchestration_runs` (the instance header)

One row per run. Holds identity, the pinned definition, lifecycle, and the
final result. The append-only event log and the per-item state are split into
their own tables (§3.3, §3.4) so this row stays small and cheap to list.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | `run_` + ms-hex + rand (mirrors `_new_id()` style) |
| `orch_id` | TEXT | FK-ish → template id; **nullable** (ad-hoc inline defs) |
| `name` | TEXT | denormalized template name at run time (display) |
| `definition` | JSON/TEXT | **pinned snapshot** of the `tofu.orchestration/v1` def |
| `input` | TEXT | the initial request / seed |
| `status` | TEXT | `pending`\|`running`\|`paused`\|`done`\|`error`\|`aborted` |
| `final` | TEXT | engine `final` output (the converged result) |
| `error` | JSON/TEXT | error envelope (nullable) |
| `created_at` | INTEGER | epoch ms |
| `updated_at` | INTEGER | epoch ms — bumped on every state change |
| `finished_at` | INTEGER | epoch ms (nullable) |
| `created_by` | TEXT | user id (multi-user / auth surface) |

> **`status` superset note:** the engine/`TaskRuntime` vocabulary is
> `pending|running|done|error|aborted`. We add **`paused`** for an instance
> blocked on a human gate (approval/input) — a first-class state for Task Mode,
> since these jobs sit waiting on a human for long stretches. `paused` is a
> run-instance concept; the in-memory `TaskRuntime` status stays `running`
> while the engine thread blocks on the gate.

### 3.3 Table: `orchestration_run_events` (durable event log)

The `TaskRuntime` event list is in-memory; we mirror each event to this table
so a run is fully replayable after a reload or restart. The engine's event
vocabulary is the source of truth — we persist it verbatim, no new schema for
event *bodies*.

| Column | Type | Notes |
|---|---|---|
| `run_id` | TEXT | FK → `orchestration_runs.id` |
| `seq` | INTEGER | monotonic per run (matches `TaskRuntime` `event['seq']`) |
| `type` | TEXT | engine event type (`step_start`, `loop_iteration`, …) |
| `node_id` | TEXT | nullable; for node-scoped events |
| `payload` | JSON/TEXT | the full event dict |
| `ts` | INTEGER | epoch ms |

PK `(run_id, seq)`. The poll endpoint becomes "DB-backed cursor replay"
instead of "in-memory slice" — same `{events, next_cursor, status}` shape as
`TaskRuntime.poll`, so the frontend contract is unchanged.

### 3.4 Table: `orchestration_run_items` (per-item state) — Phase 2

This is what makes the résumé use case real: a row per work item (one résumé,
one lead), carrying its own status as it flows through screen → shortlist →
outreach → follow-up.

| Column | Type | Notes |
|---|---|---|
| `run_id` | TEXT | FK → `orchestration_runs.id` |
| `item_id` | TEXT | stable per-item key (e.g. résumé hash) |
| `label` | TEXT | display (candidate name) |
| `stage` | TEXT | author-defined stage label |
| `status` | TEXT | `pending`\|`active`\|`done`\|`skipped`\|`needs_human` |
| `data` | JSON/TEXT | extracted fields, score, notes, outreach draft |
| `updated_at` | INTEGER | epoch ms |

> **Deliberately deferred.** The header + event tables (§3.2/§3.3) are enough
> to make runs durable and reopenable — that's the vertical slice. Per-item
> state is its own design question (how do flows *declare* items? extend the
> `artifact`/fan-out node semantics?) and should not block the slice. I'm
> flagging the shape now so the header schema doesn't paint us into a corner,
> but **§3.4 is Phase 2.**

### 3.5 Relationship to the existing `artifact` node

The Studio already has a first-class `artifact` (deliverable) node, and the
engine returns `artifacts` from `run()` + emits `artifact_declared`. Run-level
artifacts persist as part of the run header / event log. Per-item artifacts are
a §3.4 concern. No change to the artifact node itself.

---

## 4. API surface

New, all under the existing `routes/api_v1/orchestrations.py` blueprint. The
existing `/run` endpoint stays for the **chat-inline** path (ephemeral); Task
Mode gets durable siblings.

```
POST   /api/v1/orchestrations/tasks              create a run instance from {id|definition, input}
GET    /api/v1/orchestrations/tasks              list instances (filter: status, orch_id, mine)
GET    /api/v1/orchestrations/tasks/{run_id}     fetch one (header + latest state)
GET    /api/v1/orchestrations/tasks/{run_id}/events?cursor=N   durable cursor replay
POST   /api/v1/orchestrations/tasks/{run_id}/abort
DELETE /api/v1/orchestrations/tasks/{run_id}     archive/remove a finished instance
# Human gates reuse the EXISTING endpoints, keyed by run_id instead of task_id:
POST   /api/v1/orchestrations/run/human-approve
POST   /api/v1/orchestrations/run/human-input
```

Execution wiring: `POST /tasks` writes the header row (`status=pending`),
mints a `TaskRuntime` task as today, but the `on_event` sink does **two**
things — pushes over the `orchestration` channel (live UI) *and* inserts into
`orchestration_run_events` (durability). On `finish`, the header row is updated
with `final`/`error`/`finished_at`. The engine is untouched.

---

## 5. Task Mode UI

Modeled directly on paper-reader: a full-surface mode, its own feature module
(`static/js/task-mode/…`), bundled via `_BUNDLE_FILES` in `lib/js_bundler.py`
(**don't forget the allowlist** — CLAUDE.md §3.2.1), all backend calls through
`window.Api.orchestrations.*` (CLAUDE.md §3.2.0).

Layout (three panes):

- **Left rail** — run list + (Phase 2) the item/candidate list with per-item
  status chips.
- **Center** — the live run timeline (rendered from the durable event log, so
  it survives reload) and the flow graph with the active node highlighted.
- **Right inspector** — active node detail + **human-in-the-loop gates**.
  Outreach approval lands here and resolves via the existing
  `human-approve` / `human-input` endpoints.

Entry point: a saved flow in the Studio gets a **"Run as Task"** action
alongside today's "use in chat". "Run as Task" opens Task Mode; the Mode
dropdown's `activeFlow` path is unchanged for chat-inline use.

Reopening: Task Mode lists instances from `GET /tasks`; opening one replays its
durable events. A finished run is a readable record, not a dead chat scroll.

### 5.1 House rule for the Task surface: SVG-only, no emoji

**New convention, stricter than CLAUDE.md §3.4:** the Task Mode surface (and
the orchestration UI generally) uses **inline SVG glyphs only — no emoji, even
for abstract/generic concepts.** §3.4 today permits emoji for generic concepts
(🔑/📂/👥) and only bans them as brand-logo substitutes; for this "serious
tool" surface we hold a tighter line.

- All status/affordance icons come from a small inline-SVG vocabulary,
  extending the Studio's existing `_ORCH_GLYPHS` (play/loop/fanout/join/branch/
  stop/artifact/human) with the run-log/state glyphs (running/ok/fail/
  approve/reject/etc.).
- Rationale: consistent, themeable (currentColor), crisp at any DPI, and it
  reads as a product tool rather than a chat toy.
- This is an **intentional convention bump**, scoped to the Task/orchestration
  surface for now — not a claim that prior emoji use violated the rules. A
  follow-up could promote it project-wide.
- (Unchanged: the existing memory `no-emoji-in-llm-facing-tool-output` already
  bans emoji in any LLM-facing text; this rule extends "no emoji" to the
  *display* layer of this surface too.)

---

## 6. Phasing

1. **Emoji→SVG sweep of the orchestration UI** (separate, already agreed; lands
   first, independent of this doc).
2. **Vertical slice — durable runs:** §3.2 + §3.3 tables, `POST/GET /tasks`,
   dual-sink `on_event`, and a minimal Task Mode view that runs one flow and
   shows the live + reloadable timeline. No per-item state yet.
3. **Human gates in Task Mode:** wire the inspector to the existing
   approve/input endpoints keyed by `run_id`; add the `paused` state.
4. **Per-item state (§3.4):** the candidate-dashboard. Requires a design pass
   on how flows declare/iterate items.

Steps 2–4 are real work; step 1 is the safe quick win.

---

## 7. Open questions

- **Item declaration (§3.4):** do fan-out / `artifact` nodes grow an "items"
  notion, or is there a new node type for "iterate over a dataset"? This gates
  the résumé dashboard and deserves its own mini-design.
- **Resume after restart:** durable events make a run *readable* after a server
  restart, but the in-memory engine thread is gone. Do we re-attach (resume
  execution from the last durable checkpoint) or only support
  read/replay + manual re-run? Slice = read/replay; true resume is harder and
  may need engine checkpointing.
- **Template ↔ instance versioning:** we pin a snapshot. Do we also stamp the
  template's `updatedAt` into the instance so the UI can warn "the template has
  changed since this run"? Cheap; probably yes.
- **Retention:** instances are durable, so they need an archive/cleanup policy
  (unlike the TTL-purged in-memory tasks). DELETE endpoint + optional age-based
  archive.
```
