# Git workspace guard and cooperative writer lease

Domain map: [`project integration`](../modules/project_integration.md).

## Problem

A shared checkout has concurrent file writers, commands, and task-end checkpoint
publication. Presence and an approval/ack are not exclusion: an acknowledged
`checkout -B`, `stash apply`, or index rewrite can still destroy a peer's work.
Git worktrees have distinct working files/indexes but share a common Git directory.

A long test/build/read command must not become checkout ownership merely because
its effects are opaque. Giving every command an exclusive lease serialized all
shell activity; giving opaque commands an automatic read lease still blocked
writers for the entire test/build lifetime. Opaque programs can also write
artifacts, so a read lease is not evidence that they are stable readers.

## Decision

Keep three independent boundaries: existing readonly/approval authorization,
parameter-sensitive Git presence policy, and cooperative checkout exclusion.
Acquire writer admission for identified checkout mutations and uncertain shell
or utility shapes, not every shell invocation. Literal opaque test/build/read
commands run without a checkout lease; callers needing a stable cooperative read
explicitly use `workspace_read_lease`.
No second owner database or expiry-based lock stealing is introduced.

### Git classification and presence

`command_analysis.py` owns pure risk/read-effect classification.
`git_command_scope.py` is the single target resolver used by approval and
writer admission; `git_inspection_execution.py` owns execution preparation:

| Class | Examples | Policy |
|---|---|---|
| `readonly` | status/log/diff/show without output writes; branch/tag listing; stash list/show | Existing read authorization |
| `safe_write` | additive add/ordinary commit (appends history), ordinary apply/--cached, branch/tag creation, ordinary fetch/push, normal merge/cherry-pick/revert, checkout -b/switch -c without start point, branch -d/--delete | Existing write authorization/receipt, **plus writer lease** |
| `tree_destroying` | checkout -B/--orphan, switch -C/--force-create, branch -D/-M/-f and any bundled-force short flags (-df/-fd/-vd), tag -d/-f, commit --amend, update-ref/symbolic-ref mutation, force push/fetch, stash mutations, reset/restore/clean/rebase, integration abort/forced/unknown options | Presence-aware refusal plus writer lease if authorized |
| `None` | ambiguous config getter/setter, unknown options/commands, opaque shell segments | Existing attended gate, never inferred readonly |


Ordinary `commit` appends history and is already `safe_write`; rewriting history
with `commit --amend` remains `tree_destroying`. Ordinary `git apply` and
`git apply --cached` are `safe_write`, not checkout-wide destruction. Pure
`apply --check/--stat/--numstat/--summary` is `readonly`; explicit `--apply`
overrides those report-only modes and restores the write classification.
`apply --3way/--reject/--unsafe-paths` remains conservatively `tree_destroying`;
unknown options return `None`, never inferred safe.
Normal `merge`, `cherry-pick`, `revert` and maintained ordinary/continue flags
are `safe_write`: Git validates tracked/untracked overlap and reports conflicts;
writer admission protects the actual checkout/index. Mere project presence is
irrelevant to this path and is not probed. Abort/quit/autostash, forced/strategy
and unknown integration options retain destructive policy. Ordinary integration
still needs write authorization; `safe_write` is not an exclusion exemption.
Across the full command chain, `tree_destroying` outranks opaque `None`, and
opaque `None` outranks `safe_write` (which outranks `readonly`). A known
high-risk Git segment therefore keeps presence policy even with an opaque
segment; a safe write combined with opaque code still reaches the existing gate.
Risk and authorization are separate: high-risk plus opaque/unknown code may be
refused for presence, but even when solo it cannot mint an automatic Git receipt
for the whole chain. Dynamic shell arguments (including command substitutions)
never borrow Git report-only authorization. Report modes do not expand the
separate hardened status/log lease-free grammar.
Ordinary apply preflights the patch and refuses failed hunks without partial
application by default; `--reject`, `--3way`, `--unsafe-paths` and
`--allow-overlap` do not receive this ordinary-write treatment.
`--build-fake-ancestor` can write an index even with report flags and retains
the existing approval path. Option values and `--` pathspecs are not options.
Normal commit still reads the selected index/pathspec: its additive-history
classification does not prove the staged changes belong to one task.

