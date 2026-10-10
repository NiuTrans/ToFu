# Project integration

This domain owns isolated checkpoint/candidate integration, task-owned canonical delivery and opt-in linear snapshots. Candidate merge is not delivery; stable promotion has its own explicit gate.
Automatic work signals live in [`conversations_project_brain.md`](conversations_project_brain.md); no Board dispatch/handoff path exists.

## Ownership

| Concern | Owner |
|---|---|
| State machine, Git operations, gates, worker, status | `lib/integration_control.py` |
| Task root binding, terminal intent, delivery receipts | `lib/tasks_pkg/workspace_delivery.py`, `lib/project_mod/delivery.py` |
| Retry fingerprints, bounded conflict dossiers and scoped repair proposals | `lib/project_mod/delivery_retry.py`; `contracts/workspace_delivery_{retry,repair}_v1.schema.json`; [contract](../CONTINUOUS_DELIVERY.md) |
| Task delta publication and lazy dependency bridges | `lib/project_mod/task_delta_delivery.py`, `lib/project_mod/workspace_runtime.py` |
| Git command target effects and read preparation | `lib/project_mod/git_command_scope.py`, `lib/project_mod/git_inspection_execution.py` |
| Cooperative checkout writer lease | `lib/project_mod/workspace_lease.py` |
| Canonical-checkout task-end commit, short Git lock, stable/export source policy | `lib/linear_git_checkpoint.py` |
| Shared forbidden-path and semantic-gate policy | `lib/git_checkpoint_policy.py` |
| Owner-scoped transition API | `lib/integration_control.py` through `lib.storage` |
| Selected durable authority | `lib/storage_sidecar/operation_domains/integration.py` or `packages/tofu-db/src/integration_workspace.rs` |
| Isolation creation and fail-closed queueing | `lib/integration_control.py` |
| REST projection | `routes/api_v1/integration.py` |
| Retained UI | `frontend/src/runtime/sections/project-brain-integration.js` |
| Shared-mode Git guard (classification, ack nonce, refusal, dispatch) | [`GIT_WORKSPACE_GUARD.md`](../runbooks/GIT_WORKSPACE_GUARD.md) |

Every durable transition receives explicit `user_id`. A worker may claim globally, but its row carries the owner for all subsequent reads/writes; it never substitutes a process-global identity.

## Ref topology

Isolated integration:

```text
isolated writer -> immutable checkpoint -> gated refs/tofu/candidate (merged)
                         |                           |
                  task-owned delivery        explicit stable gate
                         v                           v
                  canonical branch            refs/tofu/stable
```

- Candidate updates are serialized and compare-and-swap guarded.
- Stable moves only through explicit promotion after its own gate.
- Candidate integration never moves canonical `HEAD`; task-owned delivery separately validates/fast-forwards the pinned canonical branch.
- `reconcile-head` merges clean committed HEAD history into candidate under the same gate/CAS.
- A dirty checkout is never reported as serving an immutable ref.

Automatic task workspaces (the default): Git identity checks batch `--show-toplevel` and `--git-common-dir` into one bounded subprocess, halving serial Git launches per observation without caching authority or skipping linked-workspace validation. Incomplete/error responses fail closed; verify with `tests/tasks_pkg/test_workspace_identity_latency.py` and `tests/tasks_pkg/test_workspace_delivery.py`.

- Writable canonical Git roots receive an owner/physical-task worktree before tools run.
- Two matching alternate-index captures pin dirty/untracked context; bounded retries use no writer lease. HEAD/branch movement refuses binding.
- Source HEAD/index stay intact, including unborn branches; existing registrations win. Readonly/non-Git/transient execution stays unchanged.
- Problem: completion tasks inherited a finished task's linked checkout without its registration. Decision: `completion_delivery_config` restores recorded canonical roots and readonly descendants, dropping task runtime-root metadata, before new admission.
  Old durable receipts/wakes receive the same projection without changing frozen publication bytes. Explicit roots without canonical metadata stay unchanged; adopting another task's checkout was rejected because it transfers write/publication authority.

Opt-in linear mode:

`concurrent writers → terminal task → immutable checkpoint → verification gate → current branch + refs/tofu/stable only after passing`

- Enable with `git config --local tofu.linearCheckpoint true`; process override
  `TOFU_LINEAR_GIT_CHECKPOINT=1` applies to every attached Git project.
- Tool dispatch never calls this checkpoint runtime or rewrites a result from its failure; checkout writer leases remain independent.
- The background commit-round worker captures the whole opted-in workspace
  after settlement; dirty first activation needs no clean-baseline admission.
