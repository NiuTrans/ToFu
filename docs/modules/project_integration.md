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
- **Problem (2026-11-07):** on a network volume a snapshot's cost is the number of working-tree walks, each one `lstat` round trip per tracked file; production snapshots of the 12,875-file canonical checkout took 4–25 min with each walk at 20–140 s. A dirty source cost at least seven walks: every capture ran `diff` and `ls-files --others` for path validation plus `add -A`, and every confirmation added a `status` clean check before the next capture. **Decision:** `add -A` into the private index is a capture's only walk; forbidden-path validation then reads that index (`diff --cached --name-only --diff-filter=ACMRT` against the parent, or `ls-files` without one), which holds exactly the added, modified and newly untracked paths. Every observation is one capture: a clean source captures exactly the selected tree, so the separate `status`/`ls-files -v` clean path is gone and flagged entries stay covered by the unborrowed private index. Two agreeing observations and the churn fence are unchanged; snapshots now cost two walks, with the second only `lstat`-ing through the private index's stat cache. A refused capture leaves only unreferenced loose objects for ordinary Git GC; the writer index is still never written. **Alternatives rejected:** keeping the status clean path costs a walk to save a stat-only `add`; Git's untracked cache serves only status-shaped walks and needs repository configuration; validating before staging requires the two extra walks this removes. Paired measurement on the live dirty checkout (four alternating process runs, including identical process start-up): median 14.7 s → 8.8 s. **Specification:** `test_snapshot_walks_the_working_tree_once_per_observation` and `test_capture_refuses_forbidden_paths_without_touching_the_writer_index` in `test_integration_snapshot_index.py`; clean-source reuse, dirty, flagged, ignore-rule, concurrent-commit and publication parity remain in `test_integration_delivery.py` and the same file.

