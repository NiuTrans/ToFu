# Project dispatch authority

## Ownership and authority

`repository.py` owns authorization and evidence per owner/canonical project.
`segmented.py` reads selected documents and commits their deltas through the
shared project-head CAS, including resumable legacy cutover. Request/admission definitions come only
from `contracts/project_dispatch_v1.schema.json`. Current coordination metadata
belongs to `lib/conversations/project_coordination.py`; source turns belong to
`lib/turn_lifecycle.py`. Identity, service, routes and runtime integration are
separate owners. No repository method creates a task, invokes a model/tool,
adds a worker, or writes SQL.

## Ports and invariants

`ProjectDispatchRepository(owner, project, *, client, conversations, turn_get,
binding_repository, clock, attempt_get, workspace_get)` accepts self-contained test ports. Defaults use the
selected portable storage client, owner-scoped conversation repository,
`get_turn(conversation, turn, user_id=owner, include_projection=...)` and
coordination repository. Prerequisite evidence, the delegation source and the
publication result read Turn metadata only; the human source keeps its body read.
`_check_record` skips a stored row's schema walk only for the exact canonical
digest last validated under that definition and row identity; it caches no row or
authority, and caller-supplied input is always walked.
`clock` and budget deadlines are Unix seconds; grant `createdAt` uses that clock.

Authorization replay preserves evidence, never renews a revoked/expired grant.
`validate(admission)` is the live gate: it rechecks owner/tenant/project/revision,
recipient metadata, current coordinator binding and genuine human source
projection revision. Caller API identity cannot promote synthetic input.
Initial execution binding is immutable; a restart-paused or background-completion
continuation advances only from its exact settled, unreviewed predecessor through
`continue_execution`, preserving one deterministic workspace task identity.
`revoke(id, expected_revision)` returns
that association (or `None`) for caller-owned existing cancellation.
`record_review` fences the grant revision and records an immutable decision
linked to a positive result revision; it does not assert delivery/completion.


`dependencies.py` owns bounded backward graph/readiness policy over the repository
snapshot and owner-scoped evidence ports. `validate_own` checks own authority;
`validate` additionally fences all direct/transitive result pins. Neither graph
projection nor acceptance creates a worker. File prerequisites require the exact
registered workspace, publication pin and durable delivery receipt.

`mutate(id, callback, check=None)` is trusted internal ledger access only. An
optional aggregate check executes in the same CAS after the proposed mutation.
Callbacks may run
again after CAS conflicts/uncertain commits; they must be deterministic,
idempotent and side-effect-free. They receive a deep copy and may edit it in
place or return a replacement. They cannot alter identity, request, grant,
execution association, review or publication; all other rows remain unchanged. Runtime owns
model-attempt ledger policy, not this repository.

`authorize_batch` commits exact approved child grants and a manifest in this
same CAS, not a new runtime. `batch_requests` owns child derivation and aggregate
allowance checks. `read_batch`/`find_batch` are read-only; service batch retry
uses the same ordinary commands. Physical launches are not atomic. Batch tests:
`tests/project_mod/test_project_dispatch_batches.py` and
`test_project_dispatch_batch_service.py`; the current API/resource boundary
lives in `docs/PROJECT_COORDINATION.md`.

## Runtime seams

`delegation.py` derives worker policy from the current accepted human Coordinator
Turn and pre-role tool assembly, using the same grant CAS and physical accounting.
Its review pins exact results/checkpoints as Coordinator judgment, never human
acceptance. `project` workers use normal tools in owned isolated workspaces;
legacy consult/file profiles retain their restrictions. Contract and operational
limits live in PROJECT_COORDINATION.md. Verify coordinator worker unit/native tests.
Physical resumes retain the first shared-request delegation. `worker_control`
admits current human steering over the same binding epoch while keeping synthetic
continuations scoped to their original request. Verify the handoff contracts on
all three selected authorities; passive project history grants no control rights.

`continuation.py` derives result-driven grants and immutable completion receipts
from the original policy, with no spend ceiling or deadline. Stalled turns retry
with backoff before a blocked report; `request_decision` keeps a request open and
applies the recommendation when the human stays silent. Timed wakes are a
process-local reminder over durable due times; startup/settlement recovery is the
authority. No synthetic human, durable scheduler or new task store. Verify
continuation race/Stop/backoff/decision tests and the native Coordinator journey.

`request_budget.py` registers the human source account once the accepted input is
final (an attempt owing a deferred input translation binds at worker entry in
`lib/tasks_pkg/spawn.py`) and records occurrences into that same dispatch CAS for
usage projection only; it never refuses work for spend. Its authority refusal at
dispatch is a typed `ModelAdmissionRefused`, never a builtin exception.
`accounting.py` reduces existing ExecutionBudget events and folds settled receipts
into counters with per-root watermarks so unbounded requests keep bounded records.
Earlier finite grants keep their recorded ceilings. Verify usage, uncertain ACKs,
fold/replay idempotency, restart and startup rollback.

