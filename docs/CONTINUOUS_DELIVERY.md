# Continuous workspace delivery

Owner: `lib/project_mod/delivery.py`, `task_delta_delivery.py`, `delivery_retry.py`
and `lib/integration_control.py`. See [the integration map](modules/project_integration.md).

## Scope: opt-in isolated tasks

Ordinary canonical-root tasks execute directly by default, without an eager input snapshot,
checkout, or merge-back. Direct edits are visible immediately and survive task
failure/cancel; existing file undo, freshness, readonly and writer guards remain.
File-history snapshots and explicitly enabled linear Git checkpoints are separate.

Select isolation with `git config --local tofu.workspaceMode isolated` in the
canonical repository; `git config --local --unset-all tofu.workspaceMode` restores
the default. Process `TOFU_WORKSPACE_MODE=direct|isolated` overrides the local key
(remove an override separately). Missing policy means direct; values are stripped
and lowercased. Empty/unknown/duplicate local values and failed reads refuse
admission. The bounded config read strips Git routing/config environment overrides.
Existing registered workspaces retain ownership/settlement despite policy changes;
unregistered linked checkouts still refuse. No refs/registrations are migrated.

Dispatched file grants still require isolated workspaces even with explicit `direct`:
changes must remain private until review and checkpoint-pinned publication. This
restriction does not grant execution authority; live grant and I/O fences remain
with [project coordination](PROJECT_COORDINATION.md).
**Problem:** mandatory project-input snapshotting and checkout delay even reads.
**Decision:** make direct execution independent of publication eligibility; the
owner is `lib/project_mod/workspace_policy.py`. No tree scan, input snapshot,
checkout, or publication-ref probe is needed at direct startup. Only actual
isolated creation emits `workspacePreparing`. **Alternatives rejected:** optimizing
mandatory copies retains the cost; lazy-first-write isolation postpones it and
cannot infer every shell write; linear-checkpoint eligibility couples startup to
unrelated candidate/stable history. Cooperative guards remain, but shared shell
commands and external writers are not transaction-isolated.

Validation: a local synthetic admission probe (10 samples after warmup per size)
measured median 28.654 ms for 10 dirty tracked files and 28.932 ms for 4,000, each
with 100 untracked files and incompatible linear-publication history. Both created
zero Git objects/snapshots/checkouts. Registration/coordinator storage was mocked;
this excludes context/provider/startup latency and is not an end-to-end SLO.

The task-only merge/recovery contract below applies to registered isolated
workspaces, not to direct tasks. Direct tasks have no synthetic delivery receipt.

## Decision and execution

**Problem:** task publication projected changes onto committed HEAD, so code
built on uncommitted input and compatible same-file edits could not return until
another writer committed. Validation checkouts and release gates also delayed
working code, while successful task settlement returned only a pending intent.
**Decision:** the live canonical workspace is the development authority.
Successful isolated-task settlement durably records intent, captures its owned
checkpoint and synchronously attempts a three-way merge against current workspace
bytes before returning. Commit history, branch movement and release validation
are independent of this delivery. **Alternatives rejected:** requiring a clean
checkout/committed baseline excludes normal development; committing the entire
workspace misattributes other writers' WIP; blind copying overwrites concurrent
changes; waiting for the background worker prevents immediate confirmation.

The immutable registered baseline includes dirty/untracked context. Only its
difference to the task checkpoint is merged. A private Git index initialized
from a current workspace snapshot computes the three-way merge, including binary
files, modes, additions and deletions; no temporary checkout is materialized.
The resolved source-to-target patch is applied to canonical files with native
Git whole-patch preflight, leaving HEAD and the real index unchanged. Compatible
changes in one file merge while preserving staged and unstaged layers.
Unborn/detached branches need no initial commit. A successful receipt has
`state=delivered`, `phase=workspace_adoption`, `publicationMode=working_tree`;
its target SHA identifies recovery evidence, not a commit on the user's branch.
No-change delivery uses `phase=no_changes` with the same publication mode.