Non-forced `worktree add -b <new-branch> <path> [start-point]` and
`worktree add --detach <path> [start-point]` are additive `safe_write`;
`worktree list [--porcelain [-z]]` is read-only. Force/reset and unknown
worktree administration shapes retain the existing approval gate. For a
literal chain that creates a not-yet-existing checkout and then runs Git with
`-C` at that exact destination, presence attribution uses the future checkout
path, not its existing parent repository. Approval precedes creation, so an
upward `.git` search would falsely attribute isolated `cherry-pick` to the
shared parent. Existing destinations resolve normally; subsequent destructive
operations on the parent still check parent presence. Git's own destination/ref
checks and cooperative writer admission remain in force.

`branch -d`/`--delete` is `safe_write` because Git itself refuses the delete
when the branch is unmerged (against HEAD or its upstream) or checked out in
any worktree — a successful plain delete can neither orphan commits nor break
a sibling's worktree, so active peers are irrelevant. Only the narrow shape
(one delete flag plus plain branch names) qualifies; `-D` is exactly `-df`, so
any short-flag bundle containing `f` stays `tree_destroying`.

Force deletion gets a runtime second opinion instead of a static verdict:
`git_guard.force_delete_targets_merged` runs read-only probes
(`rev-parse`/`merge-base --is-ancestor` against HEAD, then the branch's
upstream — the same preconditions `-d` enforces) when a tree_destroying
command is entirely `git branch -D` shapes on a single resolved checkout.
Every named branch provably merged downgrades the command to the safe-write
receipt path even with active peers; any unmerged/unknown branch, mixed
segment, opaque shape, or probe failure keeps the presence-aware refusal.
Probes are capped (`_PROBE_TIMEOUT_SEC`) and never raise into dispatch.

Active project presence cannot be overridden with `TOFU_GIT_ACK`: legacy token
consumption always returns false. Refusals explicitly identify the conservative
**project-presence policy**, not confirmed writers, an actual lock/lease conflict,
or proven file overlap. They show the resolved checkout roots and full `convId`
and `agentId` (main agent when absent), even when a title exists. Per-peer root
attribution comes from the dispatch probe, not another owner database. Older
three-argument refusal callers remain supported with an unspecified-root label.
Reported files/status are incomplete activity hints: missing or disjoint file
lists do not authorize `reset` or another high-risk operation. Refusals ask the
executor to wait for presence to settle or use a separately authorized isolated
worktree; they do **not** recommend committing or stashing shared work to clear
presence. No new nonce is issued by dispatch. Legacy token functions remain
callable for older callers but grant no authority.

Presence remains an advisory, process-local TTL view. Guard probe failure and
`TOFU_GIT_GUARD=0` retain the existing approval fallback; neither disables the
writer lease. No-active-peer is not proof that the workspace is clean or safe.

### Worktree creation and command targets

Problem: a not-yet-created linked worktree resolves upward to the parent
checkout; leasing both the invocation cwd and the final `git -C` target also
serializes unrelated worktrees. Decision: narrow additive `worktree add -b`
or `--detach` creation uses Git's own atomic ref/registration locks and never
claims the source checkout's files/index. A nonexistent destination may be
recognized as a future checkout only across success-dependent `&&` segments.
Existing destinations and `;`, `||` or pipeline control resolve normally.
Git writer admission follows only global `-C` hops before the subcommand and
leases the final checkout; option values, messages and pathspecs cannot add
false roots. Explicit `--git-dir`, `--work-tree`, `GIT_DIR`, `GIT_WORK_TREE`
and `GIT_INDEX_FILE` participate in target ownership; setup normalization never
strips `GIT_*` assignments. Pipeline `cd` cannot move another segment's cwd;
failed `cd` followed by `;`/`||` retains both possible checkouts. Opaque code
invalidates a prospective checkout identity. Unresolved effects retain the
existing authorization/conservative writer boundary. After writer admission,
command targets are resolved again; a changed target refuses before snapshots
or execution instead of acquiring a different checkout implicitly.
Alternative rejected: turning off coordination would admit actual overlapping
checkout mutations; retaining invocation-root admission blocks isolation.

