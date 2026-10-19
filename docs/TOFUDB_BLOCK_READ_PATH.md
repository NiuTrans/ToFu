# Verified block reads and page-source attribution

This contract owns shared block reads in `packages/tofu-db/src/block.rs`.
The immutable content ID, admitted bounds, CRC/hash checks and alternative-copy
fallback remain mandatory. Entity ownership and durable witnesses remain in
[native storage](STORAGE_TOFUDB.md); page-level spans remain in
[barrier scheduling](TOFUDB_WAL_BARRIER_SCHEDULING.md).

## Problem and measurement decision

A previous 1,000-conversation sample measured 4.37 s foreground page-source
reads versus 5.90 s inclusive page loading. Cache acquisition and page decode
were smaller. That source span combines several backends; it does not establish
physical I/O as the cause. Shared reads expose each branch when `TOFU_DB_REQUEST_PROFILE_EVERY` is
nonzero; the default-off path skips these new clocks and atomics. Existing
coarse process timers remain unchanged. Hot pending/cache reads use const-generic
measured/unmeasured bodies with one shared data algorithm. The measured
entry is cold and non-inlined; the unmeasured body and conditional timer helper
are inlined, so inactive timer frames can be eliminated outside the cold call.
This addresses observer cost, not I/O semantics.
Process/component counters remain bounded and content-free; optional request
spans keep their existing sampling and retention.

`block_shared_read` covers verified shared payload and materialized metadata
reads. `get_pending` covers pending lookup; `block_pending_wait` ends on its
mutex acquisition. `block_cache_wait` ends on cache acquisition;
`block_cache_access` covers lookup/recency mutation or insertion.
`block_history_resolve` covers routing and its children;
`block_history_source_open` covers bounded handle lookup/open;
`block_history_source_wait` ends on source-file acquisition;
`block_history_source_read` covers the exact byte-range read.
`block_content_verify` covers content/CRC checks (including error cleanup);
`block_materialized_resolve` covers loose/packed resolution;
`block_loose_read` covers bounded loose-file reading and verification.

These are nested elapsed spans, not CPU time, cache-hit counts or physical
write totals. Do not add inclusive parent/child totals. Cache and source lock
stages are classified as waits in the report. No payload or key is retained.

Alternatives considered before measurement: larger caches may reduce misses
but increase resident memory; blindly removing locks changes cursor
and cipher-cache semantics; skipping hash checks breaks immutable authority;
pre-populating decoded pages before backend admission can hide failed writes.

## Positioned history source reads

The diagnostic sample found about 1.73 s of source-file lock waiting and 3.58 s
of range-read elapsed time, versus about 75 ms each of block-cache waiting and
access. Those nested totals do not establish CPU or disjoint wall time.

`VfsPositionedReader` admits read-only, independent-offset access.
`RealVfs` on Unix uses the platform's positional exact-read operation on one
shared descriptor. `DeterministicVfs` retains the same Open/Metadata/Read fault
operations and bounded read checks. `HistorySourceReader` selects that
capability; VFS wrappers without it use the existing mutex-protected `VfsFile`.
In particular, encryption is never bypassed: `EncryptingVfs` retains its
ordinary region-decrypt reader. Non-Unix RealVfs currently retains the fallback.

Source length, framed format and footer ID are checked on opening. Every
payload still checks its offset/length bound and exact content ID. Truncation,
read errors and corruption invalidate the cached source handle; routing still
verifies published source membership and alternative-copy fallback.
Positioned source-read timers also include the small framing reads at open;
source-file wait timers belong only to the cursor fallback. Other VFS and
caller admission bounds, prepared snapshot pins and authority fences remain.

The same source-handle cache and descriptor count are used; no pool, worker or
full-source buffer is added. Concurrent I/O uses callers already admitted by
launch-derived connection/embedded-worker budgets. Resource measurement must
cover peak process RSS and the owning capacity workload before integration.

Alternatives rejected: duplicating one descriptor per caller inflates lifecycles
and descriptor budgets; removing the cursor mutex while retaining seek/read
races on file offsets; changing all VFS files to Sync would require changing
mutable cipher-cache and write semantics; raw filesystem reads outside VFS
would bypass encryption and deterministic faults. The read-only capability
preserves those seams while removing avoidable plaintext source serialization.

Source identities, raw measurements and alternatives are retained in
`tofudb_block_read_path_20261001.json`.


## Verification and defeated controls

