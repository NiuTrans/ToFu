# Experimental Turn execution frames

## Status and scope

This is an internal `cfg(test)` experiment, not a public operation or a default
optimization. The frame module, header frame port and IR entry point are excluded
from production builds; default transports cannot write experimental frames.
The production semantic compiler creates exactly one `TurnEventRecord` step per
`turn.event.record` request. No production caller invokes
`prepare_execution_frame_ir_with_deadline`. A workload delivering separate events
one second apart does not gain an N-to-one execution boundary from this change.

The experiment consumes multiple already-arrived steps in **one existing atomic
IR transaction**. Both the control and the candidate commit once, using the same
AuthorityDatabase, owner checks, transaction validation, WAL and durability policy.
No future event is awaited and no response is acknowledged before that commit.

## Problem

`event_record` already commits the projection, attempt event, conversation sync,
carried raw event, terminal continuation and queue acknowledgement together.
`event.append_batch` batches raw events; `preceding_events` preserves bounded
progress events while advancing the final projection once. WAL group commit
shares the physical flush. None of these eliminates repeated Turn materialization
when a single IR already contains several projection changes.

For a large running Turn, the ordinary step reloads the checkpoint and its patch
chain, clones and encodes the materialized projection, then updates the Turn,
lane, compaction, recovery, recency and header indexes. Repeating this in one
transaction repeatedly visits an unchanged large prefix. The historical patch
chain is bounded at 64 patches, so quadratic replay work occurs within that
window rather than growing without bound across the lifetime of a task.

## Decision

Prototype an explicit same-attempt execution boundary in
`src/turn/execution_frame.rs`, sharing `load_event_record_state` and
`publish_event_record` with ordinary `event_record`. Admission and replay remain
owned by Turn; there is no second state machine or storage authority.

The frame loads one private projection, consumes each existing typed patch,
retains every logical event and response, then publishes its final projection and
derived indexes once. The header advances by the number of logical changes and
uses the **last** input timestamp, including when timestamps are non-monotonic.
An incremental exact JSON byte witness checks the settleable snapshot limit on
every intermediate state. Away from the snapshot ceiling, only affected fields
and appended text are measured after the initial projection. Near the ceiling,
the shared settleability check still reconstructs restart settlement on every
step, scans tool history and can count the entire prefill text prefix. This
prototype does not remove that O(N × prefix) fallback. The private tree and
witness are dropped on error.

Accepted transitions are deliberately narrow: one already-running bound attempt,
one task and flow root, `projection_updated`, with top-level content, thinking or
phase set/append patches. All exact phase/raw/attempt/sync events are retained;
none is coalesced away. Pending starts, terminal settlement, tool structure,
interaction, effect acknowledgements, segment normalization, mixed attempts and
any other IR step are refused. Stored guidance receipts also require a no-op
proof through the ordinary preservation helper on a private copy of toolRounds.
A legally admitted receipt can still need normalization even when a new patch
only touches text; that state is refused without mutations. The initial proof
retains the shared helper's tool-history scan cost; arbitrary guidance history
is not claimed to admit in linear time. This is not yet the complete desired execution
frame joining checkpoint frontier, effect intent and usage receipt.

## Observable correspondence

Each original response slot and projection revision remains equal. Attempt events,
patch replay, hydrated reads, raw events, conversation sync and the final public
Turn/header must match ordinary sequential steps inside the same transaction.
There is no externally visible intermediate commit in either arm.

Physical versioned-document tokens and checkpoint layout intentionally differ:
the final Turn/header are physically replaced once, rather than once per step.
These tokens are private storage metadata, not the public projection revision or
header revision. The experimental port forbids later reads or conditional steps
inside the IR, so no same-IR consumer can observe an intermediate physical token.
A future general optimizer must prove that condition or refuse fusion.

Receipt lookup/insertion and encrypted logical outbox capture use the existing IR
owner. Retry and WAL crash tests cover their atomicity with the frame. However,
the experimental fixture payload has **no production semantic replay compiler**.
Passive logical replay and public admission are not certified by capture tests;
a wire protocol and its replay compiler are prerequisites for exposing this port.

## Alternatives considered

- More RPC batching or WAL group commit: already available and does not remove
  repeated checkpoint materialization inside one transaction.
- Coalescing separately acknowledged events: changes the durability/visibility
  boundary and is outside this experiment.
- A second event store, owner or projection state machine: duplicates authority
  and replay semantics; the prototype reuses existing Turn helpers instead.
