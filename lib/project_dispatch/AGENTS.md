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
`get_turn(conversation, turn, user_id=owner)` and coordination repository.
`clock` and budget deadlines are Unix seconds; grant `createdAt` uses that clock.

Authorization replay preserves evidence, never renews a revoked/expired grant.
`validate(admission)` is the live gate: it rechecks owner/tenant/project/revision,
recipient metadata, current coordinator binding and genuine human source
projection revision. Caller API identity cannot promote synthetic input.
Execution association is immutable. `revoke(id, expected_revision)` returns
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
from the original finite policy. Recovery reuses ordinary command/Turn admission;
no synthetic human, poller or new task store. Verify continuation race/Stop tests
and the native Coordinator restart/review/delivery journey.

`request_budget.py` composes initial human and Worker occurrence receipts into
that same dispatch CAS; `accounting.py` reduces existing ExecutionBudget events.
It creates no physical account or wallet. Future defaults belong to conversations/
coordinator_budget.py; accepted request policies cannot be edited or replenished.
Verify source/Worker allowance races, uncertain ACKs, restart and startup rollback.

`scope.py` owns the explicit file profile and descriptor fence; root dispatch
pins the first-party handler before plugin/MCP routing. `admission.py` resolves
exact bound physical task and registered workspace, never cwd or sibling tasks.
Its `requires_isolated_workspace` resolves the validated file profile before
ordinary direct-mode selection; it grants no task or file execution authority.
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
Exact commands/assignments remain point queries. Frontend history replaces one
page at a time; full recovery and allowance cohorts remain separate consumers.
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
