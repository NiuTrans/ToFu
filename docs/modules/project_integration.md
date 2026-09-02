# Project integration

This domain owns two Git modes. Isolated Project Brain writers become immutable
checkpoints and serialized candidate/stable refs without mutating the canonical
checkout. Model-only repositories may opt into task-end workspace snapshots with
linear machine commits, no worktree, and no merge. Planning/dispatch lives in
[`conversations_project_brain.md`](conversations_project_brain.md).

## Ownership

| Concern | Owner |
|---|---|
| State machine, Git operations, gates, worker, status | `lib/integration_control.py` |
| Canonical-checkout task-end commit, short Git lock, stable/export source policy | `lib/linear_git_checkpoint.py` |
| Shared forbidden-path and semantic-gate policy | `lib/git_checkpoint_policy.py` |
| Owner-scoped transition API | `lib/integration_control.py` through `lib.storage` |
| Durable transition authority | `lib/storage_sidecar/operation_domains/integration.py` |
| Isolation creation/fail-closed dispatch | `lib/conversations/project_board.py` |
| REST projection | `routes/api_v1/integration.py` |
| Retained UI | `frontend/src/runtime/sections/project-brain-integration.js` |

Every durable transition receives explicit `user_id`. A background worker may
claim globally, but the claimed row carries the owner for all later reads and
writes; it never substitutes a process-global identity.

## Ref topology

Isolated integration:

```text
isolated writer -> immutable checkpoint -> refs/tofu/candidate
                                                |
                                     explicit stable gate
                                                v
                                         refs/tofu/stable

canonical HEAD: observation-only
```

- Candidate updates are serialized and compare-and-swap guarded.
- Stable moves only through explicit promotion after its own gate.
- Isolated integration never stages, resets, checks out, cleans, or moves the
  canonical `HEAD`.
- `reconcile-head` explicitly merges clean committed HEAD history into
  candidate under the same gate and CAS rules.
- A dirty checkout is never reported as serving an immutable ref.

Shared canonical checkout (the default):

- Keep `tofu.linearCheckpoint=false` when conversations or external editors
  share a checkout. Writes remain concurrent with containment, freshness,
  atomicity, attribution, and bounded-history safeguards, but shared mode does
  not create automatic Git commits.
- Coordinate overlaps through freshness failures and Project Brain claims.

Opt-in linear mode:

`concurrent writers → terminal task → short Git-only lock → linear workspace
commit HEAD → refs/tofu/stable after the gate passes`

- Enable per repository with `git config --local tofu.linearCheckpoint true`;
  the process-wide
  `TOFU_LINEAR_GIT_CHECKPOINT=1` override applies to every attached Git project
  in that server process.
- Tool dispatch never calls the checkpoint runtime. Git state or failure can
  never reject, delay, or rewrite a project tool result.
- After a task settles, the background commit-round worker captures the whole
  workspace of opted-in repositories. A dirty checkout is normal, including on
  first activation; no clean-baseline admission exists.
- The first settlement records `HEAD` in `refs/tofu/workspace-checkpoint-baseline`
  and anchors `refs/tofu/stable` before capturing dirty bytes. Enabling alone
  moves no ref and cannot make export select an older isolated-mode stable ref.
- Capture stages through an alternate index, creates a commit with mechanical
  task/conversation metadata, CAS-advances the current branch, and repoints the
  real index without changing working-file bytes.
- The task in a commit message identifies which settlement triggered the
  snapshot, not exclusive authorship. Concurrent conversations may be
  coalesced into the same workspace commit. Bytes arriving after capture stay
  dirty and are eligible for the next task-end snapshot.
- Failed/aborted tasks still receive a WIP checkpoint, but cannot move stable.
  A later passing gate evaluates the entire stable-to-checkpoint delta, so a
  docs-only task cannot accidentally publish earlier unverified code.
- If checkpoint thread creation, Git capture, or lock acquisition fails, the
  settled task remains successful and workspace bytes remain in place. A later
  settlement can capture them.
- After baseline activation, open/internal export selects
  `refs/tofu/stable`; `TOFU_EXPORT_SOURCE_REF` or `tofu.exportRef` may pin
  another explicit ref. Before activation, the historical HEAD source remains.

## Workspace states

| State | Allowed next actions |
|---|---|
| `running` | checkpoint, submit, discard |
| `checkpointed` | checkpoint, submit, discard |
| `ready` | worker claim, or discard before claim wins |
| `integrating` | worker completion; abandoned claim recovery |
| `quarantined` / `failed` | repaired checkpoint/submit, retry, discard |
| `merged` / `discarded` | terminal |

Submitted checkpoints are immutable while ready or integrating. Terminal rows
cannot be registered or resurrected. Discard preserves commits and refs for
recovery; it does not erase user work.

## Checkpoint and integration contract

1. Checkpointing uses an alternate Git index and captures tracked changes,
   deletions, and untracked files without changing the writer index.
2. Submit queues exactly one immutable checkpoint commit.
3. The worker performs a real three-way merge against candidate.
4. A conflict moves neither candidate nor stable and records a quarantine ref
   plus conflict metadata.
5. A repaired writer whose `HEAD` moved is explicitly re-anchored; the old
   checkpoint parent is never silently reused.