- First settlement records `HEAD` in `refs/tofu/workspace-checkpoint-baseline`
  and anchors `refs/tofu/stable` before capture. Enabling alone moves no ref
  and cannot make export select an older isolated-mode stable ref.
- Alternate-index capture creates mechanical task/conversation commits under
  `refs/tofu/checkpoints/`. Branch and real index stay at the last publication
  while verification is pending, required or failed. Built-ins check conflict
  markers, syntax, JSON, JavaScript and changed-Python Ruff; semantic changes
  require the project gate. Only an unchanged passing snapshot prepares the
  index and atomically CAS-advances the branch together with `refs/tofu/stable`.
- Task metadata identifies the triggering settlement, not exclusive authorship;
  conversations may coalesce. Post-capture bytes stay dirty for a later snapshot.
- Failed/aborted tasks retain WIP refs, never advance branch/stable. Later gates
  check the entire stable-to-checkpoint delta, including earlier unverified code.
- Thread/capture/lock failure preserves settled task status and bytes; later settlement can capture them.
- After baseline activation export selects `refs/tofu/stable`; explicit
  `TOFU_EXPORT_SOURCE_REF`/`tofu.exportRef` overrides remain. Before it, use HEAD.

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

1. A writer lease plus alternate index captures tracked changes, deletions and
   non-ignored untracked files; writer HEAD, real index and bytes stay intact.
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

## Task-owned canonical delivery

Problem: candidate `merged` does not mean the user's checkout has the work; shared-root defaults
also let conversations collide. Decision: `origin.delivery` owns intent; automatic registrations
use `executionMode=isolated-task`, `sourceHead`, `sourceSnapshot`, and `sourceRef` as their baseline.
Only writable configured source roots authorize creation. Structured code paths rebind before approval/execution; namespaces freeze under physical task ID and cross-checkout shell writes refuse; runtime/data paths retain their declared store; this is cooperation, not a filesystem sandbox.

Terminal intent precedes evidence release. Failed/cancelled/stopped outcomes revoke publication;
Checkpoint/undo evidence remains. Background commands and child agents fence capture until completion; command restart reattachment retains task ID. Automatic tasks publish only baseline-to-checkpoint
changes: three-way apply the complete binary patch to a snapshot of current canonical bytes, run gates outside
canonical writer admission, then recheck branch/HEAD/tree and revocation under a short checkout lease
and repository lock. The scratch index may change; canonical index/HEAD and unrelated WIP stay intact. Publication
preflights the resolved source-to-target patch, preserving compatible same-file changes; real conflicts refuse all publication. An immutable delivery-checkpoint ref plus the pre-mutation receipt makes lost-final-
receipt recovery idempotent even after unrelated edits. Candidate/stable refs do not move in this mode.

Explicit registered integration retains candidate merge followed by gated clean-HEAD fast-forward
adoption. Dirty/busy/changed targets or conflicts remain blocked; gate failure remains visible.

Adoption captures immutable inputs under short admission, releases both canonical checkout and repository locks before merge/gates, then reacquires admission to recheck checkpoint, receipt/revocation, branch, HEAD, dirty state and presence. Passing stale validation never publishes.
Source checkpoint capture/submission retains consistency protection, but never wraps automatic delta validation. Immutable evidence plus final revalidation—not verification lifetime—is the publication fence.
Discard shares the cross-process publication admission. Source-busy handling rereads receipts there and cannot reopen `delivered`/`preserved` as retry debt. No-change cleanup scans run under source exclusion outside canonical repository admission; final identity/terminal checks and controlled Git administration remain serialized.
Alternatives: removing every lock weakens undo/publication; isolating from committed HEAD drops user
context; whole-tree delivery would overwrite or commit unrelated WIP. Only `delivered` confirms delivery.

