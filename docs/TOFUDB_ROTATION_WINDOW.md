# Rotation window preparation and attribution

Owner: `packages/tofu-db/src/{engine,daemon,fsync_stats}.rs`.
Durability and certification authority remains [TofuDB storage](STORAGE_TOFUDB.md).
The existing driver and authority mutex own the rotation lifecycle; no new
worker, queue, durability mode or configurable resource cap is introduced.
Online generation handover uses an explicit version-3 CONTROL descriptor;
WAL framing and transaction codecs remain unchanged.

## Problem

The old driver encoded the covered prefix outside authority, then encoded and
persisted every transaction committed during preparation while holding
authority. A matched-source 1,000-conversation diagnostic run captured
173,890,324 history-tail bytes across eight installs. `ck_segments` attributed
2,394.3 ms to the rotation driver, versus 6.0 ms for structural CONTROL.
These are cumulative, nested elapsed spans, not CPU time or additive wall time.

Moving the prefix also removed it from the old active read buffer. A negative
control on the original engine demonstrates `transaction_at(1)` returning
transaction 3's inline payload after two covered commits and one window commit.
Performance work must preserve complete, exact reads during preparation.

## Default delivery: durable predecessor/successor handover

Decision: seal the complete active WAL before capturing the covered prefix.
First synchronize and publish its exact tip, durably create an empty successor,
then publish CONTROL with the unchanged history checkpoint and a descriptor
binding the predecessor generation, terminal sequence and terminal hash.
Only after that publication may writes enter the successor. No predecessor
transaction is copied to the successor WAL. The existing driver encodes the
immutable predecessor outside authority while successor writes remain admitted.

Installation publishes history only through the sealed terminal sequence,
removes the descriptor and retires that predecessor. It preserves the successor
WAL, head/root, transaction buffer and unstaged byte accounting. An already
prepared CONTROL publisher delays predecessor installation without fencing
successor appends. Its completion reconciles the newer durable sequence and
root before the retried predecessor installation publishes current CONTROL;
predecessor encoding itself never raises a write fence. The successor's hard WAL
ceiling still applies. A second handover is forbidden until the first prefix
installation and previous retirement are complete.

Exact reads use shared immutable covered batches until catalog installation
succeeds. Point lookup checks exact sequence; suffix visits retain gap checks.
Snapshots, GC and backup decisions include the captured prefix. Manual engine
checkpoints retain their legacy complete-prefix behavior and defer while an
online rotation owns the prefix. No new thread, queue or duplicate payload
allocation is introduced.

Recovery scans the predecessor read-only from the history checkpoint and
requires its exact terminal sequence/hash with no extra or torn bytes. It scans
the successor from that terminal witness, then replays both in sequence.
Unconfirmed-tail truncation uses successor-relative offsets, including zero
when its first record is rejected. Reopen finishes the recorded predecessor
before exposing the authority, leaving recovered successor transactions active.
A missing/corrupt acknowledged prefix fails closed without truncating evidence.
A nonempty unreferenced successor also fails closed: selecting an older CONTROL
slot must never silently hide acknowledged writes after a damaged handover slot.
Empty orphan WALs are reused only after inode and directory barriers.

CONTROL writes version 3 when the descriptor is present, so an older reader
cannot silently ignore predecessor ownership. Descriptor-free publications use
version 2; readers accept versions 1/2/3. An older binary can still reject a
remaining version-3 alternate slot after completion. Direct binary downgrade
is unsupported; use compatible export/restore.

Physical residency can include two WALs, each subject to the existing active
WAL byte ceiling. With the current 64 MiB ceiling their combined live upper
bound is 128 MiB; it is not a new per-user capacity setting. Immutable history
and retired files have their existing explicit lifecycles. The prefix and
successor share bounded transaction ownership with readers; a slower encoder
can still cause successor-ceiling backpressure. Resource and latency comparisons
must measure this tradeoff, not assume that off-lock encoding eliminates work.

## Protocol verification and measured acceptance

The generation fault matrix targets every observed operation by kind and
occurrence, asserts the injection fired, and verifies all acknowledged carried
payloads after abrupt simulated death. Separate tests cover two-generation
recovery before/after encoding, successive rotations, exact reads while real
predecessor fsync is paused, torn/unconfirmed successor offsets, corrupt durable
predecessors, damaged CONTROL fallback, empty orphan reuse, Normal barriers and
encrypted handover. These checks establish protocol boundaries, not all SQLite
performance metrics.

