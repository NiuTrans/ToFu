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
In-place editors are parsed by their documented grammar so their operands become
literal targets: `sed -i`, awk `-i inplace`, and `sd FIND REPLACE FILES...`
(`_sd_invocation` in `command_analysis.py`). A stdin-only or `-p/--preview` `sd`
is a read-only filter; an `sd` option outside its grammar keeps the command opaque.


**Post-commit handoff:** stop lane ownership once the requested ticket completes; draining later callers' listeners delays completed edits. Later callers await earlier tickets and their own callback; nested work stays synchronous. History staging is unchanged. Phases `post_commit_wait`/`post_commit_callback` separate waiting from execution; `test_post_commit_file_release.py` proves earlier return while a later listener is held.

**Lockfile I/O isolation — Problem:** slow filesystem opens held the process-wide lease registry, coupling independent edits. **Decision:** one counter/condition under the existing registry reserves each opening. Open outside registry ownership, then register or close the FD before retiring its reservation; POSIX flock follows registration. Fork waits for pending handoffs and closes inherited FDs in the child without unlocking the parent. Interrupted retirement completes before propagating its error; the at-fork barrier finishes despite callback interruptions because CPython ignores callback errors. Blocked filesystem calls can still delay fork, not unrelated lease admissions. **Alternatives rejected:** removing the fork guard leaks unregistered inherited FDs; moving namespaces breaks running-process cooperation; retaining filesystem I/O under registry serializes independent work. No thread, queue or durable owner registry is added. `test_workspace_lock_io_isolation.py` covers held opens, isolated real forks, interruptions, cancellation, pre/post-mutation registration faults and directory-close failure.