- **Problem:** plain `read-tree` discards stat data from the reused private index, forcing unchanged tracked content to be hashed again. **Decision:** seed with index-only single-tree merge (`read-tree -m -i`) so matching entries retain stat data while membership still follows the fresh parent. Index-only mode avoids rejecting legitimate concurrent working-tree edits as an unsafe checkout. Git still checks changed/racy entries; two equal observations and the existing churn fence remain mandatory. **Alternatives rejected:** keeping stale membership loses ignore/HEAD changes; skipping the second observation weakens input consistency; ordinary merge mode refuses concurrent dirty changes. Real-Git parity tests cover changes, deletion, ignore rules, concurrent commits, modes and symlinks in `tests/storage/test_integration_snapshot_index.py`. A controlled same-volume 150-file/150 MB benchmark of two full captures (including preflight and tree writes) reduced median time from 1.208 s to 0.716 s, about 41%; this does not predict live end-to-end latency. Startup profiling uses paired `[TaskStartupTiming]` start/complete anchors and bounded `tofu_task_startup_stage_seconds` labels. Workspace provisioning, root discovery/identity/binding, snapshot, private-index seed/add/tree, snapshot commit, materialization, lock waits and registration/persistence are distinct stages. Nested spans inherit only allowlisted log identity via a context-local scope that resets on success or failure; IDs and file contents never become metric labels. Durations are inclusive: do not sum parent workspace binding with its child stages. A start without completion identifies the blocked boundary; tests cover nested identity restoration and separate-thread isolation.
- **Problem:** isolated admission refused healthy work when the input snapshot was slow (2026-10-26: `[Spawn] Executor rejected task a1c0feca before worker entry: git add failed: ... timed out after 90.0 seconds`). Dispatched file grants (and `isolated` mode) snapshot the canonical checkout, HEAD plus every uncommitted and untracked non-ignored file, through a private index (`GIT_INDEX_FILE`) because Git cannot commit working bytes without an index. The first observation started from an empty private index without stat data, so `add -A` re-read and hashed every tracked file: 11,460 files / 229.8 MB on the shared BeeGFS FUSE checkout. In the 2026-10-26 log the first `add` of each of 13 admissions took 10.4-21.9 s (one 90.2 s timeout); later observations, which reuse stat data, took 1.3-4.2 s at admission; task-end delivery captures, which also began from an empty index, took up to 39.8 s and share the fix. Untracked trees were not the cost: `data/` (task worktrees), `logs/` and dependency trees are ignored and the untracked walk takes about 1.2 s. While the volume was contended (validation 11.4 s instead of about 2.5 s) the rehash passed the fixed 90 s wall-clock deadline (30 s for the other snapshot commands) and binding failed before worker entry. The snapshot never staged anything in the writer index, but its porcelain `git diff` validation rewrote that index (stat refresh under `index.lock`) whenever it met stat-only changes. **Decision:** a first observation copies the writer index through one descriptor, keeping that version's mtime for Git's racy-entry check, before the single-tree `read-tree -m -i`, so `add -A` hashes only changed files; an index with assume-unchanged or skip-worktree entries is not borrowed, because those bits survive the reseed and would hide working bytes. The conflict check reads the writer index with `ls-files`, which never writes it; path validation now follows seeding and runs on the private index. Whole-tree snapshot commands (`ls-files`, `read-tree`, `diff`, `add`, `write-tree`) have no total deadline: `lib/project_mod/activity_supervised_process.py` renews a 90 s stall window (`GIT_STALL_SECONDS` in `lib/git_runtime.py`, a `fixed`-class constant, never derived from hardware or tree size) whenever any thread of the Git process tree executed, read from kernel scheduler counters (per-thread faults, CPU time and context switches in procfs; psutil elsewhere). A tree blocked on an unresponsive volume shows no change, is retired through `process_group` and fails admission with "made no progress"; owner Stop is observed while it waits. Ref reads that admission depends on share the same window (next decision); ref and object writes (`update-ref`, `commit-tree`) keep their fixed deadlines. Read-only phase benchmark on the live checkout (158 changed and 53 untracked files, `add --dry-run`, four alternating rounds): first observation 10.2-15.9 s became 2.5-2.7 s, its `add` 8.9-14.8 s became 1.2-1.4 s; this is a phase measurement, not an end-to-end latency claim. **Alternatives rejected:** a larger or tree-scaled deadline still refuses slow healthy work; no deadline lets a hung mount hold the worker forever; output-renewed windows (worktree materialization) need progress lines that `add`, `diff` and `ls-files` never print; the untracked cache serves only `git status`-shaped walks and needs repository configuration; scoping `add` to `diff-index` output re-implements `add -A` membership rules (deletions, modes, embedded repositories, ignore changes) for the saving the borrowed stat cache already gives; deferring the snapshot past worker entry leaves the first rounds without their bound root and still needs liveness. A command that keeps executing without progress (a busy loop) is not detected; Stop still retires it. Specifications: `tests/storage/test_integration_snapshot_liveness.py` (fake slow and hung Git under dilated deadlines, Stop), `tests/storage/test_integration_snapshot_index.py` (only changed files are hashed; the writer index stays byte- and mtime-identical) and `tests/project_mod/test_activity_supervised_process.py`.
- **Problem:** task start failed whenever the shared checkout volume paused a constant-size Git read past a fixed deadline, before any model call (2026-11-03: `task_start_failed` "Command [... 'rev-parse', '--show-toplevel', '--git-common-dir'] timed out after 10 seconds"; `logs/error.log` holds 155 such timeouts for about two dozen tasks between 15:24 and 19:44, logged by task-end workspace delivery, including for tasks whose start had failed the same way, and none in the retained logs back to 2026-10-31; also the gate's 30 s `rev-parse --verify <ref>^{tree}`). These reads normally take about 0.1 s (live samples of 120 and 240 reads: median 0.11-0.13 s, maximum 3.9 s), so the failures are intermittent volume stalls, not slow work: a stalled `rev-parse` waits on the volume exactly like a whole-tree `add`. Starting the child outside the checkout does not avoid them: sampled at the same instants, `-C <checkout>` and `--git-dir=<metadata>` reads had the same latency (median 0.11 s, p99 1.1-1.3 s, every slow instant slow in both). **Decision:** every harness Git read that task admission or workspace identity depends on runs under `run_activity_supervised` with the shared `GIT_STALL_SECONDS` window and no total deadline: `workspace_delivery._checkout`/`_has_commit` (through `_checkout_git`), `workspace_policy.resolve_workspace_mode`, and integration `_repo_root`, `_common_dir`, `_rev`, the admission `symbolic-ref`, the index `--git-path` read and the gate's tree read. Worker binding passes the task's Stop (`aborted`/`abort_event`) as the cancellation, so Stop retires a blocked read and surfaces as `AbortedError`, never as a binding failure; only a whole window in which no Git thread ran fails, with "made no progress". Per-call cost is unchanged (local repository, 60 alternating `--git-dir` identity reads per arm: median 3.9 ms fixed-deadline vs 4.1 ms supervised). **Alternatives rejected:** a larger fixed deadline still fails a healthy start on a longer pause; retrying replays a command whose stall says nothing about the next attempt and still needs a bound; caching identity would let a moved or deleted checkout keep its old authority. Specifications: `tests/tasks_pkg/test_workspace_identity_liveness.py` (slow, hung and Stopped identity/policy reads under dilated windows), `tests/tasks_pkg/test_workspace_identity_latency.py` and `tests/storage/test_integration_snapshot_liveness.py`.
- **Problem:** worker binding still failed on the contended volume after that decision. On 2026-11-06, lane 3c2ffb4b failed with `git worktree failed: Command [... 'worktree', 'list', '--porcelain'] timed out after 30.0 seconds` after 1,697 s of workspace preparation, and lane c7081533 failed with `Git worktree materialization stalled for 90s (files 0/unknown)` after 1,803 s, with no tool call. The 30 s is the wall-clock default of `integration_control._git`, which no contract defines. Its binding identity reads had been missed by the inventory above: `delivery._canonical` and `bind_task_workspace` (rev-parse, `worktree list`, merge-base, symbolic-ref), `_worktree_inventory` and the worktree listing in `task_delta_delivery`. The 90 s materialization window renewed only on file-count progress lines, which `worktree add --no-checkout` never prints and a checkout prints only after it has read its tree. **Decision:** these reads run under the shared `GIT_STALL_SECONDS` window with no total deadline. Binding passes the task's Stop, so a Stop-retired read surfaces as `AbortedError`. `run_worktree_add` defaults to the same window and also renews it whenever a thread of the Git process tree executed (`process_tree_activity`). Only a tree that neither ran nor completed a file for a whole window fails, and the message names the unresponsive volume. Stdin-fed writes keep their explicit deadlines. **Alternatives rejected:** a larger wall-clock default still fails a longer pause. Serializing checkouts per repository is the alternative the next decision rejected: it throttles independent tasks, and a saturated volume serves the same bytes either way. Re-queueing a failed start replays work whose stall says nothing about the next attempt. Specification: `tests/tasks_pkg/test_workspace_start_liveness.py` (slow and Stopped binding reads, a busy Git without progress lines, and a Git that never runs, under dilated windows).
- **Problem:** the same evening, a start failed on storage, not Git. Lane 746148a3 reported "the storage service was temporarily unavailable during startup (database_timeout): deadline_elapsed" after 1,507 s of preparation. Its workspace registration ran under the integration state repository's fixed 5 s deadline while the authority's queue waits were 25-28 s. Nothing on the start path waited out the refusal, and the rolled-back admission left a retained checkout. **Decision:** integration-state commands and queries take `storage_write_deadline` (the observed service time, with the old 5 s as its floor). The start path's state lookups, registration, binding and runtime persist run under `wait_for_persistence`, which retains the exact operation until ACK, owner Stop or shutdown. Integration commands carry a payload-derived command ID, so a replay after an uncertain ACK returns the recorded outcome. Terminal and refresh bindings keep their existing failure handling. **Alternatives rejected:** a larger fixed deadline is the per-writer constant `lib/storage/deadline_policy.py` already rejects; re-queueing the whole start repeats Git preparation for a storage pause. Specification: `tests/tasks_pkg/test_workspace_start_storage_liveness.py`.
- **Problem:** task-end delivery resolves the same identity through `delivery._canonical` (`rev-parse` plus `worktree list --porcelain`, which reads every registered worktree), and those reads still ran under `_git`'s default 30 s deadline. On 2026-11-06, under host load and about 256 registered worktrees, 57 such listings timed out; completed Workers' terminal intent stayed unacknowledged (131 retries logged), so no checkpoint was registered and their Coordinator could not review them. **Decision:** the delivery identity reads (`_canonical`, `bind_task_workspace`'s `merge-base`/`symbolic-ref`), the no-delta and retired-placeholder checks, `_worktree_inventory` and the reviewed-delivery commit's reads use the same stall window. **Alternatives rejected:** a larger deadline fails on a longer pause; deriving the main checkout from the common directory instead of listing worktrees changes identity for non-standard layouts. Specification: `test_identity_reads_outlast_a_wall_clock_deadline`.
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
  require the project gate. A passing snapshot moves only the index entries of
  its changed paths and atomically CAS-advances the branch together with
  `refs/tofu/stable`.
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
- **Linear checkpoints publish in a busy checkout.** Problem: publication
  required the shared checkout to stay byte-identical to the snapshot from
  capture through verification and promotion, re-verified the whole lag between
  `refs/tofu/stable` and HEAD, ran the gate inside the live checkout (where its
  own byproducts and every concurrent edit invalidated it) and capped whole-tree
  `git add` at 120 s. In a checkout many conversations edit it never published:
  on 2026-11-07 stable had not moved since 2026-11-03 (732 commits and 1,510
  files behind HEAD) and 367 settled paths sat uncommitted. Decision: the gate
  verifies the checkpoint's own delta from the commit it was captured on, on a
  private checkout of exactly the captured bytes
  (`landing_verification.verification_checkout`). A failing configured gate is
  attributed with landing verification's selector, so only tests that pass on
  that base and fail reproducibly on the checkpoint refuse it; a gate that
  rewrites tracked files in its checkout refuses as `workspace_changed`.
  Writers keep editing: publication moves only the index entries of the paths
  it changes and the refs by compare-and-swap, never a checkout file, so bytes
  written after capture stay dirty for the next checkpoint. A branch that moved
  after verification is carried when the moved commits touch none of the
  checkpoint's paths or already hold its bytes; otherwise the checkpoint defers
  to the next capture. Whole-tree Git commands run under the stall window, not
  a deadline. A whole-workspace capture that fails is retried once with only
  the task's own paths (the integration radar's record for its conversation,
  plus a delivered task's delta), generated outputs rebuilt from that tree's
  own sources as `git_land` does, so one writer's unfinished change never keeps
  another task's work out of history. Commits already on the branch arrived
  with their own verification (a landing, a reviewed delivery, an earlier
  checkpoint) and are not verified again. Alternatives rejected: requiring a
  quiet checkout (concurrent writers never stop); re-verifying the stable lag
  (unbounded; the measured failure); running the gate in the live checkout;
  capturing only attributed paths (shell writes the radar cannot see would
  never commit; the whole capture still sweeps them when the tree is healthy).
  Specifications: concurrent writes during capture and verification, captured
  delta versus stable lag, carried and deferred branch moves, staged entries,
  gate attribution and the own-paths retry with generated outputs in
  `test_linear_git_checkpoint.py`.
- Confirmed isolated working-tree delivery enters this same opt-in checkpoint through the commit round or recovery worker; pending delivery stays excluded.
- Failed/aborted tasks retain WIP refs, never advance branch/stable. Bytes that
  never published stay in the workspace, so a later capture verifies them again.
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
  Every snapshot export prints its source ref, SHA and, when that commit is
  not HEAD, its commit lag behind HEAD with the override; a trailing stable
  otherwise reads as a fresh export (`tests/export/test_export_head_snapshot.py`).
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
  ordinary activation runs. An owned stable may trail HEAD; publication moves
  branch and stable together. A stable or baseline that diverged but still shares history fails
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

Submitted checkpoints are immutable while ready/integrating; terminal rows cannot be registered/resurrected. Discard preserves commits/refs for recovery, never erases user work: a discarded checkout on a local volume is removed only after its work in progress is saved and Git holds everything in it ("Task checkouts stay on the data volume unless the operator opts in" below).

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

**Repository admission does not span tests.** Problem: the integration worker,
canonical-HEAD reconciliation and stable promotion held the repository-wide
admission lock while their complete test command ran, so unrelated workspace
registration and Git lifecycle work waited behind every gate. Decision: prepare
an immutable target and pin its gate policy under the short repository lock,
release that lock for gate execution, then reacquire it for the unchanged-policy
check and ref publication. Candidate integration and HEAD reconciliation keep
their compare-and-swap fence: a moved candidate requeues the integration for a
new merge/verification or refuses reconciliation for an explicit retry. Stable
promotion publishes the exact tested candidate only if stable is unchanged and
the canonical-HEAD divergence acknowledgement still holds at publication.
Durable acceptance evidence is written only after final admission revalidates
the integrating row, exact checkpoint and current delivery receipt. A retry that
changed that receipt requeues rather than receiving stale evidence or a stale
terminal verdict. Quarantine transitions retain their compare-and-set fence.
Alternatives rejected: a new testing scheduler
would preserve the lock convoy; publishing before testing or accumulating an
unverified batch would make the serving candidate regress; shortening the gate
deadline would reject healthy tests rather than remove the serialization.
Specifications: `test_all_publication_gates_run_outside_repository_admission`,
`test_workspace_registration_proceeds_while_integration_gate_runs`, and the
existing candidate-CAS and acceptance suites.

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

Terminal intent precedes evidence release. Failed/cancelled/stopped outcomes revoke publication
for the attempt that recorded them; checkpoint/undo evidence remains. Background commands and
child agents fence capture.

**Restart is not a task outcome.** Problem: a server restart quiesces running tasks
(`_abort_reason='server_shutdown'`) and restart recovery marks unsettled task results
`interrupted`; delivery read both as `cancelled`/`failed`. Both storage authorities make such a
denial irreversible for the workspace generation, and a resumed project Worker keeps its
deterministic workspace task identity, so the resumed attempt's completion was preserved, never
published (2026-11-06: the Router, hygiene and TB2.1 lanes). Decision: `workspace_outcome`
(`lib/tasks_pkg/workspace_delivery.py`) reads the abort cause through the canonical verdict;
`SYSTEM_RECOVERY` evidence, or restart recovery's durable `interrupted` status, is `interrupted`.
That receipt is not a denial: it stays `working` and is never captured, preserved or published;
the receiver skips it and the next attempt of the task settles it. `build_result_meta` persists
`_abort_reason`, so boot recovery replays read the same cause. User Stop and failures stay
sticky, and Project Brain work statuses keep `task_outcome`. Alternatives rejected: letting a
later completion lift a recorded denial, which weakens the storage authorities' irreversible
revocation; skipping settlement at shutdown, which loses the intent restart recovery needs.
A failed or stopped attempt's denial binds only that attempt (next paragraph). Specifications:
`test_restart_interrupted_delivery.py`, `test_terminal_settlement_reads_the_abort_cause`, and the
startup cases in `test_workspace_delivery_events.py`.

**Delivery follows the attempt that completes.** Problem: every attempt of a project Worker's
assignment works under one work id, so one receipt outlives its attempts. A start binding kept
whatever receipt an earlier attempt left, settlement returned early for `preserved`, and both
storage authorities refused any change to a recorded failed/cancelled/stopped outcome of the
work id. A Worker whose attempt was storage-fenced or failed, then resumed in place (the
Coordinator's `checkpoint_resume` or Continue, the owner's Continue, a restart's boot
Continue), completed with nothing captured: the receipt still showed the earlier attempt's
`preserved` checkout (a mid-turn `integration_checkpoint` stayed hidden behind its stale
`checkpointSha`), and the Coordinator's accept was recorded while nothing published, because
the receiver never acts on a preserved receipt (2026-11-08). Decision: a receipt records one
executor generation, `delivery.executorTaskId`, the physical attempt whose outcome it is. An
attempt's start binding (`bind_task_workspace(executor_task_id=)`) stamps it and, under the
repository lock, reopens a receipt another attempt left (preserved, held, failed or
restart-paused; never `delivered`) as a fresh `working` receipt with nothing captured. That
attempt's own settlement captures its checkout and holds it for review exactly like a first
attempt. A settlement naming another attempt than the receipt's generation is stale and
changes nothing; working recovery settles the generation's own verdict; the receiver never
captures or rewrites a `working` receipt. Both storage authorities enforce the generation:
within it a denial stays final, and a write naming another generation is dropped unless it is
the fresh `working` receipt that opens the next one (SQL `_delivery_patch`, native
`fence_delivery_patch`). `review_project_work` and `publish` refuse, before recording
anything, an accept its delivery cannot publish (`PublicationRefused` names the receipt's state
and reason): the receipt must record the execution's completed attempt, in a state the
receiver acts on; the pin itself must be the registered checkpoint, as before. Reviews
recorded against a preserved delivery before this change stay immutable history and never
publish: that delivery never settled the completed attempt, and its pinned checkpoint is not
that attempt's capture. A completed Worker
whose receipt still records an earlier attempt is refused at accept; a follow-up or a regenerate
of its Turn (`control_conversation`) starts an attempt in the same checkout that reopens the
delivery, or a carry assignment takes the work. Alternatives rejected: letting a completion lift a recorded
denial (a late writer could publish a stopped attempt's work; a generation keeps every
attempt's denial irreversible); a key per attempt (moves checkout, refs and ledger on every
attempt, see [Project coordination](../PROJECT_COORDINATION.md)); a startup pass over preserved
receipts (a new storage filter and a scan of every preserved row to repair a few pre-change
rows); settling pre-change receipts inside the accept (an earlier pin would publish before the
new accept is judged). Specifications: `test_restart_interrupted_delivery.py`,
`tests/storage/test_integration_revocation.py`, the native `integration_workspace` unit test,
`test_integration_delivery_denial_binds_its_executor_generation` in the TofuDB differential suite
and, through the real start path, `tests/conversation/test_worker_attempt_real_start.py`.

Successful automatic settlement immediately merges baseline-to-checkpoint changes into the
current canonical workspace, including borrowed dirty inputs and compatible concurrent edits.
A private index computes the merge; no validation checkout or release gate delays delivery.
Final admission rechecks registered authority and task-delta paths. Whole-patch application
preserves real index/HEAD, unrelated WIP and branch history; conflicts block without partial
publication. Unborn branches need no bootstrap commit. Durable recovery refs and target-byte
comparison reconcile lost ACKs without replay. Candidate/stable refs remain unchanged;
[delivery contract](../CONTINUOUS_DELIVERY.md) defines receipts, retry and commit independence.

**Reviewed project deliveries become commits.** Problem: working-tree delivery leaves a
task's bytes uncommitted, and turning them into history was left to the opt-in linear
checkpoint, which then captured the whole shared checkout (every writer's uncommitted
bytes), verified the entire stable-to-checkpoint delta and refused whenever the checkout
changed during capture (see "Linear checkpoints publish in a busy checkout" in
[Ref topology](#ref-topology)). In a checkout many conversations edit, that never succeeded: on 2026-11-07
`refs/tofu/stable` had not moved since 2026-11-03 (732 commits behind HEAD), none of 67
delivered Worker results had fully reached the branch, and 155 of the checkout's 175 dirty
source files were byte-identical to delivered checkpoints. Owners saw nothing landed, later
Workers borrowed those bytes as input, and landings that touched them conflicted.
Decision: in a repository that opted into automatic commits (`tofu.linearCheckpoint`), a
project-dispatch result whose Coordinator review authorizes publication lands in its
delivery pass as one commit of exactly its registered baseline-to-checkpoint delta (the
repair pair for a repaired delivery), never as working-tree bytes first
(`task_delta_delivery._commit_reviewed_delta`). The delta merges onto committed HEAD in a
private index. The shared checkout-landing owner (`lib/project_mod/checkout_landing.py`,
the planning and publication half of `git_land`) plans every changed path against the
checkout's current bytes, carrying uncommitted edits forward by three-way merge, and,
holding Git's `index.lock`, rechecks the plan, writes the files, moves exactly those index
entries and then the branch by compare-and-swap. The receipt says `delivered`
(`publicationMode=committed`, `targetSha` the commit) only after the commit is verified
reachable from the branch; a commit the branch no longer reaches leaves it `pending` and
the next pass lands it again. A delta that needs other writers' uncommitted bytes (it
built on borrowed input) is held `blocked` as `task_delta_entangled: <paths>`, one that
conflicts with committed history as `task_delta_conflict`, and one whose landing would
overwrite overlapping uncommitted lines, staged entries or an operation in progress as
`task_delta_entangled: <refusal>`. Each hold carries `conflict_inputs_changed` retry
evidence that fingerprints the branch tip plus the checkout's bytes and index entries at
the delta's paths; every pass rechecks it without merging again, writes nothing, and lands
the delivery once an input changes. A busy index holds `workspace_busy` and retries. The
commit names the Worker's own commits and carries `Tofu-Task`, `Tofu-Checkpoint` and
`Tofu-Assignment` trailers. The Coordinator's review is this commit's verification, as an
agent's own verification is for `git_land`; other isolated tasks keep working-tree delivery
and the whole-workspace checkpoint. Rows delivered as working-tree bytes before this rule
keep the acknowledgement pass (`commit_delivered_task_delta`, `origin.deliveryCheckpoint`);
its `entangled` acknowledgement is terminal and reports the delivery uncommitted.
Alternatives rejected: committing the merged working-tree bytes (commits other writers' and
borrowed edits); gating each delivery on the whole stable-to-checkpoint delta (the measured
failure); applying to the working tree first and committing in a later pass (an `entangled`
acknowledgement left accepted work as uncommitted bytes: 26eaddac72 and 43cf9c1765 on
2026-11-07); committing without the opt-in (automatic commits are never a surprise default).
Specifications: `tests/project_mod/test_delivery_integrity.py`,
`tests/project_mod/test_reviewed_delivery_commit.py`.

**Delivered means committed.** Problem: `canonicalDelivery` read only `state=delivered`, so a
working-tree publication whose commit was refused, skipped or covered only by a
whole-workspace checkpoint was reported canonical. Decision: `delivery.canonical_delivery`
requires commit evidence: a committed or no-change receipt, or a working-tree publication
whose `deliveryCheckpoint` acknowledges its own commit (`committed` with `commitSha`,
`no_changes`) or a repository without the opt-in (`disabled`: its working tree is the
publication). `publication.delivery_view`, the receipt projection every project-dispatch
consumer reads, shows any other working-tree publication as `pending` (commit not yet
attempted) or `blocked` with a `delivered_uncommitted:` reason. Dependency readiness reads
that projection, so the wake at a working-tree publication's `delivered` ACK finds its
dependents still waiting: recording the acknowledgement wakes them again (without it, a
repository without the opt-in left them for startup recovery). Waking only after the
acknowledgement is rejected: committed and no-change deliveries, canonical at their ACK,
never take that pass. Specification:
`tests/project_mod/test_project_dispatch_wake.py::test_real_git_delivery_wakes_other_lane_only_after_receipt_ack`.
Alternatives rejected:
probing ancestry on every view (Git per row on a read path; receipts verify reachability
right after the compare-and-swap); counting a linear-checkpoint acknowledgement (it commits a
capture of the whole checkout, not the delta). Specification of the wake:
`tests/project_mod/test_project_dispatch_wake.py`.

**No-change deliveries name HEAD.** Problem: an empty delta recorded a commit of a
live-checkout capture as its target (e83402d125 on 2026-11-07 held 12 of other writers' dirty
paths). Decision: an empty delta, like a checkpoint HEAD already contains, settles
`delivered`/`no_changes`/`publicationMode=committed` with `targetSha=headSha=HEAD` and captures
nothing. Alternative rejected: keeping the capture as evidence (no receipt may name foreign
bytes).

**Borrowed dirty inputs.** Problem: Worker input snapshots borrow other writers' uncommitted
edits (340-370 paths on 2026-11-07); a delta built on them cannot be committed alone, which
is what entangled deliveries. Decision: borrowing stays. The delivered delta is always
baseline-to-checkpoint, so a borrowed path the Worker did not change is never in it; in
commit mode a delta that changes a borrowed path is held `task_delta_entangled` until the
borrowed bytes are committed, never committed with them. Alternatives rejected: committing
borrowed bytes with the delta (no authority over another writer's unpublished work);
dropping borrowing everywhere (where the working tree is the publication, borrowed context
is what the user sees and edits); leaving entangled deltas in the working tree (the defect).

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
then runs `git worktree remove` (never forced) and `git branch -d` only while
the source branch's current tip is contained in the published result.

**Problem:** a source branch could gain commits during a landing, and cleanup
removed its clean checkout before branch deletion noticed those unlanded commits.
**Decision:** check source ancestry before any checkout retirement; preserve both
checkout and branch when commits remain, then land that branch again.
**Alternative rejected:** `git branch -d` alone protects the ref after the checkout
has already gone. Specifications cover ordinary worktrees and managed lanes in
`tests/project_mod/test_git_land_script.py`.

**Landing runs no tests (owner decision 2026-11-08).** **Problem:** every landing, a
fast-forward included, ran the test files the merged result could affect and refused a
reproduced regression, so work the lane had already verified waited for a second test
ladder: the first measured impact-based landing spent 167 s verifying (2026-11-07), and a
one-line change to `lib/tasks_pkg/manager/_stream.py`, which most of the suite imports,
selected 4,546 test files, none with a recorded run (2026-11-08). The owner is
prioritizing delivery pace. **Decision:** `git_land` publishes a cleanly merged result
without selecting or running tests; the lane's own verification before landing is the
evidence, and voluntary runs (`make test`, `landing_verification record`) remain
available. Conflict refusal, uncommitted-work carry-forward, generated-output rebuilding,
task-input snapshot refusal, the protected step and lane cleanup are unchanged;
merged-result verification still serves the CI candidate check and linear-checkpoint
attribution ([Merge verification policy](#merge-verification-policy)). A fast-forward no
longer enters the private merge checkout. `--check` merges, rebuilds generated outputs
and validates the publication plan exactly as a landing does, then reports the paths that
would change and the carried edits; it runs no test and writes nothing. Accepted risk:
semantic conflicts between concurrently landed branches (the 2026-11-07 measurement under
the merge verification policy) are no longer caught at landing. **Alternatives
rejected:** a bypass flag or environment switch (no new knob; under pressure it becomes
the default path anyway); keeping selection to print a test plan (it costs time and
implies a gate that no longer exists). Reverting the commit that records this decision
restores the gate. Specifications: the landing and command-line cases in
`tests/project_mod/test_landing_verification.py`.

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
descriptor before the rename that releases them. A lock that disappears or changes while its
holders are inspected was busy, not stale: the landing plans again at once instead of refusing.
Publication holds its own `index.lock`, descriptor open, from revalidation through the ref
compare-and-swap and installs the index by renaming a prepared copy (index mode kept) over it:
releasing the lock between index and ref let a second lander revalidate against the new index
and the old tip, so one of the two compare-and-swaps failed with its merge staged but
uncommitted. Specifications: `test_index_lock_is_held_until_the_branch_moves` and
`test_lock_released_during_inspection_replans_instead_of_refusing`.

Alternatives rejected: `git merge --autostash` stashes every writer's work and can leave conflict
markers in the shared tree; mandatory per-task isolation was rejected with tool-call leases (see
the [guard decision](../runbooks/GIT_WORKSPACE_GUARD.md#tool-calls-take-no-workspace-leases-owner-decision-2026-10-28))
and would hide edits until task end; untracking generated bundles is a packaging change for a
conflict the three-way merge already resolves; a merge driver cannot see the merged sources.
Residual race: a write to the same file between revalidation and its atomic rename is lost, as with
`git merge`. Specification: `tests/project_mod/test_git_land_script.py`.

### Landing verification

`git_land` no longer runs this verification (owner decision 2026-11-08, under
[Landing worktree branches](#landing-worktree-branches)); the CI candidate check,
`record` and linear-checkpoint attribution use it. The explicit-scope working-tree
entry point is described under
[Incremental verification](#incremental-verification).

**Completed execution evidence.** A collected case becomes executed only after
its teardown report. Empty collection, skips and expected failures never create
a reusable passing file record; interruption before teardown leaves the file
unrecorded. Problem: protocol entry previously counted as execution, allowing
`pytest.exit`, skip and xfail to manufacture passing evidence. Decision: require
completed lifecycle receipts and reject skipped files. The environment key also
binds the recorder source, so old observations cannot survive changed observation
semantics; Git-blob fingerprints retain their existing format. Alternatives:
trusting exit status alone misses skipped assertions; reusing old records would
retain the defect. Verify `tests/project_mod/test_impact_completion.py` and the
impact-selection and landing-recheck suites.

**Failure identities.** Problem: background output can splice into pytest's
console summary, turning a real failed case into a nonexistent selection and
making confirmation unavailable. Decision: failure receipts supply exact case
identities; console-only failures remain usable when their identity belongs to
the observed collection, or when no identities were recorded. This preserves
worker-crash evidence without treating appended logs as test names. Alternatives
rejected: parsing timestamps assumes a logger format; relying only on the console
also loses failures when its summary is missing. The landing-recheck suite
injects both summary corruption and loss around a real failing subprocess.

**Concurrent environments.** Problem: one mutable environment identity made
different verification contexts erase each other's completed runs; even opening
the cache could discard another conversation's evidence. Decision: v6 keys the
latest record by `(environment, test)`. Reads, writes and partial-run eviction
all use the handle's exact interpreter/recorder/environment identity under the
cache lock. Returning to an unchanged environment reuses its evidence; another
environment's failure neither supplies nor deletes that evidence. Fingerprints
remain content-addressed and shared across environments.

The first v6 open imports a locked v5 snapshot once, preserving its original
environment, fingerprints and recorded timestamps in one transaction. The v5
database stays untouched so running older processes cannot delete v6 evidence.
A committed migration marker prevents later opens from resurrecting evicted
records. Corruption remains a typed cache error and falls back to verification,
never a passing certificate. Alternatives rejected: one current environment
causes repeated validation; reusing the v5 records table cannot protect against
older writers; per-session databases defeat reuse between conversations.

Retention is the latest record for each distinct environment/test pair, plus
shared blob fingerprints; there is no per-session copy or new retention knob.
Disk use grows with distinct environments and tested files. Compatibility keeps
one v5 snapshot on disk; do not remove it while older verification processes may
still be using it. A private measurement on 2026-11-07 seeded 1,000 v5 records
and visited environments A/B/A/C/B/A, each with 1,000 records and 12 dependencies
per record. Across three alternating pairs, retained bytes (including both
versions and locks) rose from 647,168 to 2,605,056. Available matching records
rose from 1,000 to 4,000 of 6,000 reads, with identical record digests. This
measures avoided missing evidence, not test executions: database-only median
time was 0.835 s before and 1.244 s after, including the one-time migration and
decoding the extra hits. The end-to-end A/pass, B/fail, A/pass specification
executes only the first two runs and reuses A's last result without consuming
B's failure. Verify `tests/storage_authority_sql/test_impact_cache.py` and
`tests/project_mod/test_verification_engine.py`.

**Warm bytecode correctness.** Problem: timestamp bytecode accepts equal file
size and integer-second mtime, so a warm checkout can execute a previous Git
version after a same-size edit. Decision: invalidate bytecode only for Python
sources whose Git content changes, before checkout replacement and before a
landing's merge rewrites sources in the warm checkout. Unchanged bytecode stays
warm. Alternatives rejected: retaining timestamp-only validation can produce
false passes; deleting all caches loses warm collection performance.
Same-size/same-timestamp fault injection checks checkout reuse in
`test_landing_verification.py`.

### Incremental verification

**Default development entrypoint.** `make test` verifies affected offline
Python tests against the current working-tree snapshot; `make test-plan` explains
the selection without executing it. `TESTS="tests/owner/test_case.py ..."` selects
an explicit scope instead. `make test-affected` is an alias of the same default.
The CLI spells changed selection `scripts/verify.py run --changed`; `--base REF`
compares the snapshot (including uncommitted bytes) with the merge base of REF
and its captured HEAD. Deleted sources and new tests participate in the diff.
Static selection and mandatory guards come from the existing selector; changed
recorded dependencies add tests that static imports alone cannot discover.
The result names its comparison base, changed paths and selection reasons.
No changes yields `unchanged`, not a claimed test pass. An empty affected scope
yields `unselected`; both are explicit advisory outcomes.

Problem: a separate opt-in command leaves ordinary development and agents on
the old execution path. Decision: use one default Make/agent entrypoint and
reuse the existing snapshot, selector and evidence owners. Domain gates
(`test-unit`, `test-api`, `test-frontend`, browser/slow tests), coverage and CI
remain unconditional; `make test-full` aliases the existing `test-all` release
gate. Alternatives rejected: replacing those gates with affected selection
loses their scope and coverage obligations; rewriting every test is unnecessary;
a second dependency selector would drift from landing verification.

`scripts/verify.py` delegates to `lib/project_mod/verification.py`. `plan` and
`run` accept exact test file/directory scopes (default: the complete inventory),
`--into CHECKOUT` and `--json`. A missing scope refuses the entire request.
`run` returns zero only when every requested file has complete passing execution
or reusable evidence; failures and incomplete verification return nonzero.
`plan` reports decisions and never claims execution passed.

The lane owner's `capture_working_tree` freezes staged, unstaged and nonignored
untracked bytes through a private index, clearing assume-unchanged/skip-worktree
bits only in that private copy. Verification plans against Git objects first;
for an explicit scope, when every selected file has reusable evidence it creates
no checkout and starts no pytest process. Changed scopes load the frozen selector
to preserve its mandatory guards. Needed executions use the shared private checkout, leaving
the caller's branch, real index and files untouched. Edits
made after capture belong to a later snapshot; every result names its tested
tree. Dirty snapshots use deterministic synthetic commits (parent commit time),
so repeating identical input creates no new commit objects. They move no
branch/WIP/publication ref and become reclaimable by ordinary Git GC when the
private checkout moves away.

Only complete passing records with unchanged observed inputs are reused.
Missing read/import paths and every possible conftest in the test's ancestor
chain are negative dependencies: creating one invalidates earlier evidence.

**A conftest is a unit-level dependency.** Problem: every record depended on
all of `tests/conftest.py`, so one edit re-ran the whole suite. Decision:
`conftest_units` names the code that runs outside any single test file —
module-level statements, class bodies, and the transitive local helpers of
those statements and of pytest's hooks and decorated fixtures, whose
finalizers run outside the test's bucket — plus the names pytest discovers at
collection. Those are the shared units; a helper nothing session-level reaches
is an input only to the runs that executed it. Alternatives rejected: treating
conftest as an ordinary module misses session hooks and teardown; a static
fixture graph duplicates the recorder's own observation.

Measured limit, recorded so it is not re-litigated: replaying the six conftest
edits of 2026-11-08 over the 4,023 records of one real environment still
selects 100%. That is correct, not a defect — this conftest installs its sync
client shim at import time, so the helpers those commits edited really are
inputs to every test. The win is for conftests and helpers without that
shape, and for the signature-only churn below; narrowing further would mean
deciding that a session-level input does not matter.

**Inert definition headers leave the module unit.** A definition with no
decorators, only constant defaults and no evaluated annotations (postponed
annotations count as unevaluated) does nothing at import time but bind its
name. Its signature and defaults join the function's own unit; the module
unit keeps the name, so adding or removing a top-level name still invalidates
importers that discover it dynamically. Records before this change carry the
older, coarser units and are simply more conservative; the cache's v7 file
imports them once under their original identity.
Failures and files that launch unobserved subprocesses, read/list external data
or connect to a network execute on every request. Import-loader reads, including
pytest's assertion-rewriting bytecode, remain covered by source/environment identity.
Problem: caching a filesystem path's spelling can mistake external input for a
repository dependency after a working-directory change or symlink retarget.
Decision: resolve each observed read/listing against the current filesystem;
only absolute code-source paths retain lookup caching. Alternatives rejected:
cwd-only cache keys miss symlink retargets, while clearing every code lookup on
each filesystem operation discards unrelated immutable-source work. Real pytest
observations cover both mutations and both read/list operations; an external
input change also verifies that the engine executes and reports the new failure.
The test environment, interpreter/distributions, recording semantics and recorder
source participate in the cache identity. Raw environment values are never
persisted.

**The test environment is canonical, not inherited.** Problem: `_test_environment`
inherited the launching process's variables, so each conversation, CI job and
tool call produced a different evidence key, and a run recorded by one launcher
could never answer another. Measured on 2026-11-08: the record store held 17,496
records in 36 environment partitions, a landing's own partition held 16 of 4,546
test files (0.4%), and 272 variables were conversation-specific. After landings
stopped running tests the same day, the store still grew to 45 partitions (21
with fewer than ten records), and 17 live processes produced 17 distinct keys:
the execution host, which runs the CI candidate check, shared its key with no
conversation, so no lane's recorded runs could answer a candidate check. The
inherited variables also carried the server's own `TOFU_DATA_DIR`, `PORT` and
provider credentials into test processes. Decision: the run inherits only the small declared
set `_INHERITED` (user and host identity, the opt-in storage/native test
fixtures), the scenario controls a test or landing sets for itself
(`LANDING_`, `EXERCISE_`, `TOFU_TEST_SCENARIO_`), and the host's tools through
`apply_tool_paths` (interpreter directories, bundled toolchain, user tool bins,
system directories). Locale is `C.UTF-8`, `TZ=UTC`, and `VIRTUAL_ENV` names the
project interpreter. Everything else is pinned by `tests/conftest.py`, whose
code is a dependency of every record, or by the runner.

Alternatives rejected: excluding the known conversation variables keeps the
launcher's credentials and partitions again on the next unrecognised name
(measured: an exclusion list still passed 91 variables, including sandbox
tokens); inheriting `TMPDIR` or proxies makes the key follow the launcher;
keeping the inherited environment under the v6 `(environment, test)` keying
keeps each launcher's evidence correct but private to it. A variable that does
reach the tests (`_INHERITED` or a scenario prefix) still separates evidence:
`test_verification_engine.py` switches environments through a
`TOFU_TEST_SCENARIO_` variable, and returning to one reuses its pass.
Tests that need a service configure it themselves, as they already do for
storage and providers. A 14-file sample of the real suite has identical
outcomes under the inherited and the canonical environment; two native-engine
skips become passes, because the launcher had pinned a backend the suite never
selected.

**The evidence key covers recording semantics, not orchestration.**
`_environment_key` hashes the interpreter's distributions, the canonical
environment, `impact_selection.py` (units, records, planning), the recorder
plugin source, and `_record_semantics()`: the AST of the helpers that turn
observations into records (`_refs`, `_global_inputs`, `_remember`) plus the
runner's own flags and plugin name. Editing selection, retries, attribution or
the warm checkout no longer discards evidence: 6 of the 12 verification-policy
edits measured on 2026-11-08 would not have changed the key, and the 12 forced
a cold start each. `verification_policy.py` and `verification_dependencies.py`
steer static selection only, so they are not part of the key either.
Alternatives rejected: hashing this whole module is simpler
but throws away every record on an orchestration edit; hashing only the
recorder misses a change in how units become dependencies. A specification
pins `_remember` as the only producer of records, so a new producer must join
the key. Test processes must remain hermetic: arbitrary external service state,
time and unobserved native I/O are not certificates supplied by this cache.
Full domain/release execution remains the gate for those behaviors.

Execution checks its source snapshot afterward. Changed tracked bytes or new
nonignored files make the result incomplete and prevent dependency publication.
Interpreter-created `__pycache__/` directories remain disposable cache even in
repositories without an ignore rule; they do not count as newly written source.
An incomplete current run removes earlier evidence for its requested files in
the same cache transaction; untouched files retain theirs. `record` remains an
explicit unconditional run at the committed revision, and reports incomplete
files rather than silently succeeding with zero reusable records.

`record --pending` (`record_pending`) is the resumable form. Problem: a
whole-suite recording takes hours on this host, and an interrupted run kept
nothing — the next attempt started cold, so the warm-up never finished.
Decision: only files whose evidence is missing or stale at the tip run, one
top-level test scope at a time (a boundary the repository defines, not a
tuning knob), and each scope is saved before the next starts. Repeating the
command converges; it reports how many files were already fresh. Alternatives
rejected: a batch-size constant would be a tuning knob, and recording in one
process is exactly the all-or-nothing behaviour being removed.

Observation callbacks never propagate errors into the code under test. A callback
failure disables dependency publication for that process and emits receipt-only
output with the error type; the runner exposes it as incomplete verification.
Silently keeping partial dependencies was rejected because it could authorize a
false cache hit. Both monitoring and audit failures have injected specifications.

**Problem:** explicit one-file runs prepared every test source; fresh checkouts
paid the same import extraction again. **Decision:** `verification_index.py`
enumerates immutable Git paths, reads missing blobs with one `cat-file --batch`,
and invokes the existing selector once per distinct missing source. Extractor
source/interpreter identity and blob IDs bind reuse. One atomic JSON file keeps
only entries in the current inventory; scoped reads retain current neighboring
entries. A checksum also rejects structurally valid JSON whose references were
corrupted. Corrupt/unwritable cache data loses acceleration, not discovery; failed
Git reads never become an empty success. Selectors without a pure extractor and
symlinked test inventories use their ordinary discovery. **Alternatives rejected:**
mtime-only reuse can miss changed bytes; a second import policy diverges from the
selector; full collection for an explicit file performs unrelated work.

**Measurement:** `scripts/benchmark_verification.py` checks identical reference
sets on a frozen 4,513-file corpus, alternating three cold runs per implementation.
Ordinary/full content-index cold medians were 14.328/14.092 s; full content-cache
hits 0.148 s; a cold one-file scope 0.090 s. These measure preparation, not the
whole suite. The full index occupies 4,594,741 bytes in one replaced file, without
per-revision accumulation. JSON results report snapshot, preparation, index,
planning, execution, recording and total clocks. Specifications:
`test_verification_index.py`, `test_verification_engine.py`, completion/cache
tests and the existing landing/lanes suites.

**The static-import pass follows statement containers.** Problem: import
discovery walked every expression payload even though Python expressions cannot
contain static import statements. Decision: retain Python parsing and visit
statement containers, including exception handlers and match cases, for that
pass. Nested function, class, async, loop, exception and match imports remain
visible. `source_references` also performs the separate complete AST scan for
`pytest_plugins`; its literal plugin dependencies and conservative unknown-plugin
fallback are not pruned. The complete test-reference extractor still walks
expressions: string constants, including escaped and concatenated Make
invocations, retain their Makefile dependency. Fixture helper discovery still
examines decorators and defaults before following their import-bearing
functions. Relative imports, package parents and unknown-fixture fallbacks retain
their existing policy.

Selection retains one reference scan after structural reasons have been applied;
an empty change set does not read reference collections. The alternatives were a
second parser or language implementation, which would duplicate Python syntax
policy, and an additional persistent index, which would add invalidation and
retention obligations without avoiding more required work. The static-import
pass adds no cache format, persistent allocation, worker setting or selection
cutoff; the separate plugin policy requires reference-cache version 3. Verify
`tests/meta/test_test_select.py`, `test_verification_dependencies.py` and the
index/default/engine, policy and landing suites.

**Measurement scope (2026-11-08):** a comparison before the plugin policy was
added used three alternating before/after pairs on frozen `c0a545067`, with
4,537 test files and an 8,773-file Python graph. All index and graph fields, plus
expanded paths, selections and reasons for actual 2/68/733-path change sets,
matched in all six runs. Median complete cold preparation (Git tree, test index
and dependency graph) was 53.274 versus 46.128 s. That frozen comparison excludes
the later plugin AST scan, plugin dependency edges and reference-cache version 3.
The final combination has not been remeasured; those figures do not establish
its preparation speedup or any test-execution or whole-conversation speedup.

Conservative external-input classification can also rerun a file whose session
teardown reads external temporary state. The six-case `test_abort_signal.py`
probe completed in 5.15 s on a warm checkout but did not qualify for reuse after
this classification; preparation speedups are not an end-to-end suite multiplier.

### Merge verification policy

**Frequent integration (owner decision 2026-11-08).** Ordinary CI candidates verify
affected inputs and reuse complete evidence (landings run no tests; see
[Landing worktree branches](#landing-worktree-branches)); full repository qualification
belongs to the existing nightly and release/manual CI runs. A result always
names the frozen revision it checked. Incremental success is not a full-release
certificate. Genuine shared runtime/fixture changes still expand affected scope.

Problem: adding development Make targets selected all 4,529 current test files,
although the expanded `test-all` and `ci` commands were unchanged. Decision:
Makefile consumers declare dependencies through literal paths and Make
invocations; Makefile is not an input of the direct pytest runner. Selection
logic and pytest execution have separate owners. `scripts/test_execution.py`
owns the existing pytest arguments and worker-choice behavior; the selector
owns discovery, references, blast radii and guards. Runtime evidence depends
on the execution owner, while selector consumers still depend on selector code.
Legacy selectors remain supported. Moving unchanged execution functions from a
legacy selector into the standalone owner is recognized only by a conservative
source comparison; unknown shapes and actual argument changes invalidate scope.
The legacy CLI no longer broadens a selection merely because it exceeds 40%
of the inventory. Actual shared inputs still select their complete dependents.
For cold runs, `verification_dependencies.py` expands changes through static
Python imports (including relative imports, package initialization and deleted
targets). Direct literal inputs remain with the test reference index. Import
reachability selects test consumers; it does not pretend an unchanged importer
was edited or activate its whole-family blast rule. Cycles terminate by visited
path identity. Lazy conftest fixtures link to their consumers through parameters,
fixture chains, aliases and `usefixtures`/`getfixturevalue`; hooks and autouse
fixtures link to their full directory scope. Local helper calls carry imports
into their fixture or hook. Unknown dynamic fixture requests conservatively
depend on all declared fixtures in scope. Literal `pytest_plugins` declarations
are module dependencies, including nested plugins and package initializers.
Unknown declarations conservatively connect their consumers to Python changes;
they cannot disappear from selection merely because they use no import statement.
Problem: plugin strings were missing from the cold graph, so separating a shared
cleanup owner could hide fixture consumers. Decision: use the same plugin edges
in direct references and transitive selection, and invalidate older reference
caches. Reject treating a narrower, incomplete selection as a performance win.
Verify `tests/project_mod/test_verification_dependencies.py` and
`tests/meta/test_test_select.py`. One checksummed, atomically replaced graph cache retains only
the current Python inventory; blob identity decides which files need reparsing.
This complements dynamic observations, including fixture dependencies; cold
static imports cannot prove arbitrary fixture dispatch, reflection or external
system behavior. Those retain the declared domain and full qualification gates.
Problem: a dynamic request for an already-cached session fixture bypasses the
setup hook, so later test files could lose that fixture's recorded inputs.
Decision: capture the request's resolved fixture definitions through teardown,
before pytest clears the request. Every consumer inherits the session fixture's
inputs even when setup executes only once. Reject re-running setup or silently
reusing incomplete dependency records. The recorder's existing source identity
invalidates older evidence; serial and xdist specifications live in
`tests/project_mod/test_impact_selection.py`.
The `record` command emits the existing bounded failure diagnostic before
retiring its temporary runner log. Keeping only failed case names discarded
the setup traceback and forced another run to diagnose the failure; retaining
an unbounded log history was rejected. This changes no result or reuse policy.
Alternatives rejected: caching by commit identity loses valid reuse across
merges; dropping all global invalidation misses real fixture/environment changes;
an unconditional full run on every selector or Makefile edit repeats unrelated
assertions without checking the changed orchestration contract any better.

CI ordinary pushes/pull requests use the same frozen merged-result verifier.
Nightly, tag and explicit manual runs retain full unit/API/slow/coverage gates.
Missing comparison history expands to full verification. The required result
checks the jobs required by its declared scope; a failed scope decision, missing
receipt or unexpected skipped job cannot certify a revision.
`landing_verification check --base SHA --expected-revision SHA --json` runs in
the clean, dedicated CI checkout so its verified frontend artifact is present.
It checks candidate identity and source cleanliness before and after execution,
does not publish Git refs, and fails on unavailable verification. Its JSON
receipt lives outside the source tree and is uploaded under the candidate SHA.
It reports preexisting failures separately, using the same comparison rules as
local landing; it does not turn a preexisting failure into a full-suite pass.
Directory-dependent receipts in the live CI checkout execute again because
ignored entries can appear without changing Git. Private, reset snapshots keep
directory-based reuse.

**Measurement (2026-11-08).** Replaying the same 13 changed paths from
`e7f43ecd9` to frozen `64676724b` over the same 4,529-test inventory selected
4,529 files with the previous policy and 41 with the new policy (99.1% fewer).
Actual execution-owner or root-conftest edits still selected all 4,529. Cold
dependency preparation plus selection took 34.800 s; the exact warm graph took
1.074 s. The current-inventory graph occupies 34,245,417 bytes in one replaced
file; old inventories are not retained. These measure scope and preparation,
not test execution or a whole-suite speed multiplier. The selector compares each
test's references with the change set once, instead of rescanning every test for
every changed/transitively affected path.

**Problem (measured 2026-11-07).** A landing proved only that the merge was textually clean;
nothing verified the merged result. Between 11-01 and 11-07 the current branch's first-parent
history took 436 merges (and 23 direct commits); in 34% of those merges a branch living at the
same time changed the same source file, and in 43 the current branch had moved 20 or more
commits under the branch (up to 113). An SZZ sample of 70 fix commits found 11 whose fixed
lines came from two or more branches landed within 72 hours of each other, plus fix-on-fix
chains in one area (three batch-wake fixes within 37 hours; five fixes to the lines of one
Coordinator sidebar change within hours of it landing). The
execution-cutover branch overlapped at least eight concurrent branches on the same files and
landed with stubbed seams; every Worker then failed with a task identity mismatch. Agents that
did rerun tests after merging could not tell regressions from the dozens of tests already
failing on the current branch.

**Decision (2026-11-07 revision; landings exempt since 2026-11-08).** A merged result is
verified in a private checkout (`lib/project_mod/landing_verification.py`) by the CI
candidate check and linear-checkpoint attribution; `git_land` no longer runs it. Only the
test files the merged tree can affect run:

- **Selection by impact records, not by static closure.** The recorder
  (`lib/project_mod/impact_recorder.py`, a pytest plugin using `sys.monitoring` and an
  audit hook, one callback per function per test file) notes per test file which code units
  executed, which functions ran during module imports (they build registries and tables),
  which fixtures it used, which files it read and which directories it listed, whether it
  started an unobserved program (anything but git outside the repository) or forked, and its
  collected/executed/failing node ids. `lib/project_mod/impact_selection.py` maps that to
  Git blobs and to units per Python file (module bodies separately from each function body,
  by AST, so moves and comments change nothing). Its semantic cache owner,
  `lib/storage_authority_sql/impact_cache.py`, stores records in one SQLite file beside the
  warm merge checkout, scoped by exact verification environment as described above. A
  recorded file runs when an executed unit, a module body, a file it read, a directory it
  listed or a conftest in its chain changed, or when it failed and the current branch differs
  from its recorded run. Functions it never executed do not affect it. An unrecorded file
  keeps the repository's static selection. An opaque file counts every byte it references,
  and any statically selected opaque file runs again, because its external inputs
  cannot be proved unchanged. Opaque failures also require a current baseline
  run rather than a cached failure classification. A file whose collected node ids did not all execute is never recorded,
  so a crashed worker cannot install a partial dependency set.
  **Snapshot input boundary (2026-11-08):** every recorder receives the exact
  Git input manifest. Reading files or executing code outside it, or
  listing a directory outside it or entries that differ from it, makes the run opaque. Ignored
  generated artifacts cannot appear as unchanged absent blobs. Missing optional
  reads retain negative dependencies but are opaque: an ignored file can appear
  later without changing Git. Without a manifest, only execution receipts
  are emitted. A thread-local recursion guard excludes the observer's own directory
  census without disabling observations in other threads. The reproducer first
  passed on an ignored artifact, changed only its bytes, and incorrectly reused
  success; it now executes again and reports the regression.
  **Importer I/O cost (2026-11-07):** live collection stacks showed the audit
  hook canonicalizing bytecode paths before discarding importer reads.
  The existing importer exclusion now runs before path resolution for opens
  and directory listings. Application reads/listings and external-read opacity
  retain their original evidence; executed/import-time code still uses the
  monitoring callback. No new path cache, identity assumption or sampling is
  introduced. A controlled loop over bytecode paths for all 8,745 tracked Python
  files made 8,745 canonicalizations before and zero after; three alternating
  runs had median observer time 0.5463 s versus 0.00486 s on the local lane.
  This is excluded-event processing evidence, not a whole-collection speedup.
  The alternatives, disabling observations or weakening path identity for
  application reads, would weaken reuse proof. Verify importer exclusions,
  application data/directory evidence and external reads in
  `tests/project_mod/test_impact_recorder_io.py`, plus selection and completion
  specifications (including observation faults).
- **Blast-radius files are dependencies, not a switch.** The selector's `BLAST_RADIUS` files
  (pytest configuration, shared harnesses, the execution owner) can change any test in their
  scope in ways a run cannot observe, so every record they scope depends on them: by code for
  Python, byte for byte otherwise. A real change re-runs every scoped file, and complete
  non-opaque records can answer later verifications. Statically selected
  opaque files still run: the Git snapshot does not prove their external inputs unchanged.
  Previously such a change
  ignored all records on every merge attempt, so a whole-suite landing (about five hours on
  this host under load) started over whenever the current branch moved and could never land.
  A conftest or blast-radius Python file whose code is unchanged (comments, layout) selects
  only the test files that reference it instead of escalating: on 2026-11-07 a comment-only
  edit to `scripts/test_select.py` selected all 4,474 test files.
- **A warm checkout and cached selector index.** `merge_checkout` keeps one private checkout
  per repository on its fast local root (in the Git directory when placement declines),
  holds it only while one landing merges or one run tests, and resets and cleans it for the
  next user while keeping `__pycache__` and the dependency links. A user that finds it in
  use gets a temporary checkout instead of waiting. Cold collection of 30 test files took
  127 s versus 30 s warm (2026-11-07); the checkout is a reconstructible cache and owns no
  durable state.
- **Failure attribution uses current control runs.** A failure on the merged
  tree runs again at the current branch tip unless an explicit recorded failure
  still describes that case's inputs. A historical record without the failure
  does not prove the case currently passes: its assertion may have been skipped,
  or an external condition may have changed since the earlier passing run.
  Attribution selects the failing node ids, rather than their passing siblings.
  Each requested case needs both a collection and an execution receipt. A new or renamed
  case that cannot resolve at the tip falls back to its existing file, so a missing case
  never pretends to have passed there. A failure there too is preexisting and never blocks.
  The rest rerun on the merged tree: a failure that reproduces is a regression, reported
  with its test ids and output tail. A completed, non-reproducing confirmation is flaky; missing
  execution remains unavailable, and a partial confirmation retains every proven regression
  without calling unexecuted siblings flaky. Focused reruns never replace whole-file impact
  records. Collection/execution receipts survive unavailable code monitoring, while those
  receipt-only runs create no dependency-cache records. A runner that cannot start on the
  merged tree but does at the tip is a regression; one that cannot start on either side, or
  a repository without a selector, is reported with its reason and fails nothing, because
  only reproduced evidence is attributable to a merge. An interrupted verification retires
  its whole test session (`process_group`).

**Historical absence is not passing evidence.** Problem: four native cases
were refused as regressions because their historical records contained no
failure; direct execution at the current tip failed all four in 12.38 seconds.
Independent synthetic repositories reproduce both skipped and previously
passing variants. Decision: reuse only explicit matching failures, and
execute a current baseline case before interpreting absence as a pass.
Inferring passes from the failure list was rejected because that list contains
no current assertion receipt; discarding all impact records was rejected
because source selection and explicitly observed failures remain useful.

**Measured failure-recheck work.** With the same 201-case synthetic file and one existing
failure, a warm-checkout benchmark (one warmup and three measured runs per version) reduced
the baseline recheck from 201 cases to 1; its median duration fell from 0.895 s to 0.653 s.
The initial merged run still executes all 201 cases. This measures the attribution phase,
not a fixed speedup for native database or full-repository runs. Retaining whole-file
baseline rechecks was rejected because passing siblings add no failure-attribution evidence;
omitting collection receipts was rejected because pytest can otherwise return an empty or
partial selection. The change adds no worker setting, retry budget or cache schema.

Complete non-opaque records avoid repeating unaffected files; statically selected opaque
files and files without complete evidence still run. Reusing an opaque success would not
prove unobserved child processes or external data unchanged; narrower reuse requires
observing those inputs.
`python3 -m lib.project_mod.landing_verification record [PATH ...]`
records runs at the current branch tip ahead of later verification, which also re-checks
everything the selection skips. Run final verification through this command in a clean,
committed lane: merged-result verification reuses its passing records when the merged
inputs agree, and concurrent source changes invalidate them. A real-Git specification
verifies that recording a lane avoids the additional merged test invocation; a changed
receiving dependency still reruns and reports the regression. `record --into` resolves the caller's actual checkout, including a lane subdirectory,
and reports the checked commit and root; only cache placement uses the canonical receiving
repository. Resolving the receiver as the test source was rejected because it validated the
wrong revision and lost lane-result reuse. Every explicit test path must match the selected
checkout before execution. `record` exits unsuccessfully when tests fail, no requested cases
execute, or the runner cannot complete. The repository's selector and pytest configuration own its arguments
and plugins; ambient `PYTEST_ADDOPTS` and `PYTEST_PLUGINS` do not alter recorded runs.
Each test subprocess announces its checkout, selection count and exact log path on stderr
before it starts, preserving the CLI result on stdout. Concurrent runs must be followed
through that receipt, rather than by choosing the newest temporary log directory, which can
belong to another landing. `record` also names failing cases on stderr before its temporary
logs are retired, so an unsuccessful result remains actionable.

**Known blind spot:** a test observing a module-level value of a module it
neither imports nor executes code in (for example a registry entry another module adds at
import) is rerun only when its own dependencies change; the full recording runs are the
counter-measure. A recorded guard-core file whose whole run was observed is answered from
its dependencies; opaque guards keep running (`test_jsdom_runner_structured.py`, which starts
node, alone took 39–158 s on this host), so their duration is the floor of a verification
until child processes are observed too. The selector index is rebuilt only when the tests subtree's
Git id changes. Records live in one store file per schema version, so code versions running
side by side (a verification started before a release, a lane's own tools) never discard
each other's records; an interpreter-environment change discards them, and a test file that ran
with fewer than all of its collected items is never recorded. **Measured
(2026-11-07), while landings ran this verification:** the text-only gate took 260–506 s per landing; the first impact-based landing
(24 recorded test files) took 167 s of verification with 5 files running, of which the
guard-core run was the bulk.

**Cache ownership.** Problem: SQLite imports and SQL in the verification modules made
the production storage-boundary gate refuse startup. Decision: the SQL storage package
owns the reconstructible cache's connections, schema, codecs, transactions and file lock;
project code supplies records and Git-blob fingerprints through `ImpactCache` and catches
`ImpactCacheError`. This local verification cache never opens an application authority or
depends on its backend or availability. The existing versioned file and row format are
preserved, including environment invalidation and atomic record batches. Alternatives
rejected: exempting project modules from the startup guard, putting repository test evidence
in the user's database, or discarding recorded runs to change formats.

**Alternatives rejected.** A serialized merge queue or per-module lock: the owner rejected
waits on 2026-10-28, and independent landings would queue behind each other's test runs.
Running the whole suite: the current branch carries known failures and a full run costs tens
of minutes on this host, for evidence the selector already scopes. Blocking on any failure:
every landing would inherit the current branch's red tests and flakes. Verifying only
textually overlapping files: semantic conflicts (a contract changed in one file, a caller
added in another) share no file. Selecting by the static import closure: measured on this
repository, the median test file's closure is 2,552 of 8,589 Python files and nearly every
landing selects the whole suite, because a few core modules reach almost everything.
Verifying by the verdict of an older merge onto a different tip: the records make
re-verification cheap and exact instead. A bypass flag: under pressure it becomes the
default path. Specifications: `tests/project_mod/test_landing_verification.py` (a trunk
change that breaks the branch, a branch that breaks a concurrent trunk test, preexisting
failures answered from records, flaky failures, an unrecorded change, no selector, a warm
checkout kept across runs, a terminated run, landings that run no tests), the
recorder/selection specs in `tests/project_mod/test_impact_selection.py` (serial and xdist)
and the two `test_git_land_*` suites.

### Lanes

**Problem.** Every session paid for its own full checkout on the network volume (57–164 s
measured; 1.4 s of it was just registering a worktree, 3.5–6 s the checkout on local disk,
and parallel checkout workers did not help), then threw it away. Because that was slow, the
shared checkout doubled as trunk, workspace, delivery target and server source, and
uncommitted work in it leaked into every isolated task's input. **Decision.** `scripts/lane.py`
creates one **lane** (a branch plus a warm checkout on fast local storage) per unit of work;
`lib/project_mod/lanes.py` owns placement, recycling, saving and restoring. A new lane takes
the checkout a landed lane left behind when one is available and rewrites only the files
that differ (0.29 s measured for a branch switch in an existing checkout). A lane keeps its
durable state in Git: the branch's commits and, after every task that wrote in it, a
work-in-progress commit at `refs/tofu/lanes/<branch>` (the branch never moves), so a lost
checkout is rebuilt by `lane.py restore`. Placement is the new `PURPOSE_LANES` of
`serverctl_pkg/local_placement.py`: it shares the operator's `TOFU_LOCAL_WORKSPACES` switch
with task checkouts (which default to `off`) and defaults to `on`, because lane checkouts
are reconstructible; since
task checkouts became reconstructible too, `auto` accepts a volume of unknown persistence for
both instead of sending lanes back to the data volume. Temporary directories stay refused,
and when nothing is selected lanes fall back to `.tofu/worktrees`. Landing retires
a clean lane as a spare only into the directory new lanes come from (an earlier placement
or the fallback would otherwise accumulate spares nobody claims) and drops the lane's saved
work; a lane with uncommitted or untracked files is kept. Dependency links are made only
where the repository ignores them as files: a directory-only ignore pattern does not cover
a symlink, so `/.venv/` and `node_modules/` became `/.venv` and `node_modules`, and a
worktree's links can no longer be committed. **The next spare is prepared ahead (revised
2026-11-07).** Landings alone did not keep spares available: more conversations create
lanes than land them, so lane creation was usually a full checkout (6.9 s measured on
2026-11-07, against 1.24 s when recycling). `lane.py new` and `restore` now start
`lane.py prepare-spare` detached when no spare is ready or being prepared, so the next lane
recycles. Exactly one spare is the target, which the next creation consumes: the trigger is
demand, not a pool size. The checkout is made under a `.preparing-` name, held by a file
lock, and renamed to a spare only once complete; a preparation that died is cleared by the
next one, and a live one is never duplicated. The placement reserve still refuses a spare
on a nearly full disk. **Alternatives rejected.** Preparing nothing ahead (the original
decision): its premise, that landings keep spares available, did not hold. A pool of several
spares: its size would be a tuning knob, and each creation already starts the next
preparation. Reflinks and hardlinks: a hardlinked tree cannot be edited independently, and
the overlay does not reflink. `lane.py list` shows every lane with its divergence and
uncommitted files. Specifications: `tests/project_mod/test_lanes.py` and the lane purpose
case in `tests/server/test_local_placement.py`.

After a landing, `git_land` reports which conversations' unlanded work changes the landed
files and leaves each of them a notice through the
[integration radar](conversations_project_brain.md#integration-radar); the landed
checkout's radar records are retired.
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
dispatch. The worker loop hands each page's rows to per-project receiver lanes (below) and
never runs row work itself. A pass is due at worker start and after a worker-loop error (recovery passes,
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

**Receiver lanes: one project's capture never delays another project's publication
(2026-11-06).** Problem: the integration worker is one thread per serving process, and it
ran every page row itself, in global row-ID order across all owners and projects. Row work
is volume-proportional Git with no deadline by design (`checkpoint_workspace`'s `git add -A`,
the task-delta canonical capture, source reclamation), so one project's capture held every
project's publication: on 2026-11-06 a parallel-project Worker's worktree (2.26 million
untracked evaluation snapshots under `.snap_tmp/`) was captured from about 11:20 to 14:01,
and Tofu's accepted deliveries (7509d68d among them) waited behind it. Decision: the loop
pages exactly as before but hands each row to its project's lane
(`lib/project_mod/delivery.py::_queue_on_project_lanes`), keyed by canonical project root,
the domain of the repository lock that already orders a project's Git work. A lane is one
daemon thread that runs its project's rows one at a time in arrival order (row-ID order
within a pass) and exits when it has none; lanes of different projects run concurrently. A
queued row keeps its place and takes the newest page snapshot; a row in flight is not queued
again, because its own receipt write is a delivery-state commit that makes the next pass
due and wakes the loop (the existing commit hint). A row whose processing changed nothing
waits for the loop's idle backoff, as before. Two lanes admitting the same shared ancestor
barriers at once can refuse one of them with `workspace_busy`; a capture refused that way
is retried by the next pass (delivery contract, "Conflict recovery"). `stop_worker` joins the loop, then drops queued
rows (their receipts stay durable debt for the next recovery pass) and joins running lanes
within the same budget. Other callers of `resume_pending_workspace_deliveries` (the specs)
still process the page synchronously; only the loop passes `lanes=True`. Within a
project a slow or doomed capture still delays that project's later rows: that order is the
repository lock's, and the evidence capture of a denied outcome stays ("checkpoint/undo
evidence remains" above). Alternatives rejected: a per-project storage cursor (a query
change on both authorities for an ordering the page already gives); skipping a busy
project's rows instead of queueing them (a project with rows on several pages would never
reach its later pages while its first page stays due); the shared demand executor (its
non-daemon workers are joined at interpreter exit, so a multi-hour capture would hold
shutdown); a lane concurrency cap, capture size limit or capture deadline (knobs that limit
work, AGENTS.md principles #3 and #5); skipping the evidence capture of denied outcomes
(changes the evidence invariant and `test_transient_checkpoint_failure_remains_recoverable`;
lanes already remove its cross-project cost). Known limit: the explicit integration queue
(`process_ready_once`: candidate merge and release gates of `submit_workspace`) still runs
on the loop, because its 660-second claim horizon assumes one claimant per process; no
isolated-task Worker uses that queue (every live row on 2026-11-06 was `isolated-task`).
Specifications: `tests/project_mod/test_delivery_project_lanes.py`, including
`test_slow_capture_in_one_project_does_not_delay_another_projects_ready_row` (a capture held
in project alpha while project beta's ready row publishes; it fails on the serial loop).

Successful tasks reclaim source clones whose final bytes/index are pinned in recoverable checkpoints; uncaptured changes, conflicts, failures and durable artifacts retain recovery/undo workspaces. Python gets a lazy private prefix with readable source packages;
pip installs stay local and new packages retain their workspace. Node package links reuse source dependencies inside a private ignored directory.
Recorded bridge directories and Python/test caches are reconstructible; source dependencies are never deleted.
No new worker/queue is added. A private 1,000-file × 1 KiB measurement took 0.262s to create and 0.205s to settle/reclaim a no-change workspace, retaining zero clones; this is a fixture, not a latency cap.
Git objects/checkpoint refs and owner-scoped receipts retain evidence after clone reclamation. One origin acknowledgement records source retirement and recovered opt-in checkpointing; missing ACKs and transient contention use the existing worker. Acknowledged cold rows require no filesystem scans or heartbeat writes.

Frontend toolchain caches stay in their checkout. **Problem:** every `node_modules` entry was linked, including the caches Vite and Vitest write on each run (Vite's `.vite` dependency cache and `.vite-temp` config bundles, Vitest's `.vitest-cache`), so a frontend build or test in any task checkout wrote into the source checkout. **Decision:** the preparer never links those names (`NODE_TOOL_CACHE_DIRECTORIES` in `workspace_runtime.py`); the toolchain creates them in the task checkout's real `node_modules` on first use, and reclamation counts them as reconstructible like the links, so a checkout that built still reclaims. Checkouts prepared before this keep their links until retired. **Alternatives rejected:** configuring each tool's cache location reaches only projects Tofu configures, and Vite places `.vite-temp` beside the nearest `node_modules` regardless; copying the source caches spends work on checkouts that never build. Specification: `test_lazy_workspace_runtime_keeps_frontend_tool_caches_in_the_task_checkout` in `tests/storage/test_integration_delivery.py`.

Git scans bound active/problem rows; terminal history uses stored metadata. Status is cached with mutation invalidation; event detail is bounded.
Unregistered/prunable worktree counts remain visible.

**Task checkouts stay on the data volume unless the operator opts in; local
ones are rebuilt from Git (2026-11-08; opt-in since 2026-10-25).** Problem:
managed task checkouts live under the data root. On this host that is a
cross-datacenter FUSE mount where an uncached lookup costs about 290 µs
against about 5 µs on the local overlay, so serial file access (`lstat`,
Python imports, pytest collection) runs 50-95x slower. Measured 2026-11-08:
collecting the ~3,720 tests of `tests/llm` and `tests/turn_lifecycle` took
11.6-13.2 s from a FUSE checkout and 5.5-5.7 s from a local lane (2.2x, with
the shared `.venv` still on FUSE); a fresh worker checkout took p50 37.5 s /
p90 110 s and whole workspace binding up to 228 s; claude-opus-5.5 workers
spent 69.8% of their wall time in tools. Local placement existed but was
opt-in, because a local checkout held work nothing could rebuild: a restart
pause captured nothing, a missing registered checkout failed binding
("Registered workspace is missing or mismatched"), a `git worktree prune` on a
host that cannot see the volume (every scratch cleanup runs one) unregistered
it, and discard kept every directory. Lanes shared the knob, so
`TOFU_LOCAL_WORKSPACES=auto` refused this host's overlay (persistence unknown)
for lanes too and sent them back to FUSE.

Decision: a local task checkout keeps its work in Git, as a lane does, but
placing task checkouts there stays the operator's choice: `PURPOSE_WORKSPACES`
defaults to `off`, `PURPOSE_LANES` to `on`.

- Work in progress: `integration_control.save_workspace_wip` commits the
  checkout's tracked and nonignored untracked bytes on its HEAD at
  `refs/tofu/workspace-wip/u<owner>/<task>`, through the private index of
  `lanes.capture_working_tree` (HEAD and the index are never written), at
  round settlement next to the lanes save and at the restart pause, outside
  repository admission. It is not publication authority; the pause still
  captures no checkpoint. The commit names the checkpoint it saw
  (`Tofu-Checkpoint:`), and a save that finishes after a newer one is dropped
  by its compare-and-swap ref update. Capturing this repository's 13,246-file
  checkout on the local overlay took a median 0.23 s (5 runs, first 0.41 s,
  host load 30).
- Restoration: start-time binding rebuilds a missing registered managed
  checkout at its registered path (`restore_task_workspace`) from the WIP
  commit unless a checkpoint was captured after it, else the checkpoint over
  the registered base, else the input snapshot. As `lane.py restore` does, it
  checks out the saved HEAD, applies the saved tree with `read-tree -m -u` and
  resets the index, so edits are uncommitted and untracked files untracked
  again. A detached HEAD the lost checkout committed past its save is pinned
  at `<WIP ref>.lost-head`. Binding re-links shared data, drops the lost
  checkout's runtime-bridge receipts (bridges are prepared again on demand),
  records a `workspace_restored` integration event and queues an advisory
  that tells the resumed model which commit it was restored from. Ignored
  files and FileHistory undo snapshots are not in Git and do not return.
- Locks: local checkouts are created with `git worktree add --lock`; Tofu's
  own removal forces past the lock (`remove --force --force`, unlocking on
  the legacy fallback).
- Capacity is a correctness condition: every checkout (task, manual, gate,
  legacy merge, repair proposal) goes to the local root only when its volume
  holds the revision's file bytes and file/directory count above the placement
  reserve, else to the data root. The estimate is one `git ls-tree -r -l`
  (0.17-0.64 s for this repository's 13,245 files, 259 MiB). Restoration
  refuses when the registered volume cannot hold the checkout.
- Discard admits the registered path (it rebuilt one from the current root)
  and removes a discarded local checkout once its work is saved and Git plus
  rebuildable files account for everything in it
  (`task_delta_delivery.checkout_reconstructible`, the reclamation rule).
  FileHistory undo history, durable ignored files or an attempt still working
  in it keep the directory; that attempt's settlement retires it. Reclaimed
  local checkouts lose their registration too. Data-volume checkouts keep
  their directories.
- Modes: lanes and task checkouts accept volumes of unknown persistence in
  every mode, because Git rebuilds them (only the native authority still
  needs `on` for that), so the shared knob means the same for both and `auto`
  no longer sends lanes back to the data volume: `on` skips the speed
  comparison, `auto` also requires a 3x speedup, `off` keeps both on the data
  root. Temporary, age-cleaned, network, memory, noexec and same-volume
  targets stay refused. Placement is decided once per process, namespaced per
  data root under `$XDG_STATE_HOME/tofu/workspaces` (then
  `~/.local/state/tofu/workspaces`, or `$TOFU_LOCAL_DIR/workspaces`);
  `TOFU_INTEGRATION_WORKSPACE_DIR` still overrides it. Existing checkouts are
  not migrated when the mode changes: registration stays authoritative, and
  removal accepts every root this control plane could have created.

Why task checkouts stay opt-in: Git rebuilds a checkout's files, not the
FileHistory undo store inside it (`lib/file_history/store.py`: `store_dir` is
`<checkout>/.tofu/file-history`), which is durable user state. Reclamation
deliberately keeps any checkout holding it
(`task_delta_delivery.reclaim_delivered_workspace` declines through the
durable ignored files of `checkout_reconstructible`), so nearly every edited
worker checkout is never reclaimed: on 2026-11-08, 252 of the 350 managed
checkouts on the data volume held undo state beyond the store's bootstrap
marker (183 with recorded snapshots). Placing task checkouts on this host's
container overlay by default would therefore (1) put durable undo history on
a volume of unknown persistence that can vanish with its container (AGENTS.md
design principle 3: never weaken durability), and (2) fill the shared root
overlay: 252 GiB, about 93 GiB free, placement reserve min(total/4, 8 GiB,
max(2 GiB, 5%)) = 8 GiB. About 75 worker checkouts a day are retained (55,
75 and 68 of the existing ones were created on 2026-11-05, -06 and -07), about
0.3 GB each, so the reserve is reached in about 4 days; after that new
checkouts fall back to the data volume with only a log warning, while lanes,
landing verification checkouts, runtime replicas, native builds and indexes
on the same root run near-full.

Opting in: `TOFU_LOCAL_WORKSPACES=on` (or `auto`, which also requires the
local disk to be at least 3x faster) places new task checkouts on the local
volume. The operator accepts that when the volume vanishes, each checkout's
FileHistory undo history and its edits since the last save (an unsettled
round) are lost (restoration rebuilds the rest from Git, and its advisory
says so), and that retained checkouts fill the volume until it reaches the
placement reserve, after which new checkouts go to the data root.

Known limit: the pause save runs synchronously in the interrupted attempt's
terminal persistence, so a checkout with a huge nonignored untracked tree
delays that attempt's shutdown as long as a checkpoint capture of it would;
its pause receipt is saved before the capture.

Alternatives considered: defaulting task checkouts to `on` now (rejected for
the two reasons above); moving the FileHistory store of placed checkouts to
the data root and letting delivered local checkouts be reclaimed (needs an
owner decision, because undo of a reclaimed checkout would no longer be
possible); keeping the data-volume default (chosen until that decision).
Rejected for the local-checkout mechanics: capturing a checkpoint at the
restart pause (it would become publication authority for work the resumed
attempt continues; see "Restart is not a task outcome"); saving data-volume
checkouts too (they persist with the volume, and each save walks the whole
tree on it); migrating existing checkouts (registration is the authority and a
move races the writer); restoring at a new path (receipts and the row name
the old one); ordering WIP and checkpoint by commit time (clock-dependent; the
trailer is exact); a separate lanes knob or a free-space threshold (tuning
knobs; the trap was persistence semantics, and the placement reserve already
defines capacity). Specifications: `tests/storage/test_local_task_checkouts.py`
(which opts in with `TOFU_LOCAL_WORKSPACES=on`),
`tests/storage/test_integration_workspace_placement.py`, the lane and
task-checkout cases in `tests/server/test_local_placement.py` and
`test_pool_directories_under_local_placement_roots_are_inventoried`. The test
suite pins `TOFU_LOCAL_DIR` to a temporary directory, which placement never
selects, so no test places a lane or checkout in the developer's home.

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
refs), unregistered pool directories (under the data root's pool and every
local placement root of that data root), local branches merged into HEAD, merged
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