Current generation measurements and failed attempts are retained in
[generation evidence](evidence/tofudb_generational_checkpoint_20261002.json).
The comparisons use the frozen prior default with both CLI/library source
`d47361a362622c77df217227d0ab1a7a72b07dfe7e68cf0c89b7a1cf54983147`.

The isolated handover source `e1c08b79…` (commit `2b054c170`) changes only
the predecessor/successor protocol relative to that frozen baseline. It excludes
concurrent pagination changes and later orphan/barrier regression fixes.
The matched default-off ABBA stream P99 is 5,264.8 / 4,739.0 ms for the
previous default and 2,231.5 / 2,313.2 ms for generation handover. Settle P99 is
6,234.4 / 7,683.9 ms versus 5,988.6 / 4,971.0 ms. Peak process RSS is
1.889 / 1.952 GB versus 1.807 / 1.921 GB (decimal GB). All four legs recover
exactly 1,000 conversations with zero errors; all still fail the absolute
latency acceptance. Two repetitions do not certify production-tail distributions.

A second ABBA verifies the barrier/crash fixes and preserved pagination inputs
(source `8175a191…`): baseline stream P99 5,467.5 / 5,299.3 ms versus
2,511.4 / 2,354.1 ms, settle P99 6,984.8 / 7,029.9 ms versus
5,296.9 / 6,726.0 ms. All four runs recover exactly 1,000 conversations with
zero errors, but still fail absolute acceptance. SQLite sidecar at the identical
1,000-conversation load reports 641 capacity/timeout/unavailable errors and only
362 exact recovered conversations. Its timings are excluded from a fair latency
comparison: this failed application-sidecar run does not measure raw SQLite
engine superiority or justify an all-metric win. The complete error outcome is
retained, including the runner's nonzero correctness exit.

A sampled candidate run performs 12 prefix encodes outside authority
(6,518.5 ms inclusive). Twelve durable handovers take 62.7 ms cumulatively,
including 3.7 ms on foreground and 59.0 ms on the rotation driver. Twelve
predecessor installs take 206.0 ms inclusive, including 8.9 ms structural CONTROL.
Residual in-lock history-tail bytes and captured legacy-window bytes are both
zero because successor transactions remain in their WAL. These spans overlap;
they are not CPU time, disjoint lock occupancy or additive foreground latency.
The 750 sampled requests still spend 98.45% of inclusive time in classified
waiting, so this change does not establish that the broader queue bottleneck is
solved. Profile retention remains bounded (166,272 retained bytes; zero drops).

The frozen combined source `8175a191…` passes 1,491 native checks (two
ignored); the debug CLI, release CLI and embedded library identify that exact
source. It includes three independently preserved pagination inputs. This is
a frozen result in a checkout with concurrent writers. A controlled child
process is killed with SIGKILL after a strict successor ACK while real
predecessor fsync is paused; canonical CONTROL after the kill still owns sealed
generation 1 and active generation 2 with checkpoint 0. Recovery verifies both
carried payloads, installs prefix 1, and accepts a new commit/checkpoint/reopen.
This test passes on local storage and the DolphinFS volume used for measurement.

After preserving five further concurrent scoped event-guard source inputs,
source `04f83450…` passes 1,493 native checks (two ignored) and matches the
debug CLI, release CLI and embedded library. Those input files are not part of
the owned integration diff. Shared-checkout integration must preserve them.

Causal-source sequential and 32-session wire checks each recover 10,002 exact
payloads after two actual SIGKILLs. Their canonical after-kill CONTROL witnesses have no sealed
predecessor, so those wire runs do not certify the live two-generation boundary.
The controlled child test supplies that specific boundary evidence; neither
two kills nor simulated faults certify every possible crash schedule.

Default-off micro ABBA compares 2,000 small point reads, 2,000 header opens
and 5,000 append calls per engine. Candidate point-read mean is 41–42 μs
versus raw SQLite 6 μs; header-open mean is 58–63 μs versus 18–19 μs.
Native append mean is 0.682 / 0.687 ms versus raw SQLite 5.661 / 5.905 ms
and SQLite sidecar 18.180 / 14.863 ms. All four helper legs exit zero. These
are finite per-operation latency comparisons on this volume, not concurrent
throughput or universal correctness certification. The combined candidate
includes unrelated event-guard inputs; protocol-only attribution remains the
separate causal capacity ABBA. Small-read overhead is an explicit remaining
root target, not a defeated SQLite metric hidden by fast writes.