### Lease API and identity

```python
from lib.project_mod.workspace_lease import (
    WorkspaceBusyError, WorkspaceLeaseCancelled,
    workspace_read_lease, workspace_write_lease,
)

with workspace_write_lease(checkout_or_descendant_path, timeout=0.0):
    # Exclusive: read preimage, validate, mutate, finish journaling.
    ...

with workspace_read_lease(checkout_or_descendant_path, timeout=0.0):
    # Explicit stable cooperative read; not an automatic test/build lease.
    ...
```

- `timeout` is finite, 0–600 seconds; the API default is zero (fail immediately).
  An optional `cancel_event` exposes `is_set()`. Cancellation raises
  `WorkspaceLeaseCancelled`, a `WorkspaceBusyError` subclass. Busy, invalid lock
  metadata, unsupported platform, and unavailable lock storage fail closed with
  `WorkspaceBusyError`. Exceptions in the protected body are not reclassified.
- Identity is the real checkout path (symlink aliases and descendants converge).
  `.git` / `commondir` pointers resolve the Git common directory without invoking
  Git or trusting process-global Git environment overrides.
- One POSIX advisory flock per checkout lives at
  `<git-common-dir>/tofu-workspace-leases/<sha256(real-checkout-path)>.lock`.
  Different worktrees can proceed concurrently. Thread ownership controls
  reentry; other threads/processes must acquire separately.

### Task-owned execution policy

Problem: an evictable registry is not authorization; a detached task may be
idle to the cache, and basename aliases can silently shadow readonly roots.
Decision: retain collision-safe labels/access and acknowledged expansions in
the task execution view; freeze them per call before dispatch. Binding and
write guards use the same deepest declared-root policy, including realpath
readonly fences. Current namespaces outrank canonical aliases; ambiguous
aliases refuse. Children/background commands copy policy without delivery
ownership. LRU eviction affects only a projection, never the active snapshot.
Rejected: enlarging/pinning the LRU leaves all-live eviction and detached
lifetime races; blanket readonly-prefix rules disagree with writable child
admission. Snapshot work is linear in the number of explicitly registered
roots, without Git/subprocess/directory scans; memory follows task lifetime.

Host-local measurement: 16 roots, 10 warmups and 1000 scope+lookup samples
observed median **0.017 ms**, maximum **0.027 ms**; not a network-volume guarantee.

### Selective writer admission and explicit stable readers

- **Identified mutations** acquire typed writer admission: normal file-write
  tools and proven regular-file shell targets use `workspace_file_write_lease`;
  unknown/tree targets, Git mutations and whole-checkout capture/publication use
  `workspace_write_lease`. In-place utilities and redirections are still writes. Shell interpreters/wrappers, dynamic
  syntax, malformed input and uncertain mutating utilities retain writer
  admission. Parsing includes mixed/piped segments and helper-bearing Git forms;
  snapshot-readonly classification alone is not a lease exemption.
  `_segment_needs_command_writer` and `command_write_lease_paths` in
  `run_command.py` own this decision, using mutation/target analysis from
  `command_analysis.py`.
- **Opaque commands** such as pytest/make/npm/python interpreters do not acquire
  an automatic writer or reader lease when no checkout mutation is identified.
  They can overlap each other and cooperative writers, receive no automatic
  lease FDs, and make no stable-checkout claim. They may produce build artifacts;
  this is selective cooperative exclusion, not proof of filesystem readonlyness.

