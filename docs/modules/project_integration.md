# Project integration

This domain owns isolated checkpoint/candidate integration, task-owned canonical delivery and opt-in linear snapshots. Candidate merge is not delivery; stable promotion has its own explicit gate. Automatic work signals live in [`conversations_project_brain.md`](conversations_project_brain.md); no Board dispatch/handoff path exists.

## Ownership

| Concern | Owner |
|---|---|
| State machine, Git operations, gates, worker, status | `lib/integration_control.py` |
| Task root binding, terminal intent, delivery receipts | `lib/tasks_pkg/workspace_delivery.py`, `lib/project_mod/delivery.py` |
| Direct/isolated execution selection (independent of publication) | `lib/project_mod/workspace_policy.py` |
| Retry fingerprints, bounded conflict dossiers and scoped repair proposals | `lib/project_mod/delivery_retry.py`; `contracts/workspace_delivery_{retry,repair}_v1.schema.json`; [contract](../CONTINUOUS_DELIVERY.md) |
| Version-bound candidate checks and acceptance projection | `lib/project_mod/delivery_acceptance.py`; [contract](../WORKSPACE_ACCEPTANCE.md) |
| Task delta publication and lazy dependency bridges | `lib/project_mod/task_delta_delivery.py`, `lib/project_mod/workspace_runtime.py` |
| Git command target effects and read preparation | `lib/project_mod/git_command_scope.py`, `lib/project_mod/git_inspection_execution.py` |
| Cooperative lease for Tofu's own whole-workspace operations | `lib/project_mod/workspace_lease.py` |
| Canonical-checkout task-end commit, short Git lock, stable/export source policy | `lib/linear_git_checkpoint.py` |
| Shared forbidden-path, semantic-suffix and repo-scoped gate selection; retirement of publication refs from an unrelated history | `lib/git_checkpoint_policy.py` |
| Landing agent worktree branches on the canonical checkout | `scripts/git_land.py`; [Landing worktree branches](#landing-worktree-branches) |
| Owner-scoped transition API | `lib/integration_control.py` through `lib.storage` |
| Selected durable authority | `lib/storage_authority_sql/operation_domains/integration.py` or `packages/tofu-db/src/integration_workspace.rs` |
| Isolation creation and fail-closed queueing | `lib/integration_control.py` |
| REST projection | `routes/api_v1/integration.py` |
| Retained UI | `frontend/src/runtime/sections/project-brain-integration.js` |
| Shared-mode Git guard (classification, ack nonce, refusal, dispatch) | [`GIT_WORKSPACE_GUARD.md`](../runbooks/GIT_WORKSPACE_GUARD.md) |

Every durable transition receives explicit `user_id`. A worker may claim globally, but its row carries the owner for all subsequent reads/writes; it never substitutes a process-global identity.

## Execution policy: direct by default

Ordinary unregistered canonical roots execute directly without eager input capture or checkout. `TOFU_WORKSPACE_MODE` overrides repository-local `tofu.workspaceMode` (`direct` default / `isolated` opt-in); invalid/unreadable policy refuses admission. Existing task-owned workspaces retain ownership, and unregistered linked roots still refuse.
Dispatched file grants always retain isolated candidates and explicit publication; neither direct setting bypasses their [authority](../PROJECT_COORDINATION.md). Consult roots remain readonly.
Direct writes are visible immediately and survive failure/cancel; readonly, freshness/undo and cooperative guards remain. Only actual isolated checkout creation emits `workspacePreparing`. File-history and terminal Git publication remain independent. See the [execution contract and rationale](../CONTINUOUS_DELIVERY.md#scope-opt-in-isolated-tasks); tests: `test_workspace_mode.py`, `test_workspace_delivery.py`, startup-phase and execution-root suites.

## Ref topology

```text
isolated writer -> immutable checkpoint -> gated refs/tofu/candidate (merged)
                         |                           |
                  task-owned delivery        explicit stable gate
                         v                           v
                  canonical workspace         refs/tofu/stable
```

- Candidate updates are serialized and compare-and-swap guarded.
- Stable moves only through explicit promotion after its own gate.
- Candidate integration never moves canonical `HEAD`; automatic task delivery merges into current workspace bytes without advancing HEAD.
- `reconcile-head` merges clean committed HEAD history into candidate under the same gate/CAS.
- A dirty checkout is never reported as serving an immutable ref.
Workspace admission reads the nearest checkout binding through the shared metadata resolver. Git validates its common directory and bare status and parses `core.worktree` using the explicit Git administration path; repository-local workspace policy uses the same path. These read-only children and the runnable-Git probe start at the filesystem root, without entering the project mount; the metadata and policy reads run under the shared Git stall window (no wall-clock deadline, see the task-start entry under the isolated-admission decisions below). **Problem:** entering the checkout for a metadata-only probe can stall on FUSE directory resolution and fail an accepted task before the model starts. **Decision:** keep Git's repository/configuration validation and task ownership checks while removing checkout entry from these probes. Explicit `core.worktree`, linked and nested checkouts retain their identity; incomplete, malformed or unavailable metadata fails closed, and a read stopped after a whole stall window without progress carries a workspace-specific recovery hint. **Alternatives rejected:** forcing `--work-tree` overrides repository configuration; treating a failed probe as a plain folder bypasses ownership admission. No authority cache or retry loop is added. Verify with `tests/tasks_pkg/test_workspace_metadata_admission.py`, `test_workspace_identity_latency.py`, `test_workspace_mode.py` and `test_workspace_delivery.py`.
- Canonical Git roots selecting `isolated` receive an owner/physical-task worktree before tools run.
- Task admission pins the selected branch once and resolves its committed baseline after accepting the input tree. Two matching working-tree observations pin dirty/untracked context; subsequent HEAD/branch movement does not invalidate those bytes. Every observation refreshes the private index seed and ignore membership, including newly committed ignored files, while publication excludes borrowed dirty input changes against the accepted committed baseline. Existing owner/task registrations resolve before capture, including lost-ACK retries.
- **Problem:** bounded HEAD retries still fail under frequent concurrent commits, and rebuilding indexes/commits on every observation wastes I/O and creates unused Git objects. **Decision:** reuse one private index, compare tree IDs directly, and create at most one accepted input commit. Normal admission uses no writer lease; after three changing content observations, the existing cancellable stable-read lease and tool admission wait policy fence cooperative writers for only two captures. Git materialization and storage registration run after release. Non-cooperating content changes still refuse safely; cancellation retires the index directory without registration. **Alternatives rejected:** accepting one unchecked capture loses consistency; retrying the model/task repeats unrelated work; long checkout locks serialize healthy writers; ignoring a new HEAD index seed loses newly tracked ignored files; keeping the initial commit as publication base misattributes concurrent committed edits as borrowed WIP. Specifications: source snapshot cases in `tests/storage/test_integration_delivery.py`.
- Source HEAD/index stay intact, including unborn branches; existing registrations win. Readonly/non-Git/transient execution stays unchanged.
- **Problem:** a clean canonical checkout still ran the repeated private-index capture even though its selected commit is already the exact accepted input. **Decision:** the first capture remains the mandatory boundary that observes the first content change; the following confirmation observation uses `status --porcelain=v1 -z --untracked-files=all` plus `ls-files -v` (no assume-unchanged or skip-worktree entries) and reuses the selected tree, eliminating the second whole-tree capture and its `add`/`write-tree` on clean sources. Cancellation and the selected-branch reread stay in place; dirty/untracked or flagged inputs, including a tree that changes during capture, keep the full private-index capture and churn fence. **Alternatives rejected:** skipping the first capture cannot observe a change that lands in the boundary itself; status alone can hide flagged working bytes; caching or configuration knobs go stale or limit work. On a clean real 12,646-file checkout on this FUSE volume (three alternating pairs), source snapshot median went 5.378 s → 4.296 s and full workspace preparation 11.946 s → 7.330 s. **Specification:** `test_clean_source_workspace_reuses_selected_commit_without_repeated_capture`; dirty, flagged, ignore-rule, concurrent-commit and publication parity remain in `test_integration_delivery.py` and `test_integration_snapshot_index.py`.

- **Problem:** plain `read-tree` discards stat data from the reused private index, forcing unchanged tracked content to be hashed again. **Decision:** seed with index-only single-tree merge (`read-tree -m -i`) so matching entries retain stat data while membership still follows the fresh parent. Index-only mode avoids rejecting legitimate concurrent working-tree edits as an unsafe checkout. Git still checks changed/racy entries; two equal observations and the existing churn fence remain mandatory. **Alternatives rejected:** keeping stale membership loses ignore/HEAD changes; skipping the second observation weakens input consistency; ordinary merge mode refuses concurrent dirty changes. Real-Git parity tests cover changes, deletion, ignore rules, concurrent commits, modes and symlinks in `tests/storage/test_integration_snapshot_index.py`. A controlled same-volume 150-file/150 MB benchmark of two full captures (including preflight and tree writes) reduced median time from 1.208 s to 0.716 s, about 41%; this does not predict live end-to-end latency. Startup profiling uses paired `[TaskStartupTiming]` start/complete anchors and bounded `tofu_task_startup_stage_seconds` labels. Workspace provisioning, root discovery/identity/binding, snapshot, private-index seed/add/tree, snapshot commit, materialization, lock waits and registration/persistence are distinct stages. Nested spans inherit only allowlisted log identity via a context-local scope that resets on success or failure; IDs and file contents never become metric labels. Durations are inclusive: do not sum parent workspace binding with its child stages. A start without completion identifies the blocked boundary; tests cover nested identity restoration and separate-thread isolation.
- **Problem:** isolated admission refused healthy work when the input snapshot was slow (2026-10-26: `[Spawn] Executor rejected task a1c0feca before worker entry: git add failed: ... timed out after 90.0 seconds`). Dispatched file grants (and `isolated` mode) snapshot the canonical checkout, HEAD plus every uncommitted and untracked non-ignored file, through a private index (`GIT_INDEX_FILE`) because Git cannot commit working bytes without an index. The first observation started from an empty private index without stat data, so `add -A` re-read and hashed every tracked file: 11,460 files / 229.8 MB on the shared BeeGFS FUSE checkout. In the 2026-10-26 log the first `add` of each of 13 admissions took 10.4-21.9 s (one 90.2 s timeout); later observations, which reuse stat data, took 1.3-4.2 s at admission; task-end delivery captures, which also began from an empty index, took up to 39.8 s and share the fix. Untracked trees were not the cost: `data/` (task worktrees), `logs/` and dependency trees are ignored and the untracked walk takes about 1.2 s. While the volume was contended (validation 11.4 s instead of about 2.5 s) the rehash passed the fixed 90 s wall-clock deadline (30 s for the other snapshot commands) and binding failed before worker entry. The snapshot never staged anything in the writer index, but its porcelain `git diff` validation rewrote that index (stat refresh under `index.lock`) whenever it met stat-only changes. **Decision:** a first observation copies the writer index through one descriptor, keeping that version's mtime for Git's racy-entry check, before the single-tree `read-tree -m -i`, so `add -A` hashes only changed files; an index with assume-unchanged or skip-worktree entries is not borrowed, because those bits survive the reseed and would hide working bytes. The conflict check reads the writer index with `ls-files`, which never writes it; path validation now follows seeding and runs on the private index. Whole-tree snapshot commands (`ls-files`, `read-tree`, `diff`, `add`, `write-tree`) have no total deadline: `lib/project_mod/activity_supervised_process.py` renews a 90 s stall window (`GIT_STALL_SECONDS` in `lib/git_runtime.py`, a `fixed`-class constant, never derived from hardware or tree size) whenever any thread of the Git process tree executed, read from kernel scheduler counters (per-thread faults, CPU time and context switches in procfs; psutil elsewhere). A tree blocked on an unresponsive volume shows no change, is retired through `process_group` and fails admission with "made no progress"; owner Stop is observed while it waits. Ref reads that admission depends on share the same window (next decision); ref and object writes (`update-ref`, `commit-tree`) keep their fixed deadlines. Read-only phase benchmark on the live checkout (158 changed and 53 untracked files, `add --dry-run`, four alternating rounds): first observation 10.2-15.9 s became 2.5-2.7 s, its `add` 8.9-14.8 s became 1.2-1.4 s; this is a phase measurement, not an end-to-end latency claim. **Alternatives rejected:** a larger or tree-scaled deadline still refuses slow healthy work; no deadline lets a hung mount hold the worker forever; output-renewed windows (worktree materialization) need progress lines that `add`, `diff` and `ls-files` never print; the untracked cache serves only `git status`-shaped walks and needs repository configuration; scoping `add` to `diff-index` output re-implements `add -A` membership rules (deletions, modes, embedded repositories, ignore changes) for the saving the borrowed stat cache already gives; deferring the snapshot past worker entry leaves the first rounds without their bound root and still needs liveness. A command that keeps executing without progress (a busy loop) is not detected; Stop still retires it. Specifications: `tests/storage/test_integration_snapshot_liveness.py` (fake slow and hung Git under dilated deadlines, Stop), `tests/storage/test_integration_snapshot_index.py` (only changed files are hashed; the writer index stays byte- and mtime-identical) and `tests/project_mod/test_activity_supervised_process.py`.
- **Problem:** task start failed whenever the shared checkout volume paused a constant-size Git read past a fixed deadline, before any model call (2026-11-03: `task_start_failed` "Command [... 'rev-parse', '--show-toplevel', '--git-common-dir'] timed out after 10 seconds"; `logs/error.log` holds 155 such timeouts for about two dozen tasks between 15:24 and 19:44, logged by task-end workspace delivery, including for tasks whose start had failed the same way, and none in the retained logs back to 2026-10-31; also the gate's 30 s `rev-parse --verify <ref>^{tree}`). These reads normally take about 0.1 s (live samples of 120 and 240 reads: median 0.11-0.13 s, maximum 3.9 s), so the failures are intermittent volume stalls, not slow work: a stalled `rev-parse` waits on the volume exactly like a whole-tree `add`. Starting the child outside the checkout does not avoid them: sampled at the same instants, `-C <checkout>` and `--git-dir=<metadata>` reads had the same latency (median 0.11 s, p99 1.1-1.3 s, every slow instant slow in both). **Decision:** every harness Git read that task admission or workspace identity depends on runs under `run_activity_supervised` with the shared `GIT_STALL_SECONDS` window and no total deadline: `workspace_delivery._checkout`/`_has_commit` (through `_checkout_git`), `workspace_policy.resolve_workspace_mode`, and integration `_repo_root`, `_common_dir`, `_rev`, the admission `symbolic-ref`, the index `--git-path` read and the gate's tree read. Worker binding passes the task's Stop (`aborted`/`abort_event`) as the cancellation, so Stop retires a blocked read and surfaces as `AbortedError`, never as a binding failure; only a whole window in which no Git thread ran fails, with "made no progress". Per-call cost is unchanged (local repository, 60 alternating `--git-dir` identity reads per arm: median 3.9 ms fixed-deadline vs 4.1 ms supervised). **Alternatives rejected:** a larger fixed deadline still fails a healthy start on a longer pause; retrying replays a command whose stall says nothing about the next attempt and still needs a bound; caching identity would let a moved or deleted checkout keep its old authority. Specifications: `tests/tasks_pkg/test_workspace_identity_liveness.py` (slow, hung and Stopped identity/policy reads under dilated windows), `tests/tasks_pkg/test_workspace_identity_latency.py` and `tests/storage/test_integration_snapshot_liveness.py`.
- **Problem:** holding repository admission across a slow checkout serialized unrelated tasks and made lock waits uncancellable. **Decision:** a cross-process destination lifecycle lock covers capture, detached metadata creation, private checkout, registration ACK reconciliation and rollback. Only shared input-ref updates and `worktree add --no-checkout` take repository admission; private checkout never holds it. Both lock waits poll cancellation every 50 ms. Same-task retries recheck registration before capture; ambiguous registration/child retirement retains bytes. Discard takes destination admission before publication admission; optional cleanup skips an active destination without waiting (including when already under repository admission), avoiding lock-order inversion. Persistent lock inodes are never unlinked while waiters may reference them. **Alternatives rejected:** simply dropping the repo lock races same-task retries/cleanup; keeping it through checkout throttles independent tasks; background retries duplicate ownership. Tests in `test_integration_delivery.py` hold one real-Git checkout at a barrier and require another task to finish before release, with same-task retry/discard, cancellable destination/repo waits and lost-ACK/WIP checks. No executor, queue, waiter thread or in-memory lock registry is added; one bounded lock FD is held per active admission, plus the brief repo-lock FD. Paired startup stages now split snapshot observations, metadata/checkout, registration and repository/destination wait versus hold; explicit worker identity is inherited only within the timing scope. Tool admission separately records registry wait, lockfile I/O, local condition sleep and cross-process backoff, so an inclusive admission total is never reported as pure contention. Candidate/reconcile/promotion transactions still retain their authority/CAS fences; no lock is removed merely because it appears in a slow total.
- **Problem:** a fixed 90-second `worktree add` wall-clock deadline kills healthy large/slow-volume checkouts before worker entry. **Decision:** controlled creation installs detached metadata with `--no-checkout`, then force-checks out only that fresh, unregistered destination with explicit progress (also on old Git). `lib/project_mod/worktree_process.py` drains one bounded 8 KiB diagnostic tail; increasing completed-file counts with a fixed positive total renew a 90-second stall window. Noise/repeated counts do not renew it. Cancellation is observed during materialization; POSIX groups retire through the existing process-group owner before rollback. Hooks are disabled per invocation, not in repository configuration. No Git write is replayed and no shared-checkout fallback exists. Existing/ambiguous registrations survive; cleanup errors remain secondary evidence rather than masking the startup cause. Materialization timing is logged without contents. `worktree_checkout_policy.checkout_worker_config` defaults fresh checkout concurrency to every effective CPU from the shared cached launch probe, minimum one; memory never narrows it. It reads effective configuration in the new destination, preserving explicit `checkout.workers`, conditional/worktree config and Git's small-tree threshold; it never persists config or moves durable WIP to ephemeral storage. Probe failure is lean; config-read failure leaves Git policy unchanged. Children exist only during the supervised checkout and share its process-group cancellation/retirement. A controlled 1,200-file FUSE benchmark measured serial checkout 3.59–3.87 s versus 0.89–0.95 s at the host's 63-CPU default; this is a phase benchmark, not a production end-to-end latency claim. Real-Git tests in `test_worktree_checkout_policy.py` check actual worker spawning, content/mode/symlink parity, hooks and the small-tree fast path. **Alternatives rejected:** increasing a fixed deadline still misclassifies large trees; ephemeral-volume relocation risks WIP/restart durability; model retries replay admission instead of fixing it. Tests: `test_git_worktree_process.py` and admission cases in `test_integration_delivery.py`.
- **Problem:** on a loaded host a healthy checkout failed admission with `WorktreeRetirementUnproven: Worktree exited; writer retirement unproven: output pipe remains held` (`test_source_snapshot_retries_content_changes_with_one_private_index`, 2026-10-27, beside 32 busy loops at load about 200). After retiring the owned group, teardown gave the output reader 0.2 s to reach EOF, stopped it, joined 0.5 s more, and read a reader that had not yet been scheduled as an escaped pipe holder. **Decision:** after retirement the reader is stopped and joined without a deadline (its reads are non-blocking, so it leaves within one iteration), and teardown proves closure itself with a non-blocking drain: EOF means every writer is gone; EAGAIN, or more than the 1 MiB a retired pipe can buffer, means a writer outside the owned group still holds it, which stays `WorktreeRetirementUnproven` and preserves the destination. **Alternatives rejected:** longer join deadlines (still a scheduling bet); `POLLHUP` on the read end (not portable across POSIX pipes); waiting for the escaped holder to exit (hangs teardown). Specifications: `test_late_reader_after_exit_is_not_mistaken_for_a_pipe_holder` (the reader observes EOF 1.5 s late) and `test_escaped_pipe_holder_cannot_hang_teardown` (teardown returns while the holder still runs).
- **Problem:** every index-observing internal Git read (`status`, `diff`, `ls-files`, `blame`) first runs `git --no-optional-locks --version` with a 2 s deadline, and a probe still running at that bound failed the read itself: `GitInspectionPreparationError: Git read capability probe failed` blocked workspace deliveries in the 2026-10-27 full unit gate (`git ls-files failed: Git read capability probe failed`). **Decision:** the probe only chooses between two strategies that observe the same state, so a probe that times out reports unknown capability and the read takes the private index copy every Git supports; a missing executable or an unrecognized failure still fails. **Alternatives rejected:** a longer probe deadline (a host-speed bet that still fails the read); no deadline (a hung `git --version` would hold the read forever); caching the capability per executable (a separate decision: the module deliberately keeps no capability cache). Specification: `test_slow_capability_probe_selects_private_index_instead_of_failing` in `tests/project_mod/test_git_internal_inspection.py`.
- Problem: completion tasks inherited a finished task's linked checkout without its registration. Decision: `completion_delivery_config` restores recorded canonical roots and readonly descendants, dropping task runtime-root metadata, before new admission.
  Old durable receipts/wakes receive the same projection without changing frozen publication bytes. Explicit roots without canonical metadata stay unchanged; adopting another task's checkout was rejected because it transfers write/publication authority.

Capture-only probe: 30 alternating samples per arm after one warmup, synthetic 100-file repository with 10 dirty files; storage/materialization replaced by no-ops. Old → new: Git calls 24 → 22, commit-tree calls 2 → 1; local median/p95 248.131/1056.250 → 233.138/321.722 ms; FUSE 957.872/3023.893 → 800.289/2039.019 ms. Peak traced bytes local 150,067 → 151,066 and FUSE 150,233 → 151,704; FD delta zero in both arms. One temporary index directory retires per capture; no new pool, persistent waiter or owner ledger. This loaded-host observation excludes checkout time and is not a throughput/SLO claim.
Checkout-overlap probe: real Git, 100 files / 102,400 bytes, in-memory registration seam; while one checkout stayed deliberately blocked, the independent task registered in 265.010 ms. After release both finished in 507.949 ms total, with two registrations and three persistent control-lock inodes. This proves admission overlap, not large-volume checkout speed or a production latency SLO.
Opt-in linear mode: `concurrent writers → terminal task → immutable checkpoint → verification gate → current branch + refs/tofu/stable only after passing`

- Enable with `git config --local tofu.linearCheckpoint true`; process override `TOFU_LINEAR_GIT_CHECKPOINT=1` applies to every attached Git project.
- Linear eligibility controls terminal publication, never execution. Candidate/stable history no longer forces a startup copy; explicit isolation applies even if linear is eligible. Publication failures preserve refs/config/WIP.
- Tool dispatch never calls this checkpoint runtime or rewrites a result from its failure; tool calls take no workspace lease.
- The background commit-round worker captures the whole opted-in workspace after settlement; dirty first activation needs no clean-baseline admission.
- First settlement records `HEAD` in `refs/tofu/workspace-checkpoint-baseline` and anchors `refs/tofu/stable` before capture. Enabling alone moves no ref and cannot make export select an older isolated-mode stable ref.
- Alternate-index capture creates mechanical task/conversation commits under
  `refs/tofu/checkpoints/`. Branch and real index stay at the last publication
  while verification is pending, required or failed. Built-ins check conflict
  markers, syntax, JSON, JavaScript and changed-Python Ruff; semantic changes
  require the project gate. Only an unchanged passing snapshot prepares the
  index and atomically CAS-advances the branch together with `refs/tofu/stable`.
- Task metadata identifies the triggering settlement, not exclusive authorship; conversations may coalesce. Post-capture bytes stay dirty for a later snapshot.
- **Problem:** refusing whenever HEAD moved during capture meant each unrelated
  branch landing could discard the snapshot's chance to publish; frequent
  merges could starve automatic commits indefinitely. **Decision:** a captured
  workspace delta is replayed privately onto an ordinary descendant HEAD, as
  with worktree landing, then the combined immutable tree is verified and
  CAS-published. Real content conflicts and rewritten history still refuse and
  leave the workspace untouched. **Alternatives rejected:** capture retries
  (another merge can always win); dropping the HEAD check (would lose the
  landed commit); writing a merge into the shared checkout (would disturb
  concurrent edits). Specifications: unrelated and conflicting HEAD moves in
  `test_linear_git_checkpoint.py`.
- Confirmed isolated working-tree delivery enters this same opt-in checkpoint through the commit round or recovery worker; pending delivery stays excluded.
- Failed/aborted tasks retain WIP refs, never advance branch/stable. Later gates
  check the entire stable-to-checkpoint delta, including earlier unverified code.
- Thread/capture/lock failure preserves settled task status and bytes; later settlement can capture them.
  Both returned and exceptional checkpoint failures travel on the existing `round_committed`
  `linearGitCheckpoint` receipt, so a persistent publication failure is not warning-only.
- After baseline activation export selects `refs/tofu/stable`; explicit
  `TOFU_EXPORT_SOURCE_REF`/`tofu.exportRef` overrides remain. Before it, use HEAD.
  A baseline outside HEAD's history (a rewritten history) no longer owns
  publication: promotion only fast-forwards, so its stable could never move
  again and every export would ship one frozen tree. Export then uses HEAD and
  logs why. An owned baseline whose stable left HEAD's history fails closed.
  Alternative rejected: always exporting HEAD would bypass the verified-stable
  gate for repositories where linear publication still works.
- **Rewritten history and a trailing stable.** Problem: after the history
  rebuild, candidate, stable and baseline still pointed into the abandoned
  history; every task-end checkpoint then failed with "Integration candidate
  exists" (only a log warning), uncommitted work piled up in the shared
  checkout and blocked worktree merges, and an explicit workspace would have
  been seeded from the abandoned candidate tree. Separately, settlement
  required stable to equal HEAD, so any ordinary commit or landed merge
  disabled linear mode for good. Decision: both planes first apply
  `retire_unrelated_publication_refs`: a publication ref that shares no
  history with HEAD moves to `refs/tofu/retired/<UTC>-<head>/` in one
  compare-and-swap transaction pinned to HEAD (commits stay reachable), then
  ordinary activation runs. An owned stable may trail HEAD: the gate verifies
  the whole stable-to-checkpoint delta and publication moves branch and stable
  together. A stable or baseline that diverged but still shares history fails
  closed as before. Alternatives rejected: an operator deleting the refs (the
  next rewrite silently disables checkpoints again); retiring every
  non-ancestor ref (a reset could discard verified work); "no local branch
  shares the history" (thousands of pre-rewrite branches keep it alive, so it
  never fires); keeping stable equal to HEAD (incompatible with landing
  worktree branches, which repository guidance requires). Specifications:
  rewritten-history and stable-behind/diverged cases in
  `test_linear_git_checkpoint.py`, `test_git_checkpoint_policy.py` and
  `test_workspace_creation_retires_candidate_from_rewritten_history`.

## Workspace states
| State | Allowed next actions |
|---|---|
| `running` | checkpoint, submit, discard |
| `checkpointed` | checkpoint, submit, discard |
| `ready` | worker claim, or discard before claim wins |
| `integrating` | worker completion; abandoned claim recovery |
| `quarantined` / `failed` | repaired checkpoint/submit, retry, discard |
| `merged` / `discarded` | terminal |

Submitted checkpoints are immutable while ready/integrating; terminal rows cannot be registered/resurrected. Discard preserves commits/refs for recovery, never erases user work.

## Checkpoint and integration contract

1. A checkout-local lease and alternate index capture tracked/untracked changes;
   HEAD, index and bytes stay intact. Peer leases never claim the common directory.
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
8. Registered row identity owns integration; automatic delivery uses physical
   task ID. Candidate narratives never change a terminal Project Work outcome.
9. Submit/retry and release run the applicable immutable Checker versions
   before Git publication. Any failure rejects or quarantines publication.

**Legacy Git merges.** Problem: merges use `git merge-tree --write-tree`
(Git 2.38; `--merge-base` 2.40) and fall back to a throwaway-worktree merge on
older Git, but the fallback recognized only exit 129 and `unknown option`.
Legacy merge-tree reads `--write-tree A B` as `<base-tree> <branch1> <branch2>`
and dies with `unknown rev --write-tree` (exit 128), so on the Git 2.34 hosts
that run this service every non-fast-forward integration was quarantined as a
conflict and canonical-HEAD reconciliation reported one. The fallback spec
simulated a message real Git never prints for that call. Decision: one
predicate, `_merge_tree_lacks_write_tree`, recognizes every legacy answer at
both merge sites, and `tests/storage/test_integration_control.py` emulates
legacy merge-tree by operand count. Alternatives rejected: parsing
`git version` adds a second probe and a version table; passing an explicit
merge base to force the usage exit changes criss-cross merge results; treating
any non-zero exit as legacy would hide real conflicts and Git errors.

## Task-owned canonical delivery
Problem: candidate `merged` does not mean the user's checkout has the work; shared-root defaults
also let conversations collide. Decision: `origin.delivery` owns intent; automatic registrations
use `executionMode=isolated-task`, `sourceHead`, `sourceSnapshot`, and `sourceRef` as their baseline.
Only writable configured source roots authorize creation. Structured code paths rebind before approval/execution; namespaces freeze under physical task ID and cross-checkout shell writes refuse; runtime/data paths retain their declared store; this is cooperation, not a filesystem sandbox.

Terminal intent precedes evidence release. Failed/cancelled/stopped outcomes revoke publication;
checkpoint/undo evidence remains. Background commands and child agents fence capture.
Successful automatic settlement immediately merges baseline-to-checkpoint changes into the
current canonical workspace, including borrowed dirty inputs and compatible concurrent edits.
A private index computes the merge; no validation checkout or release gate delays delivery.
Final admission rechecks registered authority and task-delta paths. Whole-patch application
preserves real index/HEAD, unrelated WIP and branch history; conflicts block without partial
publication. Unborn branches need no bootstrap commit. Durable recovery refs and target-byte
comparison reconcile lost ACKs without replay. Candidate/stable refs remain unchanged;
[delivery contract](../CONTINUOUS_DELIVERY.md) defines receipts, retry and commit independence.

**A checkpoint already contained in the target is delivered, not a delta to replay.**
Problem: a Worker that lands its own work with `git_land` can name the resulting merge as its
immutable delivery checkpoint. The shared branch already contains that commit and may have
advanced with overlapping edits, but publication still replayed the task-input-to-checkpoint
delta and reported a false `task_delta_conflict`. Decision: under repository admission, after
rechecking terminal publication authority, test the selected checkpoint against live canonical
`HEAD` with `merge-base --is-ancestor`. An exact ancestry result settles as `delivered` /
`no_changes` / `publicationMode=committed`, records the containing HEAD in `targetSha`, and skips
canonical capture, merge and patch application. The existing recovery pass therefore re-evaluates
and settles an old blocked receipt whose checkpoint is now contained. Alternatives rejected:
tree/path similarity or accepting a cleanly reapplied patch cannot distinguish later rewrites from
actual task ownership; accepting a checkpoint merely because its ref exists does not prove it was
published; manually closing receipts or forcing retries bypasses durable delivery authority.
Non-ancestor checkpoints and any unsuccessful ancestry probe continue through ordinary merge and
conflict handling. Specifications: `test_landed_checkpoint_is_no_change_even_after_overlapping_target_edits`,
`test_blocked_landed_checkpoint_settles_through_recovery`, and the unchanged real-conflict control
in `test_continuous_delivery.py`.

Explicit registered integration retains candidate merge followed by gated clean-HEAD fast-forward
adoption. Dirty/busy/changed targets or conflicts remain blocked; gate failure remains visible.

Explicit adoption validates immutable inputs outside canonical admission, then rechecks authority and target freshness. Automatic merging rechecks task paths and terminal authority; commits and gates do not govern workspace delivery.
Discard shares the cross-process publication admission. Source-busy handling rereads receipts there and cannot reopen `delivered`/`preserved` as retry debt. No-change cleanup scans run under source exclusion outside canonical repository admission; final identity/terminal checks and controlled Git administration remain serialized.
Alternatives: removing locks loses updates; omitting dirty context loses inputs; whole-tree publication commits others' WIP. Only the durable receiver confirms workspace delivery.
**Tool calls take no workspace lease (owner decision 2026-10-28).** Commands, write tools, uploads, MCP local paths and project-dispatch file tools neither wait for nor take a checkout, tree, metadata or file lease; the only workspace protection left for them is the Git guard for tree-destroying Git, plus access control (read-only roots, registered roots, the isolated-workspace target refusal) and the per-call per-file edit mutex (`file_edit_mutex.py`) that keeps one file's read-modify-write atomic. Problem, decision, measurement and the rejected alternatives (finer-grained claims, mandatory per-task worktrees) are recorded in [`GIT_WORKSPACE_GUARD.md`](../runbooks/GIT_WORKSPACE_GUARD.md#tool-calls-take-no-workspace-leases-owner-decision-2026-10-28). The earlier writer-admission refinements recorded here (read-driver claims, proven readers, shell data targets, loop unrolling, owned-write observation) were superseded by it; the Git history keeps them.
 `git checkout`, `stash`, `reset`, `clean` and ambiguous tree operations retain their existing Git guard. These changes add no pool, owner ledger or cache; persistent coordination-key growth is described below.
Tofu's own multi-target operations use `workspace_claims_lease`: expand/merge the complete physical set and acquire tree, metadata, checkout, then file keys before validation or preimages; `lease_claim_order` alone cannot order hidden barriers. Metadata keys use the neutral namespace and canonical common-dir identity, independent of checkout aliases. Registries contain only active holders; managed children of a holder inherit every relevant barrier/target FD until actual descriptor quiescence.
**Absolute reads outside registered roots — Problem:** task worktrees live in `/tmp`, outside a conversation's registered roots. `read_files` rendered such paths through `lib.file_reader.read_local_file`, which issued no `ReadObservation`, so the round recorded no `_readObservedPaths` and no freshness token, and every later `edit_file` of the same path was refused "must read each target file first" until a write happened to register the tree. **Decision:** `_read_absolute_file` renders UTF-8 text from one descriptor-pinned `read_project_snapshot` whose semantic root is the filesystem root and appends that host-owned observation, so rounds and tokens name the same canonical absolute path that the gate and writers resolve, both before and after registration. Evidence conditions match project reads: regular files only (a non-regular node is never opened), no symlinked path component, UTF-8 without NUL, unchanged during the read. Other files keep the previous display and certify nothing; `..` is resolved lexically exactly as `_resolve_write_path` resolves an edit target. **Alternatives rejected:** auto-registering the read's directory as a root (a read would silently expand write authority); keying evidence by `realpath` (a symlink alias would certify a different path than the one edited); certifying the unpinned `read_local_file` result (the bytes shown and the digest recorded could differ). Specification: `tests/tasks_pkg/test_read_gate_unregistered_absolute.py`.
Paired effect-admission resource evidence: 30 samples after 5 warmups, synthetic local/nested-FUSE checkouts, immutable baseline `56db0f885` versus this effect model. Root/file median cost was 1.307/1.459 ms locally and 9.816/10.737 ms on FUSE, versus 0.236/0.368 and 8.500/9.404 ms; no samples were blocked. First-claim descriptors/persistent key files were 12/14 locally and 32/34 at the deeper FUSE hierarchy (baseline 1/2 and 3/4). Keys were zero-byte; every release left empty registries/descriptors. Keys persist per historical canonical path and consume inodes, not resident holder state: never unlink them while any Tofu process may own/open a key; namespace reclamation requires all processes using it drained. This is admission/lifecycle evidence, not throughput/SLO evidence or a claim that uncontended admission got faster. Cross-process tests establish actual exclusion and child fencing.
Specifications: `test_tool_calls_take_no_workspace_lease.py`, `test_file_scoped_write_lease.py`, `test_file_scoped_publication.py`, `test_file_scoped_upload_target.py`; validation/publication races (`test_delivery_validation_concurrency.py`) and bounded holder attribution/reclamation (`test_workspace_lease_diagnostics.py`).
Ignored dependency preparation uses SH checkout + exclusive runtime-setup identity, not exclusive source ownership. `test_workspace_runtime_admission.py` covers both admission orders, cross-process exclusion, inherited children, cancelled wait reclamation and actual edits during slow setup.
An earlier-candidate local 100-sample probe (10 warmups, tracemalloc enabled; not final regenerated results) measured file median/p95 0.729/0.790 ms and setup 0.581/0.678 ms, 20,292 peak traced bytes, zero FD delta and empty registries after release. Diagnostics hold only bounded identifiers/timestamps in active entries; no new pool, daemon, durable owner ledger or recovery loop. This is local overhead/lifecycle evidence, not FUSE throughput.
Presence is conservative adoption evidence, not authority; automatic source isolation requires task registration.

Git guard segment diagnostics, parameter semantics and metadata-only effects are specified in the [guard contract](../runbooks/GIT_WORKSPACE_GUARD.md#parameter-and-effect-evidence).

## Landing worktree branches

Problem: repository guidance has agents commit in an isolated worktree and merge the branch into
the shared checkout's current branch. `git merge` refuses whenever the branch touches any file
another conversation is editing there without committing, so one writer's in-flight edit (often a
regenerated bundle) blocked every branch that shared a file with it, and verified work stayed
unmerged. Decision: `python3 scripts/git_land.py <branch>` records the same `--no-ff` merge
(first parent: the pre-integration tip, printed for the final answer). `git merge-recursive`
computes it in a private scratch worktree. Each changed authored path is compared
with the checkout's bytes: an unmodified file takes the merged content; a file with concurrent
edits gets a `git merge-file` three-way merge, so those edits stay uncommitted on top of the new
tip. Under Git's own `index.lock` it rechecks the branch, staged state and every planned file's
stat identity (any change replans), replaces files atomically, rewrites only those index entries
and moves the branch by compare-and-swap, index before ref as Git does: an interruption leaves the
merge staged, never a HEAD whose index reverts it. Overlapping lines, staged changes, an untracked
file in the way, a type clash or an operation in progress refuse before anything is written. It
then runs `git worktree remove` (never forced) and `git branch -d`.

A task-input snapshot is the immutable input tree captured by `create_task_workspace` when the
selected branch has uncommitted or untracked context. Its exact commit SHA is retained under
`refs/tofu/inputs/u<owner>/<task>` (and in the workspace origin's `inputBaseSha`); clean inputs
reuse the selected branch commit instead of creating a snapshot. Before computing a merge,
`git_land` intersects `<current>..<branch>` with those exact ref targets and refuses if any are
present. It names each snapshot and its carried files, and requires a fresh worktree from the
shared checkout's current branch followed by cherry-picking only the worker's own commits.
Author/message similarity is deliberately not evidence: ordinary lookalike commits land as before.
Snapshots whose input refs were removed cannot be identified exactly and are not guessed at.
Alternatives rejected: a fixed trailer would not mark existing snapshots; metadata heuristics can
refuse legitimate work or miss snapshots; ignoring the base makes another writer's borrowed input
part of the worker's publication. Specification: `tests/project_mod/test_git_land_script.py`.

Problem: compact generated i18n catalogs conflict even when their authored locales merge cleanly;
binary previews cannot be text-merged. Treating either as independent authored bytes strands
verified work and demands an unnecessary repair turn. Decision: when every conflict is a known
generated artifact, `lib/project_mod/generated_integration.py` resolves it in the private merge
index. Both `git_land` and automatic task-delta delivery select the task's generated bytes to clear
the stage, then regenerate i18n contracts/catalogs, frontend runtime or compiled styles from the
already-merged sources in a temporary checkout. A `designs/**/previews/*.png` uses the task's
output, as that task owns the preview it produced. The canonical workspace and other writers'
uncommitted sources are never touched by the generator. Unknown generated outputs, mixed
source/generated conflicts and generator failures retain the existing refusal/repair receipt.

Alternatives rejected: hand-merging compact JSON duplicates its source authority and is fragile;
a Git merge driver cannot see the other side's merged sources; unrestricted command execution
for every `linguist-generated` path would execute arbitrary repository content, so the generator
registry is explicit and has no configuration knob. Requiring every generated family to exist was
rejected because plain projects can have only one family. Specifications:
`test_generated_i18n_conflict_regenerates_from_merged_sources`, `test_generated_preview_conflict_uses_task_bytes`
and `test_task_delta_regenerates_i18n_after_merging_authored_sources`.

Problem: branch-conflict recovery alone left uncommitted generated catalogs on the ordinary
line-merge path. Compact JSON blocked independent source edits; an unchanged catalog could
instead land successfully while becoming stale relative to dirty sources. Decision: the same
explicit generator registry also owns landing's input closure and complete output families.
It covers i18n catalogs/types, retained runtime bundles (including lazy bundles), and composed
styles (including generated CSS entries). Landing rebuilds and checks two distinct views:
the immutable merged sources determine the commit tree; those sources with the checkout's
three-way-merged uncommitted edits determine working files. Other writers' source/output edits
remain uncommitted. Neither a pending translation nor another writer's source becomes part of
the merge merely because its generated output shares a line with the task's output. Committed
i18n passes the generator's complete-translation check; working views preserve its existing
source-validated fallback for unfinished derived translations, without demanding that another
writer finish translation review to let an independent branch land.

Only declared input/output closures are materialized in temporary directories. Git blobs are
read in a batch. A dirty generated output must first reproduce from its own captured checkout
sources; unexplained output-only edits/deletions, unsafe file types and staged overlapping
changes refuse without modifying the checkout. Source conflicts remain ordinary conflicts.
The read fence covers every generator input, including dependencies absent from the branch's
diff, and the untracked membership of those input scopes. A concurrent change causes a fresh
plan before publication. `--check` performs the same rebuild, validation and staged/read checks,
without publishing files, index entries or refs. Generator failure leaves those unchanged.
Private branch-conflict rebuilds bridge installed dependencies from the receiving checkout;
the disposable scratch worktree is a source snapshot, not an installed Node environment.

Alternatives rejected: resetting generated files loses uncommitted work; committing the whole
checkout publishes another writer's unfinished changes; JSON key merging cannot rebuild type
declarations, hashes, removed keys or other output families; choosing one side drops the other
side's source changes; timed retries cannot resolve a deterministic derived-file collision.
No polling service, merge-driver setting or tuning parameter is added. The existing residual
write-after-revalidation race below remains; the input fence does not claim filesystem-wide
atomicity. Specifications: `tests/project_mod/test_git_land_generated_work.py`, the original
landing suite, and the task-delta generated-output test named above.

An existing `index.lock`
refuses with evidence: the processes holding it open (from `/proc`) mean Git is busy; none means
an exited Git process left it, and the refusal reports its age and Git's manual recovery step.
The tool never removes a lock itself, because Tofu's own index reservations close their
descriptor before the rename that releases them.

Alternatives rejected: `git merge --autostash` stashes every writer's work and can leave conflict
markers in the shared tree; mandatory per-task isolation was rejected with tool-call leases (see
the [guard decision](../runbooks/GIT_WORKSPACE_GUARD.md#tool-calls-take-no-workspace-leases-owner-decision-2026-10-28))
and would hide edits until task end; untracking generated bundles is a packaging change for a
conflict the three-way merge already resolves; a merge driver cannot see the merged sources.
Residual race: a write to the same file between revalidation and its atomic rename is lost, as with
`git merge`. Specification: `tests/project_mod/test_git_land_script.py`.
## Gate policy

Candidate gates validate the exact publication tree: `git diff --check`;
forbidden Git internals, dependency/cache/runtime/managed-worktree paths,
bytecode and `node_modules`; changed Python/JavaScript/module/JSON syntax;
and the selected project command for application/configuration changes.

Problem: process-global defaults borrowed Tofu's script for independent projects. Decision: `lib/git_checkpoint_policy.py` alone selects commands for candidate, canonical/task-delta delivery, HEAD reconciliation, stable/status and linear checkpoints. Direct `git config --local --no-includes` keys `tofu.integrationTestCommand`, `tofu.integrationStableTestCommand`, `tofu.linearCheckpointTestCommand` override defaults only for that Git common-dir; empty, duplicate, malformed config or failed identity/config reads refuse, never erase a gate. Ambient `GIT_*` routing/config injection is removed.
Environment defaults `TOFU_INTEGRATION_TEST_CMD`, `TOFU_INTEGRATION_STABLE_TEST_CMD`, `TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD` belong only to the application's source Git common-dir, including linked worktrees. Optional `TOFU_INTEGRATION_TEST_ROOT` explicitly pins that scope for operators/packaged use; a non-Git package has no global fallback. Stable/linear fall back to the selected integration command. Alternatives rejected: project-name/path-name guesses and filename skips transfer or weaken verification authority.
Selected commands run even for text-only changes, as direct argv without a shell. Relative scripts must exist in the immutable target snapshot; missing scripts fail, never run from live Tofu. Automatic delivery without a configured gate can rerun one host-recorded successful declared pytest/unittest/npm test/check/build/node-test/cargo-test invocation on the combined tree, using its runtime, rebased paths and original subdirectory. Arbitrary shell programs, credentials, failures, timeout/background results and another task/owner's evidence do not qualify. Semantic changes without verification refuse; syntax-only evidence is not success.

Selection rechecks after gate execution; policy mutation or a new read error refuses passing stale validation. External config writers are not atomically locked with ref publication; this is bounded freshness checking, not atomic operator revocation. Source roots require their own Git anchor; linked roots require their registered checkout backlink, never a copied `.git` pointer. Shared semantic checks include compiled-source/config suffixes and Dockerfile/Makefile/environment names, deletions and both rename endpoints.
Linear mode shares selection and forbidden-path/semantic-suffix policy, without a scratch
worktree. It syntax-checks the matching checkout, then runs the selected linear gate
outside its Git lock: `{base}` is stable, `{target}` the checkpoint. HEAD and
all non-ignored bytes must match before/after; writes or gate rewrites stay
dirty, invalidate promotion and never block tools through this checkpoint path.

## Cost bounds and operations

Capture-time Git spawn failures with EAGAIN, EINTR or ENOMEM preserve pending
delivery intent (`checkpoint_spawn_retry`) for this durable worker; their legacy
`blocked` receipts re-enter the same recovery on its next pass or at startup.
Deterministic capture errors remain blocked, with no silent recapture/publication.
See [the delivery contract](../CONTINUOUS_DELIVERY.md#conflict-recovery) and
`test_frozen_delivery_recovery.py`.

Idle polls back off through 3, 6, 12, 24, 48, then 60 seconds. Durable local
Idle polls back off through 3, 6, 12, 24, 48, then 60 seconds. Durable local
submit/retry/intent wakes the lifecycle-owned worker; API-only replicas cannot
become Git executors. Storage failures use a separate 5..30-second circuit delay.
Shutdown interrupts waits, joins the exact owner and retains a timed-out owner.
A delivery pass pages `integration.workspace.delivery_action_page` (64 complete rows, ID
cursor; backend cap 128 plus lookahead), which returns only rows whose receipt asks for
action: `working`, `pending`, `blocked`, input-changed `failed`, and delivered isolated rows
missing their checkpoint or source-retirement acknowledgement. No row is reread before
dispatch. A pass is due at worker start and after a worker-loop error (recovery passes,
which also recheck `retained` sources), when a command in `DELIVERY_STATE_OPERATIONS`
commits in this process (the worker's commit subscription wakes it), and on every tick
while the last pass returned rows, so held receipts keep their per-tick retry. Otherwise
a tick reads no delivery rows. Orphan `working` registrations use owner-scoped durable task verdicts; any live
carrier is left alone, replay outages retain retry debt and do not stop other owners.
Missing or incomplete durable success fails closed. Identical receipts are not rewritten. SQL/native checkout claims and backend limits live in [`STORAGE_TOFUDB.md`](../STORAGE_TOFUDB.md).

**Delivery resume demand (2026-11-02).** Problem: every worker tick paged through all
workspace rows and reread each one with `integration.workspace.get`, so an idle server's
delivery reads grew with delivery history, and every acknowledged retained row cost a
filesystem probe per tick. Decision: the authority selects rows needing action and returns
complete rows; the worker runs a pass only when one is due (above). Commit hints are
process-local, like automation claim wakes: an intent committed by another process waits
for this worker's next due pass. Alternatives considered: wake-only scheduling without
the per-tick retry, rejected because a held receipt whose cause clears without a commit
(background commands or child agents finishing, a cleaned or committed canonical checkout,
a cross-process lease, changed conflict inputs) would wait for an unrelated wake; a
larger page or slower cadence, rejected as a tuning knob whose cost still scales with
history; a persisted action index maintained by every receipt write, rejected as a schema
change on both authorities for a scan that now runs only on demand; client-side filtering
of the complete page, rejected because it keeps one read per row. Rows that still need
action keep their cost: the 2026-11-02 live profile attributed most of the loop's 581 GIL
samples to retried held automatic deliveries (task-delta claims and canonical tree
capture) and to publication checks of delivered rows awaiting checkpoint acknowledgement,
not to history reads. Measurement (`benchmarks/delivery_resume_reads.py`, SQLite Sidecar,
120 acknowledged rows, held receipts returned unchanged, minutes 2-3 after worker start):
idle 2,480 -> 1 integration read per minute, the remaining one being the integration
queue's `peek_ready`; with 10 pending receipts 2,720 -> 2 (one action page). Before, a
first page's `hasMore` counted as progress and reset the idle backoff, so history longer
than one page was reread every 3 seconds (20 passes and 2,400-2,600 row reads per minute);
now the cadence backs off to 60 seconds and only rows needing action are read. The first
minute, including boot recovery, went from 2,481 to 7 (idle) and from 2,721 to 11 (10
pending). Counts are load-independent; the run shared a host at load 90-160. Known limit:
more than 64 rows needing action still span pages, and `hasMore` still resets the backoff.
Specifications: `tests/project_mod/test_delivery_resume_demand.py` and the action-page
case in `tests/storage/test_storage_application_contract.py`.

Successful tasks reclaim source clones whose final bytes/index are pinned in recoverable checkpoints; uncaptured changes, conflicts, failures and durable artifacts retain recovery/undo workspaces. Python gets a lazy private prefix with readable source packages;
pip installs stay local and new packages retain their workspace. Node package links reuse source dependencies inside a private ignored directory.
Recorded bridge directories and Python/test caches are reconstructible; source dependencies are never deleted.
No new worker/queue is added. A private 1,000-file × 1 KiB measurement took 0.262s to create and 0.205s to settle/reclaim a no-change workspace, retaining zero clones; this is a fixture, not a latency cap.
Git objects/checkpoint refs and owner-scoped receipts retain evidence after clone reclamation. One origin acknowledgement records source retirement and recovered opt-in checkpointing; missing ACKs and transient contention use the existing worker. Acknowledged cold rows require no filesystem scans or heartbeat writes.

Git scans bound active/problem rows; terminal history uses stored metadata. Status is cached with mutation invalidation; event detail is bounded.
Unregistered/prunable worktree counts remain visible.

**Task checkout placement (2026-10-25).** Problem: managed task worktrees
live under the data root, so on a network/FUSE data volume every build, test
and search in a task pays network round trips (test collection measured
49.3 s on DolphinFS versus 8.2 s from a local home directory). Decision:
`_workspace_root()` asks the shared placement policy
(`serverctl_pkg/local_placement.py`, see
[infra_runtime.md](infra_runtime.md#worker-runtime-replicas-on-slow-volumes))
once per process for a private local root, namespaced per data root under
`$XDG_STATE_HOME/tofu/workspaces` (then `~/.local/state/tofu/workspaces`,
or `$TOFU_LOCAL_DIR/workspaces`). It is opt-in because a checkout holds
uncommitted task work: `TOFU_LOCAL_WORKSPACES=auto` moves only from a slow
volume onto a provably persistent local disk at least 3x faster; `on` also
accepts container overlays whose persistence cannot be proven; unset/`off`
keeps the data-root checkouts. Git objects, checkpoint refs and worktree
registration stay in the canonical repository, so only uncheckpointed
working-tree edits share the local disk's fate. Removal accepts every root
this control plane could have created, so a changed placement never strands
a checkout. `TOFU_INTEGRATION_WORKSPACE_DIR` still overrides both.
Alternatives rejected: moving checkouts automatically onto overlays (a
recreated container would take uncheckpointed work with it) and relocating
the data root (it would move durable state too).

Linear mode keeps one checkout/dependency tree/cache/watcher set. Its separate
Git-operation lock defaults to 30 seconds (cap 10 minutes); timeout returns
`deferred` without changing bytes or tool execution. This OS lock releases on
process death, covers alternate-index capture and atomic ref/index updates,
not model work or verification. One common-dir lock file follows repository
lifecycle; owner/task checkpoint refs reuse Git objects, not copied trees.

Authenticated owner-scoped `/api/v1/project/integration/` routes expose status,
create/register, reconcile-head, checkpoint, submit, retry, discard, promote and
prune. UI actions mirror the state table and separately confirm divergence.

### Repository hygiene

`python3 scripts/git_hygiene.py --repo <main checkout> [--also-merged-into REV]...`
is read-only by default (`GIT_OPTIONAL_LOCKS=0`, nothing written). It reports
registered worktrees (clean/dirty, prunable, locked, HEAD reachable from kept
refs), unregistered pool directories, local branches merged into HEAD, merged
into an `--also-merged-into` revision, patch-equivalent (bulk `git cherry` with
no `+`) or unmerged, and `refs/tofu/*` counts with oldest/newest dates; `--json`
prints every item. Merge evidence costs one `for-each-ref --merged` per revision;
patch-ids are computed only for commits whose changed-path set matches kept history.

`--apply` first writes `data/git-hygiene/<UTC>.json` and prints its
`--restore <manifest>` command, which recreates recorded refs/paths without
overwriting. It then deletes only (a) branches whose tip is an ancestor of HEAD
or of an `--also-merged-into` revision, through tip-guarded `update-ref` deletes
re-validated against the live HEAD and worktrees, never the current branch, one
checked out, rebased or bisected in any worktree, or one whose tip is such a
revision (its anchor; an unreachable revision is refused); (b) unregistered pool
directories holding only the FileHistory bootstrap marker and empty directories;
(c) with `--remove-clean-worktrees`, unlocked worktrees idle for 24 hours (HEAD,
reflog and directory mtimes; status scans refresh idle clones' indexes) that are
clean, keep HEAD reachable from kept refs and hold only cache/marker ignored
files, via `git worktree remove` without `--force` (which still deletes ignored
files, hence the check). A removed managed clone keeps its integration row and
refs; a later checkpoint request for it reports the workspace missing.
Patch-equivalent/unmerged branches, dirty worktrees,
durable ignored artifacts (FileHistory history, memories, real `.venv` trees) and
`refs/tofu/*` are report-only.

**Problem:** thousands of local branches and dozens of pool clones accumulated.
**Decision:** fix the producers, then reclaim with the guarded tool. Agent
guidance kept task and `backup/*` branches without a deletion step; it now
deletes merged task branches with `git branch -d` and records the merge's first
parent as the backup. Isolated tasks bind `projectPath` to their clone, so the
deferred round commit's `make_snapshot` recreated a reclaimed clone through
`ensure_store`'s `makedirs`; the store now never creates its project root.
Reclaim treated FileHistory's regenerated `README.txt` marker as a durable
artifact; it is now reconstructible while snapshots, the tracked index and
backups still retain the clone. **Alternatives rejected:** bulk-deleting pool
clones loses undo history and unreferenced HEADs; `--merged HEAD` alone misses
rewritten history; per-branch `git cherry` launches thousands of processes.
Clones retained for real undo history and rows already acknowledged as
`retained` remain until a retention policy decides otherwise.

Object-store maintenance is `python3 scripts/git_maintenance.py --repo <main
checkout> [--expire-days N] [--apply]`, report-only by default. Model and
harness Git never start automatic maintenance; the decision, the lease, what
the script keeps and its measured cost are in "Automatic Git maintenance never
runs inside commands" in
[`GIT_WORKSPACE_GUARD.md`](../runbooks/GIT_WORKSPACE_GUARD.md). Verify
`tests/project_mod/test_git_maintenance_script.py`.

## Invariants

- Isolation failure is fail-closed; no shared-tree fallback exists.
- Linear mode is opt-in and never controls project-tool admission; independent checkout leases serialize cooperating writers, not model execution.
- Checkpoints/submissions are immutable; candidate/stable updates are atomic and gated.
- Conflicts/gate failures preserve evidence and never consume another model turn automatically.
- Work-item `changedPaths` selects automatic Checkers; overlap advisories never
  become Integration ownership or durable Board state.
- Canonical HEAD and terminal outcome are never changed by clone cleanup; durable receipts remain.
- Linear commits never merge, stash, reset, checkout, clean, or push; stable promotion is a compare-and-swap ref move over a verified descendant whose
  canonical checkout stayed unchanged throughout verification.

## Change routing and tests

Run workspace-delivery, no-lease tool-call and workspace-lease tests, then integration control/delivery/revocation, background handoff, Git guard, spawn ownership and project-write atomicity/freshness. Worktree landing: `tests/project_mod/test_git_land_script.py`. Repository hygiene: `tests/project_mod/test_git_hygiene_script.py`, `tests/file_history/test_file_history_retired_root.py` and the reclaim cases in `tests/project_mod/test_workspace_completion.py`. Verify API/frontend Project Brain contracts for public projections; linear/export suites for ref policy.

Status scan demand — Problem: live workspace evidence was limited to eight rows and a four-thread pool, leaving older workspaces unscanned. Decision: every owner-selected live/problem workspace starts an independent scan through the shared demand owner; the undocumented TOFU_INTEGRATION_STATUS_SCAN_LIMIT control is retired. Terminal history remains metadata-only, durable ordering remains, and a genuinely unstarted worker falls back to the caller without duplicate work. Failed/missing scans retain explicit unknown evidence. Alternatives: larger windows still delay/omit evidence; scanning terminal history adds no value; guessing clean from metadata weakens freshness. Rendezvous and real thread-start fault tests verify the boundary. Measurement: a controlled 32-live-row rendezvous with idle hint 1 completed in 62.18 ms, retaining input order and zero surviving scan threads.