`scope.py` owns the explicit file profile and descriptor fence; root dispatch
pins the first-party handler before plugin/MCP routing. `admission.py` resolves
exact bound physical task and registered workspace, never cwd or sibling tasks.
One admission validates once: `validated_dispatch` hands the validated row to that
same decision only, never to a later admission or request.
Its `requires_isolated_workspace` resolves the validated file profile before
ordinary direct-mode selection; it grants no task or file execution authority.
`require_isolated_worker_root` refuses delegation for roots no Worker can be
isolated from; tool admission refusals raise `ToolAdmissionRefused` (failed,
never a user Stop), while argument-shape refusals are `ToolContractError` and
reject only that call. The root contract lives in PROJECT_COORDINATION.md.
Authority failures stop the task through `stop_on_authority_failure`, naming
`dispatch_grant_revoked` or `dispatch_authority_lost`; an earlier Stop keeps its
cause. Verify `test_project_dispatch_abort_cause.py` and its settlement specs.
`publication.py` stamps initial origin and guards all existing publication paths;
`service.publish` pins the checkpoint. `recovery.py` retries existing versioned
grants at settlement/startup and withdraws authority through real Stop entry points.
Do not add a second queue, poller, budget account or delivery store. Current limits
and semantics are in `docs/PROJECT_COORDINATION.md` and the machine contract.

## Continuity owners

`scope_changes.py` owns pinned human withdrawal and receipts within the dispatch
record; existing exact-attempt cancellation/passive mailbox ports perform effects.
`responsibilities.py` owns finite conservative occurrence debits and original
human/binding epoch; its adapter uses only the existing automation registry.
`shared_files.py` owns declared metadata versions and exact untrusted-data reads,
not writes or scans. `continuity_contract.py` resolves only registered local
schemas for HTTP. Bounds and focused guards: `docs/PROJECT_COORDINATION.md`.

Grant/responsibility epochs are immutable. Returning to an old coordinator ID
cannot restore permission; legacy unpinned records remain readable evidence,
never upgraded authority.

## Boundedness decision

`contracts/project_dispatch_storage_v2.json` defines the selected owned segmented
storage port, implemented by SQL `_project_dispatch.py` and native
`semantic_project_dispatch.rs`. Queries never initiate migration. Mutations
freeze the legacy record with its existing CAS, copy atomic prefixes, then
activate the complete segmented authority. Old writers fail closed. Retain the
small legacy locator for startup discovery; only the redundant copy is removed.
Snapshot selections must include every row needed by admission and accounting.
`history.py` selects bounded passive history pages through owned range cursors;
its policy-free display selection must never be reused for admission/accounting.
`history.RecipientOrigins` reads catalog Coordinator provenance from the order
index, each immutable admission once per process; the head's assignment count is
its only freshness witness. It is display evidence, never admission input.
`service.HistoryPreviews` lets a page reuse a result preview only for the exact
Turn revision and status it read; the route keeps one page per owner/project.
A build whose client disconnected stops before its next row (`HistoryBuildCancelled`).
Page rows are the contract's `historyRow` request projection; exact
commands/assignments remain point queries returning complete views. A page's
dependency states are display evidence evaluated against that page's `snapshot`;
admission never reuses it. Frontend
history replaces one page at a time; full recovery and allowance cohorts remain
separate consumers.
Head/count witnesses reject torn or incomplete allowance indexes; do not turn
missing evidence into free capacity. Full history and current-request cohort
reads still materialize their selected evidence; see PROJECT_COORDINATION.md.

Problem: the old 128-assignment/4 MiB project record exhausted durable history
and rewrote every grant on each receipt. Decision: individually bounded evidence,
ordered indexes and an atomic head witness without a lifetime assignment limit.
Alternatives rejected: enlarging the monolith, deleting authorization evidence,
independent reservation writes, and another task/worker state machine.

## Verification

`python3 -m pytest -q tests/project_mod/test_project_dispatch_repository.py`
retains deterministic legacy admission/boundary specifications.
`tests/storage/test_project_dispatch_repository_segments.py` exercises the
selected repository on SQLite, PostgreSQL and native authorities, including
cutover faults, racing writers, point access and retained evidence beyond 128.
`test_coordinator_segmented_allowance.py` replays physical conservation contracts
through actual segmented storage. Native journeys verify ordinary Worker birth,
review, restart and report delivery; no paid provider is used.

File-only work consumes complete granted sources and explicit batches without
private byte/count admission. Human call allowances and every live authority
fence remain mandatory. Default descriptor reads are complete; caller-supplied
limits stay explicit. Verify `test_project_dispatch_scope.py` with revocation,
reparenting, freshness and journal/undo failure boundaries.

`test_project_dispatch_lazy_validation.py` uses a fresh interpreter to verify
ordinary tool admission without loading the schema engine, refusal when a real
profile needs an unavailable validator, and valid/invalid first-use validation.