- **Static setup is not a mutation.** The command admission view may normalize
  a bounded, fully understood environment/executable setup without executing it.
  Literal `command -v` executable discovery and previously proven setup variables
  must not upgrade an otherwise opaque test/build to a whole-checkout writer.
  Nested/unknown substitutions, shell interpreters/eval, hidden redirections and
  actual filesystem/Git mutations retain conservative admission. Normalization
  never rewrites the executed command or its approval source.

  The view is bounded to 16 KiB/32 bindings and stops binding propagation after
  any non-setup command. Shell comments/grouping/unknown syntax never mint a
  proven executable binding. Unresolved dynamic executables are refused rather
  than borrowing a guessed managed-child lifetime. Command-owned or inherited
  `CDPATH` with a relative bare `cd` is refused before spawn; absolute or
  `./`-prefixed targets keep an inspectable cwd identity.
- **Recorded background execution** binds the exact server-generated wrapper
  to its original command through an internal typed admission scope, not model
  arguments. Record-file redirects do not make an opaque test a source writer.
  Both runner/dispatcher admission and undo target tracking use that binding;
  wrapper-byte drift refuses before execution. Background Git retains writer
  admission because its wrapper does not execute the hardened readonly rewrite.
  A refusal before spawn retains its not-started result, never synthesizes an
  exit code from absent record files.
- **Ignored runtime preparation** uses `workspace_runtime_setup_lease`: shared
  checkout barrier plus exclusive setup identity. Preparing `.venv` and
  `node_modules` can overlap unrelated source-file edits, but same-checkout
  setup, whole-checkout mutation and supervised inherited children remain fenced.
  This trusted runtime-only API grants no arbitrary directory-write authority.
- **Proven Git inspection** runs lease-free per segment, including in mixed
  query/runtime/file-write chains. The maintained local-read catalog covers
  status/log/diff/show, file/tree/ref/index/config queries, and worktree listing.
  Each admitted query disables optional index refresh, pager/fsmonitor,
  external diff/textconv and signature verification. Configuration getters do
  not receive overrides that would change the value being queried. Unknown
  options, signature atoms, shell wrappers and output writes retain admission.
  Neighbouring mutations claim their own targets; a read never upgrades a
  disjoint file write to a checkout-wide writer.
- **Legacy Git** capability is checked before the command body, once per
  executable per call, without a persistent cache. Unsupported optional-lock
  flags are removed only from hardened query segments. Index-consuming reads
  get distinct temporary index copies per source checkout; neighbouring writes
  retain the real index and environment. Pure ref/config/object queries keep
  their observable paths. Preparation failure reports not-started; body errors
  are not reclassified. No command is replayed after a write. Temporary copies
  live through supervised execution and disappear on return/error/cancellation;
  opaque commands and modern Git allocate none.
  Host-local measurement (100 scope samples, 10 legacy preparations): scope
  median **0.195 ms**, maximum **0.214 ms**; two-query legacy preparation median
  **6.712 ms**, maximum **8.344 ms**, one **988,359-byte** index copy for the shared
  source checkout. Probe timeout is 2 seconds; these are preparation-only
  measurements, not command runtime or a network-volume guarantee. No resident
  queue/cache/pool or persistent artifact is added; transient disk follows the
  source indexes and distinct query checkouts actually used by the call. Specifications:
  `tests/project_mod/test_readonly_compound_lease.py` and
  `tests/project_mod/test_git_inspection_execution.py`.
- **Explicit stable readers** retain `workspace_read_lease`. In-process readers
  can coexist; a writer excludes them and they exclude a writer. A writer may
  reenter read without downgrading. The sole reader may upgrade locally to
  writer without releasing the OS lock; a second concurrent reader blocks an
  upgrade. No test/build command is automatically placed in this reader gate.

