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
| Cooperative checkout writer lease | `lib/project_mod/workspace_lease.py` |
| Canonical-checkout task-end commit, short Git lock, stable/export source policy | `lib/linear_git_checkpoint.py` |
| Shared forbidden-path, semantic-suffix and repo-scoped gate selection | `lib/git_checkpoint_policy.py` |
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
Opt-in isolated task workspaces: Git identity checks batch `--show-toplevel` and `--git-common-dir` into one bounded subprocess, halving serial Git launches per observation without caching authority or skipping linked-workspace validation. Incomplete/error responses fail closed; verify with `tests/tasks_pkg/test_workspace_identity_latency.py` and `tests/tasks_pkg/test_workspace_delivery.py`.
- Canonical Git roots selecting `isolated` receive an owner/physical-task worktree before tools run.
- Task admission pins the selected branch once and resolves its committed baseline after accepting the input tree. Two matching working-tree observations pin dirty/untracked context; subsequent HEAD/branch movement does not invalidate those bytes. Every observation refreshes the private index seed and ignore membership, including newly committed ignored files, while publication excludes borrowed dirty input changes against the accepted committed baseline. Existing owner/task registrations resolve before capture, including lost-ACK retries.
- **Problem:** bounded HEAD retries still fail under frequent concurrent commits, and rebuilding indexes/commits on every observation wastes I/O and creates unused Git objects. **Decision:** reuse one private index, compare tree IDs directly, and create at most one accepted input commit. Normal admission uses no writer lease; after three changing content observations, the existing cancellable stable-read lease and tool admission wait policy fence cooperative writers for only two captures. Git materialization and storage registration run after release. Non-cooperating content changes still refuse safely; cancellation retires the index directory without registration. **Alternatives rejected:** accepting one unchecked capture loses consistency; retrying the model/task repeats unrelated work; long checkout locks serialize healthy writers; ignoring a new HEAD index seed loses newly tracked ignored files; keeping the initial commit as publication base misattributes concurrent committed edits as borrowed WIP. Specifications: source snapshot cases in `tests/storage/test_integration_delivery.py`.
- Source HEAD/index stay intact, including unborn branches; existing registrations win. Readonly/non-Git/transient execution stays unchanged.

