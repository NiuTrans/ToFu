# Recovery attribution and qualification

Owner: `packages/tofu-db/src/recovery_profile.rs` and `engine.rs`.
Durable state and generation publication remain owned by
[TofuDB storage](STORAGE_TOFUDB.md) and
[rotation handover](TOFUDB_ROTATION_WINDOW.md).

## One open, one completed population

**Problem:** the existing `engine_open` startup timer cannot distinguish WAL
scanning, payload lookup/replay and completion of a sealed predecessor. A slow
restart can therefore be incorrectly attributed to a neighboring source edit.
Copying a FUSE authority to local XFS also changes the measured operation even
when all database bytes match. Request profiling additionally changes embedded
command scheduling and cannot serve as the normal-path startup observer.

**Decision:** build the non-default `recovery-profile` Cargo feature, then set
`TOFU_DB_RECOVERY_PROFILE=1` to enable one instance-owned report per `Engine::open`.
Default builds use a zero-sized inline no-op with no environment read, clock,
state or Drop. They share the same recovery algorithm and observer call sites. The ordinary structured engine logger emits target
`recovery_profile`; its `message` contains a JSON `tofudb.recovery-profile/v1`
object. In diagnostic builds, the flag is read once per open. With the flag off, opens
read no probe clocks,
allocate no probe collection and emit no recovery report. There is no worker,
process-global accumulator, source payload, path, tenant or operation label.

A serving process can open both the primary authority and a native search
projection. Each is a separate `Engine::open` report. Never assume one report
per process or merge their work counters. Bind source reports to `startup_timing` / `authority_open` and deferred search
reports to `background_timing` / `search_projection_open`. Startup
`search_projection_prepare` constructs only the handle. Ambiguous/interleaved
spans or reports outside a known owner do not establish primary attribution.

The fixed phase vocabulary partitions elapsed time, rather than nesting spans:
control and lease setup; selected WAL scan; block catalog; recovery ID selection;
packed lookup; history catalog; payload replay; witness publication; runtime
initialization; sealed-predecessor preparation, encoding and publication; finish.
All phases appear, with `entered` distinguishing skipped work from a measured
zero. Their `elapsedNanos` sum exactly to the report's elapsed interval. These
are elapsed phases, not CPU time or proof that all work was I/O.

The work object records scan record/payload-byte counts, sealed record count,
bounded selected recovery IDs, payload segment count and admitted replay count.
A value not yet observed is null. The input scan can include an unconfirmed tail
that later fails reference admission; it must not be relabeled as committed work.
The replay count is reported only after that loop completes. Existing limits,
reference checks and unconfirmed-tail decisions remain authoritative.

`completed` becomes true only at successful return. Error or panic unwinding
closes the current phase and retains an incomplete report; it never converts an
error into success. Failed-open elapsed time includes cleanup until this owner
is dropped. Report formatting/emission is outside the recorded interval, but
still costs the enclosing startup. Instrumented clocks are diagnostics and are
excluded from default-path performance qualification.

The enabled-feature observer occupies 272 stack bytes on the measured Linux
build; the default-build no-op occupies zero bytes. Its arrays
have fixed size; output allocation happens only for an enabled report. No new
resident cache, thread, file or durable namespace is introduced.

**Alternatives:** subtracting process-lifetime request totals loses exact open
identity; timing copied fixtures on another volume changes the workload; logging
every block adds unbounded output proportional to recovery; treating unfinished
stages as zero hides the location of failure. The single existing open owner
provides the needed interval without any new execution path.

## Replay buffer ownership

Problem: recovery retained every scanned WAL frame while cloning each admitted
frame into the committed transaction list. The temporary overlap added one
entire backlog of requested allocations and copying before open could return.

Decision: after the record's existing payload/reference validation succeeds,
transfer its encoded buffer into the committed transaction. Keep sequence,
hash and file-local offset metadata in the scan result for the unchanged
durable-witness and tail-truncation checks. The preliminary complete WAL scan,
per-open source validation, cancellation boundaries and checkpoint bytes do not
change. No format, cache, worker, quota or weaker acknowledgement is introduced.

Alternatives rejected: skipping validation or trusting a previous open changes
integrity guarantees; a backlog cap refuses accepted work; a second cache keeps
the duplicate ownership. This change removes one redundant copy, not all
backlog-dependent memory: committed encoded bytes, decoded state and staged
carried content retain their existing lifetimes.

`engine_recovery_memory_tests.rs` uses the existing `allocator-diagnostics`
feature in a fresh child process. It recovers 256 exact 128 KiB transactions,
verifies every payload and its logged envelope, and rejects an extra full
backlog of peak requested bytes. Seeding and concurrent suite workers cannot
contaminate that child's counters; no allocator counter is reset. The bounded
parent owns kill/reap. These are requested Rust allocation bytes, not RSS or
an application latency promise. Ordinary generation, corruption, missing-source,
unconfirmed-tail and cancellation tests retain the recovery safety boundary.

The 33,568,000-byte encoded fixture measured 100,779,716 peak requested bytes
before transfer and 67,211,716 afterwards: exactly one encoded backlog removed
(33.31% of that peak). Retained requested bytes after open stayed 67,185,092;
all 256 transactions and logged envelopes matched. The default-feature prefix
test also checks both missing-reference outcomes after valid records: an
unconfirmed suffix alone is truncated and the prefix survives checkpoint/reopen;
a durable bad reference refuses open with CONTROL and WAL evidence unchanged.

## Shared VFS range reads

**Problem:** Unix `RealFile::read_exact_at` uses a seek followed by an exact read.
A fixed terminal image exposed thousands of such pairs. Removing a syscall is a
mechanism observation, not an end-to-end performance result.