- Enabling the frame on all ordinary events: a one-event frame can rewrite a
  checkpoint unnecessarily and currently has no work-count advantage.
- Claiming a SQLite limitation: SQLite can apply the same fold in one WAL FULL
  transaction and store the final projection once. This is a Harness algorithm,
  not evidence of an exclusive native database capability.

## Verification and work evidence

The profile-on diagnostic uses directly initialized AuthorityDatabase instances,
not an embedded/sidecar RPC benchmark. It compares ordinary IR execution with frame IR execution in
one process and one transaction each, N=1/8/64, using a 256 KiB stable projection
plus small Unicode/escaped text and phase changes. It measures actual engine work
counters and WAL bytes; debug timing is not a performance result.

Actual work measured on the prototype, with all final logical state equal:

| Events in one transaction | Checkpoint bytes, sequential → frame | Historical patches replayed, sequential → frame | Entity gets, sequential → frame | WAL bytes, sequential → frame |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 262,269 → 262,269 | 1 → 1 | 27 → 27 | 7,525 → 7,975 |
| 8 | 2,098,152 → 262,269 | 36 → 1 | 223 → 83 | 30,703 → 25,042 |
| 64 | 16,785,216 → 262,269 | 2,080 → 1 | 3,555 → 531 | 184,388 → 160,221 |

Checkpoint decode, content-hash and collection byte counters agree: N=64 reduces
64 passes over the checkpoint to one. Both arms apply all 64 new patches; the
historical-patch counter measures reconstruction only. The general-patch path
visits a large stable prefix N times; this frame visits it once, plus the actual
incoming changes and N exact event publications in this ordinary-budget case.
The near-ceiling fallback above is not covered by that scaling claim. The fixture's repeated stable
text compresses well, so these WAL figures are not an incompressible-history claim.

The N=1 WAL regression (+450 bytes, 6.0%) is retained. There is no claim of a
latency speedup, default-path improvement, or a SQLite advantage from these work
counts. The current text-tail optimization is separately checked for logical
parity; this work-count fixture also changes phase and therefore exercises the
general projection path.

Focused verification covers per-slot correspondence for N=1/8/64, both inline
and checkpoint projections and text-tail input, non-monotonic final timestamps,
patch and hydrated replay, real close/open, stale-revision rollback, owner refusal,
unsupported boundaries, deadline refusal, invalid typed append, exact Unicode
and escaped-string byte accounting, competing preparation, receipt retry,
outbox capture, every observed write/sync fault with mandatory firing, torn writes,
silently dropped individual syncs, and acknowledged state after real SIGKILL.
The child process has a bounded readiness wait and a kill-and-join drop guard.

Reproduce focused tests and work attribution from the repository root:

```sh
cargo test --manifest-path packages/tofu-db/Cargo.toml --lib execution_frame -- --test-threads=1
TOFU_DB_REQUEST_PROFILE_EVERY=1 cargo test --manifest-path packages/tofu-db/Cargo.toml --lib execution_frame_work_counts -- --ignored --test-threads=1 --nocapture
```

The evidence archive retains compilation/test failures as well as successful
runs; a faulty fixture is not silently relabeled as a storage regression.

The standard full package gate passed 2,051 tests with zero failures and ten
ignored tests, including the native library, process integrations and doc tests.
Subsequent changes are limited to the test-only guidance precondition and its
regression case, the timing fixtures/driver, a stricter fault-test crash image
cut before database destruction, and this document. Production shared helpers
are unchanged from that gate. The final focused tests passed eight tests in both
debug and release, with two diagnostic tests ignored. The final profile-on
work-count diagnostic passed separately with the same counters above.

A separate Linux-only ignored `execution_frame_timing_leg` driver accepts
`TOFU_FRAME_EVENTS=1|8|64`, `TOFU_FRAME_FOLDED=0|1` and
`TOFU_FRAME_FIXTURE=compressible|high_entropy`. It requires a release test
binary with profiling disabled. Each process measures twelve fresh preparations
and Strict commits; fixture creation, exact logical readback and reopen checks
are outside the measured interval. Per-sample wall and process CPU times, WAL
bytes and common response/state digests allow balanced independent-process ABBA
runs. No low-sample P99 is used as stable tail evidence.