A separate final-source point diagnostic samples every 16 requests. For 126
sampled `record.get` calls, mean native queue time is 5.87 μs and dispatch
11.23 μs; inclusive authority hold is 0.89 μs and query execution 7.82 μs.
The 2,000 client point calls average 42 μs. There are zero dropped samples or
invalid spans. Sampled-native and full-client populations differ, so their
means cannot prove an exact subtractive residual. Fresh writes mostly hit the
overlay; materialized and cold records still require separate measurement.
This evidence prioritizes invocation/serialization/dispatch cost over assuming
that small-point overhead is slow disk or long authority-held I/O.

The final sequential and 32-session wire suites each recover 10,002 exact
payloads after two real SIGKILLs on source `04f83450…`. One final group kill
records sealed generation 16, active generation 17, checkpoint 9,672 and sealed
terminal 10,270 before recovery. It witnesses a live handover, but the CONTROL
observer alone does not assign each ACK to a generation. The deliberately paused
child test supplies exact successor-ACK-before-kill proof. The read-only observer
was compiled from causal source `e1c08b79…` against the same canonical CONTROL
codec; its source identity is retained separately from the killed engine.

## Defeated single-window experiment

This version is also rejected as a default: matched stream and settle tails
regress despite lower peak RSS.

Capture exactly one window after prefix preparation. Reserve the existing
publication lane and fence foreground writes before moving this window to the
driver. Persist its history segments, payload indexes and routing root outside
authority, then reacquire authority to install. This keeps the original
installation work set and does not permit additional writes to expand it while
window I/O runs. Readers retain the prefix and window through shared immutable
batches. Admission can pause for the complete captured window; the existing
WAL ceiling bounds it. This is not a claim of wait-free writes.

## Defeated rolling-window experiment

This candidate is retained on an isolated branch and is not admitted as the
default: both default-off candidate repetitions worsen settle P99 and peak RSS.
The root cause of these regressions is not yet separately established.

After prefix encoding, capture the current window under authority and persist
its immutable history segments, payload indexes and routing root outside
authority. A capture at least as large as `HISTORY_SEGMENT_MAX_BYTES` leaves
foreground commits admitted on the original WAL. Capture and encode subsequent
windows with the same driver until a smaller final window reserves the existing
CONTROL publication lane and fences writes. Install under authority once all
captured bytes are durable. An empty window goes directly to installation.

The threshold is the existing history codec chunk bound. Each unsealed batch
contains at least one chunk, and all commits still obey the existing complete
generation WAL ceiling. Thus continuous writers cannot cause unlimited
catch-up iterations or retained transaction bytes. Writer fencing on the last
small batch still creates a bounded admission pause; off-lock preparation is
not a claim of wait-free writes.

Captured transactions have one backing allocation, shared with exact readers
through `Arc<Vec<CommittedTransaction>>`. Point lookup checks the requested
sequence inside each batch. Suffix traversal starts by offset in each batch,
and validates every visited sequence; it does not scan the entire active prefix
for a short suffix. Snapshots and GC marking include every captured batch.
Backup checkpoint decisions also see captured active transactions. The legacy
raw `committed_transactions()` slice remains the uncaptured buffer.

On encoding failure, restore captured batches in order and fail closed. On a
terminal installation error, reject further reads/writes and release reader
pins; recovery uses durable CONTROL, history and WAL. A transient publication
conflict retains the exact encoded snapshot in the existing deferred slot.
Sealed work releases its publication reservation only under authority immediately
before installation. Existing framing, hashes, double-sync barriers, rollback,
retirement, background drain and notification semantics remain mandatory.

## Performance anchors

Fine elapsed timers use the existing opt-in diagnostic path. Default-off
benchmarks determine product performance; sampled runs explain placement.

| Stage | Boundary |
| --- | --- |
| `rotation_prepare_wait` | Wait to acquire authority for taking work |
| `rotation_prefix_encode` | Fresh prefix preparation, including successor WAL prestaging |
| `rotation_install_wait` | Each catch-up/install authority acquisition |
| `rotation_window_capture` | Byte accounting and immutable ownership capture under authority |
| `rotation_window_encode` | Legacy/manual window persistence under authority |
| `rotation_install_dirty_control` | Flush the original generation's dirty CONTROL |
| `rotation_install_window` | Remaining legacy/offline window persistence under authority |
| `rotation_install_manifest` | History manifest creation |
| `rotation_install_wal` | Legacy complete-prefix successor initialization/open |
| `rotation_install_control` | Structural CONTROL publication |
| `rotation_install_release` | State/catalog handoff, pending release, retirement and wake |
| `rotation_generation_handover` | Durable tip flush, empty successor creation and sealed CONTROL publication |
| `rotation_predecessor_install` | Sealed-prefix manifest/publication and release; successor remains active |