**Decision:** retain the existing VFS implementation. The positioned-read
alternative removed 6,498 seek/read pairs from one exact recovery image, but
recovery and pipeline observations did not establish a general, qualified gain.
Keep the prototype, adverse observations, source/artifact identities and all
corrected comparison scopes in the external run evidence. Do not promote it
from syscall counts alone or weaken fault-injection and error behavior.

**Alternatives:** adding a read cache adds ownership/invalidation work without
first establishing the bottleneck; bypassing VFS loses its fault/encryption
boundary. A runtime optional observer still carries state and branches in an
ordinary build. The non-default diagnostic feature keeps attribution available
while compiling that observer state and environment/clock work out by default.

Verify `recovery_profile` and `tests/startup_integrity.rs` in both default and
`recovery-profile` feature builds. The former must ignore a probe environment
flag and preserve exact recovery; the latter checks exact sequential accounting,
actual replay counts, and incomplete failed-open reports without changing the
CONTROL/WAL evidence. Full native fault/recovery suites cover both builds.

## Repeated carried content

Problem: the per-open carried-payload lookup checked history before consulting
its existing verified bit. Historical hits also returned without setting that
bit. Repeated references therefore reopened routing pages and source files,
even after their exact content had already been verified during the same
exclusive recovery. A cold-cache deterministic regression records 512 reads
for 128 repeated references to one published historical payload.

Decision: consult the existing content-addressed proof first, and record
successful history and conservative-fallback verification in that same map.
Each supplied payload is still hashed; a fresh lookup must verify its source
again. Missing or damaged copies retain WAL repair, other I/O errors propagate,
and identifiers outside the bounded map retain conservative resolution. No
cross-open cache, new storage format, background worker or retention is added.
The preliminary ID selection uses the existing strict non-copying envelope
decoder; actual replay continues validating all carried bytes and references.

Alternatives: a second persistent proof introduces generation/integrity
ownership; skipping first-use source verification weakens deduplication and
corruption handling; suppressing I/O errors changes recovery semantics. Reusing
the already-owned proof removes repeated work without those changes.

`repeated_carried_history_recovery_verifies_each_content_once` checks actual
VFS reads, per-pass proof lifetime, nonrepairable errors and crash readback.
`repeated_carried_recovery_paired_measurement` is an explicit ignored test:
set `TOFUDB_RESTART_BENCH_PARENT` to the measured volume and run it with
`--release --lib -- --ignored --nocapture`. Its eight ABBA legs use the same
default-feature binary and disposable authority, compare the conservative
reader with per-open reuse, and verify bytes, sequence and segment count after
each arm. This is a repeated-content mechanism experiment, not a complete
production startup or filesystem cold-cache measurement.

On the original DolphinFS/FUSE volume, the eight-leg default release run
measured reference `[108200, 105751, 105680, 121137]` microseconds versus
reuse `[1653, 1688, 1696, 1668]` for 128 references (medians 106,975.5 and
1,678 microseconds). All exact byte, sequence and segment-count checks passed.
The deterministic regression changes repeated source reads from 512 to zero
after first verification; a fresh pass still reaches an injected PermissionDenied.
These measurements do not attribute the previously observed 111.267-second
production authority open or establish its remaining duration.

## Fixed-image comparison boundary

A recovery fixture is copied only after SIGKILL has been joined. Its exact file
hashes, source filesystem/device, original binary and acknowledged readback
population are retained. The copied authority is immutable evidence; each arm
opens a separate clone. Copying and hashing precede the clock and warm file data,
so these observations are not cold-cache results. Readiness, exact readback,
offline integrity and teardown are separate measurements.

A valid matched comparison fixes the fixture hashes, target filesystem/device,
durability, features, probe selection and readback population. Original-volume
FUSE runs and local-XFS controls are distinct cohorts. All failed/adverse arms
remain retained. A successfully opened database without exact readback is not a
qualified result. Standalone raw SQLite reopen is not an equivalent substitute
for a native crash-recovery fixture with different admitted work.

Production source fingerprints exclude integration-test files. Freeze the
integration-test/fixture inputs separately when certifying the candidate; retain
both witnesses and refuse changes during native commands. Pipeline and adjacent
raw-SQLite workloads remain required before inferring broader performance.

## Build and startup boundaries

A source digest identifies inputs; it is not an executable identity. Pin the
executable and embedded library hashes, enabled features, compiler, release
profile, code-generation concurrency and build context. Rebuild the unchanged
reference in the candidate's build context before source-level attribution.
Equal source digests with differing executable code sections are an explicit
comparison variable. Preserve earlier cross-artifact measurements, but do not
silently relabel them as an isolated source-change experiment. Non-allocatable
symbol/build-ID differences alone do not prove different executing code.

The benchmark adapter's whole `_launch_serve` interval includes Python client
imports, schema validation and client setup after reading the daemon's readiness
pipe. Keep three distinct observations: launch-to-complete-readiness-line;
readiness-line-to-client-ready; and the complete launch-to-client-ready interval.
The existing native `startup_timing` `engine_open` interval covers a narrower
boundary. Authority open and its engine-open child overlap; never sum them.
Daemon readiness also includes projection, resources and listener setup.
Neither a flat engine-open clock nor a faster full client launch establishes
that every startup component is unchanged. CPU snapshots are a separate measure.

A fixed-image report retains every raw observation and its exact readback before
ratios. A few repeated opens describe those observations, not a population P99.
Failed performance SLOs remain failed: the pipeline CLI's exit 1 can accompany
correct and latency-eligible measurements. Validate the report's correctness,
eligibility and expected CLI status separately; an SLO failure must neither be
hidden nor mislabeled as an infrastructure failure that is retried away.