The high-entropy control has the same 262,144-byte stable field. For counters
0 through 8,191, SHA-256 hashes `tofu.execution-frame.prefix.v1` followed by the
counter as eight little-endian bytes. Each digest byte selects one of 64 URL-safe
characters with its low six bits. The process emits the exact prefix SHA-256.
This is deterministic high-entropy text, not a claim of incompressible bytes.
The candidate always writes a final checkpoint, even before the existing patch
chain fills; high-entropy N=1/8 can therefore expose an early-rewrite penalty.
No patch-chain redesign is included to remove an inconvenient counterexample.

The timing binary uses opt-level 3, `CARGO_PROFILE_RELEASE_LTO=false` and
`CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16`, identically for both arms. It does not
represent production fat-LTO latency. The first unused fat-LTO test build took
9m11s and completed before the fixture change; its log is retained separately.

## Release timing and counterexamples

The coordinated quiet run on 2026-10-19, 11:03:20.699–11:03:30.571 UTC, completed
24 independent processes and 288 samples without a failed leg. Each fixture and
event count used sequential, frame, frame, sequential order, twelve samples per
process. Both request and recovery profiling were explicitly disabled. The
source and frozen test-binary hashes matched before and after the run; the three
shared production files also matched the successful full-gate snapshot.

The following P50 values pool the two legs of each arm (24 samples). WAL bytes
were identical across all samples of an arm. CPU time uses the process CPU clock;
these are preparation plus one Strict commit measurements, not server, SDK,
concurrent-arrival or production-fat-LTO latency.

| Stable prefix | Events | Wall P50 ms, sequential → frame | CPU P50 ms, sequential → frame | WAL bytes, sequential → frame |
| --- | ---: | ---: | ---: | ---: |
| Repeated text | 1 | 0.795 → 1.090 | 0.748 → 1.042 | 7,525 → 7,975 |
| Repeated text | 8 | 5.073 → 1.773 | 4.982 → 1.710 | 30,703 → 25,042 |
| Repeated text | 64 | 48.897 → 6.439 | 48.580 → 6.322 | 184,388 → 160,221 |
| High entropy | 1 | 1.052 → 1.857 | 1.001 → 1.769 | 7,525 → 204,945 |
| High entropy | 8 | 7.092 → 2.482 | 7.014 → 2.383 | 30,703 → 222,012 |
| High entropy | 64 | 67.438 → 7.148 | 67.033 → 7.020 | 381,361 → 357,194 |

All individual process medians are retained, in their actual ABBA order:

| Stable prefix | Events | Sequential A1 ms | Frame B1 ms | Frame B2 ms | Sequential A2 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Repeated text | 1 | 0.795 | 1.095 | 1.084 | 0.798 |
| Repeated text | 8 | 5.077 | 1.764 | 1.781 | 5.038 |
| Repeated text | 64 | 49.008 | 6.434 | 6.461 | 48.789 |
| High entropy | 1 | 1.049 | 1.847 | 1.861 | 1.060 |
| High entropy | 8 | 7.074 | 2.501 | 2.478 | 7.099 |
| High entropy | 64 | 67.438 | 7.147 | 7.152 | 67.430 |

The work elimination translates into 7.59× and 9.43× lower median elapsed time
at 64 events, and about 2.86× at eight events. However, one event is 37% and 76%
slower. The early checkpoint rewrite grows high-entropy WAL by 27.24× at one
event and 7.23× at eight events, even while the eight-event CPU cost falls.
Those are material counterexamples to enabling this layout by default.
The shared-prefix fold is promising; choosing when and how to publish a new
checkpoint remains a separate storage-layout problem.

Every sample verified exact public state after a real close/open, and all paired
arms matched response/state digests. The separate focused tests compare every IR
response slot and logical revision, preserve original raw/attempt/sync replay,
and check fault recovery and real SIGKILL. No receipt, raw event or durability
boundary was removed to produce these results. This matrix has no SQLite timing
arm: it isolates two execution strategies inside the same native authority.
SQLite can implement the same final-projection fold, so these numbers establish
a Harness optimization opportunity, not an exclusive database advantage.

The local evidence archive is
`data/benchmarks/tofudb-execution-frame-20261019/final/`. It contains the frozen
binary, source patch and hashes, full/focused/failure logs, reproducible runner,
all 24 raw leg outputs, per-sample CPU/wall/WAL values and summaries. The binary
SHA-256 is `a7519beb7ff127053bd7f9b5ea555ab4bd58b9b13c6246e115a96d9c2f5907a8`;
the exact case is
`turn::execution_frame::tests::execution_frame_timing_leg`. Reproduction uses
`run-abba.py --binary execution-frame-test --output <new-directory>` from that
archive, in a coordinated quiet window. The implementation remains `cfg(test)`.