`ck_segments` still measures persistence in both paths. Its total must not be
interpreted as lock occupancy after off-lock windows are introduced. The report
classifies the two acquisition stages as waiting, not engine execution.

Three fixed counters record attempted captured work, including later failures:
`rotationWindowTransactions`, `rotationWindowBytes`, `maxRotationWindowBytes`.
`checkpointHistoryTailBytes` continues to record the residual in-lock tail.
A zero residual means work moved, not that persistence disappeared. New static
stage atomics occupy 1,664 bytes across global/seven component arrays; the three
new counters occupy 24 bytes. Existing request retention/depth/byte bounds apply
to the additional stage slots; no payload contents or unbounded samples are kept.

## Alternatives and retained failures

- Keeping all window persistence under authority preserves the old long
  critical section; the matched diagnostic identifies its cost above.
- Fencing writers before persisting an arbitrary large window would move I/O
  outside the mutex while preserving a large writer admission pause. This is the subsequent single-window candidate; measurements below distinguish
  it from the rejected rolling version.
- Unlimited live-writer catch-up would permit starvation. The chunk threshold
  and existing WAL ceiling give a finite ownership sequence.
- Copying captured payloads for readers would duplicate the original generation
  in memory. Shared immutable batches retain one copy.
- The first fault matrix found retained reader pins after terminal installation
  failure. The corrected failure boundary releases them; the failed run remains
  evidence, not a successful certification cell.
- A shared-target build attempt ran the old negative-control test program.
  Its log is quarantined. Fresh source mtimes and actual CLI/library source
  identity are required before measurements count as candidate evidence.

Generation implementation failures are also retained in the evidence:

- The first real-sync test intercepted `.blk` names, but persistence fsyncs
  `.new-*` temporary files. It never entered the intended pause. Corrected
  targeting verifies actual paused predecessor fsync and successful successor
  commit/read; the timeout is excluded from performance/correctness evidence.
- Removing the existing concurrent CONTROL-publication fence broke admission,
  overlay materialization, deadline and disconnect tests. The fence is retained
  for that publication conflict; predecessor encoding remains unfenced.
- Legacy test expectations included successor transactions in the predecessor
  checkpoint and assumed zero prepare I/O. Updated assertions require exact
  predecessor coverage, retained successor bytes and no foreground payload
  materialization. Durable handover metadata barriers are explicitly measured.
- A malformed-envelope test initially demanded truncation that the old parser
  intentionally rejects. Recovery still rejects malformed authenticated
  envelopes. The offset regression instead uses a valid unconfirmed envelope
  with a missing reference, exercising the existing safe truncation boundary.
- A full-suite packer race transiently held the retirement mutex and returned
  `Busy`. The concurrent fixture now retries that classified result within a
  two-second bound; production admission and backpressure remain unchanged.
- A damaged handover CONTROL slot can leave a valid older anchor and a nonempty
  unreferenced successor. Removing the guard on negative-control branch
  `perf/tofudb-generational-fallback-control-20261002` (`6c3c8ea64`) silently
  reopened head 1 despite successor ACK 2. Open refuses that ambiguous authority
  before replay/truncation; it never presents a head omitting confirmed bytes.
- The same negative control found a legacy checkpoint publishing an unsynced
  empty orphan WAL. Simulated crash removed it and recovery failed despite the
  checkpoint ACK. All three empty-orphan reuse paths now repeat inode and
  directory barriers; a positive regression verifies recovery.
- An artifact-attestation helper incorrectly treated the buffer-writing source
  fingerprint FFI as a zero-argument string-returning function and crashed.
  Correcting its signature to `(out, capacity) -> length` verifies 64 output
  bytes against both CLI and source. This helper failure is excluded from
  engine correctness results.

## Verification and limits

Canonical measurements and failures are retained in
[rotation evidence](evidence/tofudb_rotation_window_20261002.json).
The negative control lives on `perf/tofudb-rotation-read-control-20261002` at
`bae946d00`; it changes only the regression test, not the original engine.
The rejected-candidate I/O pause test blocks a real sync in window persistence, admits a foreground
commit and exact prefix/window reads, then tests the sealed final window and
simulated crash. The fault matrix targets every observed operation by kind and
occurrence, asserts the fault fired, and checks both acknowledged transactions
and carried payload after abrupt simulated death.