- **Problem:** plain `read-tree` discards stat data from the reused private index, forcing unchanged tracked content to be hashed again. **Decision:** seed with index-only single-tree merge (`read-tree -m -i`) so matching entries retain stat data while membership still follows the fresh parent. Index-only mode avoids rejecting legitimate concurrent working-tree edits as an unsafe checkout. Git still checks changed/racy entries; two equal observations and the existing churn fence remain mandatory. **Alternatives rejected:** keeping stale membership loses ignore/HEAD changes; skipping the second observation weakens input consistency; ordinary merge mode refuses concurrent dirty changes. Real-Git parity tests cover changes, deletion, ignore rules, concurrent commits, modes and symlinks in `tests/storage/test_integration_snapshot_index.py`. A controlled same-volume 150-file/150 MB benchmark of two full captures (including preflight and tree writes) reduced median time from 1.208 s to 0.716 s, about 41%; this does not predict live end-to-end latency. Startup profiling uses paired `[TaskStartupTiming]` start/complete anchors and bounded `tofu_task_startup_stage_seconds` labels. Workspace provisioning, root discovery/identity/binding, snapshot, private-index seed/add/tree, snapshot commit, materialization, lock waits and registration/persistence are distinct stages. Nested spans inherit only allowlisted log identity via a context-local scope that resets on success or failure; IDs and file contents never become metric labels. Durations are inclusive: do not sum parent workspace binding with its child stages. A start without completion identifies the blocked boundary; tests cover nested identity restoration and separate-thread isolation.
- **Problem:** holding repository admission across a slow checkout serialized unrelated tasks and made lock waits uncancellable. **Decision:** a cross-process destination lifecycle lock covers capture, detached metadata creation, private checkout, registration ACK reconciliation and rollback. Only shared input-ref updates and `worktree add --no-checkout` take repository admission; private checkout never holds it. Both lock waits poll cancellation every 50 ms. Same-task retries recheck registration before capture; ambiguous registration/child retirement retains bytes. Discard takes destination admission before publication admission; optional cleanup skips an active destination without waiting (including when already under repository admission), avoiding lock-order inversion. Persistent lock inodes are never unlinked while waiters may reference them. **Alternatives rejected:** simply dropping the repo lock races same-task retries/cleanup; keeping it through checkout throttles independent tasks; background retries duplicate ownership. Tests in `test_integration_delivery.py` hold one real-Git checkout at a barrier and require another task to finish before release, with same-task retry/discard, cancellable destination/repo waits and lost-ACK/WIP checks. No executor, queue, waiter thread or in-memory lock registry is added; one bounded lock FD is held per active admission, plus the brief repo-lock FD.
- **Problem:** a fixed 90-second `worktree add` wall-clock deadline kills healthy large/slow-volume checkouts before worker entry. **Decision:** controlled creation installs detached metadata with `--no-checkout`, then force-checks out only that fresh, unregistered destination with explicit progress (also on old Git). `lib/project_mod/worktree_process.py` drains one bounded 8 KiB diagnostic tail; increasing completed-file counts with a fixed positive total renew a 90-second stall window. Noise/repeated counts do not renew it. Cancellation is observed during materialization; POSIX groups retire through the existing process-group owner before rollback. Hooks are disabled per invocation, not in repository configuration. No Git write is replayed and no shared-checkout fallback exists. Existing/ambiguous registrations survive; cleanup errors remain secondary evidence rather than masking the startup cause. Materialization timing is logged without contents. `worktree_checkout_policy.checkout_worker_config` defaults fresh checkout concurrency from the shared cached launch CPU/memory probe: at most effective CPUs and one worker per 128 MiB of available/capacity memory (64 MiB allowance with half reserved; not an RSS guarantee), minimum one. It reads effective configuration in the new destination, preserving explicit `checkout.workers`, conditional/worktree config and Git's small-tree threshold; it never persists config or moves durable WIP to ephemeral storage. Probe failure is lean; config-read failure leaves Git policy unchanged. Children exist only during the supervised checkout and share its process-group cancellation/retirement. A controlled 1,200-file FUSE benchmark measured serial checkout 3.59–3.87 s versus 0.89–0.95 s at the host's 63-CPU default; this is a phase benchmark, not a production end-to-end latency claim. Real-Git tests in `test_worktree_checkout_policy.py` check actual worker spawning, content/mode/symlink parity, hooks and the small-tree fast path. **Alternatives rejected:** increasing a fixed deadline still misclassifies large trees; ephemeral-volume relocation risks WIP/restart durability; model retries replay admission instead of fixing it. Tests: `test_git_worktree_process.py` and admission cases in `test_integration_delivery.py`.
- Problem: completion tasks inherited a finished task's linked checkout without its registration. Decision: `completion_delivery_config` restores recorded canonical roots and readonly descendants, dropping task runtime-root metadata, before new admission.
  Old durable receipts/wakes receive the same projection without changing frozen publication bytes. Explicit roots without canonical metadata stay unchanged; adopting another task's checkout was rejected because it transfers write/publication authority.

Capture-only probe: 30 alternating samples per arm after one warmup, synthetic 100-file repository with 10 dirty files; storage/materialization replaced by no-ops. Old → new: Git calls 24 → 22, commit-tree calls 2 → 1; local median/p95 248.131/1056.250 → 233.138/321.722 ms; FUSE 957.872/3023.893 → 800.289/2039.019 ms. Peak traced bytes local 150,067 → 151,066 and FUSE 150,233 → 151,704; FD delta zero in both arms. One temporary index directory retires per capture; no new pool, persistent waiter or owner ledger. This loaded-host observation excludes checkout time and is not a throughput/SLO claim.
Checkout-overlap probe: real Git, 100 files / 102,400 bytes, in-memory registration seam; while one checkout stayed deliberately blocked, the independent task registered in 265.010 ms. After release both finished in 507.949 ms total, with two registrations and three persistent control-lock inodes. This proves admission overlap, not large-volume checkout speed or a production latency SLO.
Opt-in linear mode: `concurrent writers → terminal task → immutable checkpoint → verification gate → current branch + refs/tofu/stable only after passing`