Cooperative leases cover only task-delta paths during capture and final
publication; unrelated file writers proceed. Merge computation runs outside
those leases and repository admission. Final admission checks
registered checkpoint, repair identity, terminal outcome, revocation and current
bytes of task-delta paths. Unrelated edits/index/history changes do not invalidate
a resolved patch. Git preflights the complete patch again during application;
conflicts never leave markers or partially publish siblings. External writers
that ignore cooperative leases remain outside the transaction guarantee.
Background commands and child agents must retire before checkpoint capture.
Failed/cancelled/stopped tasks preserve their source and checkpoint, never publish.
Existing candidate/stable/linear checkpoint release gates remain independent.
Confirmed working-tree delivery reaches the existing opt-in linear checkpoint
from the commit round. If delivery finishes later, the existing recovery worker
runs the same checkpoint owner. No opt-in means no automatic commit; configured
verification, staged-content preservation and coalesced-workspace attribution
remain unchanged. Pending, failed and explicit candidate receipts cannot borrow
this authority. One `origin.deliveryCheckpoint` acknowledgement ends recovery;
lock deferral remains retryable. No additional queue or model turn is created.

**Problem:** isolated delivery was excluded from the enabled checkpoint path,
while every changed source clone was retained and capture locked unrelated files.
**Decision:** reconnect confirmed delivery to the common checkpoint owner, scope
publication leases to changed paths and retire managed completed sources only
after proving their bytes/index are recoverable from pinned checkpoints.
Uncaptured edits, independent staged versions and durable ignored artifacts keep
their source. `origin.sourceRetirement` records reclaimed/retained once; transient
failures and lost acknowledgements retry through the existing worker. External
worktrees, failed tasks and conflicts remain untouched. **Alternatives rejected:**
blind deletion loses artifacts; committing every project ignores opt-in; removing
freshness/exclusion allows overlapping writers to corrupt delivery.

A recovery ref and durable pre-publication receipt precede file mutation. A lost
final ACK is reconciled by comparing target snapshot bytes on task-delta paths,
without replaying an already-applied patch. Later unrelated edits do not affect
that proof. If those paths also changed before acknowledgement, recovery cannot
invent delivery evidence; it retains the source and reports retry/conflict debt.
Historical committed receipts remain historical facts; they are not relabelled.

## Conflict recovery

The same durable integration worker retries transient writer/storage contention
and input movement after the synchronous first attempt. No additional queue,
model loop or detached waiting job is introduced. `origin.delivery.retry` follows
`workspace_delivery_retry_v1.schema.json`: conflict fingerprints use registered
baseline/checkpoint and current entries of task-delta paths. Unchanged conflict
inputs skip repeated merge computation. Committing unchanged workspace bytes or
editing unrelated paths does not trigger a retry. Former verification failures
can now deliver workspace bytes without waiting for a release gate.

Conflict names are capped at 64 and 4,096 encoded bytes, with count/truncation
metadata. One replaceable owner/task input ref pins the immutable dossier;
failed receipt admission restores the previous ref. Unchanged snapshots reuse
pinned evidence rather than creating timestamp-only retry objects.

Failure notifications bound their UTF-8 narrative to the existing 720-byte
Project Brain limit before storage admission; the durable delivery receipt
retains the detailed diagnostic. Idempotency still hashes the original reason,
so distinct failures sharing a displayed prefix remain separate events.
Sending the full diagnostic as a narrative would reject the notification on
both storage backends instead of making the failure visible.

Authorized repair edits the registered source, checkpoints it and uses the
existing owner-scoped retry API. Repair overlays only original task-delta paths
onto the pinned conflict input, preserving unrelated canonical WIP.
`origin.delivery.repair` follows `workspace_delivery_repair_v1.schema.json`.
Checkpoint/receipt/outcome identity is rechecked before publication. Extra repair
paths require new owned work; cancelled/cross-owner requests remain rejected.

Last cooperative write release wakes only an already-running authorized worker,
outside lease/registry locks. Cross-process/external changes retain existing
bounded recovery polling. No model polling or automatic repair permission exists.

Specifications: `tests/project_mod/test_workspace_first_delivery.py`, continuous
conflict/recovery tests, integration delivery/revocation and writer lease suites.

Resource observation: ten local disposable Git/SQLite task settlements with
completed-source retirement measured median 891.591 ms, maximum 1,222.498 ms,
zero retained clones and zero descriptor growth. This is fixture evidence, not a production/FUSE latency
guarantee. Delivery uses call-local temporary indexes, no new resident pool or
queue; the existing per-task recovery/input refs retain their lifecycle. Unchanged
conflict retries retain the existing object/ref growth guard.
