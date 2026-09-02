# Conversations & Project Brain

This map covers `lib/conversations/`: the owner-scoped coordination substrate
shared by conversations working on one project, plus conversation-domain
metadata/search/reconciliation helpers. Git publication is adjacent but has a
separate authority in `lib/integration_control.py`.

Read with [`project_integration.md`](project_integration.md) for the Git
publication state machine and [`../STORAGE.md`](../STORAGE.md) for the Sidecar
authority contract.

## Ownership map

| Concern | Runtime owner |
|---|---|
| Delete, restore, clone lifecycle | `contracts/conversation_lifecycle_v1.yaml`, `routes/conversations.py`, `lib/storage_sidecar/operations_pkg/_conversations.py` |
| Project path normalization and feed | `project_feed.py` |
| Shared charter and decisions | `project_charter.py` |
| Epic lifecycle, leases, write-sets, human blocks | `project_board.py`, `project_board_policy.py` |
| Autonomous selection, queueing, stranded recovery | `project_dispatch.py` |
| Peer status/messages/intervention | `project_peer.py` |
| Human-facing status trail | `project_status.py` |
| Standing watch items | `project_watch.py` |
| Compact UI summary | `project_brain_summary.py` |
| Sibling work digest | `project_summary.py` |
| Conversation metadata/search/reconciliation | `catalog.py`, `repository.py`, `meta_cache.py`, `settings_store.py`, `search_index.py`, `reconcile.py`, `title_gen.py` |
| Presence | `lib/presence/` |
| Isolated-writer Git publication | `lib/integration_control.py` and its repository/sidecar domain |

Modules call one another through public verbs. Routes do not reproduce joins or
write directly to Project Brain tables.

## Standing invariants

### Ownership and storage

- Every durable Project Brain read and mutation receives explicit `user_id`.
  User identity is not inferred from a module global.
- `normalize_project_path` is the shared project identity seam. Durable rows
  are owner + normalized-project scoped.
- Business logic uses semantic sidecar operations through `get_storage_client`
  or a repository. SQLite/Postgres details stay in the storage layer.
- Delete, restore, and clone are atomic Sidecar lifecycle transitions. Browser
  caches never upload transcript arrays or serve as recovery authority.
- Feed/status/watch writes are append-only or lifecycle-specific commands with
  idempotency identities. A retry must not duplicate a logical transition.

### Board and dispatch

- Effective status is evaluated at read time. A claimed epic with an expired
  soft lease reads as open and has no effective owner.
- `claims_by_conv` is the single claimed-epic-to-conversation join used by
  summaries and peer status.
- Dependencies are satisfied only by completed epics. Live claimed, blocked,
  and lease-kind rows are not dispatchable work.
- A declared `write_set` travels from board post through isolation origin
  metadata to the integration gate. Dispatch uses it to reduce concurrent
  overlap; Git integration treats it as an enforced boundary.
- Claim + workflow enqueue is one atomic sidecar command. The dispatcher does
  not create a claimed epic without its durable kickoff.
- A kickoff already present in the queue is the durable anti-duplication fact,
  including after the board lease expires.
- Isolation is fail-closed. If an isolated workspace cannot be created, the
  epic is blocked and no agent runs against the shared canonical tree.

### Attribution and recovery

- Queue workflow payloads store `boardTaskId`. On drain the canonical user turn
  stores `_boardTaskId`, `_brainDispatch`, and `_brainEpic`; task API messages
  may omit private attribution fields, but the durable turn must retain them.
- A sweep first reconciles stranded workflow kickoffs in idle conversations,
  including kickoffs whose board lease has expired, then selects new work.
- Draining and normal selection in one sweep must never spawn two tasks for the
  same epic.
- Completing one epic triggers a best-effort dependency re-evaluation. A
  follow-up kickoff may remain queued when its target conversation is busy and
  is recovered by a later sweep.

### Cross-pillar behavior

- Durable pillars define ownership. Presence is ephemeral: its TTL sweeper
  exists only while peers do and it may enrich but never authorize a view.
- Cross-pillar warm triggers are best-effort: failure to emit a feed/push/status
  hint does not roll back an already committed board transition.
- Status/watch/summary warm triggers share the probed per-lane queue budget;
  overflow is counted, repeated scopes coalesce with force preserved, and
  durable state reconstructs rejected work. Consumers retire after the probed
  idle window; submit and timeout exit share one condition, so accepted work
  always owns a live or newly started worker.
- Pending undo records remain durable in each project's session file. Their
  process-local acceleration layer is an observable LRU bounded by the probed
  `TOFU_PROJECT_UNDO_CACHE_CAPACITY`; eviction never removes an undo record and
  a later access reloads it from disk.
- Human-gated blocks that claim a question card exists must carry a structured
  question. Sibling blocks do not masquerade as human questions.
- Reconciliation is cleanup/projection logic; removing a ghost message never
  auto-starts a generation.