- Enable with `git config --local tofu.linearCheckpoint true`; process override `TOFU_LINEAR_GIT_CHECKPOINT=1` applies to every attached Git project.
- Linear eligibility controls terminal publication, never execution. Candidate/stable history no longer forces a startup copy; explicit isolation applies even if linear is eligible. Publication failures preserve refs/config/WIP.
- Tool dispatch never calls this checkpoint runtime or rewrites a result from its failure; checkout writer leases remain independent.
- The background commit-round worker captures the whole opted-in workspace after settlement; dirty first activation needs no clean-baseline admission.
- First settlement records `HEAD` in `refs/tofu/workspace-checkpoint-baseline` and anchors `refs/tofu/stable` before capture. Enabling alone moves no ref and cannot make export select an older isolated-mode stable ref.
- Alternate-index capture creates mechanical task/conversation commits under
  `refs/tofu/checkpoints/`. Branch and real index stay at the last publication
  while verification is pending, required or failed. Built-ins check conflict
  markers, syntax, JSON, JavaScript and changed-Python Ruff; semantic changes
  require the project gate. Only an unchanged passing snapshot prepares the
  index and atomically CAS-advances the branch together with `refs/tofu/stable`.
- Task metadata identifies the triggering settlement, not exclusive authorship; conversations may coalesce. Post-capture bytes stay dirty for a later snapshot.
- Confirmed isolated working-tree delivery enters this same opt-in checkpoint through the commit round or recovery worker; pending delivery stays excluded.
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

Explicit registered integration retains candidate merge followed by gated clean-HEAD fast-forward
adoption. Dirty/busy/changed targets or conflicts remain blocked; gate failure remains visible.

