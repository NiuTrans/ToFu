# Project command snapshots and undo

The runtime owners are `lib/project_mod/run_command.py` (change observations),
`snapshot_content.py` (bounded pre-images), `modifications.py` (history and undo
receipts), and `undo_filesystem.py` (anchored mutations and post-image evidence).
The persisted `postImage` field follows
[`project_undo_postimage_v1.schema.json`](../contracts/project_undo_postimage_v1.schema.json).

## Change observations

Command tracking does not infer writes for opaque commands. Up to 64 literal
relative write targets use a deduplicated scoped snapshot; a literal file never
walks unrelated directories. Globs and other ambiguous target sets retain the
bounded whole-tree fallback. Both paths share one entry budget.


Write exclusion is independent of this observation policy: explicit regular-file
targets coordinate by canonical file, and a shared checkout barrier still fences
whole-tree Git/publication work. Literal interpreter/cat heredoc input does not
itself request writer admission; real header output targets and shell tails remain
visible. Explicit directory creation coordinates its target checkout, not the
unrelated reading cwd. Shell setup/data-target boundaries are owned by
[`project integration`](modules/project_integration.md). A read/test verification is never silently promoted to
an immutable snapshot or granted extra filesystem authority by this exemption.

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

Internal projections request metadata only. Original contents are materialized
after conversation/path selection, and undo verifies each selected range before
mutation. Missing, changed or symlinked payloads fail visibly and block older
undo on the same path. An unreferenced pack is reclaimed only after its final
reference is removed by a confirmed journal transition; shared packs survive
partial undo. Cold index reads also reclaim aged unreferenced crash artifacts
through an entry-bounded pass under the same admission; referenced packs and
young artifacts are excluded. Cache capacity and bytes retain their launch-resolved total;
LRU sessions share that total rather than reserving a fixed fraction per root.

Alternatives rejected: deleting history loses undo; raising the old content
cache retains hundreds of MB unnecessarily; one payload file and fsync per
old record makes migration itself slow; unconfirmed background writes weaken
acknowledgement. Packing is bounded by the existing history cache byte budget
or one required value, with no idle worker or retained payload cache.
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

Direct edits use one launch-resolved per-file recovery budget, independently of
`MAX_FILE_SIZE` (the model-facing read/display budget). The restart-only
`TOFU_PROJECT_WRITE_RECOVERY_MAX_MIB` override is registered in the resource
policy. Automatic personal admission uses the minimum of memory capacity / 1024,
launch headroom / 256 and free volume / 1024, in MiB (1–1024 MiB); unknown memory
falls back to 4 MiB. The distributed fallback is 16 MiB. Original reads, exact
UTF-8 byte admission, staged post-images, recovery classification, reverse-patch
reads and direct-write undo fingerprints share this budget. Command snapshots
retain their separate budgets. Undo batch hashing allows at least one admitted
direct-write post-image (or the existing 20-MiB batch allowance, whichever is
larger), including the EOF probe byte. Intent JSON admission allows exact base64 expansion
plus the contract's 64-KiB envelope. A smaller restart budget may block access to
larger pending evidence visibly; it never deletes that evidence or user history.

Problem: the display cap previously rejected even a short append to a growing
journal above 512 KiB. Decision: separate display and recovery admission, scaling
exact evidence with the shared launch probe. Alternatives rejected: increasing
the display cap couples tool output to undo memory; bypassing capture or truncating
history weakens crash recovery and undo. This remains an inline exact-preimage
journal: each edit retains its complete preimage; optional cache eviction never
prunes durable records. A cold append probe on a 699,612-byte journal
with 39 appended UTF-8 bytes peaked at 14,071,847 traced allocation bytes
(including first-use validator imports), and exact undo restored the original
SHA-256. This measures allocation for one inline-preimage write. The per-file envelope bounds transient work, not lifetime
history. Verify large-file append/edit/upload, exact undo, interruption recovery,
UTF-8/base64 boundaries and budget refusal with
`tests/project_mod/test_write_recovery_budget.py`.

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

Admission allows at most 64 pending records per session, 512 KiB each for
original and post-image content, 1 MiB per encoded record, and 130 directory
entries per scan. Lock contention fails after five seconds. Excess capacity or
malformed records fail closed without eviction; this is durable recovery data,
not a cache. Descriptor lifetimes end with the context, including process exit.

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
uses an exact pre/post-image inverse. Each side is capped at the existing
`MAX_FILE_SIZE` (512 KiB); exceeding that budget rejects the edit before write.
This keeps fallback undo exact even when optional file-history blobs are off.

## Atomic edit groups

A batch that computes several edits to one file publishes only one physical
replacement. Its undo journal likewise records one transition from the file's
original bytes to the final bytes, rather than recording nonexistent disk
states for each intermediate computation. Per-edit OK/FAIL output remains
ordered and unchanged. Attribution and the combined inverse are prepared
before writing; both sides of a multi-edit inverse must fit `MAX_FILE_SIZE`
(512 KiB each), otherwise that file's group is refused without mutation.
This also avoids partially persisting a chain of intermediate undo receipts.
