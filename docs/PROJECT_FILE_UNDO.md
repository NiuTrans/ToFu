# Project command snapshots and undo

The runtime owners are `lib/project_mod/run_command.py` (change observations),
`snapshot_content.py` (bounded pre-images), `modifications.py` (history and undo
receipts), and `undo_filesystem.py` (anchored mutations and post-image evidence).
The persisted `postImage` field follows
[`project_undo_postimage_v1.schema.json`](../contracts/project_undo_postimage_v1.schema.json).

## Change observations


### Linked worktrees and file-change presentation

Problem: temporary linked checkouts were mistaken for scratch files, so edits
could be merged successfully without a File Change Panel. Decision: file writes
recognize same-repository worktrees before the temporary-directory exemption,
register the checkout root in the execution's scoped registry, and journal clean
root-relative paths. Ordinary scratch and unrelated temporary repositories remain
untracked; existing restricted-caller, read-only and task-checkout admission still
run first. Checkout identity does not grant access to another task's workspace.

Settlement discovers runtime roots and committed live journal addresses as well
as configured roots, then rereads conversation/task-stamped journals. A live row
is a discovery hint, never independent proof of a file change. Explicit foreign
task IDs never enter legacy timestamp fallback, and dynamically discovered roots
require exact task IDs. Journals survive Git checkout removal; recorded command
actions cannot be changed into deletions merely because a worktree was removed.
Orphaned-turn restart recovery uses a separate explicit conversation-journal mode
with the logical turn's creation floor to carry prior physical attempts; that
mode requires an empty task ID and never changes normal settlement isolation.

Auxiliary manually added worktrees carry durable per-file `undoAvailable: false`.
Their original root identity remains visible after integration; the single-root
undo gateway must not imply it can reverse the canonical Git merge. Normal
primary/managed task checkouts retain their undo behavior. Any denied row disables
the block's undo/redo, at both projection and command boundaries. Alternatives
rejected: scanning a shared Git diff misattributes concurrent work, remapping
worktree receipts to the main checkout invents undo authority, and treating every
temporary path as a project pollutes scratch history. No new store, worker, or
persistent ledger is introduced. Verify with worktree path, round-derivation and
conversation file-command tests.
Command tracking does not infer writes for opaque commands. Up to 64 literal
relative write targets use a deduplicated scoped snapshot; a literal file never
walks unrelated directories. Globs and other ambiguous target sets retain the
bounded whole-tree fallback. Both paths share one entry budget.


