# Conversations & Project Brain

Project Brain is a signal-driven, owner-scoped read model for project work. It
does not ask a model to maintain coordination state and it never dispatches,
claims, transfers, reopens, or blocks work. Git publication remains adjacent
in [`project_integration.md`](project_integration.md).

The public schema authority is
[`contracts/project_brain_v1.schema.json`](../../contracts/project_brain_v1.schema.json).

## Ownership map

| Concern | Runtime owner |
|---|---|
| Event append, projection fold, receipt, checkpoint | `lib/storage_sidecar/operations_pkg/_project_brain.py` |
| Operation registration | `lib/storage_sidecar/operation_domains/project_brain.py` |
| Automatic work signals, context, overlap, Checkers | `lib/conversations/project_brain.py` |
| Backup-backed cutover and restart reconciliation | `lib/conversations/project_brain_startup.py` |
| Read projection and human command HTTP API | `routes/api_v1/project_brain.py` |
| Retained browser UI | `frontend/src/runtime/sections/project-brain.js` |
| Git publication and quarantine | `lib/integration_control.py` |

## Authority and identity

- `storage_events` is the only event authority. Every project event carries an
  explicit `owner_user_id`, normalized `project_key`, and monotonic
  `project_sequence`.
- `storage_project_brain_projects` is a reconstructible fold, never a second
  write authority. One semantic Sidecar transaction appends the event, folds
  the projection, records the idempotency receipt, and returns a push hint.
- Every call is scoped by positive `user_id + normalized project`; application
  modules do not issue SQL or infer identity from a process global.
- Project rename rekeys retained events and the projection atomically.
- Work history retains 100 recent terminal items, narrative retains 500 entries,
  and cursor/active/checker/watch/decision collections have explicit capacity
  ceilings. Long-lived Charter, Checker, and Watch state rejects overflow rather
  than silently evicting state.
- Periodic projection checkpoint events retain the full rebuild state before an
  old reconstructible event prefix is reclaimed. `project_brain.rebuild`
  verifies ownership and every sequence before replacing a projection.

## Automatic work lifecycle

One physical task maps to one deterministic work ID:

```text
pw_ + sha256(taskId)[:24]
```

The first successful signal creates the item:

1. accepted `todo_write`;
2. successful project file write;
3. an execution already started in an isolated workspace.

Concurrent signals share the same command identity, so only one `work_started`
event is possible. At physical worker entry, the runtime checks whether the
deterministic work ID already owns an active Integration workspace; this check
is fail-soft and later todo/file signals remain authoritative fallbacks.
`conversationId` is captured at creation and is immutable.
Title priority is active todo, unfinished todo, Goal title, request first line,
then first edited path. One later higher-priority signal may refine the title.

`ProjectWorkItem.status` is exactly `active | completed | failed | cancelled`.
Normal task settlement, including a reply that asks the human a question, is
`completed`; execution errors are `failed`; user abort/revocation is
`cancelled`. There is no open/claimed/blocked/lease/dependency/reopen/transfer
state. A later user turn is a new physical task and therefore a new work item.

A terminal `work_result` narrative is emitted only when the work produced
changed paths/artifacts or ended failed/cancelled. Ordinary no-output success
does not grow Feed. Startup reconciles orphaned `active` items against the task
authority and terminally settles them without changing conversation ownership.

## Project Context and narrative acknowledgement

Project Brain never changes the system message. When unseen narrative exists,
the context composer appends one final user-role `[Project Context]` meta message
containing the current executable Charter, active Watch, active work, and the
next narrative page. It uses no `<system-reminder>` wrapper.

- New conversations and project switches initialize their cursor at the current
  head and receive no history snapshot.
- A steady-state turn with no unseen narrative injects zero Project Brain tokens.
- A page contains at most 12 entries and 900 tokens in sequence order. Each
  stored narrative summary is capped at 720 UTF-8 bytes so a row is never
  clipped during delivery and then incorrectly acknowledged.
- The cursor advances only after the first model call successfully returns.
  Request failure or a lost acknowledgement replays the same page. The agent
  core invokes the semantic `ConversationStore.confirm_project_context_delivery`
  port; only the host adapter knows the Project Brain sidecar operation and
  owner-scoped Push hint.
- Compaction and restart restore the cursor projection and never synthesize a
  snapshot fallback.
- Human Watch mutations fold a bounded narrative on their existing event, so
  current Watch state reaches existing conversations without a second event.

## File overlap advisory

After a successful write, the runtime compares normalized changed paths against
other running tasks owned by the same user/project. A prefix overlap queues one
user-role advisory for both tasks before their next tool round and pushes a UI
hint. Deduplication is by task pair plus overlap path, with at most 20 keys per
task. Settlement discards undelivered advice. This data is process-local: it is
never written to Feed, Board, or a persistent inbox, and detection
failure is fail-soft.

## Executable Charter and Checkers

`CheckerDefinition` versions are immutable. They carry `checkerId`, `version`,
`label`, `argv`, `cwd`, `pathGlobs`, `timeoutMs`, and `enabled`. Execution passes
`argv` directly to the process API with `shell=False`; `cwd` must remain inside
the project and output is bounded.

`CharterDecision` always references one registered `{id, version}` and records
its source conversation/turn plus the latest verification. Assistant-turn
promotion pre-fills the conclusion and requires the human to select an exact
Checker version. Text without a Checker cannot enter Charter or prompt; the
human may export it to docs manually.

Checkers run manually, after a terminal work item whose changed paths match
`pathGlobs`, and as a complete enabled set before Integration/release. Failure
or timeout adds one narrative, and release is rejected or
quarantined. It never changes a terminal work item or creates a block state.

## HTTP and UI

Read projections:

- `GET /api/v1/project/board` → `{active, recentOutcomes}`
- `GET /api/v1/project/feed` → `NarrativeEvent[]`
- `GET /api/v1/project/charter`
- `GET /api/v1/project/brain/status`
- `GET /api/v1/project/brain/watch`

Human commands:

- Watch add/update/delete;
- Checker catalog/register/run;
- `POST /api/v1/project/charter/decision/promote`;
- existing Integration review/promote operations, keyed by automatic work ID.

Board has no mutation API or UI controls. Legacy Charter proposals, peer
messaging/intervention, synthesized status Q&A, Board mutations, and Watch
promotion/follow-up routes are not registered. Model tools retain only
`integration_checkpoint` and `integration_submit`; all Project Brain read/write
tools and `integration_status` are absent from the model schema.

## Cutover and rollback

Startup readiness first creates a standard SQLite backup (or requires an
explicit platform PostgreSQL backup receipt), then runs one atomic cutover.
Migration keeps current Watch state and its newest result. Legacy
Charter/North Star/decision text and old Board, Feed, and Status history are
not imported. Historical attention events in the event log stay inert and
never re-materialize projection state. Verification
precedes removal of legacy tables/records; any failure rolls back the Sidecar
transaction and keeps readiness closed.

There is no dual read/write or compatibility alias. Rollback means restore the
pre-cutover backup and run the old release.

## Verification

Primary guard: `tests/test_project_brain_signal_driven.py`.

Also run storage process-boundary tests, Project Brain/Integration route tests,
tool inventory generation, i18n/runtime composition checks, frontend build, and
the docs catalog gate. Full-suite results are a moving target when another
writer is changing shared generated/runtime files.
