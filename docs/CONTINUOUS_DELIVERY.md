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

**Restart handoff.** Live task completion still attempts delivery synchronously.
Boot recovery only acknowledges the same owner-scoped durable terminal intent
before finishing an orphaned work item. Checkpoint capture, merge, validation
and source reclamation belong to the existing lifecycle-owned integration worker.
Problem: a boot recovery called the live completion path and waited more than
eight minutes in one network-volume `git add -A`, holding HTTP readiness closed.
Decision: expose intent recording separately through the common settlement
owner; its receipt remains pending until the worker actually delivers or
preserves the checkpoint. A failed intent commit leaves the work item active
for retry, and failed/cancelled outcomes still cannot become publishable.
Worker startup discovers these durable receipts even if the process dies before
any wake hint. No new queue or worker is introduced. Alternatives rejected:
declaring delivery complete before capture loses evidence; imposing a Git wall
deadline interrupts valid work; replaying checkpoint capture inside boot couples
all service availability to the slowest old checkout. Verification:
`tests/project_mod/test_startup_workspace_intent.py`, existing delivery recovery,
revocation, pagination and startup lifecycle specifications.

Equivalent-work measurement (2026-11-05): three disposable SQLite-authority/Git
trials per path, each with the same 1,500 new text files of 4 KiB plus their
ordinal. Time starts after workspace registration and file creation. The old
boot receiver is `settle_task_workspaces`; the restart receiver is
`record_task_workspace_intents`. The existing resume pass then runs for both,
and every canonical file is compared byte-for-byte after delivered ACK.

| Receiver | Boot receiver median (range), seconds | Complete delivery median, seconds | Full Git adds before boot handoff |
|---|---|---|---|
| Synchronous settlement | 44.455 (41.801–51.602) | 44.480 | 4 |
| Durable intent handoff | 0.182 (0.170–0.218) | 43.259 | 0 |

Kept: boot receiver waiting fell 99.6%, while the same capture/publication work
still completed through the existing worker (complete delivery ranges overlap).
This measures critical-path separation, not faster total Git processing or a
production/native-store whole-restart guarantee. No tuning settings, retention
changes or durability reductions are introduced. The correctness tests inject
unavailable capture, failed intent writes, a crash between intent ACK and
work.finish, repeated recovery and attempted cancellation upgrades.

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
In a repository that opted into automatic commits, a reviewed project delivery
lands as its own commit instead (`publicationMode=committed`, target SHA the
commit on the branch; [project integration](modules/project_integration.md),
"Reviewed project deliveries become commits"). No-change delivery uses
`phase=no_changes`, `publicationMode=committed` and names HEAD as its target;
it captures nothing.

Cooperative leases cover only task-delta paths during capture and final
publication; unrelated file writers proceed. Merge computation runs outside
those leases and repository admission. Final admission checks
registered checkpoint, repair identity, terminal outcome, revocation and current
bytes of task-delta paths. Unrelated edits/index/history changes do not invalidate
a resolved patch. Git preflights the complete patch again during application;
conflicts never leave markers or partially publish siblings. External writers
that ignore cooperative leases remain outside the transaction guarantee.

**Problem (CR/CRLF corruption):** the delta and resolved patches were captured
through `integration_control._git` with `subprocess.run(..., text=True)` and
written with `Path.write_text`. Python's universal-newline decoding rewrites
every `\r\n` and bare `\r` in the captured diff to `\n`. A file whose content
carries carriage returns (vLLM progress-bar serving logs) produced a hunk whose
header line count no longer matched its body, so `git apply` either failed with
`corrupt patch at line N` or silently published only the first line; CRLF files
lost every `\r`. Strict text decoding also rejected non-UTF-8 content.
**Decision:** patches travel as bytes end to end. `_git` gained a `binary` mode
(raw capture, no newline translation, no UTF-8 decode); the delta, resolved and
conflict-repair patch captures and writes use it, and the final `git apply`
passes `--binary`. Delivery Git invocations pin `-c core.autocrlf=false` so a
user's personal Git configuration cannot translate bytes between the index and
the working tree. The published working-tree bytes equal the checkpoint blob
byte for byte. **Alternatives rejected:** translating CR/CRLF back after capture
(re-derives content that the diff already destroyed and cannot recover bare
`\r`); shelling `git apply` only for non-text files (the corruption is in the
patch transport, not the file type); leaving text mode and relying on `--3way`
blob fallback (the corrupted hunk still fails to apply to the original blob).
Specifications: `tests/project_mod/test_task_delta_byte_fidelity.py` (add CR,
add CRLF, modify CRLF, modify-with-CR, dirty canonical CR target).