### Cross-conversation awareness

- Structured `convRefs`/`convRefTexts` on a user turn are the authoritative
  explicit reference signal. Assistant prose never enables conversation tools.
- `list_conversations` scopes by normalized project when one is present,
  excludes the current conversation, and searches current owner-visible state.
- One owner/project-filtered sibling snapshot feeds the bounded digest and UI
  metadata; one board snapshot feeds active-claim gating/rendering. Neither is a store.
- Summary refresh keys content revision/message count and degrades to the title;
  failure must not block the current task or inject stale foreign-owner data.

### Conversation catalog reads

- Same-owner, same-shape metadata-list arrivals share one repository read. The
  gather closes before that read starts, so a later request never reuses an
  already-started snapshot; no TTL or completed result survives the callers.
- The process-local gather registry is capped by the launch-probed Sidecar RPC
  capacity, fails open at saturation, creates no worker pool, and returns
  independent metadata copies. Different owners and projection keys never
  share a flight.

## Core flows

### Epic post to autonomous start

```text
post_task
  -> persist epic + write_set
  -> create/register isolated writer workspace
       failure -> block epic, stop
  -> on_epic_posted
  -> select dispatchable target
  -> atomic board.dispatch (claim + workflow queue row)
  -> drain only when target conversation is idle
  -> create task + spawn agent
```

An epic posted mid-turn sees its target as busy, so the post-time hook defers.
The scheduler sweep later claims and self-drains it. This is the cold-start path,
not an exceptional fallback.

### Completion and dependent work

```text
complete_task(A)
  -> durable A=done
  -> append completion evidence
  -> re-evaluate dependent B
  -> atomic claim + enqueue B
  -> drain now if idle, otherwise later reconciliation
```

### Isolated Git publication

```text
writer edits isolated worktree
  -> checkpoint (alternate index; writer index untouched)
  -> submit immutable commit
  -> integration worker merges into refs/tofu/candidate
       conflict/gate failure -> quarantined
       success -> merged
  -> explicit gated promotion -> refs/tofu/stable
```

If canonical HEAD advanced independently, the explicit `reconcile-head` action
can merge its committed history into candidate under the same project gate.
Dirty files and conflicts fail closed. The canonical branch remains
observation-only. Full transition and gate rules live in
[`project_integration.md`](project_integration.md).

## Read models

- `build_brain_summary` is the small, hot UI projection: board counts,
  claims-by-conversation, charter/proposal signals, peer presence, and current
  status.
- `collect_pillar_state` is the richer status/watch evidence projection. It may
  read more fields than the UI summary but must compose the same primitive
  owners instead of reimplementing them.
- A third composite reader should project one of these existing read models or
  justify a new contract; it must not hand-roll board/charter/presence joins.

## Failure semantics

- Missing/invalid input returns a structured unsuccessful result at public
  conversation-tool seams; storage authority failures retain their typed
  taxonomy at HTTP/repository boundaries.
- Board claim conflicts, dependency blocks, and cooldowns are expected domain
  outcomes, not generic internal errors.
- Dispatch logs and continues on optional notification failure, but it does not
  fail open across storage, ownership, queue atomicity, or isolation boundaries.
- Git conflicts and red gates quarantine immutable evidence. They do not move
  candidate/stable or consume another model turn automatically.

## Test map

| Behavior | Tests |
|---|---|
| Board CRUD, leases, dependencies, write-sets, blocks | `test_project_board*`, `test_project_board_sidecar.py` |
| Dispatch selection, atomic queueing, dedupe | `test_project_dispatch*`, `test_project_brain_dispatch_dedup.py` |
| Cold start, dependent dispatch, stranded recovery | `test_project_brain_integration.py` |
| Isolation failure is fail-closed | `test_project_board_isolation_fail_closed.py` |
| Queue attribution/provenance | `test_brain_dispatch_provenance.py`, `test_project_brain_integration.py` |
| Summary, feed, charter, status, watch | corresponding `test_project_*` modules |
| Git control plane | `test_integration_control.py`, `test_integration_control_repository.py` |
| Integration REST/UI | `test_api_v1_integration_control.py`, `test_frontend_project_brain_integration.py` |

Use the smallest row first. For a dispatch-to-Git change, verify board/queue
contracts before the broader flywheel and integration suites.

## Change routing

- Add or change a persisted field: define it once in the semantic storage
  operation and update its public projection/tests.
- Change dispatch eligibility: edit `project_dispatch.py`; do not add a second
  filter in scheduler or UI code.
- Change lease/cooldown rules: edit `project_board_policy.py` and keep reads and
  mutations aligned.
- Change Git state/ref behavior: edit `lib/integration_control.py` plus its
  repository/sidecar transition contract; do not revive `project_commit` as a
  parallel authority.
- Change retained Project Brain UI: edit source sections under
  `frontend/src/runtime/sections/`, never generated `app-runtime.js`.
