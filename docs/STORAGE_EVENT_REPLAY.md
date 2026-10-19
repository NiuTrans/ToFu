# Canonical Attempt events and conversation replay

## Deleted conversation event lifetime

**Problem:** native soft deletion walked all Turn bodies to discover Attempt
events. Conversations exceeding 2000 identities or 64 nonempty event ranges
could fail deletion despite range-based trash snapshots.
**Decision:** `turn/event_retirement.rs` atomically pins the committed identity
sources and stores an owner-scoped, execution-generation cursor while the
existing delete transaction detaches public and executable state. Restore
remains immediate and inert. The existing `turn.events.prune` maintenance path
reclaims event sequences in bounded transactions, round-robins queued generations
and releases each pin only with its completed cursor. Cursor-only work commits
even when no payload rows exist. Identity claims remain reserved while cleanup
is pending; purge reclaims pending generations before releasing those claims.
Random-key legacy Attempts remain inaccessible after restoration.
**Alternatives considered:** raising row/range limits preserves the foreground
scan and eventual failure; omitting reclamation leaks event data; a new worker
duplicates existing maintenance ownership.

Bounds reuse the generated event-prune attempt, row and materialization budgets.
Each generation holds one cursor and four immutable source ranges; it does not
copy Turn payloads or add a worker. Pins retain source pages until maintenance
finishes, so prolonged maintenance failure retains reconstructible bytes.
The two-generation native fixture measured 218 serialized cursor bytes and two
private pins, then verified zero remaining cursors/pins after bounded draining
and reopen. This excludes index/page and allocator overhead and establishes no
latency/RSS improvement. Fault injection covers both partial progress and final
pin release. Soft deletion/restore is covered at 4096 Turns and 97 event-bearing
Attempts; permanent purge and clone retain their existing identity/range bounds.
Verification: `turn::event_retirement::tests`, `conversation_delete_` tests and
maintenance's `idle_round_commits_event_retirement_without_payload_rows`.
The retained measurements
also record foreground blob collection/decoding: 65,953 / 6,398,259 bytes for
old one/97-Turn fixtures, zero in both candidates. Payloads are approximately
64 KiB per Turn; deferred background work is excluded from this dispatch scope.

## Canonical payload references

Problem: each Attempt event was persisted twice in the same transaction: once
as the canonical event and once inside its conversation-sync envelope. Growing
projection and terminal payloads therefore doubled serialization, blob and WAL
work even though the existing retention index already protected the source.

Decision: new native `attempt.event` sync rows retain their public metadata and
only `payload.event.seq` under the distinct physical document identity
`conversation_sync_attempt_ref/v1`. The canonical Attempt event remains the
single immutable payload, with physical version 1. Both records, the monotonic
sync head and the existing owner-scoped retention reference commit together.
No new worker, cache, log, acknowledgement rule or storage authority is added.
The machine-readable invariant lives in `contracts/tofudb_ir_v1.json`.

Replay and operator export validate the physical/logical sync key, authenticated
owner, conversation/Turn/Attempt identity, source sequence, source version and
matching retention reference. They validate the full source JSON before
splicing its original bytes into the public envelope. Missing or inconsistent
references fail closed. Public wire formats and SQLite behavior are unchanged.
Legacy full envelopes remain readable and preserve their exact bytes on export.
The write limit still counts the expanded envelope; public pagination counts
expanded bytes and preserves the existing resumable-prefix/deadline behavior.

Pruning reads the compact row without loading the source. Attempt pruning
cannot retire a source while a sync reference remains. Sync pruning releases
the reference in the same transaction; pinned snapshots retain both records.
Ordinary backup, restore and GC follow the existing entity/blob roots. Operator
migration exports full envelopes and imports the legacy self-contained format.

Alternatives considered: dropping transport rows loses acknowledged replay;
adding another canonical log introduces another publication and recovery
boundary; retaining duplicate payloads preserves the measured write amplification.
The existing exact reference index supplies the required lifetime fence.

Older native engines reject the new physical identity. Downgrade requires a
current-engine canonical export into a fresh, fully verified target authority
whose replay rows use the old representation, plus folding active text heads as described in
[STORAGE_TURN_TEXT.md](STORAGE_TURN_TEXT.md). Never rewrite runtime documents by
hand or switch backend to bypass a format failure. Insert-or-match import into
the original authority does not rewrite existing reference rows; downgrading
in place is unsupported. Full target migration needs its own source/target
certification before activation.

Verification: `turn::sync_attempt_reference::tests`, native neighbor/full tests,
application storage parity, native backup/restore and browser lifecycle tests.
Performance and release claims require matched source/artifact identities and
same-volume workloads with exact body/event replay after crash. Functional
correctness alone does not establish terminal latency or capacity SLOs.

Measured scope: 16-way, 100 frames of 8 KiB at 20 ms, full carried task events,
same-FUSE source/authority ABBA. Scheduled completion P99 was 954–1222 ms for
the frozen baseline and 492–564 ms for references. With the default 3 s
structural cadence and 40 ms frames, it was 493–652 versus 410–451 ms;
invoked settlement ranges overlapped in that workload. Single/four-way tails
varied. Full 200-event conversation replay after crash remained about 43–45 ms.
Physical endpoint sizes varied; this does not certify steady-state growth or
half-sized storage. Delta coalescing was explicitly disabled. Exact bodies and
replay survived every leg. Artifact pins, all legs and limits live in
`tofudb_canonical_event_replay_20261001.json`.

## Task-event source guards

