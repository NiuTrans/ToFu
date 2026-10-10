# Native session lifecycle

Owner: `lib/storage/v2_client.py` owns application reservations and retry policy;
`tofu_db/wire.py` owns an authenticated socket's framing and idle liveness.
The selected authority, wire schemas and receipt rules remain in
[Storage](STORAGE.md). Sessions are reconstructible transport state.

Problem: the native client previously admitted only the advertised pool size,
queued excess callers in FIFO order, and immediately refused long requests
beyond a quarter-pool quota. Healthy independent work consequently consumed
its attempt deadline waiting for local slots or returned `database_busy`.

Decision: every caller immediately claims an exclusive idle session or a new
creation reservation. The resource-derived pool size controls idle retention;
active sessions have no count quota and long deadlines have no separate lane.
Probes, handshakes and teardown run outside the lock. Close and attempt deadline
are checked before lending the validated socket, with typed pre-dispatch errors.
Broken sessions retire without affecting unrelated callers. Excess healthy
sessions close on return, so bursts do not leave a permanently enlarged cache.

Live, idle and retiring sessions remain observable. A slow close retains its
accounting until teardown completes but cannot stop another caller creating a
socket. Resize closes only surplus idle sockets; active calls retain ownership.
`resource_snapshot.capacity` is the idle reuse target for existing resource
controls; `active_capacity=0` denotes no active count quota and `waiting=0`
denotes no local admission queue. No authority or replay rule changes.

The native listener likewise accepts loopback connections without an active
count quota. Its legacy `maximumConnections` readiness value sizes initial
worker-registry allocation and application idle reuse; the canonical listener
contract explicitly records that role. Each socket owns one demand-created
thread with its probed stack size. Parent EOF stops accepts, interrupts reads
and joins every worker before authority teardown. Authentication, owner scope,
frame-byte limits and semantic deadlines remain. Embedded jobs use the owned-input policy below; other engine reader and
transaction budgets remain separate audit work.

Alternatives considered: a larger quota still refuses bursts; a separate long
lane refuses valid work; a FIFO queue spends request deadlines on local count
policy. Demand-created exclusive sockets preserve isolation without those gates.

An idle peer cannot send unsolicited data: readability, EOF or socket error all
mean the session is unsuitable. Liveness therefore uses a zero-time `poll`
(`select` where poll is unavailable), receiving no bytes and changing no socket
mode or timeout. Poll avoids the descriptor-number ceiling of Unix select.
There is still an unavoidable close-after-probe race; receipt-aware request
handling remains the authority for ambiguous outcomes.

Verification: demand bursts, mixed long/short requests, slow probe/create/close,
close during probe/handshake, post-probe deadlines, BaseException cleanup and
idle shrink in `tests/storage/test_storage_v2_{client,deadlines,pool_handoff}.py`;
real socket EOF/pending-byte checks in `tests/tofudb/test_tofudb_v2_client.py`.

Lifecycle measurement: 64 independent Unix socket-pair sessions entered in
0.51 ms with idle target 1 and no admission wait. Holding them added 128 file
descriptors; return retained 2 descriptors (one pair), and close returned to
the baseline. Sampled RSS growth rounded to zero at process-page resolution;
this does not certify zero allocation or native daemon throughput. The mixed
64-thread barrier test separately proves simultaneous ownership and exactly-once
surplus close. Listener acceptance is verified separately with 64 authenticated TCP sessions
held live at an initial reservation hint of 1; every semantic query succeeds
and parent shutdown joins every connection. On the measured local/FUSE setup,
all 64 authentication/query exchanges completed in 267.94 ms; parent-stop and
join completed at 270.52 ms from start. This is acceptance/lifecycle evidence,
not a parallel-write throughput claim. Empty worker registries release burst
capacity back to their initial allocation hint.

Measurements use the same native source/artifact and FUSE volume, 100 streamed
8 KiB frames per conversation, default 3 s structural cadence, immediate
producer persistence and exact public patch folding before/after SIGKILL.
12-leg ABBA probes cover 1/4/16 conversations. Medians improve at 16-way, but
query P99 increases; this is not a universal tail-latency certification. Native
serialization/settlement and client scheduling remain measured concerns.
Complete workload scopes, preliminary variants, pins and test outcomes are in
[session-pool evidence](evidence/tofudb_session_pool_20261001.json).

Frame accounting — Problem: a 64-frame aggregate count gate closed valid tiny
requests despite available byte budget. Decision: the native frame budget
tracks checked serialized bytes; counts and peaks remain observation.
Source callers migrate `FrameAdmissionBudget::new(count, bytes)` to
`FrameAdmissionBudget::new(bytes)`; wire encoding and protocol version remain
unchanged. The canonical frame contract
records `activeFrameCountAdmission=none`; no count constant is generated.
Malformed/oversize frames, checksum/authentication/owner validation, per-frame
validation, deadlines, and guard-owned release remain. Raw buffer pressure uses
the representation policy below; embedded inputs use the ownership policy below.
Alternatives: increasing the count retains arbitrary refusal; disabling byte
accounting would lose actual buffer ownership and allocation evidence.
Measurement: 128 real authenticated TCP requests held incomplete frames using
11,648 serialized bytes while a separate connection authenticated and queried.
All requests settled and joined in 516.91 ms; frame count and bytes returned to
zero. This validates acceptance and ownership, not authority throughput.
A separate guard test holds 1,024 64-byte reservations and releases all 65,536
bytes. Write-frame reservations use
the codec's exact encoded-body preflight before allocation; the old maximum-body
contract description has been corrected to match the retained implementation.

