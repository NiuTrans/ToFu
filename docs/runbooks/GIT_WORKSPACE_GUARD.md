# Git workspace guard and internal workspace leases

Domain map: [`project integration`](../modules/project_integration.md).

## Tool calls take no workspace leases (owner decision 2026-10-28)

Problem: command admission derived lease claims from every shell command and
fenced the whole checkout tree and the shared Git metadata whenever it could
not prove a shape. The claim granularity was tuned again and again (effect and
fence split, per-command metadata release, read-shape proofs, queue order,
both-sides diagnostics), yet read-only commands kept being misclassified as
whole-tree writers (`git log -i -E --grep=...`, a quoted `$(date)`,
`git -C $VAR`, filters over `$VAR` paths), and tool calls queued for up to an
hour behind other sessions' long commands. Logged waits of model tool calls,
one per waiting call, lower bounds read from `[WorkspaceLease] wait` lines:
on 2026-10-27, 92 `run_command` waits, 38 longer than 60 s, the longest at
least 4,642 s, at least 9,945 s in total; on 2026-10-28 until 16:30, 46
`run_command` waits (16 over 60 s, longest at least 1,743 s, at least
9,048 s in total) and 18 `edit_file` waits (longest at least 1,563 s).

Decision (repository owner, 2026-10-28): "Remove the protection on run
commands; keep only the protection that prevents operations affecting the
entire workspace, such as stash and branch switching." Model tool calls take
no workspace lease, never wait for workspace admission and are never refused
for lease reasons: `run_command` (foreground, interactive and background),
`write_file`, `edit_file` and the other write tools, uploads, declared local
paths of MCP tools and project-dispatch file tools. What remains:

- The Git guard (`git_guard.py`, "Git classification and presence" below):
  tree-destroying Git (stash, checkout or switch of branches, reset --hard,
  clean, restore and the other classes) is refused while other conversations
  are active, with the existing receipts and approval flow. It is the only
  workspace protection left for tool calls.
- Access control, which is not locking: read-only roots, path authorization
  and registered roots, the isolated-workspace target refusal
  (`workspace_delivery.workspace_target_refusal`), approvals and sandboxing.
- Atomicity of one file edit: `file_edit_mutex.py` holds an in-process mutex
  per resolved path for exactly one write call's read-modify-write, never
  across commands or tool calls, so two concurrent edits of one file both
  apply. Write freshness still refuses a stale observed version, and a target
  that changed while the call waited is refused (`workspace-target-changed`).
- Tofu's own whole-workspace operations keep their leases among themselves
  ("Lease API and identity" below): integration snapshots, delivery and
  task-delta publication, linear checkpoints, Git maintenance and dependency
  setup. They never wait for a tool call, and a tool call never waits for them.

Removed: claim derivation and fences for commands (`command_write_lease_claims`,
the `[CommandAdmission] fence` log line), per-command metadata release
(`command_lease_lifetime.py`, `command_lease_exec.py`), the
`workspace-target-unresolved`, `workspace-cwd-ambiguous`,
`workspace-lease-background-unsupported` and `workspace-self-blocked`
refusals, file leases of the write tools, MCP local-path leases, admission
waits and their model-facing "waiting Ns for workspace admission" text, the
owned-formatter token reconciliation, and the lease-only `GitCommandScope`
fields. Command analysis stays for its other consumers: file history and undo
targets, project guidance path discovery (`command_segment_targets`), Git
guard classification, hardened Git read execution, and HTTP probe and transfer
preparation.

Lease residue in execution (removed 2026-10-29). Problem: hardened Git read
execution still resolved `git_command_scopes(strict=False)` with the lease-era
effect derivation, which refused `worktree remove|move` whose operands the
shell resolves (`R=/abs; git -C $R worktree remove $R/wt`, `"$WT"`, a loop
variable, a relative operand after a `cd` decided at run time) with "Git
worktree lifecycle requires literal operands" or "Unresolved Git worktree
lifecycle source" before start: 11 of the 39 run_command refusals logged on
2026-10-27/28, each costing a model round. It also probed hooks and Git
configuration for every Git segment of every command (about 20 ms and 60 file
system calls for `git status; git diff --stat; git log` on this checkout,
under 1 ms and none without). Decision: execution scopes carry only segment
bounds, subcommands and execution cwds; tree effects, presence attribution and
the unresolved-target refusals belong to approval (`strict`), so the Git guard
and the attended gate decide exactly as before. Alternatives rejected:
catching the refusal in preparation (still pays the probes for effects nobody
reads); keeping it for `--force` cleanup (literal targets never had it, and
the guard and approval gate own force review).

Consequences: a command or edit no longer waits for an integration snapshot,
delivery or checkpoint reading or publishing the same checkout; those
operations already verify trees and refs by compare-and-swap and refuse or
retry when the workspace changed. Commands of different tasks may write the
same file: the last writer wins, as with an external editor. Shell detach
(`&`, `nohup`, `setsid`) is no longer refused, because no lease descriptor can
escape with it. Isolated task worktrees remain the way to keep heavy parallel
work apart.

Alternatives rejected: finer-grained claims (rejected after repeated
failures: every refinement left another misclassified shape, and each
misclassification cost a whole-tree wait); mandatory per-task worktrees as a
precondition for running commands (they stay optional isolation, not a gate);
keeping file leases for the write tools (the per-call mutex gives the same
lost-update protection inside the server without waiting behind commands).

Measurement (2026-10-28, private Git checkout on local disk, isolated lease
namespace; before = `db8f55c48`, after = this change; uncontended rows are
medians of nine calls, contended rows one call started while the holder ran
for 6 s):

| Tool call | Holder | Before | After |
|---|---|---|---|
| `echo fresh > out.txt && git add out.txt` | another task's `sleep 6 > "$PWD/long.out"` | 6,290 ms | 14 ms |
| `R=$PWD; git -C $R log --oneline -1` | the same | 6,060 ms | 17 ms |
| `L=lib; sed -n 1p $L/x1.py` | the same | 6,034 ms | 13 ms |
| `edit_file`, one line | the same | 6,096 ms | 273 ms |
| `edit_file`, one line | internal whole-checkout lease | 6,105 ms | 168 ms |
| `echo fresh > out.txt && git add out.txt` | none | 328 ms | 14 ms |
| `git log -E --grep=two`, `git ls-files lib/x*`, `for f in $(git ls-files lib); ...`, `git diff ... > /tmp/...` | either holder | 18-81 ms | 13-49 ms |

The last row's shapes were already proven lease-free in the before revision
in this layout (see "Read-only Git shapes" below); the live server, loaded
before those fixes, still queued them. The contended `edit_file` times
include the holder's concurrent process start, not a wait.

Verification: `tests/project_mod/test_tool_calls_take_no_workspace_lease.py`
(a writing command beside another task's long command, the formerly
misclassified read shapes and the write tools beside an internal writer, two
concurrent `edit_file` calls on one file, read-only roots, and the Git guard
for stash, checkout, switch, reset --hard and clean),
`test_command_execution_preparation.py`, `test_git_read_hardening.py`,
`test_network_probe_lease.py` (execution plans), `test_file_scoped_write_lease.py`
and `test_file_scoped_upload_target.py` (edit mutex), the Git guard suites and
the internal lease suites (`test_workspace_lease*.py`).

## Automatic Git maintenance never runs inside commands

Problem: Git writers (commit, merge, rebase, am, fetch) start `gc --auto`.
Detached, it escaped the command's process group with inherited workspace
lease descriptors and blocked later delivery (2026-10-26). Forced into the
foreground (`gc.autoDetach=false`), it became unbounded hidden work inside
model and harness commands: 89,465 unreachable loose objects carried mtimes
newer than `gc.pruneExpire` (two weeks), so `gc --auto` could never get below
`gc.auto` (6,700) and every later writer repeated it. On a lab copy of this
repository the first commit took 76.5 s and each later one 6.9 s (2026-10-27).

Decision: every model command (`run_command`, interactive and frozen-input
runs) and every internal Git call through `git_policy_environment()` carries
`'gc.auto=0' 'maintenance.auto=false' 'gc.autoDetach=false'
'maintenance.autoDetach=false'` in `GIT_CONFIG_PARAMETERS`
(`lib/git_runtime.py`). An explicit `git -c gc.auto=N` still wins and stays in
the foreground. Pytest drops the inherited value before importing the project
([`TESTING_STRATEGY.md`](../TESTING_STRATEGY.md), "Inherited Git selection
environment"); the server's own environment never carries it, so its admission
is unchanged. Maintenance is the explicit, supervised
`python3 scripts/git_maintenance.py --repo <checkout> [--expire-days N] [--apply]`.
It only reports by default. `--apply` holds the repository's workspace Git
metadata lease and runs `pack-refs --all --prune`,
`repack -d -l -A --unpack-unreachable=N.days.ago`, `prune --expire=N.days.ago`
and `commit-graph write --reachable`, then `commit-graph verify`. In a shallow
repository, such as this one, Git neither reads nor writes a commit-graph, so
a stale one is removed instead of failing every verification and fsck.
N is at least one day. Every ref, reflog, worktree HEAD and index is a root and
newer objects keep what they reference, so staged, stashed and not yet
referenced in-flight objects survive. Reflogs are never expired and worktree
administration is never pruned.

Measured on a lab copy of this repository's `.git` (8.6 GB, 83 worktrees,
shallow, local disk, host load 20-57): the report took 220 s; `--apply
--expire-days 1` took 195 s (repack 166 s, prune 26 s) and left 2,809 of 89,759
loose objects (771 MiB to 30 MiB) and one pack instead of 42 (7.76 GiB to
7.75 GiB); an idempotent second run took 253 s; afterwards `git fsck` (333 s)
and `commit-graph verify` were clean. The shared checkout sits on FUSE and will
be slower.