Writer admission covers file-tool preimages through undo and identified shell writes through the supervised worker lifetime. **Problem:** checkout-wide exclusion serializes unrelated edits and incorrectly treats interpreter heredoc input as shell mutation.
**Decision:** ordinary runtime/test/build commands and hardened read-only Git have no implicit checkout lease; a complete literal interpreter heredoc is data input, not a filesystem redirect. Its body cannot hide subsequent shell commands, substitutions or output redirections. This is scheduling, not a promise that arbitrary interpreter code is read-only or an immutable source snapshot.
Known file-tool and bounded, explicit regular-file shell writes use canonical-path exclusion, so unrelated files overlap while the same target still waits/fails visibly. File holders retain a shared checkout barrier through child lifetime; Git tree/ref mutation, capture/publication and ambiguous filesystem write scopes retain checkout-wide exclusion. Ordinary reads require no exclusion; callers needing stable whole-checkout evidence explicitly request `workspace_read_lease`.
**Explicit shell data targets — Problem:** mixed diagnostic commands regained checkout-wide exclusion through `export`, quoted `cat` heredocs, external `mkdir` targets and harmless status/count formatting, even when no declared output belonged to that checkout. **Decision:** scalar-only `export` shares the setup projection; complete quoted Python/node/cat stdin bodies are masked only for admission, retaining header output redirects and every real shell tail. Literal `mkdir` operands claim their actual target checkout trees, not the reading cwd. A single regular output leaf may have missing ancestors (an earlier `mkdir` may create them); existing symlink/hardlink/special-file/metadata targets still widen or refuse. Echo/printf argument scalars and static read-only grep substitutions do not invent a write scope; a strictly bounded single sed substitution without in-place, script-file, execution or write syntax is a read filter. Unsupported grammar retains conservative admission, including comments/grouping in projected grep substitutions, legacy backticks across physical lines, and `mkdir` parent traversal (which claims every intermediate path prefix and the starting checkout, since directories can be created before `..`, including in peer checkouts). Original execution, approval, display and frozen-input source are unchanged. **Alternatives rejected:** exemptions based on the `/tmp` spelling miss symlinked source targets; dropping all writer admission loses same-file/tree protection; extending waits leaves unrelated tasks serialized. `git checkout`, `stash`, `reset`, `clean` and ambiguous tree operations retain their existing guard and checkout barrier. These changes add no pool, owner ledger, cache or persistent-growth policy.
Canonical aliases, permission/read-only boundaries, post-wait freshness, undo admission and cancellation remain unchanged. Data-only file/copy writes retain their source-lease exemption. Registries contain only active holders; managed children inherit every relevant barrier/target FD, and unmanaged detach is refused.
**Alternatives rejected:** whole-checkout locks make unrelated work unusable; dropping all locks or ignoring stale evidence allows lost edits; pretending opaque programs are proven read-only would invent a safety guarantee. Non-Git paths and external/opaque writers remain outside cooperative checkout containment. No lock is stolen or deleted.
Resource evidence: one file claim owns two descriptors (SH checkout + EX target); a second same-thread target reuses the barrier, for three total. A 100-cycle local temporary-volume probe (10 warmups) measured whole-checkout median/p95 0.107/0.123 ms and file claim 0.190/0.204 ms, 9,295 peak traced bytes, zero FD delta and empty registries after release. This is local overhead, not FUSE timing/throughput; event-synchronized cross-process tests establish concurrency and inherited-child fencing.
Specifications: `test_file_scoped_write_lease.py`, `test_command_writer_scope.py`, `test_shell_data_write_admission.py`, `test_file_scoped_publication.py`, `test_file_scoped_upload_target.py`.
Contention specifications: static setup and recorded-background source binding (`test_command_setup_admission.py`), validation/publication races (`test_delivery_validation_concurrency.py`), and bounded holder attribution/reclamation (`test_workspace_lease_diagnostics.py`). The five-second admission budget stays unchanged: remove false contention, do not hide it with timeout growth or model retries.
Ignored dependency preparation uses SH checkout + exclusive runtime-setup identity, not exclusive source ownership. `test_workspace_runtime_admission.py` covers both admission orders, cross-process exclusion, inherited children, cancelled wait reclamation and actual edits during slow setup.
A local 100-sample probe (10 warmups, tracemalloc enabled) measured file median/p95 0.729/0.790 ms and setup 0.581/0.678 ms, 20,292 peak traced bytes, zero FD delta and empty registries after release. Diagnostics hold only bounded identifiers/timestamps in active entries; no new pool, daemon, durable owner ledger or recovery loop. This is local overhead/lifecycle evidence, not FUSE throughput.
Presence is conservative adoption evidence, not authority; automatic source isolation requires task registration.

## Gate policy

Candidate gates validate the exact publication tree: `git diff --check`;
forbidden Git internals, dependency/cache/runtime/managed-worktree paths,
bytecode and `node_modules`; changed Python/JavaScript/module/JSON syntax;
and `TOFU_INTEGRATION_TEST_CMD` for application/configuration changes.

Explicit `TOFU_INTEGRATION_TEST_CMD` wins. Automatic task delivery can otherwise rerun one host-recorded,
successful declared pytest/unittest/npm test/check/build/node-test/cargo-test invocation, using its runtime, rebased
workspace paths and its original subdirectory, on the exact combined tree. Arbitrary shell programs,
credential arguments, failures, timeout/background results and another task/owner's evidence do not qualify.
Missing semantic verification refuses publication; syntax-only evidence is not success. Stable promotion
uses `TOFU_INTEGRATION_STABLE_TEST_CMD` or the configured candidate gate. Gates execute without a shell.