6. Candidate movement is CAS-protected. A concurrent move requeues instead of
   overwriting another integration.
7. Stable promotion evaluates the exact candidate tree and explicit
   canonical/candidate divergence acknowledgement.
8. An isolated board epic cannot become `done` before its checkpoint reaches
   candidate. A successful merge completes the board epic automatically, so
   dependent epics are released only against source that actually contains
   the dependency.
9. Editing an epic's write-set updates the active integration metadata in the
   same application operation. Once submitted, both checkpoint and declared
   scope are immutable; a sync failure is returned instead of silently gating
   against stale paths.

## Gate policy

Every candidate integration runs against the exact tree it would publish:

- `git diff --check`;
- forbidden-path policy for Git internals, dependency/cache/runtime trees,
  managed worktrees, bytecode, and `node_modules`;
- the board-declared write-set, fetched from owner-scoped workspace metadata;
- Python, JavaScript/module, and JSON syntax checks for changed files;
- `TOFU_INTEGRATION_TEST_CMD` for application/configuration changes.

Missing semantic test configuration quarantines application/config changes;
syntax-only evidence is not called a successful project gate. Stable promotion
runs `TOFU_INTEGRATION_STABLE_TEST_CMD` or the configured candidate gate over
the exact stable-to-candidate delta. Commands execute directly without a shell.

Linear mode applies the same shared forbidden-path and semantic-suffix policy
without a scratch worktree. It syntax-checks the captured commit's matching
canonical checkout, then runs `TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD` (falling
back to `TOFU_INTEGRATION_TEST_CMD`) after releasing the Git operation lock.
`{base}` is current stable and `{target}` is the immutable checkpoint commit.
Stable advances only if `HEAD` and every non-ignored workspace byte match that
commit both before and after the observational gate. Concurrent writes or a
gate rewrite remain dirty for the next snapshot and invalidate promotion; they
never block project tools.

## Project Brain handoff

`post_task` persists the declared write-set and requests an isolated workspace.
Creation failure blocks the epic and starts no agent against the canonical
tree. Queue payloads retain `boardTaskId`; the canonical turn retains
`_boardTaskId`, `_brainDispatch`, and `_brainEpic` for UI attribution and
stranded-kickoff recovery.

## Cost bounds and operations

Idle worker polls are read-only and back off through 3, 6, 12, 24, 48, then 60
seconds. A local durable submit/retry increments a condition generation and
wakes the same worker immediately when that process has lifecycle-owned task
worker authority. An API-only replica writes the durable row but cannot promote
itself into a Git executor; the 60-second ceiling preserves bounded discovery
by the real worker and abandoned-claim recovery. Compared with the
old fixed three-second loop, a continuously empty queue falls from 28,800 to at
most 1,440 Sidecar reads per day (95% fewer). Storage failures retain their
separate 5..30-second circuit delay so repeated local signals cannot hammer an
unavailable authority. Shutdown interrupts either wait, joins the exact owner,
and retains a timed-out owner to prevent duplicate workers.

Git scans cover a bounded set of active or problem rows; terminal history uses
stored metadata. Status is briefly cached, mutations invalidate it, and event
detail is response-bounded. Unregistered and prunable worktree counts remain
visible.

Linear mode keeps one checkout, dependency tree, build cache, and watcher set.
Its Git-operation lock wait defaults to 30 seconds and is capped at 10 minutes;
timeout produces a `deferred` settlement receipt and leaves workspace bytes
untouched. It never reaches the tool execution path. OS advisory locks release
on process death and are held only for alternate-index staging plus atomic ref
and real-index updates, not for model work or the verification command. The
single persistent advisory-lock file lives in the repository's Git common
directory, so its count follows repository lifecycle rather than accumulating
in a central runtime registry. Checkpoint refs are owner/task-scoped and Git
stores content by object identity; there is no copied source tree to reclaim.

Authenticated owner-scoped routes under `/api/v1/project/integration/` expose
status, create/register, reconcile-head, checkpoint, submit, retry, discard,
promote, and prune. UI actions mirror the state table and require a separate
confirmation for acknowledged divergence.

## Invariants

- Isolation failure is fail-closed; no shared-tree fallback exists.
- Linear mode is explicit opt-in and never participates in project-tool
  admission. Only short Git checkpoint operations serialize; project writers
  remain concurrent.
- Checkpoints and submissions are immutable evidence.
- Candidate and stable ref updates are atomic and gate-bound.
- Conflicts/gate failures quarantine evidence and never consume another model
  turn automatically.
- Board write-set metadata is enforced at integration, not just scheduling.
- Terminal records and canonical HEAD are never mutated by cleanup.
- Linear commits never merge, stash, reset, checkout, clean, or push; stable
  promotion is a compare-and-swap ref move over a verified descendant whose
  canonical checkout stayed unchanged throughout verification.

## Change routing and tests

```bash
pytest -q tests/test_integration_control.py tests/test_integration_control_repository.py
pytest -q tests/test_api_v1_integration_control.py tests/test_frontend_project_brain_integration.py
pytest -q tests/test_project_board_isolation_fail_closed.py tests/test_project_brain_integration.py
pytest -q tests/test_linear_git_checkpoint.py tests/test_export_head_snapshot.py
```