Python buffer ownership — Problem: weighted byte slots queued or rejected valid
responses, including durable command acknowledgements; SQL replies reserved a
64 MiB maximum even when tiny. Decision: receiving v1/v2 bodies claim RAM up to
the process threshold and otherwise use private anonymous files with bounded
read chunks and read-only mappings. Frame validation, CRC, decoded-value
ownership, request deadlines and receipt rules remain. Each lease releases
once; files/mappings close on decoding, checksum, short-read and I/O failures.
Already-encoded SQL replies charge their exact serialized body without waiting.
The standalone client wheel includes the stdlib buffer helper. Decoded user
values are still owned heap data; raw buffers and embedded input ownership
are distinct from those decoded allocations.
Alternatives: unlimited raw RAM abandons buffer control; raising a quota still
rejects bursts; waiting for a slot spends deadlines after a command committed.
Measurement: eight 4 MiB bodies at a 4 MiB RAM threshold claimed 4 MiB RAM
and 28 MiB anonymous-file buffers, with 14 extra descriptors while mapped.
Completion in 30.46 ms returned descriptors and byte accounting to baseline.
This measures real overflow-file ownership, not decoded-object RSS or native
storage throughput. Fixed read strides use at most 1 MiB per transfer; decoded
user values remain outside this serialized-buffer threshold.

Native raw buffers — Problem: aggregate serialized-byte admission rejected
valid frames and streamed requests below their operation bounds. Decision:
serving reads/writes choose heap or private anonymous mapped files under one
RAM threshold. The threshold scales from launch headroom without a fixed
128 MiB ceiling; each owner records RAM/file/payload bytes until actual drop.
Decoded messages and streamed request payloads remain independently owned
heap values with exact accounting, not raw-frame admission. Actual allocation
or temporary-file I/O errors remain real errors; no duplicate dispatch occurs.

Secret-class and unclassified bodies are cleared on drop; the file encoder
clears its private write buffer without retrying a failed write. Scratch-file cleanup
also covers incomplete reads/encodes and mapping failure. Private files are
never cloned/exposed/truncated in production and outlive their mapping, matching
[memmap2 safety](https://docs.rs/memmap2/0.9.9/memmap2/struct.MmapOptions.html#safety).
Canonical fields replace the old aggregate maximum with a default RAM hint and
headroom divisor; wire encoding/version and per-frame validation remain.
Compatibility guard-only APIs keep owned heap accounting; serving uses explicit
representation plans. Alternatives: larger quotas still refuse bursts,
unbounded extra raw heap weakens buffer control, and retry after publication
risks duplicate effects. CRC, short I/O, secret cleanup and guard release have
focused regressions. Real TCP evidence: with an 8 MiB threshold held by an
independent owner, all 64 authenticated exchanges used overflow buffers and
completed in 334.28 ms; connections joined by 336.78 ms, releasing every file
claim while retaining the original owner. This proves acceptance and cleanup,
not throughput or a bound on decoded-user-data RSS.


Embedded inputs — Problem: an eight-times-envelope estimate rejected valid
FFI calls before decoding whenever a launch-derived byte quota was saturated;
it neither allocated RAM nor measured actual parser ownership. Decision:
remove estimated-byte admission and account the copied encoded payload's
actual vector capacity. Copy allocation uses `try_reserve_exact`; a real
allocation error or an unstarted worker failure remains a pre-dispatch error.
No retry occurs after ownership has reached a worker. The borrowed caller
envelope and decoded semantic values are separate allocations, not purported
parts of this payload counter. Headroom still produces an observation-only
memory hint (`embeddedRequestMemoryBytes` in readiness); it cannot block dispatch. The input lease survives watchdog
expiry and handle abandonment until the actual worker releases ownership.

Alternatives: increasing the multiplier quota preserves arbitrary refusal;
dropping accounting loses lifecycle evidence; retrying published work risks
duplicate effects. Contract fields retire the multiplier/minimum reservation
and declare `admission=none`. Regressions include a committed large checkpoint
with another real input allocation above the lean hint, 64 independent calls
holding more than 16 MiB of copied input with idle target 1, exact retirement,
pre-dispatch allocation/unstarted worker failures and a watchdog-retired
caller whose worker still owns
its payload. These verify input delivery and ownership, not whole-engine RSS.

Measurement: 64 real FFI calls rendezvoused inside native dispatch with one
idle-reuse target, holding a peak of 16,778,112 owned payload bytes; all calls
settled in 1009.03 ms and input accounting returned to zero before close.
The standalone probe measures acceptance and retirement, not production tail
latency, decoded-object RSS or authority throughput.