POSIX `flock` is **open-file-description scoped**, not thread scoped. The
implementation keeps one `LOCK_EX` flock per checkout for either API mode and
uses an in-process `Condition` plus writer-thread/reader counts for admission.
Explicit readers in different processes therefore serialize, even with each
other; the API is not a cross-process `LOCK_SH` reader-parallelism guarantee.
Busy messages identify a known local **writer** or **reader**; a conflicting
process flock reports **holder type unknown**, never inventing its mode.

Local conflicts include the actual holder PID/thread, elapsed hold time and,
when available from the existing tool admission scope, bounded tool/task/
conversation identifiers. Diagnostics live only in active lease entries and
are removed on release/fork; they never retain command arguments, task bodies,
credentials or a durable ownership ledger. Cross-process conflicts still report
unknown ownership; a lock-file inode is not holder evidence.

### Wait, freshness, descriptor, and journal lifecycle

- Tool admission waits at most `TOFU_WORKSPACE_TOOL_WAIT_SECONDS` (default 5;
  finite 0–600 seconds), with one shared deadline across the call's target
  leases. Task abort/runtime cancellation is checked during acquisition and
  again before admitting the body. Contended waits probe at most every 50 ms;
  uncontended paths do not sleep. Commands distinguish `workspace-busy` from
  `workspace-lease-cancelled` and state that the command was not started.
- File-write/edit tools acquire all resolved target checkouts before preimage
  reads, then recheck any known conversation freshness token **after acquisition**.
  A target changed while waiting refuses before any edits: re-read and reconcile,
  not blind retry. Missing tokens retain the existing freshness policy. These
  tools currently represent admission cancellation with their workspace-busy
  result shape; the single-write dispatcher renders the detail as `Write failed`.
- Writer admission remains held through file replacement, persistence and undo
  journaling. Existing readonly/path/outside-workspace checks and the separate
  session-journal admission boundary remain; the lease does not replace either.
- Identified command mutations hold admission across dispatch preimage snapshots,
  in-process copy, supervised worker execution, postimage snapshots and journaling,
  not just `Popen`. Nested runner calls reenter the same owner. Pipe and PTY
  subprocesses inherit the live lease FDs. Opaque commands do not join this
  lifetime merely because they use the same runner.
- Release is **close-only, never `LOCK_UN`**: an inherited child descriptor
  continues excluding until its final close, even after parent release. Crash
  cleanup follows OS descriptor ownership. After `fork`, the child closes
  inherited active/pending registry descriptors and resets process-local gates;
  inherited context teardown cannot close a reused FD or erase new child
  ownership. An exec child can inherit deliberately passed FDs without inheriting
  a Python registry claim.
- Registry entries exist only for active holders/waiters. Lock files have no
  durable owner state, remain one small inode per seen checkout, and are
  **never unlinked on release** (unlink would split authority). No TTL stealing,
  worker daemon, polling service, or periodic task is added.
- Recognized shell `&`, `nohup`, `disown`, and `setsid` detached shapes (including
  readonly-looking forms) fail before spawn with
  `workspace-lease-background-unsupported`. Use the tool background option with
  a foreground command. A deliberately daemonizing program that closes inherited
  FDs is outside this cooperative contract.
- A lease is scheduling/exclusion, never write authorization. Non-Git paths are
  no-ops and are not claimed to have Git-checkout exclusion.

### Linear checkpoint publication

`linear_git_checkpoint.py` acquires the same lease before its existing Git lock
for capture/baseline activation and again for publication. Verification itself
runs outside the checkout lease. Promotion rechecks HEAD, symbolic branch,
stable identity, workspace tree, and candidate absence after verification.

Initial baseline activation uses one ref transaction with HEAD/candidate CAS.
It refuses pre-existing unowned stable/candidate history and divergent owned
HEAD/stable history rather than advancing another integration controller.
Publication CAS verifies candidate absence and old branch/stable SHAs.

