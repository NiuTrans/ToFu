# Conversations & Project Brain

Project coordination has a separate binding owner (`lib/conversations/project_coordination.py`), not Board dispatch. Its [schema](../../contracts/project_coordination_v1.schema.json) defines owner/project CAS revision, revalidated availability and capability projection. Passive message/consideration behavior is owned by [PROJECT_COORDINATION.md](../PROJECT_COORDINATION.md); child steering by [RETAINED_AGENTS.md](../RETAINED_AGENTS.md). `/api/v4/project/coordination` GET and `/bind` POST use conversation scope; binding starts no execution. The lazy `frontend/src/features/project-coordination.ts` drawer reuses ordinary conversation navigation. Removed/rebound targets stay unavailable, never silently reassigned; project rename requires explicit rebinding. Guard: `tests/project_mod/test_project_coordination.py`.

Project Brain is a signal-driven, owner-scoped read model for project work. It
does not ask a model to maintain coordination state and it never dispatches,
claims, transfers, reopens, or blocks work. Git publication remains adjacent
in [`project_integration.md`](project_integration.md).

Schema: [`project_brain_v1.schema.json`](../../contracts/project_brain_v1.schema.json).

## Ownership map

| Concern | Runtime owner |
|---|---|
| Event append, projection fold, receipt, checkpoint | `lib/storage_sidecar/operations_pkg/_project_brain.py` |
| Operation registration | `lib/storage_sidecar/operation_domains/project_brain.py` |
| Automatic work signals, context, overlap, Checkers | `lib/conversations/project_brain.py` |
| Backup-backed cutover and restart reconciliation | `lib/conversations/project_brain_startup.py` |
| Read projection and human command HTTP API | `routes/api_v1/project_brain.py` |
| Retained browser UI | `frontend/src/runtime/sections/project-brain.js` |
| Git candidate/quarantine and canonical delivery | `lib/integration_control.py`, `lib/project_mod/delivery.py` |
| Registered task roots and terminal delivery intent | `lib/tasks_pkg/workspace_delivery.py` |

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

Concurrent signals share one command identity: only one `work_started` event.
Before tools run, the task adapter binds writable configured Git roots to
owner/physical-task registrations, creating isolated source-snapshot worktrees
for unregistered canonical roots, and persists origin metadata; errors fail closed.
Read-only roots, cwd visits and file receipts never authorize adoption of another tree.
Project Work stays keyed by canonical project; its deterministic ID is distinct
from the physical task ID used by delivery. The work-start signal is fail-soft,
not the binding barrier. `conversationId` is immutable once created.
Title priority is active todo, unfinished todo, Goal title, request first line,
then first edited path. One later higher-priority signal may refine the title.

`ProjectWorkItem.status` is exactly `active | completed | failed | cancelled`.
`completed` requires the canonical completed verdict plus a terminal success
status (a normal question reply can qualify); missing finish evidence, truncation
and internal fences are `failed`, user abort/revocation is `cancelled`. There is
no open/claimed/blocked/lease/dependency/reopen/transfer state. Later turns create
new work. Neither work completion nor candidate `merged` confirms canonical
delivery: settlement records intent separately in Integration `origin.delivery`.

A terminal `work_result` narrative is emitted only when the work produced
changed paths/artifacts or ended failed/cancelled. Ordinary no-output success
does not grow Feed. Startup reads task metadata/verdict evidence for orphaned
`active` items; it acknowledges workspace intent before `work.finish`, preserving
active retry eligibility if intent persistence fails. Missing execution evidence
fails closed, never adopts a sibling checkout or changes conversation ownership.
Problem: a one-shot 1000-project snapshot blocked boot once project history
reached the scan limit, even with no active work. Decision: the recovery
operation returns bounded pages (1000 scanned projects plus one lookahead)
with a storage-issued `nextCursor` whenever `capped` is true. Startup settles
one page before reading the next, including empty active-work pages. Index-key
continuations survive earlier projects moving forward when settled; offsets
would skip projects as the update order changes. Missing/nonadvancing cursors
fail closed. Raising the global limit or accepting incomplete recovery was
rejected: neither guarantees complete recovery with bounded page memory.
The request/page schema lives in `project_brain_v1.schema.json`.

### `work_result` entry composition

Problem: a sign-off alone does not identify completed work or overlapping files.
Decision: compose summaries and render context at separate seams:

- Settlement composes the stored summary (pure application layer, no storage
  contract change): line one keeps the human/assistant outcome for the project
  board, and a bounded `完成项:` line appends the final completed checklist
  items (`todo_state_from_task`, at most the last five, 80 chars each). The
  composition fits the 720-byte narrative budget. Workspace receipts append a
  bounded `Workspace delivery:` status with an explicit not-confirmation caveat;
  later candidate/delivery progress never rewrites the terminal work outcome.
- Project Context rendering joins the entry with projection state and keeps
  internal bookkeeping (`#sequence`, `[kind]`, the English page header) out of
  the model-visible text. A `work_result` row is one plain sentence:
  `- 本会话|同一项目的其他工作者 会话「title」(id) 完成了|未能完成|已取消:
  <completed checklist items, else the stored outcome>。改动文件: <last five
  paths>`. Other narrative kinds render as `- <text>`, with `decision`/`note`
  prefixed by a plain `项目决策:`/`备注:` label.

Alternative rejected: structured todo/path payloads would require a cross-engine
contract migration (Python sidecar and native tofu-db) for no authority gain;
render-time joins already read the same projection.

## Project Context and narrative acknowledgement

Problem: broad project feeds distract ordinary workers. Decision: only the exact
available `ProjectCoordinationRepository` coordinator for the owner/project may
acquire automatic context. Unbound, foreign, deleted, archived, read-only, moved
or lookup-failed bindings deny access before cursor/projection/token acquisition.
Preparation clears uncommitted delivery; ACK revalidates binding and task identity.
With unseen narrative, one final user-role `[Project Context]` meta suffix includes
Charter, Watch, other conversations' Active work and the next narrative page.
Own coordinator work is excluded; system bytes stay unchanged, with no reminder
wrapper. Workers retain existing scoped inbox/overlap advice; UI reads remain.
Alternatives rejected: parallel state or text/LLM filters. Context acquisition and binding grant no dispatch.

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
  current Watch state reaches the eligible coordinator without a second event.

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
- existing Integration review/promote operations, keyed by registered workspace identity.

Board has no mutation API or UI controls. Legacy Charter proposals, peer
messaging/intervention, synthesized status Q&A, Board mutations, and Watch
promotion/follow-up routes are not registered. Project Brain model tools retain only
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

Primary guard: `tests/project_mod/test_project_brain_signal_driven.py`.

Also run storage process-boundary tests, Project Brain/Integration route tests,
tool inventory generation, i18n/runtime composition checks, frontend build, and
the docs catalog gate. Full-suite results are a moving target when another
writer is changing shared generated/runtime files.

Retired refresh lane — Problem: an unreferenced private bounded queue and its tests still encoded dropped refresh scopes after runtime owners moved away. Decision: remove the unreferenced private lane and its obsolete capacity/refusal tests; current runtime ownership stays in the referenced services. Alternative rejected: maintaining a second, unused scheduler invites accidental reinstatement of rejection behavior. A repository reference audit found no runtime consumers before removal.