Write exclusion is independent of this observation policy: explicit regular-file targets coordinate by canonical file, including neutral keys for runtime/data and non-Git files. Native same-checkout transformations/publication fence source edits there; destructive tree effects also fence nested and runtime/data targets. Git metadata-only operations and unrelated parent/peer native checkout changes do not exclude source edits. Multi-target admission uses `workspace_claims_lease` to flatten tree, metadata, checkout and file barriers before any preimage reads; logical `lease_claim_order` grouping alone is not an acquisition transaction. The effect-scoped admission contract lives in [`Git workspace guard`](runbooks/GIT_WORKSPACE_GUARD.md#lease-api-and-identity). Literal interpreter/cat heredoc input does not itself request writer admission; real header output targets and shell tails remain visible. Explicit directory creation coordinates its target checkout, not the unrelated reading cwd. Shell setup/data-target boundaries are owned by [`project integration`](modules/project_integration.md). A read/test verification is never silently promoted to an immutable snapshot or granted extra filesystem authority by this exemption. Git output redirects union their output-target claims with Git's own effects; inline includes/helpers and ambiguous forms retain conservative admission. Compound claims span preimages, the full supervised command, postimages and journaling. Inherited descriptors exclude until final close/group quiescence, not merely leader exit; background execution does not shorten that lifetime.

`TOFU_COMMAND_SNAPSHOT_MAX_ENTRIES` counts directories and ignored entries too:
personal defaults are probe-derived 5,000–20,000, lean fallback is 5,000, and
distributed default/hard ceiling is 20,000. The existing 5,000-file and depth-6
caps remain. Directory iteration streams rather than materializing wide lists;
iterators are released with the walk. Whole-tree and scoped metadata scans
open directories through no-follow handles; a directory swapped into a link
cannot reveal external child names. Fingerprint metadata shares the 5,000-file
cap and command lifetime; the synthetic allocation test stays below 4 MiB.

`TOFU_COMMAND_SNAPSHOT_MAX_SECONDS` adds a monotonic soft deadline (personal
probe-derived 1–5 seconds, lean fallback 1, distributed/default hard ceiling 5).
Checks occur between filesystem operations; the deadline cannot interrupt a
blocked kernel filesystem call. Errors, elapsed deadlines and budget exhaustion
mark a snapshot incomplete. An omitted path then
cannot prove creation or deletion. Common entries compare both mtime and size.
A command result explicitly reports incomplete coverage.

## Pre-image and post-image evidence

Pre-images retain the existing 100-KiB per-file and 20-MiB per-command limits.
No-follow descriptor opens pin the root and parent chain, reject non-regular
files, and avoid blocking on a substituted FIFO. Reads are limited to budget+1;
identity/change metadata and the current parent chain are checked afterwards.
Missing originals are visible in command results and never mean a file was
created. These metadata checks are not transactional filesystem snapshots.

New modification records separately capture a post-image: a BLAKE2b-256 content
digest and byte count, explicit absence, or unavailable evidence. Root device
and inode bind this evidence to the selected root. Content hashing streams in
64-KiB chunks and uses the existing 512-KiB project-file cap. Command batches and
undo selections each share the existing 20-MiB byte budget, defined once by
`UNDO_BATCH_CONTENT_BYTES`; commit rechecks hash only admitted files. Oversized,
unreadable, changed-during-read or over-budget files do not get a stat-only
substitute. Moving/replacing a project root invalidates its old root evidence.

Only explicit command creation evidence permits undo-by-delete. A missing
pre-image cannot authorize deletion of an old file. Before undo, current content
must match the recorded post-image even when size and timestamps were preserved.
Content evidence deliberately excludes the file inode so multiple changes to
one file can unwind in reverse order across atomic replacements.

Legacy full-file undo without post-image evidence fails visibly. Legacy reverse
patches can still match their exact replacement against a bounded live read,
but cannot bypass that check using a full backup blob. They must decode as UTF-8
and compare the live bytes again before publishing.

## Authority, mutation and journal publication

Undo validates each record's project attribution and target before accessing
it. Traversal, cross-root absolute paths, symlink/reparse components and special
files cannot expand the selected project authority. Actual reads, deletion and
restoration use directory handles. Restores fsync a private same-directory
temporary, revalidate the parent and current evidence, atomically replace the
name, and fsync the directory; failed temporaries are cleaned through the same
handle. Editor timestamp notifications use that same anchored authority and
never reopen a substituted path. The final content check is optimistic: external writers do not
participate in a cross-process transaction.

Snapshot rewind and task redo reuse this same mutation boundary, including
no-follow timestamp notifications and metadata refresh. Stored snapshot keys
must be relative without empty, dot or traversal components. A refused file
retains tracked evidence; partial redo reports applied paths and failure reasons
with `ok: false`, without appending a successful redo snapshot. There is no
whole-round rollback promise. See [file history](modules/file_history.md).

Platforms without the anchored-open primitives decline capture/mutation
visibly rather than falling back to path-based writes. Records remain available
for inspection. No automatic override authorizes stale or unavailable evidence.

Only a missing journal file means empty history. Invalid envelopes/records,
parse failures and permission errors preserve the original file and any
recovery evidence, fail visibly, and never populate an empty cache that could
replace existing history. Journal cache and listeners publish only after persistence succeeds. A failed
write retains the previous cached and durable evidence. Successful command
execution whose history cannot persist explicitly reports unavailable history.
Undo consumes only successful `changeId` receipts from its original selection;
failed and concurrently appended records remain. A failed newer change blocks
older undo on the same path. A partial undo can therefore leave both changed
files and retained failure records; failure is not reported as successful undo.

## Undo index and immutable contents

Problem: a 318 MB inline undo journal was reparsed and rewritten for every
small file edit; a five-file edit spent 30 seconds on unrelated prior content.
Decision: `modifications.json` remains the sole commit authority, but contains
lightweight references for `originalContent` and `reversePatch`. Immutable
`undo-payloads-v1/*.pack` files contain packed ASCII JSON values. References
follow `contracts/project_undo_payload_ref_v1.schema.json`: pack name, byte
range and SHA-256 for that exact value. Packs are fsynced before the atomic
index publication; failed migration leaves the previous inline journal valid.
Older inline records are converted once on the next confirmed journal update.
There is no change to owner/root attribution or the selected storage backend.

## Append commits and bounded folds

Problem: an 8,838-record, 8.1 MB journal was rewritten in full for every file
edit while the project journal admission was held. That admission is one per
repository root, shared by every conversation, so at ~200 ms per commit a second
conversation editing an unrelated file exhausted its 30 s wait and reported
`[journal-busy]` although no two writers touched the same file. The published
file-scoped lease already covers the edited path; what serialized unrelated
projects of work was the commit, not the file.

Decision: a small record is **appended** to `modifications.journal` (one
newline-terminated line, fsynced before the change is acknowledged) and the
index is not touched. A record larger than `_JOURNAL_APPEND_MAX_RECORD_BYTES`, or
one that would push the log past `_JOURNAL_TAIL_MAX_BYTES`, folds instead: the
live log is renamed to `modifications.journal.fold`, an index containing every
record plus the `absorbedFold` identity of that artifact is published, and only
then is the artifact unlinked. A crash before publication replays the artifact;
a crash after it replays nothing. A torn trailing line has no newline and is
never trusted — its prepared intent is the recovery evidence. A failed append is
truncated back to its previous size, so an unacknowledged record never becomes
visible.

History is index + fold artifact + log, replayed in that order. The index stays
the readable authority for anything that needs joined history (undo,
projections, payload reclamation), so undo and its payload ledger keep their
existing contract. Appending requires an index whose payloads are already packed
references; a legacy inline index is folded once, which is the existing migration
point rather than a new one.

Write admission validates the authority by journal identity instead of
re-parsing the index per edit: an unchanged journal is validated once per
process, and any external append or replacement changes the identity and forces
a full validation. Duplicate `changeId` detection uses an identity-keyed id set
too, so an append never materializes the history.

Alternatives rejected: narrowing the admission to the edited file (writers to
one append log must still exclude each other, so it removes nothing); raising
the admission timeout (turns a visible failure into a hidden stall); one file
per record (a directory scan per read, and it moves ordering into the
filesystem); keeping the index resident (it already exceeds the history cache
budget); trimming history to keep commits cheap (loses undo).

Measurement: on a copy of the real 8,838-record journal, 60 small commits went
from 220 ms to 2 ms each and from ~493 MB of repeated index rewrites to 30 KB of
appended lines, with identical history content afterwards. Executable specs:
`tests/project_mod/test_project_journal_append.py`.

Internal projections request metadata only. Original contents are materialized
after conversation/path selection, and undo verifies each selected range before
mutation. Missing, changed or symlinked payloads fail visibly and block older
undo on the same path. An unreferenced pack is reclaimed only after its final
reference is removed by a confirmed journal transition; shared packs survive
partial undo. Cold index reads also reclaim aged unreferenced crash artifacts
through an entry-bounded pass under the same admission; referenced packs and
young artifacts are excluded. Cache capacity and bytes retain their launch-resolved total;
LRU sessions share that total rather than reserving a fixed fraction per root.

Completion diagnostics use `get_task_modification_paths`: exact task/optional conversation, distinct addresses, remaining count bound (200 across roots), recovery before selection under session admission. Detached addresses survive tool release without copying historical rows or hydrating content. Cold/invalidated caches still validate the entire JSON authority: projection allocations, not cold I/O, are bounded.
Moving capture after release or reusing truncated diagnostics for asynchronous presentation was rejected: it loses capture timing or complete/legacy-attributed file changes. Tests: `test_task_modification_paths.py` and `test_project_write_recovery_crashes.py`.

Alternatives rejected: deleting history loses undo; raising the old content cache retains hundreds of MB unnecessarily; one payload file/fsync per record slows migration; unconfirmed background writes weaken acknowledgement. Packing uses the existing history cache byte budget or one required value, with no idle worker or retained payload cache.
Verification: `tests/project_mod/test_project_undo_payloads.py`, journal
failure/recovery, identity, concurrent writers and undo evidence suites.

## Verification

Executable contracts include `tests/tasks_pkg/test_run_command_scoped_snapshot.py`,
`tests/tasks_pkg/test_run_command_snapshot_bounds.py`, `tests/tasks_pkg/test_run_command_snapshot_content.py`,
`tests/tasks_pkg/test_run_command_snapshot_path_races.py`,
`tests/tasks_pkg/test_run_command_undo_evidence.py`, `tests/project_mod/test_project_undo_journal_receipts.py`,
`tests/project_mod/test_project_modification_durability.py`, `tests/project_mod/test_project_undo_path_boundary.py`,
and `tests/project_mod/test_project_undo_postimage.py`. Keep real filesystem substitutions,
FIFO races, disk-failure injection, same-size/coarse-mtime external edits,
concurrent append, reverse-order restoration and allocation measurements.

## Edit-batch preflight

`edit_file` defaults to `atomic: true`: all operations, canonical destinations,
source reads, matches, final UTF-8 encoding, intent-relative destination validation
and tracked journal admissions are planned before any requested file edit is
published. Canonical paths identify shared buffers and leases; each original
lexical destination retains its root containment and tracking policy. A symlink
cannot turn an attributed project write into untracked temporary scratch. Operations on each physical file see earlier
in-memory edits, even when different path aliases name that file. A failed
preflight returns zero applied edits, a failure for the rejected operations and
`SKIP` receipts for otherwise valid operations, in their original request order.
No undo, freshness or syntax-success receipt is earned by a skipped operation.
Only explicit `atomic: false` requests the legacy partial-application behavior.
The file-only scoped handler uses the same compute functions and whole-batch
preflight policy, while retaining descriptor-fenced I/O and its prohibition on
replaying pending recovery outside the grant.

This is **preflight atomicity, not a cross-file transaction**. A later actual I/O
or journal-publication failure can follow an earlier committed file; retain the
existing per-file intent/undo evidence and report applied-but-unconfirmed writes
rather than claiming rollback or suggesting a blind retry. Reconciliation of
pre-existing write intents remains the existing recovery owner's responsibility.

Problem: eager per-file publication left a partly changed project after a later
missing anchor, invalid path or unavailable journal. Decision: plan the complete
batch under existing workspace/session admissions, then publish one transition
per physical file. Alternatives rejected: looser semantic matching can target
unrelated code; rollback after an external writer races us can destroy user work;
a second patch writer would duplicate authority, freshness and undo owners.
Verify `test_edit_file_atomic_preflight.py`, `test_project_dispatch_scope.py`,
`test_project_edit_demand.py` and `tests/tools/test_edit_result_meta.py`.

## Direct-write journal failure

Text writes, uploads, replacements, insertions, and batch edit groups require
an explicit successful journal acknowledgement when the target is tracked.
A missing acknowledgement or journal exception after atomic file replacement
returns `undo-record-failed`, with `mutationApplied: true` and
`undoRecorded: false` on structured results. Batch tool text retains that
failure tag and explicitly says the mutation was already applied and must not
be retried automatically. No rollback overwrites a concurrent user edit.

Tracked direct writes now persist a bounded intent before replacing the file.
The intent remains until the undo journal confirms the same stable `changeId`.
`lib/project_mod/write_recovery.py` reconciles intents when a project is opened,
its modifications are read, undo is requested, or another tracked write starts.
Task/root lookup also recognizes pending intent attribution. Shell commands
retain their separate post-execution capture contract; this is not a transaction
for arbitrary subprocess or external filesystem mutations.

`contracts/project_write_intent_v1.schema.json` defines the intent. Preparation
preserves exact original bytes or explicit absence, logical project attribution,
the resolved root path and identity, the staged inode, content digest, and
conversation/task attribution. A symlinked project keeps its existing journal
identity, but a retargeted alias is a recovery conflict. The caller fsyncs the
stage; preparation fsyncs its directory before durably publishing the intent.

Direct edits retain complete original bytes, staged post-images and exact undo
identity without a private byte quota. `TOFU_PROJECT_WRITE_RECOVERY_MAX_MIB` is
retired and ignored; changing it cannot hide pending durable evidence after
restart. Pending-intent and directory-entry counts no longer refuse preparation
or limit recovery discovery. Every recognized record still validates schema,
base64, root/parent/inode identity, content digest and stage ownership before it
can classify or authorize cleanup. Corrupt, retargeted and conflicting evidence
fails closed. Actual write/flush/validation failure still prevents publication.

Problem: a launch-derived 1–1024 MiB ceiling and 64-record/130-entry quotas
rejected valid writes and could make older evidence inaccessible after a lower
restart setting. Decision: preserve exact evidence and consume the complete
owned recovery set. Alternatives considered: larger constants repeat the same
failure; omitting preimages or deleting pending rows weakens recovery and undo.
This remains an inline exact-preimage journal and materializes complete bytes;
removing admission quotas does not imply constant memory. Command snapshots and
namespace transaction policies retain their separate owners/audit targets.

Verification: `test_project_write_intents.py` reopens 137 real pending records
alongside 180 auxiliary entries and classifies applied/unpublished/conflicting
states without losing evidence. `test_write_recovery_budget.py` covers original
and post-image bytes above the retired configured ceiling, exact multibyte undo,
large base64 evidence and recovery after a lower legacy setting. Crash, symlink,
inode-retarget, malformed-record and real evidence-I/O failure tests stay
mandatory. No durable history is reclaimed to satisfy a resource target.


Recovery never restores or overwrites user files. A matching staged inode and
content at the target proves publication. A matching stage still at its private
name plus unchanged original proves non-publication. Missing stages, replaced
roots, content-only matches on other inodes, and later edits remain conflicts;
the original bytes remain available and new tracked writes fail visibly.
Non-publication is durably marked `aborted` before reclaiming the identified
private stage, so interruption during cleanup is itself recoverable. Cleanup
never removes another inode that reused the stage name.

`session_lock.py` provides the same bounded, thread-reentrant filesystem lock
to intents, journal writers and undo. It is acquired before the cache lock;
commits reload current disk state instead of publishing a stale process cache.
Fork does not transfer ownership: descriptor registration is serialized with
fork, and the child closes inherited session descriptors from every thread
without unlocking the parent's open file description. It clears inherited
reentrant state and must acquire its own lock. Context cleanup closes descriptors
only in their owning process, so a reused child descriptor is not mistaken for
an inherited lock. The registry contains only active descriptors and is emptied
on normal and exceptional release; it adds no retained lock cache.
Recovery publishes the prepared post-image proof, not a later user edit sampled
at replay time. Repeated stable IDs must match their original evidence and
confirm the journal again without duplicate history or listener publication.
After a confirmed journal commit, failed intent cleanup retains the intent for
idempotent cleanup on the next access; it never retries the file mutation.

### Contention and projection cost

File-scoped admission protects the exact canonical target, not the whole
checkout. The session admission still spans intent recovery/preparation,
atomic publication, journal RMW/acknowledgement and file-history staging;
parallel targets never consume another active intent. Presentation tickets stay
unready until file leases are released and write-freshness evidence is current.
Only then do ordered/reentrant callbacks drain, so cross-file listeners cannot
create a FILE-lock wait cycle or overwrite a newer freshness token. Upload
rename candidates require protection and revalidation of the actual final path,
not merely the requested filename. Tests exercise real Git-backed file leases,
intent/journal durability and callback interleavings rather than only non-Git
fixtures with freshness disabled.

**Problem:** slow history/listener work and redundant journal reads extended a
project-wide critical section and turned unrelated edits into lock timeouts.
Holding the global cache lock during filesystem I/O also stalled other projects.

**Decision:** session admission covers recovery, validated file publication and
one authoritative journal read/modify/flush. Empty recovery skips journal loading;
every tracked write still reads and validates its authority **before** preparing
or replacing the target. That private snapshot feeds the RMW under the same
admission, never from a stale cache. Prepared post-image evidence avoids hashing
again. Malformed journals preserve target bytes and recovery evidence unchanged.

Live listeners run after the outermost session lock closes. `post_commit.py`
registers per-path, in-process notification order under admission, then drains
synchronously outside it. Different paths remain independent; reentrant
notifications follow the current change's complete listener list without
cross-path wait cycles. Lanes exist only while callers are active, and are
cleared after fork; no background worker or persistent queue is added.
File-history staging stays inside admission: its process-local history lock
cannot fence an older process's late pre-image against a newer snapshot.
Failures of these projections cannot revoke an already durable undo record.
New undo records use exact pre-images/inverses without rewriting the journal
solely to attach an optional history version; old history-version rows still work.

Cache lookup validates device/inode/size/mtime/ctime against the atomic journal.
Unchanged reads reuse an LRU snapshot; replacement or failed flush forces reload.

**Write critical section (2026-10-04).** Each durable write holds session
admission across prepare, file replace, and the journal read-modify-write. The
RMW input is, in priority order: a fresh private journal prepared under the
same admission, else an identity-validated cached snapshot (a copy, so the
mutator never aliases the resident entry), else an authoritative disk read.
Reusing the validated cache keeps a repeated writer's critical section off a
full journal re-read — the main driver of `journal-busy` when histories grow
on slow (FUSE) filesystems. Correctness is preserved because the identity
check runs *under* admission: another in-process writer cannot interleave, and
a cross-process/external commit changes the journal identity, so a stale cache
never substitutes for durable history. A cached entry exists only after a
confirmed durable flush, so a torn or corrupt journal forces the fail-loud
disk read and never persists as reusable state. A failed flush still
invalidates the projection. The contract's admission budget (`lockWaitSeconds`)
is 30s: the original 5s produced false `journal-busy` on FUSE even for
legitimate serialized writes; the fail-fast semantics and the retry contract
(re-read + recompute, never blind replay) are unchanged.

**Problem:** repeated durable writes re-read the entire append-only journal
inside the lock, so one writer's critical section grew without bound and
peers timed out at 5s with `journal-busy`.
**Decision:** reuse the identity-validated cached snapshot as the RMW input
(cache-miss falls back to the authoritative disk read), and raise
`lockWaitSeconds` to 30.
**Alternatives rejected:** an async projection worker (durable lifecycle/replay
obligations beyond this boundary); a compaction/rollup of the append-only
history (separate change; this fix removes the per-write re-read cost that
made the unbounded history hurt, without changing the durable format).
Count and conservative resident-byte budgets both apply, derived from the host
cache capacity (256 KiB per capacity unit; one session at most one quarter of
the aggregate byte budget). Oversized histories stay readable from disk, not
resident in the LRU or active projection; durable history is never evicted.
Compact JSON output is streamed in 64 KiB chunks rather than materializing a
whole serialized journal (the encoder may still allocate one scalar token).
Session admission precedes the global cache lock, which protects memory only,
never filesystem I/O. After fork, the shared RLock is reinitialized in place to
preserve imported aliases without inheriting a vanished thread's ownership.
Different projects remain independent during slow reads and flushes.

Contention waits up to the contract's admission budget. A pre-mutation timeout
returns `journal-busy`, `retryable: true`, `mutationApplied: false`, and measured
`waitedSeconds`; text batch receipts preserve the category and re-read guidance.
Retry requires a fresh read and recomputation, never blind replay. Partial or
unconfirmed writes retain their distinct non-retryable result semantics.

**Alternatives rejected:** dropping locks/journaling weakens recovery; extending
waits or blind retries hides slow holders and risks stale edits. Deferring
history with only an in-process lane can regress cross-process snapshots, so
only presentation listeners move outside admission. An asynchronous projection
worker would add durable lifecycle/replay obligations beyond this boundary.
Executable coverage: `test_project_journal_contention.py` checks listener
isolation, cross-project I/O independence, bounded admission, parallel undo,
stale-write rejection and one journal load per normal direct write.
`test_project_post_commit_order.py` covers corrupt-journal prevalidation,
reentrant notification order and separate-process history/snapshot order;
`test_project_journal_cache_budget.py` covers bytes, eviction and streamed output;
`test_project_cache_lock_fork.py` covers inherited cache-lock ownership.

**Measurement (2026-09-30):** disposable local-filesystem comparison with the
original HEAD, 2,000 seeded records and 20 writes: journal loads fell 60 → 20;
20 unchanged history reads fell 40 → 0 loads; durable flushes remained 20 → 20.
A paused listener made the baseline competing write time out at 103 ms; the
revised write succeeded in 23 ms. Serial median was 20.03 → 21.49 ms in this
single run, so this establishes contention/I/O improvement, not a serial
latency speedup. Whole-journal persistence and ordered history staging still
scale with durable history; neither is bypassed to mask slow storage.

Recovery admission has no private original/post-image, encoded-record or
pending/directory count quota. Lock contention follows the session admission
contract. Malformed or conflicting evidence fails closed without eviction;
this is durable recovery data, not a cache. Descriptor lifetimes end with the
context, including process exit.

Journal snapshot publication flushes the JSON file before rename and then
flushes its parent directory. Removing an empty journal also flushes the
parent. A failed or unsupported directory flush is reported as a journal
failure; it is never silently treated as durable success. Any failed journal
mutation invalidates its cache, including interruption: rename/unlink may
already have happened, so subsequent operations reload disk rather than
publishing stale cached history over an uncertain commit.

Creating a session or direct-write parent directory publishes missing ancestors one at a time and
flushes each parent before creating the next child or writing a project marker.
A retry also flushes the deepest existing ancestor: existence alone does not
prove that an earlier failed flush persisted its name. Flush errors stop journal
initialization visibly. The walk uses memory bounded by path depth and opens
only one flush descriptor at a time; existing sessions require one parent flush.

Direct atomic writes set final permission bits on the staged file before its
file flush, replace the target, then flush the already-open parent directory.
A failure before replacement preserves the original. A directory flush failure
after replacement returns `write-durability-unconfirmed` with
`mutationApplied: true`, `durabilityConfirmed: false`, and `undoRecorded: false`;
batch outputs preserve the failure category. This outcome never suggests
retrying the mutation or moving a staged salvage file over the current target.
Each write owns one additional directory descriptor and releases it on every
terminal path. These guarantees do not make file and journal one transaction.

New-file staging uses exclusive random-name creation with requested mode
`0666`, so the kernel applies the current umask without changing process-global
permissions. Existing-file staging starts at `0600` and receives the original
mode before flushing. Name collisions retry at most 32 times and never truncate
or unlink the colliding file; failed allocation leaves the target unchanged.

Original-file reads are bounded before decoding or allocating recovery evidence and are required before overwrite: text writes and upload
replacement fail before mutation when the existing file cannot be read.
Text overwrite preserves the exact UTF-8 pre-image (including CR/LF and BOM)
or stores undecodable originals using the existing binary history encoding.
Append, replacement, and insertion edits require strict UTF-8 and preserve
newline bytes during reads; lossy replacement decoding never becomes the
content to publish or the evidence to undo. Missing original bytes are never
interpreted as proof that an existing file was newly created.

Edit computations retain the original input before matching normalization.
The compact inverse is checked by applying it to the computed post-image;
if it would not reproduce the original (for example, repeated replacement
text, multi-match replacement, deletion, or newline normalization), the journal
uses a complete exact pre/post-image inverse. Actual evidence capture or
validation failure rejects the edit before write; no private byte cap clips it.
This keeps fallback undo exact even when optional file-history blobs are off.

## Atomic edit groups

A batch that computes several edits to one file publishes only one physical
replacement. Its undo journal likewise records one transition from the file's
original bytes to the final bytes, rather than recording nonexistent disk
states for each intermediate computation. Per-edit OK/FAIL output remains
ordered and unchanged. Attribution and the combined inverse are prepared
before writing; both sides of a multi-edit inverse retain complete bytes.
Failure to preserve that exact evidence refuses the group without mutation.
This also avoids partially persisting a chain of intermediate undo receipts.
Same-file exclusion covers preimage/freshness checks, computation, replacement,
persistence and journal acknowledgement. Lease exclusion and atomic replacement do not by themselves make a multi-file
batch all-or-nothing or commit file and journal atomically; the separate batch
preflight/failure policy above still applies. Post-replacement durability/journal failures retain the
explicit mutation-applied outcomes above; external writers remain outside the
cooperative transaction.

## Complete edit batch admission

Problem: legacy replacement/insertion batches silently discarded entries after
30, while unified `edit_file` both declared and enforced the same private count
ceiling. A late failed preflight could therefore disappear and let an atomic
call publish changes whose complete requested batch was invalid. Decision:
process the entire ordered batch. Unified tool schemas no longer reject the
31st operation; result lines preserve every operation's original position,
including late invalid entries and subsequent valid work.

Grouped replacements still compute in order and publish one physical replacement
and one verified undo transition per file. Atomic preflight considers every
requested operation before publication. Insertion keeps its existing per-edit
undo transitions. Path/read-only authority, freshness, real write failures and
recovery-evidence checks retain their owners; count admission grants no write
authority. Alternatives considered: raising the threshold still drops work;
separate small calls erase batch-wide preflight and grouping semantics.

`test_project_edit_demand.py` verifies schema admission and actual 64-edit
chains with exact undo, a failed 41st atomic preflight with zero publication,
a late invalid operation followed by successful work, 32 ordered inserts
with complete undo, and a physical failure on the 32nd of 40 files that leaves
39 completed files independently undoable and no false success history. Atomicity, write-failure, ownership and recovery neighbors
remain required; independent byte/recovery policies are separate audit targets.