Matched default-off ABBA settle P99: baseline 7,206.6 / 7,014.5 ms; candidate
7,604.6 / 8,101.5 ms. Peak process RSS: baseline 1.965 / 1.920 GB; candidate
2.063 / 2.271 GB. All five capacity runs recover exactly 1,000 conversations
with zero errors, but fail their latency acceptance. Sampled candidate work:
227,577,488 captured bytes, 14 off-lock encodes (3,395.2 ms), zero residual
in-lock tail. This validates movement of work, not a product performance win.
Sequential and 32-session wire checks each survive two actual SIGKILLs with
10,002 exact payloads recovered. Failed micro helper imports produce no result
and their runner's false success is excluded.

Finite correctness and benchmark checks cannot establish every SQLite metric,
production concurrency tail, crash boundary, or recovery distribution. Remaining
work follows the [performance charter](STORAGE_PERFORMANCE_CHARTER.md).

Single-window default-off ABBA stream P99: baseline 5,332.2 / 4,698.8 ms;
candidate 5,657.4 / 4,853.1 ms. Settle P99: baseline 6,771.2 / 4,925.6 ms;
candidate 7,200.1 / 6,722.5 ms. RSS: baseline 1.964 / 1.752 GB; candidate
1.891 / 1.623 GB. Sampled off-lock window persistence is 2,468.2 ms across
eight captures (172,971,149 bytes), with zero residual in-lock history tail.
Writer fencing remains; cumulative park spans are concurrent waits, not wall
time. These measurements establish a tradeoff and cannot justify the default.

## Root obligation exposed by the rejected window experiments

Removing authority lock occupancy did not remove the admission fence, and both
controlled window candidates regressed overall latency. The preceding default
required the complete durable old-WAL prefix to be checkpointed before switching.
The root solution gives commits a recoverable successor generation while the
sealed predecessor is checkpointed, with explicit, bounded ownership of every
old-WAL byte. The default generation handover above implements this ownership
with cross-generation crash, read, backup and GC checks. The failed window-moving
experiments did not. Extra WAL copies, unreferenced retained files,
or weaker fsync/CONTROL rules are not substitutes for that ownership.

## Prior default verification (source d47361a3)

Combined source `d47361a3…` matches the tested CLI and embedded library: 1,474
native checks pass, two are ignored. The profile analyzer has 25 passing cases;
58 existing performance-harness checks and its new schema rejection check pass.
The shared checkout has concurrent writers; this is a frozen source result.

The sampled default run captures 183,990,422 window bytes; the residual in-lock
counter agrees exactly. Eight window installs take 2,672.6 ms cumulatively;
structural CONTROL takes 6.8 ms across nine installs. New encode/install spans
are nested and cannot be added. This exposes the remaining work rather than
claiming it has vanished.

All five combined capacity runs recover exactly 1,000 conversations with zero
errors; none passes the latency gate. The default-off append microbench retains
one slow candidate repetition (mean 1.508 ms, P99 10.004 ms) and the faster repeat
(mean 0.807 ms, P99 3.116 ms). Concurrent read-guard/catalog changes are included
in this source, so these comparisons do not isolate the read-lifetime fix or
probes. There is no stable overall latency or SQLite all-metric claim. SQLite
and raw-SQLite microbenchmarks retain their own footprints and timings.

Two initial wire runs used an old SDK schema (62) against native schema 63,
closing during Hello before the crash workload began. They are excluded and
retained as harness failures. Copying the matching generated contract resolves
the mismatch. Sequential and 32-session checks then each survive two real
SIGKILLs and recover 10,002 exact payloads; two kills cannot certify recovery P99.
The maintained benchmark now checks advertised `schemaId` before opening a
session, with an executable rejection test. Verification requires matching
native source, CLI/library binaries, SDK contract and protocol schema.

## Deferred catalog publication and successor admission

Problem: predecessor installation waiting for an unlocked catalog publication
raised the legacy write fence. Slow publication I/O then rejected every Turn
event while the authority mutex and health probes remained responsive.
Decision: park the encoded predecessor without fencing successor commits.
The existing catalog completion rebase preserves their sequence and authority
root; installation uses that current state. The successor WAL hard ceiling,
publication-error restart fence and exact parked snapshot remain unchanged.
Alternative rejected: extending Flow retries or RPC deadlines preserves the
unnecessary serialization and postpones the same terminal failure. The legacy
single-WAL install keeps its fence because it retires the window WAL.

Verification: the deferred-rotation test admits a successor while publication
owns CONTROL. The generation regression crashes before/after catalog publication,
after its in-memory reconciliation, and after predecessor installation, under
Strict and Normal durability, and verifies both acknowledged carried payloads
and the exact successor transaction and authority root (eight crash cases).
A negative control restoring the old fence fails the same admission regression.
The fixed eight-case regression completed in 0.01 seconds on this host; that is
fixture runtime, not production latency. No workers, queues or capacity are added.
