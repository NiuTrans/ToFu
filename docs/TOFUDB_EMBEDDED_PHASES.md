# Embedded command phases

Owner: `packages/tofu-db/src/embed_command_phases.rs`,
`embed_continuation.rs`, `embed_phase_executor.rs`, and
`server_command_continuation.rs`. Admission and publication remain in
`authority_admission.rs` and `server.rs`; this is scheduling, not authority.

## Problem and decision

A worker that waits for writer admission or an exact WAL confirmation occupies
an OS carrier despite having no CPU work. Eligible commands retain owned input
and admission permits, execute preparation on demand, then park continuations
while awaiting writer/WAL readiness. Wake registration occurs before parking;
FIFO positions and exact generation/sequence confirmations survive resumes.
Work needing CPU uses execution carriers. Queries, unsupported operations,
profiled calls and checkpoint control preserve their existing dispatch paths.
No count quota, resource cap, relaxed sync or implicit command retry is added.

Input memory leases last through reply or cancellation. Weak task wakers and
registry teardown preserve ownership without cycles. Deadlines, panic poison
and cancellation must release every owned permit and wake registration.
Private preparation, OCC conflict fallback, receipts, logical outbox and
commit notices retain the same authority boundaries and exact WAL wait.

Background watch/backup/archive idle waits use interruptible parking: stop
sets its existing flag and unparks the worker before the existing join.
In-progress I/O, shutdown order and join limits remain unchanged.

## Alternatives and evidence

Blocking additional workers adds carrier/stack costs; imposing a fixed worker
cap can starve capable machines; moving I/O into the serving loop blocks user
progress. Parking only dependency waits retains independent CPU concurrency.

A matched normal-release A-B-B-A 1,000-conversation workload passed all content,
event, idle/reopen, 1,000-identity and three exact-body search gates. Candidate
ratios in the two pairs: stream p99 0.840/0.771, peak RSS 0.886/0.947, running
restart 0.408/0.479, terminal restart 0.557/0.715. Productive time improved in
both pairs; settle/idle/full-index results were mixed. Process-thread peaks
were nearly unchanged, so no large thread-reduction claim follows this run.
Real SIGKILL/reopen across 1,000 conversations passed. The original candidate
passed 1,783 native tests (7 ignored); current integration C ABI tests passed
37 cases. Final current-code native verification passed 1,767 cases (7 ignored).
These finite paired measurements are not statistical or universal SQLite
superiority certificates. Broader workload/SQLite and sustained-soak gates
remain part of the active performance goal.


Preparation retains its original FIFO capture phase. Reusing the published
query view to prepare before acquiring that position passed fault and lifecycle
tests but regressed the measured 1,000-conversation tails. The rejected variant
and application/embedded transport coverage are documented in
[the write-path decision](TOFUDB_WRITE_PATH_OVERLAY.md#published-preparation-admission).
A successful capture or lower admission time alone never qualifies a default.


## Profiling boundary

`embed_phase_executor::eligible` intentionally excludes native request profiling.
A profile-on mutating request therefore measures the legacy thread-bound
executor, not default continuation scheduling. The complete per-request ledger
can describe work within that measured path; it cannot qualify the default
path's queueing latency. Pipeline reports must use the authority-incarnation
fences in `STORAGE_PERFORMANCE_CHARTER.md`, retain sample loss, and run separate
profile-off comparisons for performance admission.