Problem: task-event retention can delete a middle sequence and a later append
can reuse it. Equal `event.bounds` count/base/next values then describe different
payloads, so they cannot fence a paginated diagnostic read.

Decision: `event.bounds` accepts the optional boolean `include_read_guard`.
The additive `read_guard` has the exact `EVENT_READ_GUARD_CONTRACT` from
`lib/task_replay/task_event_contract.py` (exported in the generated storage catalog) and an
opaque 64-character hexadecimal `stamp`. Default bounds retain their old shape.
A stamp binds one task to the selected authority's open lease. Repeat reads and
identical event replay retain it; changed task events or retention invalidate
it. Other tasks do not invalidate it. Reopen/restore requires a new read: stamps
are not archive identities or durable executor-resume receipts.

Problem: whole-task stamps also restart snapshot replay for unrelated usage or
streaming appends. Decision: optional `types` selects 1..64 exact nonempty types
of at most 128 characters. Scoped bounds count only that selection; a guarded
response uses `EVENT_READ_GUARD_SCOPED_CONTRACT` and echoes sorted unique types.
Consumers require that exact contract and scope; old engines cannot admit it.
No prefixes or empty selection are accepted. Omitted `types` preserves v1.

The scoped revision is `A-(R-S)`: canonical append count/watermark A, whole
retained count R and selected retained count S. Unselected appends increase A
and R together; selected appends change the revision. Selected retention
changes scoped bounds, and prune/reinsert changes A even with equal bounds.
Unselected retention invalidates conservatively. Native metadata and two index
seeks per selected type avoid event-body scans. SQL reads counts and existing
UUID/append/prune witnesses in one statement, with no new durable state or
backfill. Its signed revision may be negative for legacy rows; the first
mutation publishes a UUID and may invalidate once even if unselected.

Native reads combine the existing physical append watermark, retained bounds,
owner/task identity, authority UUID and a lease epoch shared by read replicas.
SQL stores one compact UUID/append/prune witness per changed task in schema 63;
mutations and witnesses commit or roll back together. A batch coalesces witness
updates by task. Retention returns at most 1,000 deleted task IDs and updates
only those tasks. Empty-stream witnesses remain: deleting them during a live
lease could recycle an earlier empty-source stamp. Their lifetime follows the
authority, as does the native physical stream watermark. Existing SQL rows need
no startup backfill; before their first mutation, a legacy stamp includes exact
bounds, and the first canonical mutation publishes a new UUID witness.
Offline migration admits only the exact key/column layout and bounded, valid
UUID/counter rows beside mapped task events; unknown shapes remain blockers.
Target leases produce new stamps, so these payload-free witnesses are rebuilt.

The SQL bounds/witness projection uses one statement, including on PostgreSQL
READ COMMITTED. Query-pool and writer sessions share their backend lease epoch.
These witnesses detect canonical source changes, not arbitrary storage
corruption. Existing payload and snapshot-dependency integrity checks remain.
Readers must confirm the exact contract and compare stamps before and after a
multi-page read; mismatch means discard its completeness/latestness claim.
The source guard is a primitive; selected omitted-history loading and delta
replay belong to `lib/tasks_pkg/request_inspector_history.py`, under the
bounded continuation contract in [RESOURCE_CONTROL.md](RESOURCE_CONTROL.md).

Alternatives: count/base/next alone admits retention ABA; an authority-wide
commit counter invalidates unrelated-task reads; a full payload digest per page
adds repeated whole-history materialization; new per-type counters need upgrade
and lifecycle machinery. Existing metadata supplies bounded, scoped witnesses.

Verification: `tests/storage/test_event_read_guard.py` and native
`indexed_stream::read_guard_tests` cover repeat/dedup, unrelated-task isolation,
rollback, retention ABA, empty/recreated streams, read replicas and reopen.
Scoped cases also cover noise appends, sparse sequences and exact scope binding.
Backend performance and long-history UI acceptance require separate evidence.

A synthetic SQL measurement with 100 events across 10 tasks retains 10 witness
records, including after all event rows are pruned. Their logical key/UUID and
two 64-bit counter fields total 540 bytes; this excludes table/index/page
allocation and is not RSS or a throughput result. Physical growth and
steady-state append/retention latency require separate capacity measurements.

## Materialized task-event page size

Problem: `event.list` can select individually valid image-bearing snapshots whose
combined replay envelopes exceed the native 8 MiB materialized-page boundary.
Reporting this deterministic rejection as `database_resource_exhausted` hides its
cause behind dispatch's deliberately sanitized error text and incorrectly asks
clients to retry an unchanged page as transient authority pressure.

Decision: the authority reports the existing non-retryable
`storage_payload_too_large` code for that boundary. Request Inspector halves its
positive page limit at the **same** sequence cursor, down to one row, preserving
all payload bytes and chronological snapshot dependencies. Both ordinary reads
and source-fenced long-history continuation share this classification. A
one-row rejection, corruption, protocol error, or generic availability failure
remains an explicit failed read; it is not EOF and never creates a successful
partial transcript. The 8 MiB cap, owner fences and source guards are unchanged.

Alternatives rejected: treating all resource exhaustion as page size would hide
real admission pressure; enlarging response budgets merely moves the failure;
truncating page payloads destroys diagnostic/wire evidence. Old engines that
sanitize the size failure into generic exhaustion require normal engine
deployment to gain this distinction; application source changes alone cannot
repair an already-running older native authority.

Verification: native
`event_list_materialized_page_overflow_is_payload_size_not_availability` and
`tests/diagnostics/test_request_inspector_page_size.py` exercise complete exact
paging, sparse cursors, bounded retries, one-row failure and unrelated errors.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