The same-source concurrency test blocks two actual reads from one verified
history source and requires both to enter before releasing either. The fault
contract warms that source, injects a read failure, corrupts bytes, then
truncates it; every invalid source fails closed and invalidates the handle.
The frozen comparison suite passes 1,464 tests with two existing ignores, including
encryption, deterministic faults, publication/replay and online collection.

Always-on fine probes were rejected as the default after both candidate
capacity legs used more peak RSS and point-read means increased from
0.041–0.042 to 0.052–0.055 ms. Gating probes on existing profiling restored RSS
to the measured baseline range, but one point-read leg remained slower and
conversation-open means still increased. The supplemental BAAB point/open
control is retained in addition to, not instead of, the original outlier.
Const specialization improved the first micro wave, but the reversed-order
control again regressed conversation-open and one point-read leg. The capacity
confirmation improved stream P99 while regressing settle P99; it does not erase
the opposite preceding stream signal. Those intermediates were withheld.

The final source isolates measured stack frames behind cold calls and inlines
the unmeasured body. The full-test, CLI and loaded FFI identities agree
(`cbe71edcc5ee` prefix; full hashes in evidence). The report parser has 23
passing tests. Sequential and 32-session wire workloads each survived two real
SIGKILLs with 10,002 exact acknowledged payloads and no loss. Two crash samples
do not certify recovery P99; the embedded capacity reopen is a separate scope.

| Final default-off 1,000-conversation ABBA | Baseline A1/A2 | Candidate B1/B2 |
|---|---:|---:|
| Admission P99, ms | 2841.6 / 2806.1 | 2754.2 / 3002.0 |
| Scheduled stream P99, ms | 4486.1 / 4454.8 | 5075.6 / 4418.2 |
| Settle P99, ms | 7012.4 / 6413.0 | 6442.6 / 6759.4 |
| Peak process RSS, bytes | 1867014144 / 2042650624 | 1768906752 / 1918312448 |

Final point means are 0.042–0.043 ms in ABBA and 0.042/0.042 ms in the reversed
control. The large point-read regression did not recur. Open means still vary;
the reversed control's open P50 is 0.057/0.058 ms versus 0.056/0.056 ms baseline.
That small observer/layout overhead is retained, not certified away.

Decision: admit the read-only capability and opt-in anchors on the strength of
actual same-source read concurrency, retained fault/encryption/authority
contracts and final bounded resource measurements. No new cache, pool,
per-caller descriptor or durable growth is introduced. This removes a verified
source-cursor serialization constraint; it does not certify an end-to-end win.
One final stream leg is slower and latency ranges overlap. All capacity tail
budgets in every wave failed. The final diagnostic selected 750 of 12,000
requests with zero drops; foreground page-source elapsed was 3,382.8 ms and
source-file wait was absent. Rotation parking still accumulated 44,748 ms
across callers. These nonpaired nested samples are not speedup or CPU proofs.

Sustained-write FIFO admission, WAL rotation/drain and physical-root publication
remain the next root dependencies to measure and change under their existing
durability and reclamation fences. The broad SQLite/Agent Harness performance
objective is unachieved; defeated controls and every original outlier remain
in evidence and isolated Git history.


## Integration identity boundary

Shared integration preserves concurrent typed-tool projection edits in
`turn.rs` and its settlement test. Fixed-source performance comparisons above
belong to the earlier frozen read implementation, not the later combined build.
The combined source (`38e6f34c627e` prefix) passes 1,465 native tests with two
existing ignores. Actual debug CLI, release CLI and loaded FFI digests match.
Its default-off and 1/16-profile 1,000-conversation checks each have zero errors
and 1,000 exact contents after reopen; profile selection drops zero samples.
Both still fail absolute tail budgets. Its sequential and 32-session workloads
each repeat two actual SIGKILL recoveries with 10,002 exact contents. Those
single capacity legs verify combined correctness and attribution, not paired
superiority. Shared integration must preserve both concurrent files exactly;
the external integration receipt binds the reviewed paths to the isolated
commit and final shared source digest.

One copied-input verification initially reused stale Cargo artifacts: the
claimed checkout digest differed from the actual CLI digest and the new test
was absent. The identity guard rejected that pair before integration. Those
logs are retained as invalidated output, not passes for the copied source.
Invalidate isolated input/build-script timestamps after preserving old mtimes;
require the actual debug CLI digest and fresh test presence as well as the
release CLI and loaded FFI digest. A checkout fingerprint computed after a
suite alone cannot attest the executable that ran it.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
