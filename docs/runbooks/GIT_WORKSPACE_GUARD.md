# Git workspace guard and cooperative writer lease

Domain map: [`project integration`](../modules/project_integration.md).

Tasks execute directly by default; independent parallel edits can opt into
isolation through the [execution policy](../modules/project_integration.md#execution-policy-direct-by-default).
Both modes retain the authorization, freshness and cooperative guards below.
Direct writes remain visible after task failure/cancel; they are not transactions.

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
| `readonly` | status/log/diff/show without output writes; branch/tag listing; stash list/show; proven clean dry-run | Existing read authorization |
| `safe_write` | additive add/ordinary commit (appends history), ordinary apply/--cached, branch/tag creation, ordinary fetch/push, normal merge/cherry-pick/revert, checkout -b/switch -c without start point, branch -d/--delete | Existing write authorization/receipt, **plus writer lease** |
| `tree_destroying` | checkout -B/--orphan, switch -C/--force-create, branch -D/-M/-f and any bundled-force short flags (-df/-fd/-vd), tag -d/-f, commit --amend, update-ref/symbolic-ref mutation, force push/fetch, stash mutations, reset/restore/actual clean/rebase, integration abort/forced/strategy options | Presence-aware refusal plus writer lease if authorized |
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
irrelevant to this path and is not probed. Abort/quit/skip/autostash and forced/strategy forms retain destructive policy.
Unknown, malformed or signing/external-program integration options retain the
existing approval path, not an invented proof of working-tree destruction.
A known destructive option elsewhere in the same invocation still wins risk
classification; unknown options prevent an automatic receipt even when solo. Ordinary integration
still needs write authorization; `safe_write` is not an exclusion exemption.

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
rejected alternative: bypassing presence or checkout leases for all merges.
Specifications: `test_command_analysis_git_guard.py`, `test_git_guard_pipeline.py`.
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

Problem: isolated-worktree cleanup was first unclassified, then inherited
presence from the invoking checkout merely because it shares Git metadata.
An unrelated conversation generating in the main checkout could veto removal
of an idle verification tree. Decision: literal absolute-path `worktree
remove|move` remains `tree_destroying`, with presence derived from its actual
tree effects. Removal checks the target; movement checks the source and actual
destination, including Git's basename append when moving into an existing
directory. Missing destinations retain their own identity instead of inheriting
presence from a containing checkout.
Shared Git metadata still takes its existing exclusive lease. Main-checkout
source edits can proceed while an unrelated tree is retired; target-file
writers and shared Git metadata mutations still exclude cleanup. Configured
helpers or unresolved redirects retain conservative source-tree admission
and presence. Each other destructive segment retains its own presence roots.

The shared `command_analysis.git_worktree_lifecycle` parser owns syntax for
classification, effects and diagnostics. Relative operands retain the existing
approval gate; execution resolves them only with proven same-repository source
identity. `~`, unknown options and missing/extra operands remain unresolved.
Diagnostics identify worktree retirement and target/administrative effects,
rather than suggesting the invoking checkout's files/index will be reset.

`--force`/`-f` bypasses Git's dirty/locked-target protection, so solo presence
never mints an automatic cleanup receipt. Force keeps the existing explicit
approval review for target ownership, active users/commands, retained commits
and discarded tracked/untracked/ignored content. This applies even to `/tmp`,
known virtualenv links, repeated force flags and otherwise ordinary chains.
Native Git refuses dirty/locked/main-tree removal without force. Neither a
clean status nor absent presence proves task ownership or external-process
quiescence. Existing execution/task ownership and cooperative writer leases
remain independent; opaque tests/builds still provide no stable-read promise.
No cleanup nonce, persisted ownership registry or pre-approval status snapshot
is introduced. Alternatives rejected: allowing every force cleanup, a `/tmp`
whitelist, or treating project-wide presence as a writer lock.
Specifications: `test_worktree_cleanup_admission.py`,
`test_git_guard_presence_targets.py`, `test_command_analysis_git_guard.py`.

Concurrency evidence: private real-Git tests complete removal/movement while
another thread holds a main-source file lease, refuse under a target-file or
common-metadata lease, and fence a target-status callback writing main-source
bytes. A 100-call local FUSE scope-resolution sample measured median 7.6 ms
before / 11.4 ms with target configuration inspection (p95 21.8 / 30.6 ms).
Resolution starts no subprocess and retains no cache, worker or registry;
the extra metadata reads preserve callback evidence while removing unrelated
source-tree exclusion. These host-dependent timings are not latency limits.

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
effect domains and action codes. Shell arguments/messages/configuration values
are not copied into the diagnostic, and neither advisory peer labels nor
suggested actions are authority. Unknown syntax is distinct from known danger.

Native effect annotations do not exclude configured helper effects: the physical
resolver still owns target unions and lease broadening. A refusal always states
`execution=not_started` and denies automatic-receipt eligibility even when its
syntax is fully known. Unknown-form approval denials carry the same diagnostic
source with `evidenceLevel=syntax_unresolved`, not advisory writer evidence.
Guidance starts with read-only `git status`, staged diff and target-path checks.
For task integration, do not stash/reset/abort shared work as preparation:
ordinary merge/cherry-pick retain their own admission; if Git itself refuses
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
`TOFU_GIT_GUARD=0` retain the existing approval fallback; neither disables the
writer lease. No-active-peer is not proof that the workspace is clean or safe.

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
LLM judgment as execution authority, or disabling presence/leases to improve UX.

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
no-optional-lock, fsmonitor-disabled read path; output redirects and subsequent
writers retain their own claims.
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
checkout; leasing both checkouts' working files serializes unrelated work.
Decision: additive `worktree add -b` or `--detach` takes source Git metadata
admission plus the actual destination tree, not source checkout ownership.
The destination remains fenced throughout success-dependent follow-up commands:
Git's own ref lockfiles do not exclude raw file edits after creation. Configured
helpers add conservative source-tree admission. `extensions.worktreeConfig`
alone is not helper evidence: admission also inspects `config.worktree` in
each relevant Git directory (including linked targets). Includes, executable
settings, redirects and unreadable/unsupported configuration stay conservative.
A nonexistent destination is
recognized as a future checkout only across success-dependent `&&` segments.
Existing destinations and `;`, `||` or pipeline control resolve normally.
Git writer admission follows only global `-C` hops before the subcommand and
leases the final checkout; option values, messages and pathspecs cannot add
false roots. Explicit `--git-dir`, `--work-tree`, `GIT_DIR`, `GIT_WORK_TREE`
`GIT_INDEX_FILE` and `GIT_COMMON_DIR` participate in target ownership (relative
common-dir resolves against effective cwd after `-C`); setup normalization never
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

- Exclusion follows significant effects, not conversation presence or the
  invocation directory. Ordinary Edit holds a canonical-file transaction;
  different files and unrelated checkouts can proceed concurrently. A real
  native transformation of the same checkout or deletion of its containing tree
  still excludes source-file transactions. Native checkout barriers do not
  turn exempt runtime/data files into source writes; actual tree effects fence
  those files too. Git index/ref-only work uses metadata
  exclusion, not source-file exclusion. Risk approval remains independent.

| Effect | Admission |
|---|---|
| Hardened reads, opaque tests/builds | No implicit lease or stable-read promise |
| Proven literal regular-file writes / Edit | Canonical target file transaction, including non-Git workspaces |
| Git index/ref-only operations | Git metadata domain; source edits continue |
| Checkout transformations / publication | Affected checkout, not parent/peer checkouts |
| Recursive removal, movement, checkout retirement | Actual affected tree, including nested/external checkouts |

Problem: ancestor checkout barriers made short integration writes plus long
validation block unrelated nested worktrees. Decision: distinguish source-file,
metadata, checkout and tree effects; preserve same-file atomicity, freshness and
explicit destructive containment. Alternatives rejected: removing every lock
loses updates; treating every command as a checkout writer serializes unrelated
work; splitting/replaying arbitrary shell source changes execution semantics.
- A compound command's claims cover its full supervised lifetime. Separate
  short setup/mutation from long tests where shell semantics permit; plain
  build/test calls are already lease-free. `background=true` releases the model
  loop, not a writer's inherited exclusion. Never drop the lease while that
  process can still mutate its protected checkout.
- Diagnose the exact admitted command and loaded runtime version before
  changing classification: a fixed source file does not prove a long-running
  server has loaded it. Do not weaken leases to work around an unknown suffix
  or assume the logger's bounded command prefix is the complete invocation.
### Identity, topology and acquisition transactions

`workspace_lease.workspace_claims_lease(claims)` is the combined transaction
boundary for multi-target command, batch-edit and MCP admission. It expands all
implicit barriers, merges duplicate physical identities using the stronger
mode, then acquires the flattened set in phase order: **tree, metadata,
checkout, file**. Within each phase it sorts by lock namespace/common identity
and canonical key; metadata ordering is independent of checkout aliases.
Only after the complete physical set is held does it revalidate the original
target bindings and enter the public scopes reentrantly. One deadline covers
all acquisition; failure unwinds acquired descriptors before the body runs.

Optional release hints are coalesced until the final thread-owned physical scope
has closed and left the registry; callbacks never race its remaining barriers.
The bounded depth/pending state is reset after fork and is not an owner ledger.
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
resident cache/worker or concurrency cap was introduced; opaque builds/tests
remain lease-free.

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

Host-local measurement: 16 roots, 10 warmups and 1000 scope+lookup samples
observed median **0.017 ms**, maximum **0.027 ms**; not a network-volume guarantee.

### Selective writer admission and explicit stable readers

- **Identified mutations** acquire typed writer admission: FILE for normal
  file tools and proven regular-file shell targets; METADATA for proven Git
  index/ref-only effects; ROOT for native checkout transformations and
  whole-checkout capture/publication; TREE for destructive or ambiguous effects.
  Multi-target callers use `workspace_claims_lease`, not separately nested
  single-target leases. In-place utilities and redirections are still writes.
  Git output redirects are unioned with Git's own effects, including hardened
  queries: an output leaf cannot replace or erase metadata/checkout/tree claims.
  Inline `-c` includes/conditional includes, unknown config and executable
  helper evidence remain conservative; disabling hooks alone does not prove
  includes, filters, signers, fsmonitor, pagers or shell aliases harmless.
  Native checkout transformations exclude source edits only in that checkout;
  shared metadata can still serialize linked Git writers, not unrelated Edit.
  Helper-bearing/ambiguous Git forms combine tree and metadata admission, scoped
  from the actual checkout even when invoked from a subdirectory. Helper-free,
  non-staging commits use metadata only; signatures, staging and index/rewrite
  callbacks must not borrow this exemption. Unresolved Git output filenames or
  inherited descriptor targets refuse before execution rather than lose scope.
  Shell interpreters/wrappers, dynamic
  syntax, malformed input and uncertain mutating utilities retain writer
  admission. Parsing includes mixed/piped segments and helper-bearing Git forms;
  snapshot-readonly classification alone is not a lease exemption.
  `_segment_needs_command_writer` and `command_write_lease_claims` in
  `run_command.py` own this decision, using mutation/target analysis from
  `command_analysis.py`.
- Literal utility targets share `command_analysis.command_segment_targets` and
  its source-span shell tokens: grouped sort output/temp flags, cp/mv target
  directories, quoted sed/awk inplace operands and find action boundaries retain
  the actual written checkout. Predicate/format data is not interpreted as an
  action. Unknown mutating options retain conservative literal candidate roots;
  a dynamic source does not erase a separate literal destination. Bounded
  hardened `git check-ignore` stays lease-free; safe cherry-pick/revert mainline
  grammar retains ordinary writer admission without changing execution.
  Explicit `find -L/-follow` mutations and execdir/okdir relative parent-traversing
  writes refuse before spawn (`workspace-write-scope-unsupported`): runtime
  traversal cannot be replaced by guessed cwd claims. Use concrete/absolute
  targets instead. The parser is still cooperative, not a sandbox for embedded
  programs or wholly dynamic destinations. Specs:
  `tests/project_mod/test_audited_command_admission.py`.
- **Bounded substitutions coordinate their files.** Problem: target discovery
  recognized `sed -i` operands, but writer admission discarded that evidence and
  claimed the entire cwd. A simple edit beside internal lease storage therefore
  looked like removal of the storage itself. Decision:
  `command_analysis.literal_sed_effects` owns the maintained substitution
  grammar for readonly classification, target discovery and file admission.
  Literal punctuation delimiters, ordinary pre-operand flags, `--`, and
  individually proven `-e`/`--expression` programs share that proof.
  No-suffix inplace edits acquire all verified existing regular target files
  together, including multiple/outside-cwd operands. These claims remain live
  through the entire supervised command chain; a following opaque interpreter
  gains no separate readonly or write-set guarantee.
  Escapes, addresses, script files, additional commands, execute/write flags,
  backups, option permutation and unknown options retain conservative admission.
  Symlinks, hardlinks, metadata and unverified leaves cannot borrow FILE scope.
  [GNU sed's option contract](https://www.gnu.org/software/sed/manual/html_node/Command_002dLine-Options.html)
  includes temporary replacement and optional backups; a named inplace operand
  alone is not evidence that every effect is confined to that file.
  Alternatives rejected: exempting `/tmp`, treating every inplace editor as a
  file-only writer, or maintaining another argv parser in the runner.
  Real-process specifications in `test_sed_write_admission.py` demonstrate
  unrelated file-writer overlap, all-target exclusion before snapshot/spawn,
  native substitution results, and release after both success and refusal.
  This adds no worker, cache or persistent lock namespace.
- **Opaque commands** such as pytest/make/npm/python interpreters do not acquire
  an automatic writer or reader lease when no checkout mutation is identified.
  They can overlap each other and cooperative writers, receive no automatic
  lease FDs, and make no stable-checkout claim. They may produce build artifacts;
  this is selective cooperative exclusion, not proof of filesystem readonlyness.

- **HTTP probes are not workspace ownership.** Problem: blanket `curl` and
  shell-control classification turned remote status probes into TREE writers,
  so an unrelated long mutation could block a command with no workspace effect.
  Decision: `command_network_probe.prepare_network_probe` supplies one bounded,
  complete-source plan to `run_command` admission and execution. Maintained
  GET/HEAD HTTP(S) probes to stdout or `/dev/null`, quoted scalar header/URL
  references, ordinary non-uppercase scalar assignments, probe-result `$(...)` assignments,
  literal `for … in …; do …; done` lists and scalar echo/printf qualify without
  a workspace reader/writer lease or inherited lease FDs. The plan does not
  evaluate variables, run a preflight command, replay curl, or grant permission.
  Approval/display retain original source. Execution puts `-q` first on each
  qualifying curl to disable implicit curlrc, and removes inherited
  `SSLKEYLOGFILE`, `QLOGDIR` and debug-build `CURL_MEMDEBUG` file sinks.
  Explicit setup of these variables remains conservative writer admission.
  Real outputs, remote filenames, header/cookie/trace/HSTS/alt-svc/etag/code
  sinks, explicit config, write-out `%output{…}` or file/dynamic formats,
  uploads/non-GET methods, unquoted scalar arguments, special/uppercase setup
  variables (shell arithmetic can execute assignment data), dynamic or
  variable-writing printf formats, unknown flags, shell interpreters,
  hidden substitutions and any mixed write keep existing
  protection. `SHELL_PREFIX` and non-POSIX execution cannot borrow the plan.
  Recorded background execution must verify its exact trusted wrapper and
  contain a single verbatim original body before hardening that body.
  Bounds are 16 KiB source, 256 words per segment and eight substitution levels;
  unsupported grammar returns no partial exemption. No lock implementation,
  queue, cache, worker, stable-read promise or runtime concurrency cap changes.
  Alternatives rejected: remove `curl` from the writer set (misses explicit and
  implicit outputs); add a read lease (still blocks behind writers); trust a
  `readonly` model argument (intent is not effect evidence); disable tree locks
  (loses lifecycle protection). Executable specs:
  `tests/project_mod/test_network_probe_lease.py`, including the four-path user
  probe under a held TREE writer, runner/dispatch/background parity, hidden
  writes refused before spawn, and a real private-loopback curlrc/no-replay test.

  Host-local pure-plan measurement (30 warmups, 1,000 samples each): simple
  probe median **0.067 ms**, maximum **0.106 ms**; four-path compound probe
  median **0.363 ms**, maximum **0.722 ms**. This measures parsing only, not HTTP
  latency or total tool throughput; it adds no resident concurrency/growth.

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

### Wait, freshness, descriptor, and journal lifecycle

- Tool admission has no default wait cutoff. An explicit
  `TOFU_WORKSPACE_TOOL_WAIT_SECONDS` supplies an exact nonnegative finite wait
  (zero is fail-fast); combined target leases share that explicit deadline. Task abort/runtime cancellation is checked during acquisition and
  again before admitting the body. Contended waits probe at most every 50 ms;
  uncontended paths do not sleep. Commands distinguish `workspace-busy` from
  `workspace-lease-cancelled` and state that the command was not started.
- A write scope covering internal neutral lease storage raises
  `WorkspaceLeaseNamespaceError`; command entry points report
  `workspace-protected-namespace` before snapshots or execution. This is a
  static scope refusal, not evidence of a competing holder. Waiting does not
  resolve it: use supported explicit targets or a narrower cwd. Actual
  namespace removal remains forbidden. The typed exception retains the
  existing `WorkspaceBusyError` base for other lease consumers, but the command
  wrapper no longer suggests a conflict/retry for this case.
- File-write/edit tools admit all resolved target file effects in one combined
  transaction before preimage reads, then recheck any known conversation
  freshness token **after acquisition**.
  A target changed while waiting refuses before any edits: re-read and reconcile,
  not blind retry. Missing tokens retain the existing freshness policy. These
  tools currently represent admission cancellation with their workspace-busy
  result shape; the single-write dispatcher renders the detail as `Write failed`.

  Post-admission content changes report `write-freshness-stale`; changed resolved
  targets report `workspace-target-changed`, not `workspace-busy`. Both refuse
  before mutation and release acquired leases. Waiting for a writer cannot
  refresh stale evidence: re-read/reconcile or re-resolve before retrying.
  Problem: mapping failed write preconditions to lock contention suggested a
  useless wait/retry loop even with no competing holder. Decision: distinguish
  these outcomes without relaxing freshness or target identity checks.
  Specification: `test_file_scoped_write_lease.py`.
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
  durable owner state: physical ancestor, metadata, checkout and file keys retain
  small inodes, while released transactions leave no live registry ownership
  or FDs. They are **never unlinked on
  release**: live/inherited FDs could otherwise lock a displaced inode while a
  replacement admitted another writer. Safe namespace reclamation requires a
  quiescent ownership protocol, not age/TTL stealing. No polling service is added.
- Recognized shell `&`, `nohup`, `disown`, and `setsid` detached shapes (including
  readonly-looking forms) fail before spawn with
  `workspace-lease-background-unsupported`. Use the tool background option with
  a foreground command. A deliberately daemonizing program that closes inherited
  FDs is outside this cooperative contract.
- A lease is scheduling/exclusion, never write authorization. Non-Git ROOT,
  read and metadata scopes invent no checkout identity; canonical FILE and
  explicit TREE scopes still protect non-Git targets.

### Descendant quiescence before command settlement

Problem: TERM could end the session leader while a descendant ignored it and
retained inherited lease FDs. Clearing PID fields/local registry did not release
the OS exclusion, so Stop returned but subsequent writes remained busy.
Decision: `process_group.py` retires the owned group, not just the leader: TERM,
finite leader grace, KILL remaining members, then bounded live-member observation.
Normal identified-writer completion also waits for its live group after leader
exit; closed output pipes cannot turn that wait into a hot loop. Opaque commands
without inherited leases retain their existing completion semantics. Linux
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


### Default lock waiting decision

Problem: a healthy conflicting writer caused file, command, and MCP preparation
tools to refuse work after five seconds; caller wait bounds above 600 seconds
were rejected independently of actual machine capacity. Decision: tool waits
own acquisition until cancellation or an explicit operator bound. `None`
propagates through checkout, tree, metadata and file fences; no layer silently
recreates a deadline. Low-level omitted lease timeouts remain fail-fast for
callers choosing immediate probes. Alternatives considered: skipping exclusion
permits concurrent corruption; increasing the private deadline still rejects
healthy work; infinite numeric sentinels bypass finite-value validation.
Tests hold real conflicting writers for over six seconds before file append
and command execution succeed, and verify local/process cancellation, exact
long waits, descriptor cleanup, freshness, and target identity after admission.

### Workspace admission and background records

Problem: snapshot completeness was incorrectly reused as write authorization:
opaque build commands could lose their known literal output targets. Background
execution also introduced system-owned log redirects into checks intended for the
user command, while dropping the task's runtime-root context.

Decision: workspace admission retains known literal write targets independently
of whether a complete undo snapshot is possible. Resolve them physically and
reject canonical/peer checkout writes; incomplete analysis remains incomplete
for snapshotting. Opaque execution is not an operating-system sandbox.
Background dispatch preserves the issued runtime-root context and uses the
original command only when the existing internal receipt binds it to the exact
recording wrapper. User arguments or a lookalike wrapper never confer that
provenance. Recording paths remain owned by the background-record lifecycle;
this is not a general path or authorization exemption.

Alternatives rejected: disabling workspace checks, treating incomplete targets
as an empty write set, or trusting a caller-supplied original-command string.