The real index is reserved with Git's `index.lock`. Only an ordinary clean index
proved equal to the base revision is synchronized from a private prepared index
after successful ref publication. Staged work, sparse/assume-unchanged flags, or
missing index are preserved. Failure removes only this operation's temporary
lock/index: it never `read-tree`s an old revision over a concurrent index and
never rolls back another writer's refs or working files. A post-publication
failure is reported; immutable checkpoint refs remain recoverable.

## Alternatives rejected

- Ack nonce / commit-or-stash advice: deliberation does not exclude active writers.
- One common-dir lock: needlessly serializes independent worktrees.
- Lease only around spawn: background processes immediately escape exclusion.
- TTL lock stealing: paused live writers would overlap; OS descriptor ownership
  provides crash cleanup without a second state store.
- Blanket writer lease for every shell command: serializes unrelated tests,
  builds and reads behind long workers instead of identified mutations.
- Automatic read lease for opaque commands: still blocks writers for the entire
  test/build lifetime, serializes cross-process runs under the existing flock,
  and falsely suggests that artifact-writing opaque programs are stable readers.
  Keep stable reads explicit rather than weakening genuine writer exclusion.
- Per-mode `LOCK_SH`/`LOCK_EX` redesign: separate opens can self-conflict and
  SH→EX conversion is not an atomic upgrade. The retained single EX flock plus
  local reader gate preserves reentry and close-only child lifetime; removing
  unnecessary automatic holds addresses the contention without an API rewrite.
- Rewrite real index before CAS, then rollback: assumes index equals HEAD and
  can overwrite staged or concurrent edits. Private preparation plus index.lock
  and CAS preserve the preimage without speculative rollback.

## Owners, integration seams, and limits

| Concern | Owner |
|---|---|
| Parameter classification / mutation target extraction | `lib/project_mod/command_analysis.py` |
| Command writer-admission decision | `lib/project_mod/run_command.py` |
| Per-call wait/cancellation scope | `lib/project_mod/tool_admission.py` |
| Presence and refusal/legacy ack compatibility | `lib/project_mod/git_guard.py` |
| Cooperative lease | `lib/project_mod/workspace_lease.py` |
| Write entry points | `lib/project_mod/write_tools/_ops.py` |
| Command lifetime / inherited lock FDs | `lib/project_mod/run_command.py` |
| Checkpoint capture / publication | `lib/linear_git_checkpoint.py` |
| Dispatch, in-process copy fast path, outer snapshot/journal | `lib/project_mod/tools.py` (must join the same lease at its owning boundary) |
| Approval / active-peer decision | `lib/tasks_pkg/tool_dispatch/_pipeline.py` |

This is not a filesystem sandbox. External editors, direct Git commands, scripts
writing undisclosed roots, Git-environment redirects, and remote filesystems
without coherent flock semantics do not become cooperative automatically.
Literal `cd`, `git -C`, `--work-tree=`, and extracted write targets join command
writer leases when mutation is identified; opaque dynamic targets are not
claimed to be discovered. Opaque test/build programs can mutate artifacts
concurrently with a writer; use an isolated worktree or explicit writer admission
for programs whose checkout-wide mutation must be excluded.

The post-acquisition conversation freshness recheck belongs to file-write/edit
tools, **not arbitrary `run_command` shell writes**. A tracked `cp` waits for
writer admission, snapshots the then-current target and journals that preimage,
but does not refuse because the conversation read older target bytes. Do not
infer stale-intent protection for shell commands from their writer lease.

Presence's `git -C` attribution is a separate dispatch concern. Shared refs/stash
across otherwise independent worktrees need the existing integration/Git CAS
boundary; per-checkout leases intentionally do not serialize every shared ref.

## Verification

Only private temporary repositories are mutated by these directed tests; no
application service or real shared-repository destructive command is required.

