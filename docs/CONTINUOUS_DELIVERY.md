# Continuous workspace delivery

Owner: `lib/project_mod/delivery.py`, `task_delta_delivery.py`, `delivery_retry.py`
and `lib/integration_control.py`. This extends the existing task-owned delivery
authority in [the integration map](modules/project_integration.md).


## Decision and execution

**Problem:** direct patch application rejected compatible overlapping context;
unchanged failures repeatedly created scratch trees or reran verification. A
project-wide idle condition cannot establish whether a particular delta is safe.
**Decision:** reuse the existing owner/task queue and immutable inputs. Three-way
application runs in a private scratch checkout; only the resolved combined tree
passes to gates and final branch/HEAD/tree/outcome/registration revalidation.
Unrelated project presence does not gate automatic task delivery. Exact-tree
verification remains mandatory, including cross-file/interface effects that a
text merge cannot establish. **Alternatives rejected:** waiting for every agent
to stop starves delivery; publishing a model's merge judgment bypasses verification;
a second autonomous model queue duplicates execution/admission and can expand authority.

`origin.delivery.retry` follows `workspace_delivery_retry_v1.schema.json`:
conflict retry fingerprints include registered baseline/checkpoint, target branch
and entries of all task-delta paths; unrelated canonical edits do not rerun a
conflict merge. Verification failures retain `state=failed`, with a retry condition
covering the full combined source tree and host-selected gate command/options.
Unchanged inputs retain the exact receipt and skip scratch/gates; a changed input
resumes through the same worker, including after restart. Forbidden-path gate
failures stay permanent. Gate timeouts and transient lease/storage failures remain ordinary retry debt;
a timeout never becomes an unchanged semantic-failure fingerprint.
Conflict names are capped at 64 and 4,096 encoded bytes, with full count/truncation
metadata. One replaceable owner/task input ref pins the current immutable dossier;
failed receipt admission restores the previous ref. Unchanged snapshots reuse the
pinned Git commit instead of producing timestamp-only dangling objects.

A separately authorized agent/human repairs the registered source and checkpoints
it, then uses the existing owner-scoped `retry` API. Automatic registrations retry
canonical delta delivery, not candidate integration; terminal/revoked/cross-owner
requests refuse. The repair proposal overlays only original task-delta paths onto
the pinned canonical conflict input, preserving unrelated canonical WIP absent from
the source clone. `origin.delivery.repair` follows `workspace_delivery_repair_v1.schema.json`;
it pins original baseline, registered repair checkpoint and effective combined
baseline/checkpoint. Proposal preparation releases admission; final admission
rechecks the registration/receipt/outcome. Gates and publication recheck this recipe;
extra repair paths require new owned work. Lost final receipts reconcile the effective
checkpoint without replaying writes. Source clones and original task baselines remain.
Optional Project Feed evidence points authorized repair at the immutable inputs;
it does not launch a model or treat narrative as execution permission.

Last cooperative write release wakes only an already-running, lifecycle-authorized
worker, outside lease/registry locks. The worker cannot wake itself via its own
scratch/publication releases; API-only replicas cannot start an executor this way.
Cross-process/external changes retain bounded recovery polling. No model polling,
new thread pool, detached waiting job or automatic repair permission is introduced.
Specifications: `tests/project_mod/test_continuous_delivery.py`, neighboring delivery,
revocation, writer exclusion and validation-concurrency suites.

Resource measurement: 20 identical blocked ticks in an isolated local SQLite/Git
fixture (tracemalloc enabled) took median/p95 67.899/70.141 ms, peak traced allocation
224,865 bytes, zero FD delta, zero merge/scratch calls and zero Git-object/ref/worktree
growth. This measures local observation overhead, not FUSE throughput or application
capacity. The existing worker/page bounds and capacity policies remain its limits.