Explicit adoption validates immutable inputs outside canonical admission, then rechecks authority and target freshness. Automatic merging rechecks task paths and terminal authority; commits and gates do not govern workspace delivery.
Discard shares the cross-process publication admission. Source-busy handling rereads receipts there and cannot reopen `delivered`/`preserved` as retry debt. No-change cleanup scans run under source exclusion outside canonical repository admission; final identity/terminal checks and controlled Git administration remain serialized.
Alternatives: removing locks loses updates; omitting dirty context loses inputs; whole-tree publication commits others' WIP. Only the durable receiver confirms workspace delivery.
Writer admission covers file-tool preimages through undo and identified shell writes through the supervised worker lifetime. **Problem:** checkout-wide exclusion serializes unrelated edits and incorrectly treats interpreter heredoc input as shell mutation.
**Decision:** ordinary runtime/test/build commands and hardened read-only Git have no implicit checkout lease; a complete literal interpreter heredoc is data input, not a filesystem redirect. Its body cannot hide subsequent shell commands, substitutions or output redirections. This is scheduling, not a promise that arbitrary interpreter code is read-only or an immutable source snapshot.
Known file-tool and bounded regular-file shell writes use canonical target transactions, including non-Git workspaces: unrelated files overlap, while the same target waits/fails visibly. Git metadata-only work does not block source edits; native same-checkout transformations and capture/publication exclude source edits there, while destructive target trees also fence nested checkouts and runtime/data files. Ordinary parent/peer native checkout work no longer blocks nested Edit. Inline Git includes/helpers retain conservative tree admission, and output redirects union their target effects with Git effects rather than replacing them. Compound claims stay held through the entire supervised command, not just the mutating segment; separate short mutations from long tests when semantics permit. Exact effect classification and lifecycle fencing live in the [Git workspace guard](../runbooks/GIT_WORKSPACE_GUARD.md#lease-api-and-identity). Stable evidence explicitly requests `workspace_read_lease`.
**Explicit shell data targets — Problem:** mixed diagnostic commands regained checkout-wide exclusion through `export`, quoted `cat` heredocs, external `mkdir` targets and harmless status/count formatting, even when no declared output belonged to that checkout. **Decision:** scalar-only `export` shares the setup projection; complete quoted Python/node/cat stdin bodies are masked only for admission, retaining header output redirects and every real shell tail. Literal `mkdir` operands claim their actual target checkout trees, not the reading cwd. A single regular output leaf may have missing ancestors (an earlier `mkdir` may create them); existing symlink/hardlink/special-file/metadata targets still widen or refuse. Echo/printf argument scalars and static read-only grep substitutions do not invent a write scope; a strictly bounded single sed substitution without in-place, script-file, execution or write syntax is a read filter. Unsupported grammar retains conservative admission, including comments/grouping in projected grep substitutions, legacy backticks across physical lines, and `mkdir` parent traversal (which claims every intermediate path prefix and the starting checkout, since directories can be created before `..`, including in peer checkouts). Original execution, approval, display and frozen-input source are unchanged. **Alternatives rejected:** exemptions based on the `/tmp` spelling miss symlinked source targets; dropping all writer admission loses same-file/tree protection; extending waits leaves unrelated tasks serialized. `git checkout`, `stash`, `reset`, `clean` and ambiguous tree operations retain their existing guard and checkout barrier. These changes add no pool, owner ledger or cache; persistent coordination-key growth is described below.
Multi-target callers use `workspace_claims_lease`: expand/merge the complete physical set and acquire tree, metadata, checkout, then file keys before validation or preimages; `lease_claim_order` alone cannot order hidden barriers. Metadata keys use the neutral namespace and canonical common-dir identity, independent of checkout aliases. Canonical aliases, permission/read-only boundaries, post-wait freshness, undo admission and cancellation remain unchanged. Runtime/data file/copy writes retain their source-lease exemption but use neutral canonical-file exclusion and tree fences. Registries contain only active holders; managed children inherit every relevant barrier/target FD until actual descriptor quiescence, and unmanaged detach is refused.
**Alternatives rejected:** blanket checkout locks make unrelated work unusable; dropping same-file transactions or freshness evidence loses edits; pretending opaque programs are read-only invents a guarantee. Non-Git whole-checkout scopes remain no-ops, but canonical file transactions and explicit destructive-tree effects are protected. External/opaque writers remain non-cooperating; no lock is stolen or deleted.
**Owned-write observation — Problem:** a formatter launched by the same task changes bytes outside file-tool recording, while pure append was incorrectly rejected as stale replacement. **Decision:** append reads the current preimage inside its exact file transaction and preserves the prefix without asking the model to reread. Replacement/anchor edits retain stale-token checks. Proven literal Ruff write targets use file claims; a matched existing content observation is captured after admission and CAS-advanced only after supervised descendants retire, before release. No token is created for unobserved files; pre-stale, interleaved, large-file metadata-only and mixed/opaque effects do not gain ownership. Supervised completion advances a content-free cache generation before observations: cached project reads bind the generation from execution start, so old and late in-flight receipts cannot masquerade as fresh after background completion. **Alternatives rejected:** resetting tokens or disabling the guard loses sibling changes; refreshing every disk difference misattributes external writes; increasing lock waits hides false contention. Specifications: `test_append_write_freshness.py`, `test_owned_write_freshness.py`, and formatter command admission/completion tests.
Paired effect-admission resource evidence: 30 samples after 5 warmups, synthetic local/nested-FUSE checkouts, immutable baseline `56db0f885` versus this effect model. Root/file median cost was 1.307/1.459 ms locally and 9.816/10.737 ms on FUSE, versus 0.236/0.368 and 8.500/9.404 ms; no samples were blocked. First-claim descriptors/persistent key files were 12/14 locally and 32/34 at the deeper FUSE hierarchy (baseline 1/2 and 3/4). Keys were zero-byte; every release left empty registries/descriptors. Keys persist per historical canonical path and consume inodes, not resident holder state: never unlink them while any Tofu process may own/open a key; namespace reclamation requires all processes using it drained. This is admission/lifecycle evidence, not throughput/SLO evidence or a claim that uncontended admission got faster. Cross-process tests establish actual exclusion and child fencing.
Specifications: `test_file_scoped_write_lease.py`, `test_command_writer_scope.py`, `test_shell_data_write_admission.py`, `test_file_scoped_publication.py`, `test_file_scoped_upload_target.py`.
Contention specifications: static setup and recorded-background source binding (`test_command_setup_admission.py`), validation/publication races (`test_delivery_validation_concurrency.py`), and bounded holder attribution/reclamation (`test_workspace_lease_diagnostics.py`). Admission still has no default wait quota; remove false contention instead of hiding it with timeout growth or model retries.
**Proven readers and visible waits — Problem:** a compound diagnostic `cd repo && ls -d evals benchmarks 2>/dev/null; …` claimed the entire checkout exclusively and then spent 21 minutes waiting behind another writer with no process, no output and no log line; handoff refused because it requires a live PID. **Decision:** a shell segment whose only otherwise-unproven syntax is a trailing discarded output redirect (`/dev/null`, descriptor dup) keeps its proven-reader status, and named output files still claim their file. Ledger-free proofs extend to `sed` print/delete-only programs and `git grep` (its pager option stays admitted). The pre-spawn admission phase is marked on the detachable task, so both the elapsed-time rule and the operator's move-to-background apply while the command is still waiting, and waits longer than two seconds are logged with waiter and actual holder identity. **Alternatives rejected:** a fixed wait quota (ends legitimate long acquisitions and hides real exclusion); treating every redirect as a write (the observed defect); treating heredoc bodies or input redirects as discardable (executable shell source); claiming admission-phase handoff only after spawn (leaves the round un-settleable). Substitutions, comments, input redirects and unknown flags retain conservative admission. Specifications: `test_readonly_admission_scope.py`, `test_command_outer_writer_lease.py`.
Ignored dependency preparation uses SH checkout + exclusive runtime-setup identity, not exclusive source ownership. `test_workspace_runtime_admission.py` covers both admission orders, cross-process exclusion, inherited children, cancelled wait reclamation and actual edits during slow setup.
An earlier-candidate local 100-sample probe (10 warmups, tracemalloc enabled; not final regenerated results) measured file median/p95 0.729/0.790 ms and setup 0.581/0.678 ms, 20,292 peak traced bytes, zero FD delta and empty registries after release. Diagnostics hold only bounded identifiers/timestamps in active entries; no new pool, daemon, durable owner ledger or recovery loop. This is local overhead/lifecycle evidence, not FUSE throughput.
Presence is conservative adoption evidence, not authority; automatic source isolation requires task registration.

Git guard segment diagnostics, parameter semantics and metadata-only effects are specified in the [guard contract](../runbooks/GIT_WORKSPACE_GUARD.md#parameter-and-effect-evidence).
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

Successful tasks reclaim source clones whose final bytes/index are pinned in recoverable checkpoints; uncaptured changes, conflicts, failures and durable artifacts retain recovery/undo workspaces. Python gets a lazy private prefix with readable source packages;
pip installs stay local and new packages retain their workspace. Node package links reuse source dependencies inside a private ignored directory.
Recorded bridge directories and Python/test caches are reconstructible; source dependencies are never deleted.
No new worker/queue is added. A private 1,000-file × 1 KiB measurement took 0.262s to create and 0.205s to settle/reclaim a no-change workspace, retaining zero clones; this is a fixture, not a latency cap.
Git objects/checkpoint refs and owner-scoped receipts retain evidence after clone reclamation. One origin acknowledgement records source retirement and recovered opt-in checkpointing; missing ACKs and transient contention use the existing worker. Acknowledged cold rows require no filesystem scans or heartbeat writes.

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
- Linear mode is opt-in and never controls project-tool admission; independent checkout leases serialize cooperating writers, not model execution.
- Checkpoints/submissions are immutable; candidate/stable updates are atomic and gated.
- Conflicts/gate failures preserve evidence and never consume another model turn automatically.
- Work-item `changedPaths` selects automatic Checkers; overlap advisories never
  become Integration ownership or durable Board state.
- Canonical HEAD and terminal outcome are never changed by clone cleanup; durable receipts remain.
- Linear commits never merge, stash, reset, checkout, clean, or push; stable promotion is a compare-and-swap ref move over a verified descendant whose
  canonical checkout stayed unchanged throughout verification.

## Change routing and tests

Run workspace-delivery/command-lease tests, then integration control/delivery/revocation, background handoff, Git guard, spawn ownership and project-write atomicity/freshness. Verify API/frontend Project Brain contracts for public projections; linear/export suites for ref policy.

Status scan demand — Problem: live workspace evidence was limited to eight rows and a four-thread pool, leaving older workspaces unscanned. Decision: every owner-selected live/problem workspace starts an independent scan through the shared demand owner; the undocumented TOFU_INTEGRATION_STATUS_SCAN_LIMIT control is retired. Terminal history remains metadata-only, durable ordering remains, and a genuinely unstarted worker falls back to the caller without duplicate work. Failed/missing scans retain explicit unknown evidence. Alternatives: larger windows still delay/omit evidence; scanning terminal history adds no value; guessing clean from metadata weakens freshness. Rendezvous and real thread-start fault tests verify the boundary. Measurement: a controlled 32-live-row rendezvous with idle hint 1 completed in 62.18 ms, retaining input order and zero surviving scan threads.