Background commands and child agents must retire before checkpoint capture.
Failed/cancelled/stopped tasks preserve their source and checkpoint, never publish.
Existing candidate/stable/linear checkpoint release gates remain independent.
Confirmed working-tree delivery reaches the existing opt-in linear checkpoint
from the commit round. If delivery finishes later, the existing recovery worker
runs the same checkpoint owner, naming the delivered paths as the task's own so
a failed whole-workspace capture can still commit them alone
([project integration](modules/project_integration.md#ref-topology),
"Linear checkpoints publish in a busy checkout"). A Coordinator-reviewed project
delivery is the exception: the recovery worker commits exactly its own delta onto
the current branch, without the whole-workspace capture ([project integration](modules/project_integration.md#task-owned-canonical-delivery),
"Reviewed project deliveries become commits"). No opt-in means no automatic commit; configured
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

**Problem:** publication permission was checked before recognizing an empty
task delta, leaving completed read-only tasks blocked and retaining their
clones. A removed checkout recreated as a file-history bootstrap directory
also inherited the parent repository's Git identity and stayed retained.
**Decision:** prove an ordinary checkpoint equals its immutable baseline before
waiving publication permission for terminal bookkeeping. Changed checkpoints
and conflict repair retain their publication fences. Retire a source only after
the pinned checkpoint and durable completed receipt are verified. Revisit a
retained path when its Git marker disappears; an unregistered directory with
only empty directories and the file-history bootstrap marker is reconstructible.
A different repository, registered worktree, symlink or durable file retains it.
**Alternatives rejected:** deleting failed/conflicted sources loses recovery
evidence; a blanket permission bypass could publish unauthorized writes; Git
discovery without checking the checkout root mistakes the parent for the source.
The paired regression fixtures change both the empty task and bootstrap-only
placeholder from one retained directory to zero, while unauthorized changed
tasks and a placeholder containing an experiment keep their one source. No
retention timer, retry policy or extra delivery state is introduced.

A recovery ref and durable pre-publication receipt precede file mutation. A lost
final ACK is reconciled by comparing target snapshot bytes on task-delta paths,
without replaying an already-applied patch. Later unrelated edits do not affect
that proof. If those paths also changed before acknowledgement, recovery cannot
invent delivery evidence; it retains the source and reports retry/conflict debt.
Historical committed receipts remain historical facts; they are not relabelled.

## Conflict recovery

Problem: one workspace lookup, checkpoint or publication exception aborted the
whole recovery page and postponed unrelated deliveries. Decision: isolate each
row's recovery, retain its existing durable debt and continue the same page.
The existing paged worker revisits it; a page-read failure still reaches the
worker's storage backoff. Alternatives: a second retry queue duplicates state;
marking failed rows delivered would discard evidence. No new rows, refs or
retention policy are introduced. The three fault cases in
`tests/project_mod/test_delivery_recovery_isolation.py` each deliver the healthy
row on the first pass, the failed row on the next, and perform no third-pass
publication.

The same durable integration worker retries transient writer/storage contention,
input movement and Git spawn failures (EAGAIN, EINTR or ENOMEM with their typed
OSError cause preserved) after the synchronous first attempt. No additional queue,
model loop or detached waiting job is introduced. A failed capture keeps pending intent
with `phase=checkpoint_spawn_retry`; its next durable resume re-captures through the
existing writer lease and never trusts an empty or partial checkpoint. A legacy
`blocked` checkpoint receipt recording exactly those spawn errors is re-attempted by
that same pass, including after restart. Other capture failures remain blocked with
their diagnostic and require the existing owner-scoped retry; an unchanged blocked
receipt is not recaptured or published merely because it happens to name a checkpoint.
Admission holds that refused a capture before it ran are not capture failures: running
background commands, running child agents and a busy source (`workspace_busy`) are
retried by each pass until the capture runs. A busy source was left blocked forever
until 2026-11-06, which concurrent receiver lanes would have made common: two lanes
admitting shared ancestor barriers at once can refuse one of them for that instant
(`test_capture_refused_by_a_busy_source_is_retried_once_it_is_free`).
Conflict/input retry evidence retains its existing separate recovery path.

Problem: a successfully captured checkpoint awaiting project publication was
recorded with the capture phase, so recovery treated it as a deterministic
capture failure and never resumed after review. Decision: publication holds use
`phase=workspace_adoption` and revalidate the existing grant against the pinned
checkpoint on resume. Older `checkpoint` receipts carrying a
`project_publication_*` reason receive the same treatment. Recovery does not
recapture later source edits or require the source writer lease for this frozen
checkpoint; genuine capture failures retain their explicit retry requirement.
Alternatives rejected: retrying every blocked capture weakens failure evidence;
recapturing after review could publish unreviewed changes. The frozen recovery
and project file lifecycle tests cover both current and older receipts.
Ancestry-probe fault injection runs before delivery acknowledgement and verifies
the receipt and target bytes stay unchanged; an already delivered receipt takes
the idempotent terminal path and cannot exercise that probe.

`origin.delivery.retry` follows
`workspace_delivery_retry_v1.schema.json`: conflict fingerprints use registered
baseline/checkpoint and current entries of task-delta paths. Unchanged conflict
inputs skip repeated merge computation. Committing unchanged workspace bytes or
editing unrelated paths does not trigger a retry. Former verification failures
can now deliver workspace bytes without waiting for a release gate.

**A settled conflict is rechecked at its own paths (2026-11-06).** Problem: the
receiver re-evaluated every settled conflict on every pass, and each evaluation
captured the whole canonical checkout (`_canonical_tree`) only to find the same
fingerprint. After the 14:54 restart on 2026-11-06, Tofu had 16 settled conflicts at
about 4.5 seconds per capture; passes ran about 70 seconds apart, and the boot pass took
about 27 minutes to reach lane 7509d68d.
Decision: the settled conflict is the cache entry and its fingerprint is the exact
key, so a receipt settled as `conflict_inputs_changed` is first rechecked from its
inputs alone. `task_delta_delivery._canonical_path_entries` stages only the task-delta
paths into a private index seeded from HEAD at those paths, by the capture's rules
(tracked paths always, untracked ones unless ignored), and lists them exactly as
`ls-tree` lists a capture; it writes no tree and reads no other path. An equal
fingerprint keeps the receipt without capturing the checkout. Invalidation is explicit:
any change of the baseline, the checkpoint or the bytes at those paths changes the
fingerprint, and the receipt takes the whole capture and merge as before. So does a
path that holds a directory in the checkout (`ls-tree` would name a tree this index
never writes), a receipt that adoption recovery could still settle, and any error of
the recheck. Changes elsewhere in the checkout cannot alter a conflict's outcome, so
they no longer cost a capture per conflict per pass. Alternatives rejected: one
capture per project per pass reused across its conflicts (a whole-tree walk still runs
every pass, and nothing invalidates the reuse when another session edits the checkout
between rows); a persistent private index as a stat cache (still a whole-tree walk per
conflict); keying on canonical HEAD or a time window (other sessions edit the checkout
without moving HEAD; a window is a tuning knob). Specification:
`test_boot_pass_rechecks_settled_conflicts_without_recapturing_the_canonical_tree` in
`tests/project_mod/test_continuous_delivery.py` (a boot pass over three settled
conflicts captures nothing; changing one conflict's path re-merges and delivers only
that row); the object-growth and literal-pathspec cases in the same file still pass.

**A held conflict is rechecked when an input changes, not on every pass
(2026-11-08).** Problem: that path recheck still ran on every pass, after taking the
whole-delta claims lease (one file claim per delta path; a 1,100-path delta holds about
3,700 lease keys and cost 7-15 s of CPU per admission, `benchmarks/workspace_claims_lease.py`),
and each lane's lease release woke the integration worker, whose next pass requeued the
same unchanged rows. With 29 held conflicts in two projects, the delivery lanes held
34-48% of the execution host's GIL samples in every 2026-11-08 profile window
(py-spy `--gil --nonblocking`, 20:54-21:58). Decision: before any lease, a held
conflict (`conflict_inputs_changed`, workspace or commit adoption) observes its inputs
without reading file content: HEAD; the stat identity of every delta path and of each
`.gitignore` and `info/exclude` that decides whether an untracked delta path is staged;
for a commit landing also the real index entries at those paths and Git's in-progress
markers. With the receipt and row identity this is the exact key: when it equals the
observation taken before this process's last unchanged verdict for that task
(`task_delta_delivery._UNCHANGED_HOLDS`), the hold stands with no lease, capture or
merge. Any difference runs the full recheck under the lease, which remembers its
observation only when it again finds the hold unchanged. A file changed within the
filesystem's timestamp resolution of the observation (DolphinFS records whole seconds)
is not trusted, git's racily-clean rule; the full recheck decides. Publication holds
and authority gates are still evaluated on every pass, before this check. A delivery
lane's lease release no longer wakes the worker (`notify_workspace_write_released`): a
lane that changed a receipt already woke it through that delivery-state commit.
Alternatives rejected: HEAD alone as the key (working-tree publications and other
sessions change bytes without moving HEAD); hashing the delta paths each pass (reads
every byte, the cost being removed); a recheck interval (a tuning knob); a filesystem
watch (no reliable notification on DolphinFS). Known limit: a change to the user's
global excludes file is observed at the next change of another input. Specifications:
`test_held_conflict_is_rechecked_when_an_input_changes_not_on_every_pass` and
`test_a_delivery_lanes_release_does_not_wake_the_worker_that_queued_it`
(`tests/project_mod/test_continuous_delivery.py`) and
`test_held_landing_takes_no_claims_lease_until_an_input_changes`
(`tests/project_mod/test_delivery_integrity.py`).

Conflict names are capped at 64 and 4,096 encoded bytes, with count/truncation
metadata. One replaceable owner/task input ref pins the immutable dossier;
failed receipt admission restores the previous ref. Unchanged snapshots reuse
pinned evidence rather than creating timestamp-only retry objects.

### Delivery polls compare trees

Problem: every poll committed its capture of the live canonical checkout, but
reuse of the pinned conflict source matched only when the whole checkout was
unchanged. In a shared checkout other sessions keep editing, so each poll of a
blocked task left one unreachable `Tofu checkpoint: <task>` commit whose parent
is the canonical HEAD; the recheck after a merge minted another. A copy of
Tofu's repository on 2026-10-27 held 63,012 such commits among 89,465
unreachable loose objects (133 tasks since 2026-10-13, about 6,000 a day,
75-160 s apart per task), which kept `git gc --auto` running on every commit
(see "Automatic Git maintenance never runs inside commands" in
[`runbooks/GIT_WORKSPACE_GUARD.md`](runbooks/GIT_WORKSPACE_GUARD.md)).

Decision: captures are trees (`task_delta_delivery._canonical_tree`). The
source commit is minted only for a no-change receipt or before a merge, under
the same reuse rule; the recheck compares trees. Fingerprints are unchanged,
because `ls-tree` of a tree equals `ls-tree` of its commit. Trees and blobs of
canonical changes are still written: they are content-addressed and shared by
all tasks.

Alternatives rejected: deterministic commit dates still mint a commit per
distinct capture (12,568 distinct task/tree/parent triples in that copy) and
change checkpoint metadata; recording every poll's capture in the receipt
changes `workspace_delivery_retry_v1.schema.json`. Verify
`test_blocked_polls_after_unrelated_canonical_edits_mint_no_commits` and
`test_delivery_mints_only_commits_its_refs_keep` in
`tests/project_mod/test_continuous_delivery.py`.

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