Alternatives rejected: keeping foreground automatic maintenance (the measured
cost per commit, without converging); detached maintenance (escapes the process
group and lease); `git gc --prune=<date>` as the explicit step (it expires
reflogs, which drops other sessions' stashes, and prunes the administration of
worktrees whose directory is missing or briefly unreadable on FUSE); an expiry
such as `now` (deletes objects that concurrent writers wrote but have not yet
referenced); `repack -a -d -k` keeping every unreachable object packed (the next
full repack loosens them again with a fresh mtime and the storm restarts);
`gc.auto=0` in the repository configuration (changes the owner's own Git).
The largest producer of the unreachable objects is recorded in
[`CONTINUOUS_DELIVERY.md`](../CONTINUOUS_DELIVERY.md) ("Delivery polls compare
trees"). Verify `tests/tasks_pkg/test_git_maintenance_ownership.py` and
`tests/project_mod/test_git_maintenance_script.py`.

## Housekeeping overrides keep inspections hardened

Problem: agents pass `-c gc.auto=0 -c maintenance.auto=false` on every Git
call. The hardened-inspection grammar refused both settings, so reads such as
`git log` or `git grep` ran unhardened (and, until 2026-10-28, were fenced as
uncatalogued Git: 182 of the 202 such fences on 2026-10-27).

Decision: `command_analysis._HARMLESS_GIT_CONFIG` is the single allowlist for
both the hardened-inspection grammar and Git helper analysis. Beside the
presentation pins it accepts exactly `gc.auto=0` and `maintenance.auto=false`.
The hardened form drops them, because no inspection runs housekeeping. Model
commands already carry both settings in `GIT_CONFIG_PARAMETERS` (previous
section), so the flags are redundant there, but callers keep passing them.
Other values (`gc.auto=1`, `maintenance.auto=true`) are not hardened.
Alternatives rejected: asking agents to omit the flags on reads (guidance cannot
fix every caller, and the flags are harmless); accepting any `gc.*` or
`maintenance.*` value (enabling values can start background work).
Verify `tests/project_mod/test_git_read_hardening.py`.

## Reading plumbing is catalogued by form, not by verb

Problem: the risk classifier read `symbolic-ref` only as the exact argv `HEAD`,
`-q HEAD` or `--quiet HEAD`, so `git symbolic-ref --short HEAD` counted as a
reference mutation and the presence guard refused it (2026-10-27). `cherry` and
`hash-object` were unknown verbs: the guard fell back to the attended gate and
admission fenced them as uncatalogued Git.

Decision: `symbolic-ref [-q|--quiet] [--short] <name>` (exactly one name) and
`cherry [-v|--verbose] [--abbrev[=<n>]] [<upstream> [<head> [<limit>]]]` are
`readonly` and join the hardened inspection grammar. `hash-object` without `-w`
(`-t <type>`, `--path`, `--no-filters`, `--stdin`, `--stdin-paths`) is
`readonly`, but it is hardened only when no filter driver can run:
`--no-filters`, or `--stdin` without `--path`. Every other form keeps its class:
a second operand, `-m`, `-d`, bundles and abbreviations stay reference
mutations; `-w`, `--literally` and unknown options stay unknown. `merge-base`
and `for-each-ref` were already catalogued.
Alternatives rejected: cataloguing these verbs whole (their mutating forms share
the verb); hardening `hash-object` path inputs (configured clean/process filter
drivers run on them, no hardening flag disables them, and git-lfs clean stores
the object it hashes); resolving `R=...; git -C $R ...` reads (a parameter
expansion may add options or select another repository, see the expansion rules
below). Replaying the 2026-10-27 log through this change leaves such expansions
as the main source of uncatalogued-Git fences (92 segments), not unknown verbs.
Verify `tests/project_mod/test_command_analysis_git_guard.py` and
`tests/project_mod/test_git_read_hardening.py`.

## Read-only Git shapes (2026-10-28)

Earlier the same day, before the owner decision above, a precision fix made
admission prove more read shapes lease-free (the abandoned 2026-10-26 branch
`fix/workspace-lock-stalls` had shown one parity loop fencing the main
checkout's tree and metadata for 4,672 s). What remains of it is hardening
and Git guard classification; none of it decides whether a command may start:

1. The hardened log/show/rev-list grammar accepts `-E`, `-F` and `-P` like
   their long forms; `--group` values get the `--format` signature-atom check.
2. `command_analysis.git_read_expansion_inert` proves an expanded Git read
   inert: a literal read verb whose operands cannot select a mode, every
   expanded word after it starting with a literal character and holding only
   `$NAME` parameters or pathname patterns. `_expansion_may_select_target`
   consults it when resolving Git targets.
3. Plumbing reads (`git_command_scope._GIT_PLUMBING_READ_OPTIONS`: rev-list,
   show-ref, diff-tree/-index/-files, verify-pack, var, check-ref-format) are
   catalogued by form: every option is listed, and `-c` values must be in
   `_HARMLESS_GIT_CONFIG`.
4. `literal_loop_body_openers` and `loop_body_cd_targets` resolve a
   `cd /abs/$NAME` at the start of a literal loop body for Git target
   resolution.
5. `GIT_CONFIG_PARAMETERS` holding exactly the maintenance-off entries every
   Tofu-run shell carries (`lib/git_runtime.py`) is not helper evidence
   (`_git_config_injected`).

Its measurement is part of the evidence for the decision: replaying 2,549
distinct logged commands of 2026-10-27/28 still left 273 with a tree or
metadata claim, dominated by expansions that select a repository, unquoted
variables bound by substitutions, shell-variable file operands of filters,
grouped redirects and opaque runtimes.
Verify `tests/project_mod/test_git_read_hardening.py` and
`tests/project_mod/test_git_command_scope.py`.

## Git observations preserve the writer index

Problem: internal status/diff queries bypassed shell read preparation and
rewrote the writer index. Failed status commands also appeared as a successful
zero-change scan. Legacy shell preparation could report an old staged view
after an earlier command changed the index.

Decision: internal integration/checkpoint argv and recognized shell query
segments share `git_inspection_execution.py`. Status uses optional-lock
suppression when supported. Porcelain diff and dirty/broken describe use a
private index on all versions, because optional-lock suppression does not stop
their refresh. Legacy index readers use the same mechanism. Copies preserve
bytes and mtime from one descriptor so Git retains its racy-index check.
A proven read-only batch with one known execution cwd pins its observation at
entry; mixed command bodies and queries with multiple possible cwd paths resolve
Git's current index (including exported/unset index overrides) and refresh each
independent private index immediately before its query. The shell selects the
actual cwd; ordered literal `git -C` operands come from `git_command_scope.py`
and are applied by the snapshot child without changing the parent shell cwd. The
stdlib-only child replaces itself with Git in the same supervised process
group. Real writes keep their original index/environment and never replay.
Nonzero status exits remain unknown (`scanned=false`); actual timeout causes
alone set `timedOut=true`.

Alternatives rejected: a repository-wide lease blocks unrelated writers;
optional-lock flags alone leave diff/describe writes; taking every snapshot
before a mixed body loses preceding staged changes; failed scans cannot prove
a clean workspace. No new admission ceiling, worker pool or durable state is
introduced. Temporary copies retire with the existing command lifetime.

Problem: `cd target && git show … | cat; git diff …` can reach its final query
from either the original or target directory when `cd` fails or succeeds.
Refusing that static ambiguity prevented ordinary inspection batches from
starting. Decision: retain both candidates for approval attribution and
use the existing execution-time snapshot owner for the read. Alternatives
rejected: assuming `cd` succeeds selects the wrong index on failure; dropping
index protection permits refresh writes; replaying the batch duplicates prior
effects. Conditional/pipeline behavior and original index bytes remain intact.

Problem: the server keeps `git_inspection_execution.py` in memory but spawned
the snapshot child by path from the live checkout. When 49053ced2 changed the
child's argv from an execution cwd to JSON `-C` steps, every hardened
`git diff` of the running server crashed with `JSONDecodeError` until the
2026-10-27 restart; the reverse pairing (a new caller with an older on-disk
child) fails the same way. Decision: the caller pins the child source when it
is imported and spawns that copy (`lib/runtime/pinned_helper_source.py`, rule
in [DEFENSIVE_PATTERNS.md](../DEFENSIVE_PATTERNS.md#long-running-processes-spawn-helpers-from-their-loaded-source)),
so caller and child always come from one code version. The child rejects any
other argument shape with exit 126 and "restart the Tofu server" instead of a
traceback; that reaches only servers started before pinning, which still spawn
the on-disk file and therefore keep receiving today's unchanged argv.
Alternatives rejected: an explicit protocol-version argument alone turns skew
into a clear failure but cannot keep reads working, and every by-path caller
already running sends the untagged form; inlining the child with `python -c`
puts ~3 KB into each rewritten command string; resolving the child from the
checkout at every spawn is the defect itself.

Local phase measurement: 2,048 tracked files, two diff queries, ten alternating
trials per arm after two warmups, pinned baseline `6d5e25397`. Writer-index
changes fell from 10/10 to 0/10. Median invocation time was 79.297 → 99.149 ms;
maximum transient index copies were 327,944 bytes with zero retained directories.
This measures correctness overhead on a local fixture, not production latency.
Specs: `test_git_internal_inspection.py`, `test_git_inspection_execution.py`
(including the caller/child skew cases), integration snapshot/liveness and
linear-checkpoint suites.

Tasks execute directly by default; independent parallel edits can opt into
isolation through the [execution policy](../modules/project_integration.md#execution-policy-direct-by-default).
Both modes retain the authorization, freshness and Git guard below.
Direct writes remain visible after task failure/cancel; they are not transactions.

## Problem

A shared checkout has concurrent file writers, commands, and task-end checkpoint
publication. Presence and an approval/ack are not exclusion: an acknowledged
`checkout -B`, `stash apply`, or index rewrite can still destroy a peer's work.
Git worktrees have distinct working files/indexes but share a common Git directory.

A long test/build/read command must not become checkout ownership merely because
its effects are opaque. Giving every command an exclusive lease serialized all
shell activity; giving opaque commands an automatic read lease still blocked
writers for the entire test/build lifetime; deriving claims per command
misclassified reads as whole-tree writers (top of this document).

## Decision

Keep three independent boundaries: existing readonly/approval authorization,
parameter-sensitive Git presence policy for model tool calls, and cooperative
checkout exclusion among Tofu's own whole-workspace operations only. Model tool
calls take no lease; one write call's read-modify-write is serialized per file
in process. Callers needing a stable cooperative read explicitly use
`workspace_read_lease`.
No second owner database or expiry-based lock stealing is introduced.

### Git classification and presence

`command_analysis.py` owns pure risk/read-effect classification.
`git_command_scope.py` is the single target resolver used by approval and
the Git guard; `git_inspection_execution.py` owns execution preparation:

| Class | Examples | Policy |
|---|---|---|
| `readonly` | status/log/diff/show without output writes; branch/tag listing; stash list/show; proven clean dry-run; one-name symbolic-ref reads; cherry; hash-object without -w | Existing read authorization |
| `safe_write` | additive add/ordinary commit (appends history), ordinary apply/--cached, branch/tag creation, ordinary fetch/push, normal merge/cherry-pick/revert, checkout -b/switch -c without start point, branch -d/--delete | Existing write authorization/receipt |
| `tree_destroying` | checkout -B/--orphan, switch -C/--force-create, branch -D/-M/-f and any bundled-force short flags (-df/-fd/-vd), tag -d/-f, commit --amend, update-ref/symbolic-ref mutation, force push/fetch, stash mutations, reset/restore/actual clean/rebase, integration abort/forced/strategy options | Presence-aware refusal (Git guard) |
| `None` | ambiguous config getter/setter, unknown options/commands, opaque shell segments | Existing attended gate, never inferred readonly |


Ordinary `commit` appends history and is already `safe_write`; rewriting history
with `commit --amend` remains `tree_destroying`. Ordinary `git apply` and
`git apply --cached` are `safe_write`, not checkout-wide destruction. Pure
`apply --check/--stat/--numstat/--summary` is `readonly`; explicit `--apply`
overrides those report-only modes and restores the write classification.
`apply --3way/--reject/--unsafe-paths` remains conservatively `tree_destroying`;
unknown options return `None`, never inferred safe.
Normal `merge`, `cherry-pick`, `revert` and maintained ordinary/continue flags
are `safe_write`: Git validates tracked/untracked overlap and reports conflicts, and its own
index and ref locks protect the checkout. Mere project presence is
irrelevant to this path and is not probed. Abort/quit/skip/autostash and forced/strategy forms retain destructive policy.
Unknown, malformed or signing/external-program integration options retain the
existing approval path, not an invented proof of working-tree destruction.
A known destructive option elsewhere in the same invocation still wins risk
classification; unknown options prevent an automatic receipt even when solo. Ordinary integration
still needs write authorization.

Integration options are parsed per verb, consuming message values rather than
classifying their text as options. `merge --no-ff … -m <message>`, `--message`,
`--message=<message>` and attached `-m<message>` remain ordinary writes, even
when the message begins with `--abort`. A subsequent real `--abort` or
`--autostash` still engages presence policy. Merge `-s` selects a strategy and
is conservative, unlike cherry-pick/revert `-s` (signoff); their `-m` mainline
selection is not merge's message option. Missing values, unknown options and
dynamic shell arguments never borrow ordinary-merge authorization.
Problem: a flat shared flag allowlist caused message-only false refusals and
misread verb-specific short options. Decision: parameter-aware per-verb parsing;
rejected alternative: bypassing presence for all merges.
Specifications: `test_command_analysis_git_guard.py`, `test_git_guard_pipeline.py`.
Across the full command chain, `tree_destroying` outranks opaque `None`, and
opaque `None` outranks `safe_write` (which outranks `readonly`). A known
high-risk Git segment therefore keeps presence policy even with an opaque
segment; a safe write combined with opaque code still reaches the existing gate.
Risk and authorization are separate: high-risk plus opaque/unknown code may be
refused for presence, but even when solo it cannot mint an automatic Git receipt
for the whole chain. Dynamic shell arguments (including command substitutions)
never borrow Git report-only authorization. Report modes do not expand the
separate hardened status/log grammar.
Ordinary apply preflights the patch and refuses failed hunks without partial
application by default; `--reject`, `--3way`, `--unsafe-paths` and
`--allow-overlap` do not receive this ordinary-write treatment.
`--build-fake-ancestor` can write an index even with report flags and retains
the existing approval path. Option values and `--` pathspecs are not options.
Normal commit still reads the selected index/pathspec: its additive-history
classification does not prove the staged changes belong to one task.

Non-forced `worktree add -b <new-branch> <path> [start-point]` and
`worktree add --detach <path> [start-point]` are additive `safe_write`, in
any option order (Git's option parser permutes, so `add <path> -b <branch>`
is the same creation);
`worktree list [--porcelain [-z]]` is read-only. Force/reset and unknown
worktree administration shapes retain the existing approval gate. For a
literal chain that creates a not-yet-existing checkout and then runs Git with
`-C` at that exact destination, presence attribution uses the future checkout
path, not its existing parent repository. Approval precedes creation, so an
upward `.git` search would falsely attribute isolated `cherry-pick` to the
shared parent. Existing destinations resolve normally; subsequent destructive
operations on the parent still check parent presence. Git's own destination/ref
checks remain in force.

Problem: isolated-worktree cleanup was first unclassified, then inherited
presence from the invoking checkout merely because it shares Git metadata.
An unrelated conversation generating in the main checkout could veto removal
of an idle verification tree. Decision: literal absolute-path `worktree
remove|move` remains `tree_destroying`, with presence derived from its actual
tree effects. Removal checks the target; movement checks the source and actual
destination, including Git's basename append when moving into an existing
directory. Missing destinations retain their own identity instead of inheriting
presence from a containing checkout.
Configured helpers or unresolved redirects retain conservative source-tree
presence. Each other destructive segment retains its own presence roots.

The shared `command_analysis.git_worktree_lifecycle` parser owns syntax for
classification, effects and diagnostics. Relative operands retain the existing
approval gate. Execution resolves no lifecycle operands: Git resolves them
against the shell's actual cwd. `~`, unknown options and missing/extra
operands remain unresolved for approval.
Diagnostics identify worktree retirement and target/administrative effects,
rather than suggesting the invoking checkout's files/index will be reset.

`--force`/`-f` bypasses Git's dirty/locked-target protection, so solo presence
never mints an automatic cleanup receipt. Force keeps the existing explicit
approval review for target ownership, active users/commands, retained commits
and discarded tracked/untracked/ignored content. This applies even to `/tmp`,
known virtualenv links, repeated force flags and otherwise ordinary chains.
Native Git refuses dirty/locked/main-tree removal without force. Neither a
clean status nor absent presence proves task ownership or external-process
quiescence. Existing execution/task ownership remains independent; opaque
tests/builds provide no stable-read promise.
No cleanup nonce, persisted ownership registry or pre-approval status snapshot
is introduced. Alternatives rejected: allowing every force cleanup, a `/tmp`
whitelist, or treating project-wide presence as a writer lock.
Specifications: `test_worktree_cleanup_admission.py`,
`test_git_guard_presence_targets.py`, `test_command_analysis_git_guard.py`.

A 100-call local FUSE scope-resolution sample measured median 7.6 ms
before / 11.4 ms with target configuration inspection (p95 21.8 / 30.6 ms).
Resolution starts no subprocess and retains no cache, worker or registry.
These host-dependent timings are not latency limits.

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

Per-command administrative/config routing and inherited Git directory/index/
namespace overrides cannot borrow a mergedness proof from a differently resolved
HEAD; they retain the guard. Harmless read environment switches are not routing.
Active project presence cannot be overridden with `TOFU_GIT_ACK`: legacy token
consumption always returns false. Refusals explicitly identify the conservative
**project-presence policy**, not confirmed writers, an actual lock/lease conflict,
or proven file overlap. They show the resolved checkout roots and full `convId`
and `agentId` (main agent when absent), even when a title exists. Per-peer root
attribution comes from the dispatch probe, not another owner database. Older
three-argument refusal callers remain supported with an unspecified-root label.
Reported files/status are incomplete activity hints: missing or disjoint file
lists do not authorize `reset` or another high-risk operation. Refusals identify the actual numbered shell segments and operation effects;
ordinary merge elsewhere in the chain must not be blamed for stash/abort.
The existing rejection descriptor carries bounded `gitDiagnostics` evidence:
source, execution state, evidence level, classification, segment reasons,
effect domains and action codes. Merge/cherry-pick/revert and apply segments
also carry `riskyOptions` (refusal text `Risk options:`): the risky option
spellings found by the classifier's own value-aware parser. Problem: an apply
refusal named only its reason code, so `apply --index --3way` did not show
that `--3way`, not ordinary apply, triggered the presence policy. Decision:
apply evidence comes from the same parser as its classification; rejected
alternative: re-scanning argv in the guard, which reads option values and
`--` pathspecs as flags. Shell arguments/messages/configuration values
are not copied into the diagnostic, and neither advisory peer labels nor
suggested actions are authority. Unknown syntax is distinct from known danger.

Native effect annotations do not exclude configured helper effects: the physical
resolver still owns target unions and presence broadening. A refusal always states
`execution=not_started` and denies automatic-receipt eligibility even when its
syntax is fully known. Unknown-form approval denials carry the same diagnostic
source with `evidenceLevel=syntax_unresolved`, not advisory writer evidence.
Guidance starts with read-only `git status`, staged diff and target-path checks.
For task integration, do not stash/reset/abort shared work as preparation:
ordinary merge/cherry-pick retain their own write authorization; if Git itself refuses
because of unrelated staged work, reviewed-path integration requires current
content/patch preflight, `commit --only` for reviewed paths and verification
that unrelated staged blobs survived. Splitting a compound command does not
authorize its risky segment or discard dependencies. A refusal applies only to
the flagged segment: read-only or non-Git work elsewhere in the command (e.g.
running tests or inspections) is unaffected and is re-issued on its own,
without the refused wrapper; the refusal text says so explicitly, and the
executing agent must not treat a segment refusal as a reason to stop the task.
Problem: the earlier phrasing ("does not authorize it or erase command
dependencies") was read as vetoing the whole compound command, so executors
abandoned tasks whose only blocked step was an unnecessary stash wrapper.
Decision: name the unaffected-work continuation explicitly; rejected
alternative: leaving the integration-only guidance, which never covered the
stash-wraps-test pattern. Do not execute an unrelated
abort: inspect operation state and task ownership first. No automatic retry,
forced overwrite, protection disable or index/lock deletion is suggested.
Waiting is only an option if the same risky operation is genuinely necessary;
project-wide idle is not a prerequisite for ordinary integration. No new nonce is issued by dispatch. Legacy token functions remain
callable for older callers but grant no authority.

Presence remains an advisory, process-local TTL view. Guard probe failure and
`TOFU_GIT_GUARD=0` retain the existing approval fallback. No-active-peer is
not proof that the workspace is clean or safe.

### Parameter and effect evidence

Problem: unknown parameters were reported as destructive operations, previews
were treated like deletion, and generic wait guidance encouraged model retry
loops instead of diagnosis. Decision: consume global and per-verb value options
before classifying; support separated, attached and equals forms without letting
message/path data masquerade as flags. Maintained cleanup/empty/fast-forward and
rerere flags keep ordinary integration semantics. Proven `clean -n/--dry-run`
(including short bundles) is a preview; unknown or interactive forms do not gain
read-only authorization. Actual deletion, history rewrite and shared-index
mutation keep protection. Option coverage is bounded, not a promise to parse
all Git versions or aliases. Rejected alternatives: a blanket merge allowlist,
LLM judgment as execution authority, or disabling presence to improve UX.

The shared parser also recognizes bounded `env NAME=value git …`; complex env
options keep approval. Parser acceptance is not Git-version compatibility:

Invocation-local `-c` settings are not harmless because their syntax parses:
only bounded presentation/helper-disabling settings qualify. Dynamic config-env,
executable paths, pagers and inline `GIT_CONFIG_*` retain approval and cannot
borrow automatic receipts; enabled merge autostash retains destructive risk.
Stash inspection requires explicit leading list/show with maintained options;
`stash -m list` is a push message, not a read verb. Reference format and push-option
values are consumed as data before scanning risk flags; unknown ref options
retain approval and cannot hide a known force/delete flag.
portable separated `-C <path>`/`-c <key=value>` remain preferred if the installed
Git rejects attached globals. Internal revision tilde (`HEAD~1`) is literal;
leading home/assignment tilde, variables and command substitutions remain
unresolved target effects. Proven clean previews share the hardened no-pager,
no-optional-lock, fsmonitor-disabled read path.
Effects remain separate from authorization: proven native `reset --soft` changes
metadata, not source bytes; proven staged-only restore changes the index. They
still carry destructive risk and do not gain task ownership or safe-write
receipts. Unknown options, worktree targets, metadata redirects and executable
helpers keep conservative effects. Diagnostics request no subprocess, network,

Dynamic `--config-env` (both spellings) and bounded Git config helper detection,
including same-line section keys, prevent metadata-only narrowing when a callback
may write source bytes. Unsupported syntax, includes, continuations, oversized
configuration and read failures keep broader effects; helper detection executes
neither callbacks nor Git and follows no include files.
new queue, durable ledger or cache; lists and copied identities are bounded.
Specifications: command-classification, guard-pipeline, presence-target and
Git-scope tests, including private real Git repositories and mixed chains.

### Worktree creation and command targets

Problem: a not-yet-created linked worktree resolves upward to the parent
checkout, so presence would attribute isolated work to the shared parent.
Decision: the nonexistent destination of additive `worktree add -b` or
`--detach` is recognized as a future checkout only across success-dependent
`&&` segments, and presence uses it. Existing destinations and `;`, `||` or
pipeline control resolve normally. Global `-C` hops before the subcommand
select the final checkout; option values, messages and pathspecs cannot add
false roots. Explicit `--git-dir`, `--work-tree`, `GIT_DIR`, `GIT_WORK_TREE`,
`GIT_INDEX_FILE` and `GIT_COMMON_DIR` participate in target attribution
(relative common-dir resolves against the effective cwd after `-C`); setup
normalization never strips `GIT_*` assignments. Pipeline `cd` cannot move
another segment's cwd; a `cd` that may fail, followed by `;`/`||`, retains both
possible checkouts, and a leading `cd` that cannot fail retains only its target
(next paragraph). Opaque code invalidates a prospective checkout identity.
Configured helpers widen the source-tree effects; `extensions.worktreeConfig`
alone is not helper evidence, `config.worktree` is inspected in each relevant
Git directory. Until 2026-10-28 the same resolution also derived command
writer leases; tool calls no longer take any (top of this document).

**A leading cd that cannot fail selects the presence checkout (2026-10-31).**
Problem: presence attribution kept the starting cwd as a fallback after every
`cd`, because a failed cd lets later `;`, `||` or newline segments run where
the command started. A task working in its own linked worktree with
`cd <wt> && grep …; git rm -q x; git status` was therefore checked against
the shared parent too: while any conversation was active there (live
2026-10-31: nine peers) the guard refused the worktree-local `git rm`, named
both checkouts and sent the agent to "a separately authorized isolated
worktree", the one it was already in. `git -C <wt> rm x` was always
attributed to `<wt>` alone. The leading-cd proof of 717e40a17 served only
writer admission and was deleted with it in f1c0e2bf0. Decision: the guard
calls `git_command_scopes(..., certain_leading_cd=True)`. `leading_cd_enters`
proves that the script's first command is a literal cd (`cd`, `command cd`,
`-L`/`-P`, discarded output) into existing directories the server may search,
with no `..` component, so no later segment can run in the starting cwd,
whatever separator follows except a pipe. `shell_cwd_change` marks bash's
`cd -e`/`-@` uncertain: dash rejects them and stays put. Since 2026-10-28 the
guard decides on the call's own worker right before the command starts, with
no admission wait, so the proof holds for the dispatch-to-spawn latency. Read
preparation keeps both candidates and replays the actual cwd at run time.
Every other cd keeps the fallback: after any other command, in a subshell or
pipeline, with a missing, non-directory or unsearchable target, `builtin cd`
and `pushd` (absent from POSIX sh); a backgrounded `cd X &` stays unresolved
and reaches the existing gate. Alternatives rejected:
- Keep both candidates: the systematic false refusals above.
- Use the proof only for automatic receipts: a `cd` segment already makes a
  command ineligible for receipts, so the reported command would still be
  refused; making `cd` eligible would move Manual-mode approval authority.
- Re-prove after a human approval: presence itself is not re-checked after
  approval, so a second guard pass would close a narrower race than the one
  already accepted.
- Spawn the shell in the cd target: changes relative-cd and `$OLDPWD`
  semantics of the user's command.

Residual risk: a target removed or made unsearchable between the guard's
decision and the shell's cd, including while a Manual-mode approval is
pending, sends later segments to the starting cwd without a presence check
there. Specs: the certainty, may-fail, unsearchable and background cases in
`tests/project_mod/test_git_command_scope.py`, the reported command and
may-fail refusals in `tests/meta/test_git_guard_presence_targets.py`, and
real `/bin/sh` runs in a nested linked worktree in
`tests/project_mod/test_git_guard_real_semantics.py`.

**A command substitution resumes the parent's cwd (2026-11-01).** Problem:
the span splitter cuts an unquoted `$(…)` body at its parentheses, and every
closing parenthesis put back every cwd the command had ever had. After
`cd <wt> && git commit … && cd <shared> && PRE=$(git rev-parse HEAD) && git
merge … && git worktree remove .tofu/worktrees/<wt>`, the removal was also
resolved from `<wt>`. Its relative source then had no proven identity, so read
preparation refused the whole command before anything ran. Decision:
`git_command_scopes` pairs each body that `command_substitution_bodies`
delimits with its real closing parenthesis. At that close the parent resumes
the cwds and fallback cwds it had when the body opened, since a child shell
cannot move them. The parent's own `;`/`||` fallbacks still apply. Any other
close (a `( … )` group, process substitution, case pattern, or a scanner
`None`) still widens to every earlier cwd. Alternatives rejected: pairing every
parenthesis by depth lets a `case` pattern close a group and drop the cwd a
later segment runs in; skipping the lifecycle source proof for relative
operands in approval (`strict`) removes the Git guard's protection for an
ambiguous cwd. Execution preparation neither proves nor refuses lifecycle
sources ("Lease residue in execution" above). Specs:
`test_command_substitution_resumes_the_parent_cwd` and
`test_substitution_resume_keeps_other_cwd_paths` in
`tests/project_mod/test_git_command_scope.py`.

### Lease API and identity

```python
from lib.project_mod.workspace_lease import (
    WorkspaceBusyError, WorkspaceLeaseCancelled,
    workspace_read_lease, workspace_write_lease,
    workspace_file_write_lease, workspace_git_metadata_lease,
    workspace_tree_write_lease,
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

  Raw file transactions cannot replace checkout `.git` markers or administrative
  `commondir` pointers: those can split a live file's lock namespace. Use admitted
  Git lifecycle operations instead, including for external administrative layouts.
- Checkout barriers and source-file POSIX advisory flocks live in the Git
  common directory. Physical tree and Git metadata keys, plus canonical file
  keys for non-Git and runtime/data targets, use the fixed per-OS-user
  `/tmp/tofu-workspace-tree-<uid>/tofu-workspace-leases` neutral namespace,
  independent of child `TMPDIR`. Metadata keys identify the canonical common
  directory independently of the invoking checkout alias; their lock storage
  is not inside that common directory. Runtime/data file writes retain their
  source-lease exemption, not an exemption from same-file exclusion or tree
  retirement fences. Keys hash canonical targets, never task IDs. Namespace
  ownership, mode, no-follow opens and lock inode identity fail closed.
  Zero-byte lockfiles persist; release never unlinks active keys. Different
  targets share ancestor barriers without excluding one another. Thread
  ownership controls reentry; other threads/processes acquire separately.
  Combined acquisition follows the physical order below. Upgrading an already
  held same-thread shared tree barrier to destructive exclusion is refused,
  not unlocked and reacquired.

- Callers are Tofu's own whole-workspace operations; each names its complete
  claim set. Model tool calls take none (top of this document).

| Caller | Claims |
|---|---|
| Stable integration snapshot (`integration_control.py`) | stable read of the source checkout |
| Integration submit and delivery publication (`integration_control.py`, `delivery.py`) | checkout write, or metadata plus the reclaimed tree |
| Task-delta publication and clone reclamation (`task_delta_delivery.py`) | the published files |
| Linear checkpoint capture (`linear_git_checkpoint.py`; publication writes no checkout file) | checkout write |
| Dependency setup (`workspace_delivery.workspace_runtime_setup_lease`) | stable read of the checkout plus its runtime-setup key |
| Git maintenance (`scripts/git_maintenance.py --apply`) | Git metadata |

Problem (2026-10-22): `git add`, `update-index` and `write-tree` were
classified as checkout transformations. Decision: index-only verbs carry the
Git metadata effect; `update-index` is covered as well as `add`/`write-tree`
because all three can run `post-index-change` and configured clean filters,
and `extensions.worktreeConfig` selects an additional config source that is
inspected rather than treated as a callback. These effects now inform Git
target resolution and presence only.

### Identity, topology and acquisition transactions

`workspace_lease.workspace_claims_lease(claims)` is the combined transaction
boundary for Tofu's own multi-target transactions (delivery, task-delta
publication, reclamation). It expands all
implicit barriers, merges duplicate physical identities using the stronger
mode, then acquires the flattened set in phase order: **tree, metadata,
checkout, file**. Within each phase it sorts by lock namespace/common identity
and canonical key; metadata ordering is independent of checkout aliases.
Only after the complete physical set is held does it revalidate the original
target bindings and enter the public scopes reentrantly. One deadline covers
all acquisition; failure unwinds acquired descriptors before the body runs.

`lease_claim_order` remains a logical claim ordering helper, not sufficient
physical admission: callers must not acquire hidden barriers one logical scope
at a time. In particular, mixed TREE(A)+ROOT(B) and ROOT(A)+TREE(B) must share
the combined transaction rather than invert their physical barriers.
Native checkout transformations and stable reads take only their own checkout
barrier, never implicit common-directory metadata ownership. Explicit metadata
claims still serialize shared administration. Git retirement combines metadata and
actual tree claims (including controlled cleanup), not a tree-only Git mutation.
Explicit tree effects hold descendant-excluding lifecycle barriers;
ordinary file/checkout/metadata operations share those barriers without
excluding each other solely because one checkout is nested in another. Topology discovery follows existing ancestor markers only; it
never scans the descendant tree or introduces a project-global mutex.
After admission, complete checkout/Git/common-dir pointers and inode bindings
are rechecked. Lock files are opened through nofollow directory descriptors and
verified against the held inode. Existing-FD publication, reentry and sole-reader
upgrades roll back their own transaction on cancellation/exception; other
readers' ownership survives. External rebinding after validated admission is
still non-cooperating mutation, not a sandbox guarantee. Specs:
`tests/project_mod/test_workspace_lease_hardening.py`.
Physical tree keys validate their namespace/inode per descriptor; checkout
metadata/topology is revalidated after combined admission, without timestamp
caches or repeated Git subprocesses. Paired effect-admission/FD measurements
live in [project integration](../modules/project_integration.md). Added lifecycle
barriers have an explicit overhead; independent-task throughput comes from
removing false exclusion, not claiming every uncontended call is faster. No
resident cache/worker or concurrency cap was introduced.

The rebinding check on every key entry is a lookup in a root -> active key
index maintained beside the registry, never a scan of all active keys. A
task-delta delivery admits one file claim per changed path, so a 1,100-path
delta holds about 3,700 keys at once; the former per-entry scan made that
one admission quadratic (`benchmarks/workspace_claims_lease.py`, local disk:
14.6-15.0 s CPU before, 7.1-8.8 s after). The remaining cost is per-claim
chain/realpath stats and held-descriptor validation, which the admission
still repeats for every public scope it reenters.

**Problem:** implicit common-directory ownership made a checkpoint in one linked
worktree fail while another checkout's writer/read snapshot was active.
**Decision:** remove that implicit claim from both single and combined checkout
admission. Alternate-index checkpoints keep their own checkout exclusion;
Git native index/ref locks, ref CAS and integration repository transactions
protect actual shared publication. Same-checkout edits, stable reads, metadata
operations and destructive tree retirement retain their existing exclusion.
**Alternatives rejected:** longer waits/model retries preserve false contention;
dropping all leases loses same-checkout capture and file transaction protection.
Specs: linked cross-process admission in `test_workspace_lease_precise.py` and
real checkpoint/submit with staged peer edits in `test_integration_control.py`.
Local/FUSE admission probe (30 samples after 5 warmups, same host, baseline
`b6191b736`): median root admission changed 1.480→1.169 / 7.019→6.374 ms;
held descriptors 12→11 / 26→25, zero descriptors/holders after release.
Peer checkout changed from blocked to admitted on both volumes. This removes
one common-directory key from each checkout admission, adds no cache/worker or
persistent key family, and is overhead/lifecycle evidence, not a throughput SLO.

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

Every project tool admission re-registers its frozen policy into that
projection, so the process-wide `config._lock` covers dictionary updates only.
Problem: sticky-cwd realpath containment, cross-DC latency classification and
Git branch probes ran under it, so concurrent admissions queued behind
filesystem and subprocess latency. Decision: those checks run after release;
retiring a cwd the new roots no longer contain is compare-and-set against the
exact registered roots object, and re-registering the same object keeps its
sticky cwd (`get_conv_cwd` still re-validates every read). Rejected: caching
realpath results needs filesystem invalidation, because a retargeted symlink
changes containment; skipping an unchanged re-registration loses LRU recency.
Spec: `tests/project_mod/test_root_registry_lock_io.py`. With the per-admission
guidance read memo (`docs/CONTEXT_INJECTIONS.md`), admission on this host's
FUSE checkout went from p90 46.0 to 20.1 ms on one thread and 81.9 to 54.3 ms
across eight concurrent threads.

Host-local measurement: 16 roots, 10 warmups and 1000 scope+lookup samples
observed median **0.017 ms**, maximum **0.027 ms**; not a network-volume guarantee.

### Explicit stable readers

Command writer admission (claims derived per shell command, fences for
unproven shapes, file leases for the write tools) was removed on 2026-10-28
(top of this document). Explicit stable readers retain `workspace_read_lease`.
In-process readers can coexist; a writer excludes them and they exclude a
writer. A writer may reenter read without downgrading. The sole reader may
upgrade locally to writer without releasing the OS lock; a second concurrent
reader blocks an upgrade.

POSIX `flock` is **open-file-description scoped**, not thread scoped. Explicit
whole-checkout read/write APIs keep one `LOCK_EX` flock per checkout and
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

### Conflict model: nested worktrees, admission order, both sides named

Every logical claim expands into physical keys (`_physical_claim_plan`).
`write` excludes everything on its key; `file-write` barriers share with
each other and exclude writes and reads; `read` shares with reads only.

| Claim | Exclusive key | Shared `file-write` barriers |
|---|---|---|
| `tree:P` | `tree:P` | `tree:` every strict ancestor of P; `git-metadata:` P and its ancestors |
| `root:C` | root key of checkout C | `tree:` ancestors of C, its gitdir, common dir and lease dir; `git-metadata:` ancestors of the target |
| `file:F` (checkout C) | file key F | root key of C, plus the `root:` barriers with F's own path |
| `metadata:C` | `git-metadata:<common dir>` | root key of C; the `root:` tree barriers; `git-metadata:` ancestors of C and of the common dir |
| stable read of C | root key of C in `read` mode | the `root:` barriers |
| no claims (every model tool call) | none | none |

Consequences for linked worktrees nested under the checkout they belong to
(the swarm layout `ROOT/.tofu/worktrees/A`):

- Any claim inside A holds a shared barrier on `tree:ROOT`, so exactly the
  exclusive `tree:` claims on ROOT or another ancestor of A conflict with it.
  Such a claim (a destructive tree claim on the root checkout) waits for, and
  then blocks, every claim in every nested worktree.
- `root:ROOT` (a delivery or checkpoint publication), `metadata:ROOT`,
  `tree:B` + `metadata:ROOT` (reclaiming worktree B) and file claims in B
  (task-delta publication) touch none of A's keys and are admitted while A's
  holder runs. Reclaiming A and publishing A's files wait for A's tree/root
  claims, as they must.
- All worktrees share one `git-metadata:<common dir>` key: a long metadata
  holder (Git maintenance, a reclamation) delays every other metadata claim.
  Refs, objects and worktree administration are genuinely shared.

`tests/project_mod/test_workspace_lease_conflict_model.py` pins this matrix
for four holder kinds in A.

The ancestor barrier stays among Tofu's own operations. Model tool calls hold
no claims since 2026-10-28, so the incident that motivated that decision (a
`git log -i -E --grep=...` classified as a tree claim on ROOT waited an hour
behind a test run whose quoted `$(date)` fenced its nested worktree) cannot
recur.

**Admission order (2026-10-28).** Problem: admission had no order. A
`file-write` barrier was admitted whenever no writer held the key. An
exclusive request could therefore wait forever while overlapping shared holders
(file claims in sibling worktrees) kept its key busy. Measured: an exclusive
`tree:ROOT` request against a relay of overlapping 0.2 s edits was refused
after 10 s. Decision: a blocked request queues on its key in arrival order. A
fresh request may not overtake an earlier queued request of a conflicting mode
while every holder blocking that request is younger than
`_QUEUE_ORDER_MAX_HOLDER_AGE_SECONDS` (2 s, a fixed constant). The same writer
is now admitted in 0.21 s. Behind an older, long-running holder the earlier
request cannot be admitted anyway, so compatible newcomers proceed at once
(7 ms measured) instead of freezing until that holder ends. A thread that
already holds the key is never held back. Cancelled and timed-out requests
leave the queue, and a fail-fast probe that would overtake is refused with
`admission is queued behind an earlier conflicting request`. Because the bound
is on holder age, queue order can never sustain a lock-order cycle: holders in
a cycle stop releasing, cross the bound, and the order lapses. Alternatives
rejected: writer preference (every file claim in every nested worktree would
freeze behind a writer waiting for an hour-long holder); strict FIFO without the bound
(a lock-order inversion between shared requests would become a deadlock); a
queue or limit sized by load or hardware (principle 3); cross-process turnstile
lock files (the server is one process; `flock` itself stays unfair across
processes, a documented residual).

**Both sides named (2026-10-28).** Problem: the wait line and the
`admission_wait` span said only `write lease on tree:tofu behind ... (task ...,
held 2996s)`, so diagnosis needed a log join. Decision:
`workspace_claims_lease(purpose=)` names the request; single-target APIs name
their own claim. The outermost request of a thread is captured with each
published hold and queued request. The wait line adds `claims=`/`cmd=` for the
waiter and `mode=`/`claims=`/`cmd=` per blocker. The
`workspace_admission_wait` event carries `waiterClaims`, `holderMode`,
`holderClaims` and `holderCommand`; the span adds `lock` and `holderTask`. The
purpose summary (one line, at most 160 characters) reaches a refusal only when
the blocker's task has the waiter's owner, the `TODO(enterprise)` seam on
`_userId`; operator logs always carry it. Alternatives rejected: a registry of
running operations (a second owner of state the hold already carries).
Specs: `tests/project_mod/test_workspace_admission_wait_both_sides.py`.

### Wait, freshness, descriptor, and journal lifecycle

- Internal lease waits have no default cutoff. An explicit
  `TOFU_WORKSPACE_TOOL_WAIT_SECONDS` supplies an exact nonnegative finite wait
  for the task-bound internal callers that read it (zero is fail-fast);
  combined target leases share that deadline. Task abort/runtime cancellation
  is checked during acquisition and again before admitting the body.
  Contended waits probe at most every 50 ms; uncontended paths do not sleep.
- **A wait is observable.** After two seconds of waiting, and then at most once
  per minute, the lease owner logs `[WorkspaceLease] wait ...` with the
  waiter's bounded pid/thread/thread_name/tool/task/conversation, claims and
  purpose, and the actual local blocker (pid, thread, held seconds, mode,
  thread_name/tool/task/conversation, claims, purpose; see "Both sides named"
  above). The thread name identifies a holder outside any tool scope
  (settlement, delivery, checkpoints). Cross-process flocks report the holder
  type as unknown instead of inventing one. No worker, timer or retained state
  is added: the notice is emitted from the existing 50 ms probe loop.
- **The wait is not a wedge.** A task-bound internal wait (dependency setup
  before a worker command, a stable integration snapshot) produces no process
  and no output, so both task-liveness clocks are stale from its first second.
  The wait owner (`workspace_lease._publish_admission_wait`, keyed by
  `tool_admission.WORKSPACE_ADMISSION_KEY`) publishes `{since, depth}` for
  exactly the acquisition span and drops it before the body; parallel waits of
  one task derive one marker from the acquisitions still in progress, so they
  may end in any order. The stuck-task reaper and the invocation stall guard
  read one class owner (`lib.tasks_pkg.explained_wait`). A command proxy
  (`DetachableCommandTask`) drops another call's marker at creation. This
  grants no cancellation immunity: exit is explicit Stop, an execution
  deadline, or the operator bound above. Specs:
  `tests/project_mod/test_workspace_admission_wait_marker.py`.
- A claim covering internal neutral lease storage raises
  `WorkspaceLeaseNamespaceError`, a static scope refusal, not evidence of a
  competing holder. Actual namespace removal remains forbidden.
- Write tools serialize one call per resolved file in process
  (`file_edit_mutex.py`): the mutex covers the preimage read, edit,
  publication and undo journaling of that call and is released before
  optional publication callbacks run (`post_commit.defer_publication`). After
  acquiring it, the writer re-resolves every target (a changed target refuses
  with `workspace-target-changed`) and refuses a stale observed version with
  `write-freshness-stale` before reading a preimage; pure append consumes the
  current preimage instead. Different files never wait for each other, a
  thread may re-enter a path it holds, multi-file batches acquire in sorted
  order, and entries exist only while a call holds or awaits them. The mutex
  does not coordinate shell commands, other processes or external editors;
  across processes the project journal session lock still refuses a
  competing tracked write.
- Release is **close-only, never `LOCK_UN`**: an inherited child descriptor
  continues excluding until its final close, even after parent release. Crash
  cleanup follows OS descriptor ownership. After `fork`, the child closes
  inherited active/pending registry descriptors and resets process-local gates;
  inherited context teardown cannot close a reused FD or erase new child
  ownership. An exec child can inherit deliberately passed FDs without inheriting
  a Python registry claim. A command started by an internal lease holder
  inherits its descriptors, so its exclusion lasts until the group exits.
- Registry entries exist only for active holders/waiters. Lock files have no
  durable owner state: physical ancestor, metadata, checkout and file keys are
  small inodes, and released transactions leave no live registry ownership or
  FDs. A lock file is unlinked only under a quiescence proof, never by age/TTL
  stealing: the last local release of a key (and the boot sweep
  `reclaim_unheld_lock_files` of the per-user tree namespace, for files killed
  holders left) opens a fresh descriptor, takes a non-blocking exclusive flock
  and unlinks the name only if it still names that inode. Any holder, shared
  holder or descendant that inherited a lease FD keeps the flock and therefore
  the file. An acquirer that opened the name before such an unlink finds, after
  its own flock, that the name no longer names its inode and reopens it, so no
  writer is ever admitted on a displaced inode. A cross-process fail-fast
  acquirer that races a reclamation sees the ordinary busy refusal, as if the
  release had lasted the reclamation longer. Measured 2026-11-08: reclamation
  adds about 0.05 ms per released key on local disk and 1.0 ms on the dolphinfs
  FUSE mount (acquire plus release I/O was 0.15 ms and 1.15 ms). Before it,
  2.45M never-unlinked files had accumulated under
  `/tmp/tofu-workspace-tree-<uid>/tofu-workspace-leases`. No polling service is
  added.
- A lease is scheduling/exclusion, never write authorization. Non-Git ROOT,
  read and metadata scopes invent no checkout identity; canonical FILE and
  explicit TREE scopes still protect non-Git targets.

### Descendant quiescence before command settlement

Problem: TERM could end the session leader while a descendant ignored it and
retained inherited lease FDs. Clearing PID fields/local registry did not release
the OS exclusion, so Stop returned but subsequent writes remained busy.
Decision: `process_group.py` retires the owned group, not just the leader: TERM,
finite leader grace, KILL remaining members, then bounded live-member observation.
A command that inherited an internal lease holder's descriptors also waits
for its live group after leader exit; closed output pipes cannot turn that wait into a hot loop. Opaque commands
without inherited leases retire what they leave in their group when they
finish ("A finished command's result survives its group's retirement" below). Linux
zombies are quiescent because their FDs are closed. Launch records the
owned new-session PGID before callbacks; Linux recovered records require a
valid matching leader starttime and observed group before signalling. Missing
signal authority is not confirmed retirement: cancellation stays owned/visible
while any recorded group members remain, without signalling guessed or reused
PIDs. Invalid/current-group records do not allocate a recovery watcher.
Namespace-supervisor retirement
retains its dedicated init/reaping path. Alternatives rejected: dropping FDs
before workers stop weakens exclusion; unlinking lockfiles splits authority;
trusting an arbitrary/stale PID risks signalling unrelated work. No new resident
worker, timer or durable state is added. Specs:
`tests/tasks_pkg/test_run_command_descendant_retirement.py` cover normal abort,
TERM-resistant descendants, an already-reaped leader, reattachment, inherited
lease reacquisition, and stale/current-group refusal.

### Groups a command's descendants created retire with it

Problem: GNU `timeout` (without `--foreground`), `setsid` and job-control
shells move a child into a process group of its own. Stop, a deadline, the
`kill` tool and the cancel callback signalled only the command's group, so
those subtrees outlived all of them. The shell died and its children were
re-parented, still running in the worktree. One test gate under `timeout` left
32 xdist workers, its pytest, multiprocessing trackers and a `cargo build`
behind (2026-11-02 and 2026-11-06). The `kill` tool promises the process tree.

Decision: before any signal, `process_group.escaped_descendant_groups` finds
every group that a still-linked descendant of the leader leads, or that lies in
the leader's own session (`start_new_session` makes every member of that
session the command's). It walks kernel child lists (`descendant_pids`) and
reads the leader's start time before and after, so a reaped and reused PID
links no subtree; reattached background records pass their verified start
time. `retire_process_group(..., escaped_groups=...)` sends those groups TERM
with the owned group, gives them the same grace, then KILLs and awaits them; an
unconfirmed one raises the usual retirement error, which `run_command` reports
as its typed warning. `run_command` discovers while its leader is unreaped and
remembers the groups on the process: the cancel callback's immediate TERM ends
the shell and re-parents its children, yet still reaches those groups, and the
retirement that follows still awaits them. Work that detached completely before
the signal (`setsid -f`, a double fork) is not linked, is never found and keeps
running, as after a normal exit. A command that exits normally keeps its
contract: `_retire_lingering_group` stops only leftovers of its own group.

Alternatives rejected:
- Signalling each descendant PID: a re-parented member is reachable only
  through its group, and a bare PID can be reused between TERM and KILL.
- Scanning the host for the command's session: a per-caller whole-host scan,
  and a `setsid` child still escapes into a new session.
- A child subreaper for every command: it changes reaping and exit-status
  ownership for all commands.
- Rewording the tool description: the defect would become the contract.

No new resident worker, timer, knob or durable state is added. Specs:
`tests/tasks_pkg/test_run_command_descendant_retirement.py` covers `timeout`,
`setsid` and a TERM-ignoring child under `timeout`, each through Stop on both
runners, the cancel callback's TERM-first path and reattached records. It also
covers discovery, the leader-identity refusal and refusal of the current group.
Measurement (2026-11-06, this host): before the change all 14 of those cases
failed, every escaping subtree outliving Stop, cancel and reattached kill;
after it, none survives and the existing retirement specs still pass.

### A finished command's result survives its group's retirement

Problem: `retire_process_group` raises `Process group N did not retire` when a
member is still alive two seconds after SIGKILL, and both runners turned that
exception into the tool result (`Error executing command: ...` or `Error during
interactive execution: ...`, `[exit code: -1]`), discarding the collected
output and the command's own verdict. Members in uninterruptible (D) waits on
the FUSE checkout outlive that bound: after stall-watchdog interrupts, one
group quiesced one second later and another 52 s later. Separately, a command
that exited normally left anything it started with `&` running in its group,
and its foreground record was released, so nothing would ever retire it.
Decision: retirement never replaces a decided outcome. `run_command` decides
output, exit status, timeout, interrupt and Stop first; `_retire_command_group`
then kills the group and, when it cannot confirm quiescence (or the leader
outlives its wait), appends the typed line `[warning
process-group-retirement-incomplete: ...]` before the terminal status lines,
names the group with a `pgrep -a -g` recipe, logs a WARNING, and keeps the
foreground record so the boot sweep retires survivors after a restart. When the
leader exits normally, `_retire_lingering_group` gives members the TERM grace by
group observation, kills what remains and reports `[warning
process-group-lingering-stopped: ...]`; work meant to outlive a command uses
`background=true` or `setsid`. Background results rebuilt from their record
carry the runner's warnings (`split_group_warnings`). `group_has_live_members`
answers an empty group with one `killpg(pgid, 0)` probe instead of a `/proc`
scan. Alternatives rejected: a longer KILL wait (a tuned bound that a stalled
mount still exceeds; a D-state member exits only when its I/O returns); waiting
without bound (one stuck FUSE request would hang the tool call); leaving
leftovers running (an unrecorded process outlives its owner); retrying
retirement in a new watcher thread (resident work for processes already sent
SIGKILL). Measurement (2026-10-31, this host, real runners): before, 4/4
leftover fixtures (TERM-honouring and TERM-ignoring, both runners) stayed alive
after the call returned with their records released, and 2/2 unconfirmed
retirements replaced the output and interrupt verdict with the error line;
after, 0/4 survive, 4/4 keep output and exit code with the warning, and 2/2
unconfirmed cases keep output, verdict, warning and record. The empty-group
probe costs 0.001 ms against a 0.63 ms scan of 137 processes. Specs:
`tests/tasks_pkg/test_run_command_descendant_retirement.py`,
`test_group_warning_reaches_the_durable_result` in
`tests/tasks_pkg/test_poll_kill.py`,
`tests/project_mod/test_foreground_command_records.py` and
`tests/project_mod/test_process_group_thread_quiescence.py`.

### Linear checkpoint publication

`linear_git_checkpoint.py` acquires the same lease before its existing Git lock
for capture/baseline activation. Verification runs on a private checkout of the
captured commit, outside the lease; publication writes no checkout file and
takes only the Git lock and `index.lock`. Promotion rechecks the symbolic
branch, stable identity and candidate absence; a branch that moved since
capture is carried or deferred, and writers may keep editing throughout (see
[project integration](../modules/project_integration.md), "Linear checkpoints
publish in a busy checkout").

Initial baseline activation uses one ref transaction with HEAD/candidate CAS.
It first archives publication refs that share no history with HEAD (a rewritten
history; see [project integration](../modules/project_integration.md)), then
refuses pre-existing unowned stable/candidate history and divergent owned
HEAD/stable history rather than advancing another integration controller. A
stable that merely trails HEAD is not divergent.
Publication CAS verifies candidate absence and old branch/stable SHAs.

The real index is reserved with Git's `index.lock`. Only the entries of the paths
the publication changes move, each only from the base version to the target (an
entry already at the target stays), in a private prepared index installed after
successful ref publication. Entries another writer staged, unmerged or
sparse/assume-unchanged entries and a missing index are preserved; a busy index
or a Git operation in progress defers publication. Failure removes only this operation's temporary
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
- Writer leases derived per shell command, however fine-grained (2026-10-28,
  owner decision at the top): every refinement left another read misclassified
  as a whole-tree writer, and each such misclassification cost a wait as long
  as another session's command.
- Automatic read lease for opaque commands: still blocks writers for the entire
  test/build lifetime, serializes cross-process runs under the existing flock,
  and falsely suggests that artifact-writing opaque programs are stable readers.
  Keep stable reads explicit rather than weakening genuine writer exclusion.
- Cross-process reader-parallel redesign: separate opens can self-conflict and
  SH→EX conversion is not an atomic upgrade. Explicit stable-read APIs retain
  the single EX flock plus local reader gate and close-only child lifetime;
  FILE transactions use shared barriers with exclusive targets. Removing
  unnecessary automatic holds addresses the contention without an API rewrite.
- Rewrite real index before CAS, then rollback: assumes index equals HEAD and
  can overwrite staged or concurrent edits. Private preparation plus index.lock
  and CAS preserve the preimage without speculative rollback.

## Owners, integration seams, and limits

| Concern | Owner |
|---|---|
| Parameter classification / mutation target extraction | `lib/project_mod/command_analysis.py` |
| Git target resolution for approval and presence | `lib/project_mod/git_command_scope.py` |
| Per-call cancellation, recorded command source, isolated-workspace target check | `lib/project_mod/tool_admission.py` |
| Presence and refusal/legacy ack compatibility | `lib/project_mod/git_guard.py` |
| Cooperative lease for Tofu's own operations | `lib/project_mod/workspace_lease.py` |
| Write entry points | `lib/project_mod/write_tools/_ops.py` |
| One write call's per-file read-modify-write | `lib/project_mod/file_edit_mutex.py` |
| Command lifetime / inherited lock FDs of internal holders | `lib/project_mod/run_command.py` |
| Checkpoint capture / publication | `lib/linear_git_checkpoint.py` |
| Dispatch, in-process copy fast path, outer snapshot/journal | `lib/project_mod/tools.py` |
| Approval / active-peer decision | `lib/tasks_pkg/tool_dispatch/_pipeline.py` |

This is not a filesystem sandbox. External editors, direct Git commands, scripts
writing undisclosed roots, Git-environment redirects, and remote filesystems
without coherent flock semantics do not become cooperative automatically.
Model tool calls, shell commands included, are not cooperative either: they
take no lease, so a command may write a checkout while Tofu snapshots,
publishes or checkpoints it; those operations verify trees and refs by
compare-and-swap. Use an isolated worktree for programs whose checkout-wide
mutation must be kept apart.

The conversation freshness recheck belongs to file-write/edit tools, **not
arbitrary `run_command` shell writes**. A tracked `cp` snapshots the current
target and journals that preimage, but does not refuse because the
conversation read older target bytes.

Presence's `git -C` attribution is a separate dispatch concern. Shared refs/stash
across otherwise independent worktrees need the existing integration/Git CAS
boundary; per-checkout leases intentionally do not serialize every shared ref.

## Verification

Only private temporary repositories are mutated by these directed tests; no
application service or real shared-repository destructive command is required.

```bash
python3 -m pytest -q tests/project_mod/test_tool_calls_take_no_workspace_lease.py \
  tests/project_mod/test_command_execution_preparation.py \
  tests/project_mod/test_git_read_hardening.py \
  tests/project_mod/test_file_scoped_write_lease.py \
  tests/project_mod/test_file_scoped_upload_target.py
python3 -m pytest -q tests/project_mod/test_workspace_lease.py \
  tests/project_mod/test_workspace_lease_conflict_model.py \
  tests/project_mod/test_workspace_admission_wait_both_sides.py \
  tests/project_mod/test_workspace_admission_wait_marker.py
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
```

Use host-compatible Git/Python/Node binaries; the checkpoint ESM syntax test
invokes Node. When validating an occupied worktree, run from a private temporary
cwd with absolute test paths and that worktree on `PYTHONPATH`; put pytest cache
and temporary repositories outside the shared checkout.

`test_tool_calls_take_no_workspace_lease.py` is the executable form of the
owner decision at the top. `test_workspace_lease.py` specifies the lease
primitive for Tofu's own operations: exclusion/reentry, realpath aliases,
independent worktrees, cross-process busy/crash cleanup, explicit-reader
coexistence and sole-reader upgrade, finite/cancellable waits, accurate local
reader/writer versus unknown-process errors, active/pending descriptor cleanup,
fork/reused-FD teardown safety and persistent lock inodes.
Freshness tests retain the conversation overwrite gate; file-write and
batch-edit tools reject peer-changed bytes after acquiring the edit mutex,
whereas shell `cp` uses the current preimage without a conversation-token
refusal. Checkpoint tests exercise staged index preservation, failed CAS without
rollback, HEAD movement, foreign candidate/stable refusal, and capture deferral.
Journal tests retain admission, ordered history projections and fork cleanup.

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


### Default lock waiting decision

Problem: a healthy conflicting holder caused internal lease callers to refuse
after five seconds; caller wait bounds above 600 seconds were rejected
independently of actual machine capacity. Decision: internal lease waits own
acquisition until cancellation or an explicit operator bound. `None`
propagates through checkout, tree, metadata and file fences; no layer silently
recreates a deadline. Low-level omitted lease timeouts remain fail-fast for
callers choosing immediate probes (delivery and integration publication).
Alternatives considered: skipping exclusion between Tofu's own publications
permits concurrent corruption; increasing the private deadline still rejects
healthy work; infinite numeric sentinels bypass finite-value validation.
Model tool calls take no lease and therefore never wait (top of this document).

### Isolated-workspace targets and background records

Problem: snapshot completeness was incorrectly reused as write authorization:
opaque build commands could lose their known literal output targets. Background
execution also introduced system-owned log redirects into checks intended for the
user command, while dropping the task's runtime-root context.

Decision: the isolated-workspace target check
(`workspace_delivery.workspace_target_refusal`, access control, not a lease)
retains known literal write targets independently of whether a complete undo
snapshot is possible. Resolve them physically and reject canonical/peer
checkout writes; incomplete analysis remains incomplete for snapshotting.
Opaque execution is not an operating-system sandbox. Background dispatch
preserves the issued runtime-root context and uses the original command only
when the existing internal receipt binds it to the exact recording wrapper.
User arguments or a lookalike wrapper never confer that provenance. Recording
paths remain owned by the background-record lifecycle; this is not a general
path or authorization exemption.

Alternatives rejected: disabling workspace checks, treating incomplete targets
as an empty write set, or trusting a caller-supplied original-command string.