Write exclusion is independent of this observation policy. Model tool calls take no workspace lease ([`Git workspace guard`](runbooks/GIT_WORKSPACE_GUARD.md#tool-calls-take-no-workspace-leases-owner-decision-2026-10-28)): one write call holds only the in-process edit mutex of each resolved target (`file_edit_mutex.py`) from preimage read through publication and journal acknowledgement, so two concurrent edits of one file both apply, while shell commands are not coordinated with it. Literal command write targets still feed snapshots and undo history; they no longer request any admission. A read/test verification is never silently promoted to an immutable snapshot or granted extra filesystem authority.

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

## Refusals and faults

Problem: undo and redo reported every failure as `ok: false` data, so the
conversation gateway answered all of them as the user's 400. That channel also
carried engine faults (file history failing to load, a restore crash's error
envelope), and per-file rows carried raw exception text, including server
paths, into the 400 body and the turn's durable `fileChanges.error`.

Decision: refusals are typed data; faults are typed server errors.

- A refusal is the engine's authored answer, marked `refused: true` on the
  result (no session, no snapshot for the task, history disabled) or on a
  `details.failed` row: missing or stale evidence (no post-image, original,
  reverse patch or backup; the file changed after the recorded change; a
  later change blocks it) or a target that the path validators or the
  anchored undo filesystem refuse with `ValueError` (escape, symlink,
  non-regular file, a target that changed during the restore).
- Every other failure is a fault. A fault row has `refused: false` and a
  reason that is either fixed engine text or `internal error (<type>)`; the
  exception and traceback are logged where it was caught. A fault with no
  per-file row (file history unavailable, a restore crash) raises
  `modifications.FileHistoryFault`, a `RuntimeError` whose message is fixed,
  disclosable text.
- The conversation gateway (`lib/conversation_sync/runtime.py`) translates
  only typed refusals to `BadRequest`. A fault row, an untyped `ok: false`
  result, or any other engine exception (an unreadable journal, a busy
  session lock) becomes a `FileHistoryFault`. The file command records its
  message as `fileChanges.error`, returns the block to its source state, and
  the command boundary answers a logged, redacted 500.
- Undo semantics are unchanged: completed records are consumed, failed ones
  retained, and a refused or faulted undo never settles as `undone`.

Alternatives rejected: classifying `ok: false` text in the gateway cannot tell
an authored reason from an exception string. Raising refusals from the engine
would turn the v1 project routes' refusal receipts into 500s and drop
partial-undo receipts. Redacting every reason would hide user-actionable
answers such as a file changed after the turn. Verify with
`tests/conversation/test_conversation_file_change_undo_outcome.py`,
`tests/conversation/test_conversation_file_change_fault_boundary.py` (SQLite
Sidecar, real route) and the redo-scope and undo-resolution suites.

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
a crash after it replays nothing. A later fold that still finds the artifact
keeps only rows its caller never loaded, so records an undo removed are not
taken back from it. A torn trailing line has no newline and is
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
partial undo. Aged unreferenced crash artifacts are reclaimed under admission
as described in "Crash-artifact sweep" below; referenced packs and young
artifacts are excluded. Cache capacity and bytes retain their launch-resolved total;
LRU sessions share that total rather than reserving a fixed fraction per root.

Completion diagnostics use `get_task_modification_paths`: exact task/optional conversation, distinct addresses, remaining count bound (200 across roots), recovery before selection (admission only while intent evidence exists); the history read itself takes no admission (see "History reads take no journal admission"). Detached addresses survive tool release without copying historical rows or hydrating content. Cold/invalidated caches still validate the entire JSON authority: projection allocations, not cold I/O, are bounded.
Moving capture after release or reusing truncated diagnostics for asynchronous presentation was rejected: it loses capture timing or complete/legacy-attributed file changes. Tests: `test_task_modification_paths.py` and `test_project_write_recovery_crashes.py`.

Alternatives rejected: deleting history loses undo; raising the old content cache retains hundreds of MB unnecessarily; one payload file/fsync per record slows migration; unconfirmed background writes weaken acknowledgement. Packing uses the existing history cache byte budget or one required value, with no idle worker or retained payload cache.
Verification: `tests/project_mod/test_project_undo_payloads.py`, journal
failure/recovery, identity, concurrent writers and undo evidence suites.

## History reads take no journal admission

Problem (2026-11-01): every read-only projection of the undo history held the
project's exclusive journal admission, the writers' lock, while it parsed the
whole history. After each foreground `run_command` the tool round re-read the
complete history only to rebuild its own file-change card; settlement,
completion diagnostics and the turn diff did the same per root. A 16,787-record,
15 MB history exceeds the history cache budget, so nothing was cached and each
read repeated a parse, validation and cache-weight walk (about 0.47 s of CPU)
plus crash-artifact cleanup under the lock. Concurrent commands convoyed on
it, and an edit of an unrelated file in another conversation failed with
`[journal-busy]` after the 30 s admission budget. The admission is legitimate for writers, not readers.

Decision (owner decision 2026-11-01: remove writer admission from reads):
the admission orders writers only. A `run_command` round lists the changes
that command itself recorded for undo (`LocalProjectResult.recorded_file_changes`)
instead of reading history. Metadata reads (`get_modifications(include_content=False)`,
`get_task_modification_paths`, `get_conv_ids_with_modifications`) first
reconcile pending write intents, taking the admission only while intent evidence
exists (`write_recovery.has_pending_intents`), then read the committed history
without it (`modifications._committed_rows`). Writers only append to the log
or replace the index and fold artifact by rename, so a read that sees the same
index, fold artifact and log inode before and after, and parses only the log
prefix it sealed, is one consistent prefix of history. A read that overlapped a
fold, undo or replacement is repeated, never served mixed. A reader publishes
its snapshot only while the session's cache generation, advanced by every
publication, extension and invalidation, is unchanged, so it never replaces a
newer writer's entry. Content hydration (`include_content=True`), undo and
project open keep the admission: a selected payload pack must not be reclaimed
before its exact read.

Alternatives rejected: a shared/exclusive lock (a reader would still delay
writers for a whole-history parse); raising `lockWaitSeconds` or the cache
budget (hides the convoy behind tuning knobs, and histories outgrow any budget);
trimming history (durable undo state); keeping journal-derived run cards (cost
proportional to history per command, and every later command repeated earlier
commands' changes); dropping recovery from reads (crash evidence must be
reconciled before history is presented, as `test_project_write_recovery_crashes.py`
requires).

Measurement (one run each, local copy of the real 16,787-record journal, one
warm commit first): 10 edits committed while 4 threads read metadata in a
loop. Before, the median commit took 16 ms but the slowest waited 21.1 s behind
the reader convoy; after, no commit waits on reader admission (slowest 1.09 s)
and reads rose from 2.0 to 3.7 per second. The after-median of 0.93 s is GIL
contention from four concurrent full parses, a stress rate far above
production once run cards stopped reading. Executable specs:
`tests/project_mod/test_project_journal_read_admission.py`,
`tests/tasks_pkg/test_run_command_file_changes_meta.py` and the
recovery-before-listing cases in `test_project_write_recovery_crashes.py`.

**Oversized history verdict.** Problem: a history whose decoded rows exceed the
cache budget is never admitted, yet every cold read walked all rows to weigh
them again: about 0.25 s of the 0.47 s of CPU per read of that history
(50,331,746 bytes against a 50,331,648-byte budget). Decision: remember per
session the journal identity and budget of the refused snapshot. A later
snapshot with the same index and fold artifact and a log of the same inode at
least as long only adds rows, so it is refused without a walk. A fold, undo or
replacement changes the index identity and is weighed again; a different budget
never matches. Alternatives rejected: raising the budget (a knob that growing
histories outrun); comparing JSON bytes with the budget (decoded rows outweigh
their JSON, but a 15 MB journal under a 48 MiB budget still needs the walk).
An ABAB run of the benchmark above (16,845 records) measured, before both
changes, a median commit of 9/13 ms, slowest 13.7/12.7 s, at 2.1/2.3 reads
per second; after, 0.58/0.71 s, slowest 0.92/1.05 s, at 4.6/4.4 reads per
second. Verify `test_oversized_history_is_weighed_once_until_its_index_is_rewritten`.

**Key strings are charged once.** Problem: the cache weight charged every dict
key at every row, but decoded index rows share their key strings. The live
17,413-row history retains 41.0 MB (tracemalloc and an identity-deduplicated
walk agree) yet weighed 62.6 MB against its 48 MiB budget, so it was never
admitted and every settlement metadata read re-parsed and re-validated all of
it (about 240 ms of CPU per read). Decision: a weight walk charges each key
object once; values are still charged at every occurrence, so shared values
stay over-counted. The set of charged keys is bounded by the record schema and
the rows of the bounded append log. The same history weighs 42.6 MB and is
admitted; budgets are unchanged. Alternatives rejected: an identity-deduplicated
walk of every object (exact, but its seen-set grows with the history); raising
the budget (a knob the history outgrows). Once the retained rows outgrow the
budget (near 20,500 rows of this history, at about 2.4 KB per row), conversation
reads use the located index described below; reads of the whole history decode
it again. Verify `test_budget_admits_a_history_by_its_retained_size`.

**Appends never send a warm history cold.** Problem: the cache identity covers
the whole log, so a read between another writer's append and that writer's
cache extension, or after another process's append, missed and parsed the
entire history; a reader that then published its older snapshot made the next
writer's extension drop the entry, so under continuous writes the history
thrashed between warm and cold (31 of 40 reads parsed it in one probe round).
Decision: when the current identity only grew the cached prefix's log (same
index, no fold artifact, same log inode, prefix ending on a complete line), a
reader returns the warm rows plus the log lines after their offset, without
publishing; a writer whose entry is such an older prefix catches it up the
same way under its admission instead of dropping it, and keeps an entry a
reader already published at the current identity. A replaced index, a torn
prefix or a fold artifact still takes the full read. Alternatives rejected:
readers publishing the extended snapshot (races the writer's in-place
extension); skipping the generation bump when no entry exists (lets a reader
publish over a newer commit, which
`test_reader_never_publishes_its_snapshot_over_a_newer_commit` forbids); a
reader lock (reads take no admission by owner decision).
Measurement of both changes (`benchmarks/project_journal_write_read.py`, a copy
of the live journal with 17,629 rows and a 15 MB index on this host's FUSE
mount, 4 threads with different conversations each making 10 `write_file`
calls followed by the settlement metadata read, fresh alternating processes,
three rounds, medians): wall 12.3 to 0.94 s; write p50 630 to 85 ms and p95
1,930 to 119 ms; read p50 262 to 2.8 ms with CPU 248 to 1.9 ms; full history
parses during the 40 reads 41 to 0. The first write of a process still
validates and weighs the history once (0.6 to 1.2 s). Verify
`test_reads_after_unextended_appends_parse_only_the_appended_lines` and
`test_append_catches_a_stale_warm_prefix_up_instead_of_dropping_it`.

**Conversation reads of an over-budget history.** Problem: accurate accounting
only moves the cliff. A history whose retained rows exceed the cache budget is
never admitted, so each metadata read decoded and validated all of it again,
and settlement (`commit_round` `_derive`, `_commit`, `_turn_diff`) and
workspace evidence (`get_task_modification_paths`) pay that per call and root.
Decision: those reads name a conversation. Once a history is known to exceed
the budget (the verdict that already skips its weight walk), a conversation
read scans the index once into an `_IndexLocator`: two byte offsets per row,
grouped by conversation and keyed by the exact index file identity. Later
reads decode only that conversation's index rows, adjacent rows with one
`pread` after `fstat` proves the index is still that file, plus the parsed
append log, which is kept and advanced by appended bytes only and is bounded by
`_JOURNAL_TAIL_MAX_BYTES`. A fold, undo or replacement changes the index
identity and is located once more; a fold artifact or a non-ASCII legacy index
takes the whole-history read, and in-budget histories keep the row cache.
Alternatives rejected: keeping over-budget rows resident (an unbounded cache);
settlement from in-process recorded changes (settlement deliberately rebuilds
from the journal authority, and its tests inject rows at `get_modifications`);
a summary of the task ids in the index (tasks spanning a fold and the
unattributed-timestamp fallback would still decode everything); reading the
whole index file per read (I/O proportional to the history).
Measurement: the live 17,719-row index locates in 0.32 s into 365 KB (386
conversations, about 20 bytes per row); its largest conversation (870 rows)
reads in 9 ms against 232 ms for the whole history. The write/read probe above
with a 32 MiB budget below the copied 41 MB history (three ABAB rounds,
medians): wall 11.5 to 1.3 s; write p50 534 to 72 ms and p95 1,709 to 600 ms;
read p50 251 to 2.7 ms with CPU 242 to 0.4 ms; whole-history decodes during the
40 reads 41 to 0, plus one or two index scans per run (concurrent first reads
each scan). Verify
`test_oversized_history_serves_conversations_without_reparsing_it`.

**Crash-artifact sweep.** Problem: every admitted cold history load (undo,
project open, content hydration, and the scoped edit batch, which loaded the
history on every call) scanned the payload directory for unreferenced crash
artifacts while holding the project admission; the 2026-11-01 stall dump caught
the holder in that scan while seven writers waited. An over-budget history is
cold on every load, so the scan repeated per call, and its entry window
(`_MODS_CACHE_CAPACITY * 8`, 1,536 entries at the default capacity) examined
only the same leading entries of an 11,935-entry directory, so later artifacts
were never reclaimed. Decision: the sweep needs the admission (no commit is
between packing and publishing) and the complete committed rows read under it,
but artifacts only appear when a commit crashes or fails. A process sweeps a
session once, in one complete pass, on its first admitted complete load (the
first write's validation or a cold read); write recovery sweeps again whenever
it reconciles a crashed writer's intents. Later loads and writes never scan.
The scoped edit batch validates the journal by identity (`_assert_journal_ready`)
like every other writer instead of loading it. An artifact of a failed commit
that leaves no intent (a command recording, an undo) waits for the next
process's sweep: it is reconstructible garbage, never history. Alternatives
rejected: the per-load pass (repeats a directory scan under the writers' lock
without new evidence); a crash marker written before each commit (one more
fsync per edit); sweeping without admission (an in-flight commit's fresh pack
looks unreferenced, and the age rule is not exclusion); keeping the entry
window (an arbitrary bound that leaves a once-per-process pass incomplete).
On the live session (11,935 packs, 17,268 rows) a complete pass is one
directory scan of about 0.1 s; it found no unreferenced pack. Verify
`test_crash_artifacts_are_swept_once_per_process_and_on_crash_evidence`,
`test_crash_artifact_sweep_is_one_complete_pass` and
`test_scoped_edit_batches_never_reread_or_sweep_an_oversized_history`.

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
batch under the per-file edit mutexes, take the tracked journal admissions only
for a batch that will publish, then publish one transition per physical file
(see "Admission order and holder evidence"). Alternatives rejected: looser semantic matching can target
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
its modifications are read, undo is requested, or another tracked write starts;
a read takes the journal admission only while intent evidence exists.
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

The per-file edit mutex protects the exact canonical target, not the whole
checkout. The session admission still spans intent recovery/preparation,
atomic publication, journal RMW/acknowledgement and file-history staging;
parallel targets never consume another active intent. Presentation tickets stay
unready until the edit mutex is released and write-freshness evidence is current.
Only then do ordered/reentrant callbacks drain, so cross-file listeners cannot
create a mutex wait cycle or overwrite a newer freshness token. Upload
rename candidates require protection and revalidation of the actual final path,
not merely the requested filename. Tests exercise real Git-backed checkouts,
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

**Warm append followed by a read (2026-10-27).** Problem: a validated small
journal append invalidated the entire warm cache, so the next metadata read
decoded, validated and weighed the historical prefix again. Decision: after
the append is durable, extend only a still-admitted cache whose identity
matches the pre-append journal. Charge the new record, identity and list
growth conservatively under the existing byte/count budgets; update the
primary count projection without traversing historical rows. Session admission
fences writers (readers take none and never publish over a newer entry; see
"History reads take no journal admission"), filesystem work stays outside the global
cache lock, and an entry evicted during commit remains evicted. Cold caches,
changed authorities and oversized entries retain authoritative disk reads.
Alternatives rejected: raising cache budgets leaves append invalidation intact;
reusing a prefix without its disk identity risks losing external edits;
rescanning all rows to calculate cache weight preserves the CPU stall.

A fresh-process ABBA comparison on this host's FUSE mount used 10,000 initial
records and 12 durable append plus complete metadata-read operations per leg.
Baseline median time was 95.5/89.7 ms, versus 7.0/7.7 ms after this change;
median process CPU was 83.0/82.6 ms versus 2.2/2.3 ms. Every response and final
disk replay matched the same 10,012 authored rows. Accounted resident cache
grew from 9,677,059 to about 9,697,667 bytes (about 0.21%) within the same 64 MiB
test budget. This component measurement includes fsync and makes no browser
latency claim. Reproduce with `benchmarks/project_journal_cache_append.py`;
verification includes `test_project_journal_cache_append.py`, append/crash,
cache-budget, contention, payload, durability and post-commit-order suites.
An earlier baseline leg aborted on the journal-changed-during-read guard;
the fresh complete comparison above excludes that incomplete run. A separate
40-read filesystem identity probe did not reproduce the change; consistency
checks remain strict and the one-off cause is unresolved.

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
cache capacity (256 KiB per capacity unit; a session may use the aggregate
byte budget). Oversized histories stay readable from disk, not
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

**Admission order and holder evidence (2026-11-01).** Problem: admission was a
10 ms flock poll, so a later arrival could take the journal ahead of an earlier
waiter. Under a stream of admitted operations (a copy of the 15 MB journal, four
looping history readers) one edit waited 11.1 s while 19 admissions requested
after it were granted first, and live edits exhausted the 30 s budget. The
refusal named only the holder's thread, and every task's tool pool names its
first worker `tool-parallel_0`, so it implicated the sibling call of a parallel
batch while another conversation's history read held the journal (see "History
reads take no journal admission"). `edit_file` also held the admission while it
read and matched its files and ran the report-only syntax check (up to two 5 s
compiler runs for C/C++), and an atomic batch that would write nothing still
queued for it.

Decision: the waiters of one process are admitted in arrival order. Only the
oldest waiter competes for the cross-process flock; a release or an earlier
waiter's departure wakes the next one at once, and only a holder in another
process is polled. A timeout or failure removes the waiter from the line, and a
forked child inherits no place in line or holder evidence. A refusal names the
holder's tool call, whether it belongs to the waiting task, how long it has held
the admission and how many earlier waiters of the process are still ahead. The
`lockWaitSeconds` budget, the `journal-busy` result fields and the retry contract
are unchanged. `edit_file` plans every file under its edit mutexes, takes the
destination journals' admissions only when the batch will publish, publishes,
then reports and runs the syntax check after release; publication still
re-verifies each original before replacing it. The scoped file handler validates
its journal by identity (`_assert_journal_ready`) instead of re-reading the whole
history under its admission.

Alternatives rejected: raising `lockWaitSeconds` (hides the convoy and turns
refusals into longer stalls); failing fast on a guessed holder duration (a
heuristic cutoff that refuses legitimate serialized work); a cross-process fair
queue file (durable queue state and crash cleanup for an ordering the in-process
line already provides where the contention is); per-file journals (writers of
one append log must still exclude each other).

Measurement (two ABAB runs on this host's FUSE mount, copy of the real
16,909-record, 15 MB journal, after one warm-up edit and read: four threads
re-read history under admission in a loop while two one-replacement `edit_file`
calls on different 1,200-line Python files start together): before, the slower
edit waited 11.1 s and 1.6 s while 19 and 1 admissions requested after it were
granted first; after, 1.6 s in both runs with none, the wait covering only the
readers already in line. Each edit's own admission hold fell from 42–50 ms to
27–28 ms. Without readers, two such edits from a cold process both committed
within 0.2 s. For writers alone (16 commits of different files from four threads
into one journal, eight copies at once on local disk), the slowest admission
wait per copy was 3.7–4.5 s before and 0.6–1.4 s after. Verify
`test_project_journal_admission_order.py`, the parallel-edit and rejected-batch
cases in `test_project_journal_contention.py`, `test_project_session_lock_fork.py`
and `test_scoped_batch_validates_the_journal_without_rereading_history`.

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
persistence and journal acknowledgement. The edit mutex and atomic replacement do not by themselves make a multi-file
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