Linear mode shares forbidden-path/semantic-suffix policy, without a scratch
worktree. It syntax-checks the matching checkout, then runs
`TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD` (fallback `TOFU_INTEGRATION_TEST_CMD`)
outside its Git lock: `{base}` is stable, `{target}` the checkpoint. HEAD and
all non-ignored bytes must match before/after; writes or gate rewrites stay
dirty, invalidate promotion and never block tools through this checkpoint path.

## Cost bounds and operations

Idle polls back off through 3, 6, 12, 24, 48, then 60 seconds. Durable local
submit/retry/intent wakes the lifecycle-owned worker; API-only replicas cannot
become Git executors. Storage failures use a separate 5..30-second circuit delay.
Shutdown interrupts waits, joins the exact owner and retains a timed-out owner. Each tick reads one `integration.workspace.delivery_page` (64 rows, ID cursor;
backend cap 128 plus lookahead), rereads owner-scoped rows and retries pending/blocked
receipts. Orphan `working` registrations use owner-scoped durable task verdicts; any live
carrier is left alone, replay outages retain retry debt and do not stop other owners.
Missing or incomplete durable success fails closed. The cursor wraps/resets on restart;
cold rows cause reads, not heartbeat writes. This bounds each page, not total
history-scan cost. Identical receipts are not rewritten. SQL/native checkout claims and backend limits live in [`STORAGE_TOFUDB.md`](../STORAGE_TOFUDB.md).

No-change successful tasks reclaim source clones; task changes, conflicts, failures and durable artifacts retain recovery/undo workspaces. Python gets a lazy private prefix with readable source packages;
pip installs stay local and new packages retain their workspace. Node package links reuse source dependencies inside a private ignored directory.
Recorded bridge directories and Python/test caches are reconstructible; source dependencies are never deleted.
No new worker/queue is added. A private 1,000-file × 1 KiB measurement took 0.262s to create and 0.205s to settle/reclaim a no-change workspace, retaining zero clones; this is a fixture, not a latency cap.
Git objects/checkpoint refs and owner-scoped receipts retain evidence after clone reclamation.

Git scans bound active/problem rows; terminal history uses stored metadata. Status is cached with mutation invalidation; event detail is bounded.
Unregistered/prunable worktree counts remain visible.

Linear mode keeps one checkout/dependency tree/cache/watcher set. Its separate
Git-operation lock defaults to 30 seconds (cap 10 minutes); timeout returns
`deferred` without changing bytes or tool execution. This OS lock releases on
process death, covers alternate-index capture and atomic ref/index updates,
not model work or verification. One common-dir lock file follows repository
lifecycle; owner/task checkpoint refs reuse Git objects, not copied trees.

Authenticated owner-scoped `/api/v1/project/integration/` routes expose status,
create/register, reconcile-head, checkpoint, submit, retry, discard, promote and
prune. UI actions mirror the state table and separately confirm divergence.

## Invariants

- Isolation failure is fail-closed; no shared-tree fallback exists.
- Linear mode is opt-in and never controls project-tool admission; independent
  checkout leases serialize cooperating writers, not model execution.
- Checkpoints/submissions are immutable; candidate/stable updates are atomic and gated.
- Conflicts/gate failures preserve evidence and never consume another model turn automatically.
- Work-item `changedPaths` selects automatic Checkers; overlap advisories never
  become Integration ownership or durable Board state.
- Canonical HEAD and terminal outcome are never changed by clone cleanup; durable receipts remain.
- Linear commits never merge, stash, reset, checkout, clean, or push; stable
  promotion is a compare-and-swap ref move over a verified descendant whose
  canonical checkout stayed unchanged throughout verification.

## Change routing and tests

Run workspace-delivery/command-lease tests, then integration control/delivery/revocation, background handoff, Git guard, spawn ownership and project-write atomicity/freshness. Verify API/frontend Project Brain contracts for public projections; linear/export suites for ref policy.

Status scan demand — Problem: live workspace evidence was limited to eight rows and a four-thread pool, leaving older workspaces unscanned. Decision: every owner-selected live/problem workspace starts an independent scan through the shared demand owner; the undocumented TOFU_INTEGRATION_STATUS_SCAN_LIMIT control is retired. Terminal history remains metadata-only, durable ordering remains, and a genuinely unstarted worker falls back to the caller without duplicate work. Failed/missing scans retain explicit unknown evidence. Alternatives: larger windows still delay/omit evidence; scanning terminal history adds no value; guessing clean from metadata weakens freshness. Rendezvous and real thread-start fault tests verify the boundary. Measurement: a controlled 32-live-row rendezvous with idle hint 1 completed in 62.18 ms, retaining input order and zero surviving scan threads.