```bash
python3 -m pytest -q tests/project_mod/test_workspace_lease.py \
  tests/project_mod/test_command_outer_writer_lease.py
python3 -m pytest -q tests/project_mod/test_command_analysis_git_guard.py \
  tests/project_mod/test_command_analysis_extraction.py \
  tests/meta/test_git_guard_pipeline.py \
  tests/meta/test_git_guard_presence_targets.py \
  tests/tasks_pkg/test_linear_git_checkpoint.py \
  tests/tasks_pkg/test_write_approval_gate.py \
  tests/tasks_pkg/test_write_freshness_gate.py \
  tests/project_mod/test_project_post_commit_order.py \
  tests/project_mod/test_project_journal_contention.py \
  tests/project_mod/test_project_session_lock_fork.py
python3 scripts/check_documentation.py
```

Use host-compatible Git/Python/Node binaries; the checkpoint ESM syntax test
invokes Node. When validating an occupied worktree, run from a private temporary
cwd with absolute test paths and that worktree on `PYTHONPATH`; put pytest cache
and temporary repositories outside the shared checkout.

`test_workspace_lease.py` specifies exclusion/reentry, realpath aliases,
independent worktrees, cross-process busy/crash cleanup, explicit-reader
coexistence and sole-reader upgrade, finite/cancellable waits, accurate local
reader/writer versus unknown-process errors, active/pending descriptor cleanup,
fork/reused-FD teardown safety, and persistent lock inodes.
`test_command_outer_writer_lease.py` specifies lease-free opaque commands
(overlapping each other, writers and explicit readers), pipe/interactive paths
without automatic read holds, target-identified writer exclusion before spawn,
preimage/copy/subprocess/postimage/journal lifetime, inherited-child descriptor
exclusion, detached-shell refusal, and hardened lease-free Git inspection.
Directed private probes also verify a clean exec-child registry with a still-live
passed FD, and the captured project-background callback's preimage/spawn/postimage/
journal lifetime in a supervised thread (background publication and journal
persistence stubbed; admission and subprocess real).
Freshness tests retain the conversation overwrite gate; a directed contended
private-repository probe additionally verifies that file-write/batch-edit tools
reject peer-changed bytes after acquisition, whereas shell `cp` uses the current
preimage without a conversation-token refusal. Checkpoint tests exercise staged
index preservation, failed CAS without rollback, HEAD movement, foreign
candidate/stable refusal, and capture deferral. Journal tests retain admission,
ordered history projections and fork cleanup.

Uncontended outer acquisition performs ancestry metadata reads and one
lock-directory/open/flock path; registry entries/descriptors return to zero after
release, while the lock inode remains. A directed host `/tmp` measurement
(2026-10-05; Python 3.12.13, Git 2.53.0; one private repository; 10 warmups then
100 empty acquire/release samples per mode) measured writer median **0.106 ms**,
max **0.125 ms**, and explicit-reader median **0.106 ms**, max **0.114 ms**.
The first writer acquisition, including lock-directory creation, was
**0.291 ms**. Git subprocesses were outside the timed region. These are
host-local measurements, not a timing gate or network/FUSE latency/coherence
guarantee; validate flock semantics on deployment volumes.

## Internal Git executable admission

Problem: a Conda Git ahead of system Git can require unavailable GLIBC symbols,
causing workspace admission to fail before the agent starts.
Decision: internal workspace identification, integration and linear checkpoints
use `lib/git_runtime.py` to probe Git candidates with `--version` and cache the
first runnable executable for the PATH/loader environment. Repository failures
retain bounded stderr and remain fail closed.
Alternatives: changing global PATH affects unrelated tools; bypassing binding
loses ownership validation; retrying repository writes risks duplicate effects.
None of these alternatives is used.

The runnable system fallback can be Git 2.11. Linear baseline activation and
publication therefore use `update-ref --stdin`'s implicit atomic batch on EOF,
with the same CAS verifications and updates. Explicit `start/prepare/commit`
commands require newer Git and provide no additional isolation when the whole
transaction is submitted together. Splitting ref updates into separate commands
was rejected because it would permit partial publication.
