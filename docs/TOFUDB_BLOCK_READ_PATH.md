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

## Block payload cache: the byte budget is the only bound

*Problem (2026-10-27):* a 3.5 h live window on the BeeGFS FUSE authority served
3,296,537 of 3,330,324 block-cache lookups as uncached reads (4,694 s, 1.42 ms
each, mostly under the writer), with an 11.5 GB block budget. Two policies made
the budget unreachable. A fixed 65,536-entry cap held only about 150-300 MB,
because live loose blocks are small (p50 2,120 B, p90 4,127 B), so any larger
working set thrashed. And every payload-catalog install cleared the whole cache,
although 42 of 44 installs were background pack publications that only add
segments.

*Decision:* each entry is charged its allocated capacity plus a fixed
`entryOverheadBytes` (256) against the one launch-derived budget, which bounds
zero-byte entries without a count cap (`blockPayloadReadCache` in
`contracts/tofudb_ir_v1.json`). Recency is an exact LRU kept as an intrusive
list over a slot slab, constant time with no recency records to compact;
eviction runs before admission, so neither the budget nor the slab is ever
exceeded. A replacement catalog invalidates entries only when it drops a
previously referenced segment (since 2026-10-28 only the dropped IDs that no
added segment holds; see below). Cached payloads are content-addressed and
verified, so they never go stale; only a dropped segment can remove the last
durable copy that `put`/`put_deferred` dedup trusts as an existence proof, and
loose-block GC already evicts its own IDs.

*Alternatives rejected:* raising the constants (recency compaction then scans
millions of records under the cache lock); CLOCK or second-chance eviction
(changes the contract's exact LRU for no measured gain); per-entry durability
epochs (more state than needed once pure additions are distinguished).

*Measurement:* `block::tests::block_cache_working_set_measurement` (ignored;
deterministic VFS, where a miss is one payload read reaching the filesystem
layer), with 162,816 packed 2 KiB blocks, a 1 GiB budget and one 562,816-read
uniform trace, paired with the previous source tree. Old: 367,345 misses, 60% in
the steady pass and 67% after one pure catalog addition. New: 158,175 misses,
the ≈157,700 distinct blocks the trace touches plus ≈480 one-time segment
framing reads, so no capacity or invalidation misses remain. This measures
cache residency, not end-to-end latency; the production effect shows in
`system.metrics` as `get_cache` versus `get_unc_ok` stage counts.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

## Catalog-wide negative witnesses and drop-scoped eviction (2026-10-28)

*Problem:* on 2026-10-28 the live dolphinfs FUSE authority answered the sidebar
catalog (`conversation.list`, 590 headers, about 5 MB logical) in 5-9 s whenever
it touched a block that was absent from the payload cache, and in 0.1-0.4 s
otherwise; back-to-back requests alternated between the two. Per-thread `/proc`
deltas put one slow request on its single connection thread: about 5 s of CPU
and 3.2-3.3 GB read through roughly 7,500 page-cache reads, with `openat` and
descriptor `fstat` on `payload-segments/*.pseg`. Passive sampling showed the
same signature for `record.list`, the most frequent slow operation (1.4-3.5 GB
and 2.1-5.4 s of CPU on one connection thread per 2.8-7.4 s query), and slow
queries of different operations rarely overlapped (7-25%), so the cost was per
request, not a writer or authority stall. The catalog was full (4,096
segments of 7k-34k blocks, median index 944 KiB). Every packed segment, like any
content-addressed pack, spans the whole block-ID range, so first/last bounds
exclude nothing and prefix routing cannot afford such segments (a 20k-block
segment covers about 27% of the 16-bit prefixes and would cost about 5 MB of
routing budget). The only exclusion was the per-segment membership filter, and
filters lived in a FIFO limited to one quarter of the index budget, which held
about a third of the catalog. A miss therefore opened and CRC32C/BLAKE3-verified
the full index of every other candidate, and those opens rotated the FIFO, so
the next miss found a different two thirds unknown. Misses were frequent:
adopting a background pack into the full catalog merges it into the smallest
segment, and every catalog that dropped a segment cleared the whole payload
cache, about once per 24 s from adoption alone (five merged segment files in two
minutes). Online GC marks, maintenance and archive sealing read through the same
`BlockStore::get`; together they read about 2 GB/s on that daemon.

### Catalog-wide negative witnesses

*Decision:* retain one verified membership filter per catalogued segment outside
the index byte budget. The packed-union scan at first install, every verified
index open and every newly published segment (`verify_payload_segment`) supply
them; installing a catalog forgets the segments it no longer references; the
count bound is twice the catalog bound, so a publication in flight never
displaces a catalogued witness. An explicit zero segment budget still disables
both caches. Filters keep 16 bits per block but use four hash positions instead
of two (block IDs are uniform, so the four 64-bit words are independent). On the
sampled production segments this lowers the false-positive rate from about
0.75% to 0.08%, from about 31 to about 3 spurious index reads for a
4,096-candidate miss. Production filters average 51 KiB, about 205 MiB for a
full catalog (512 MiB worst case); full indexes keep the whole probe-derived
budget. A cold read now verifies its owning segment plus false positives.
Positive answers still require the exact sorted index and per-read CRC32C and
content hash; negative answers still validate cached metadata and first/last IDs
against the manifest reference; unknown segments still fall back to exact reads.

*Alternatives rejected:* a larger probe-derived share still loses to the cyclic
miss scan once the catalog outgrows it, and would tie a correctness-neutral
negative cache to memory headroom; prefix routing cannot exclude full-range
segments within any reasonable budget; persisting filters adds a durable format
and recovery surface for reconstructible memory; probing candidates newest-first
does not bound the work of a miss.

### Drop-scoped payload-cache eviction

*Decision:* when a replacement catalog drops segments, evict only the cached
payloads whose IDs the dropped segments' verified indexes list and no segment
added by the same catalog holds. A full-catalog adoption merge re-homes every
block of the segment it replaces and evicts nothing; GC rewrites and
consolidations evict exactly the blocks they discard. If a dropped or added
index cannot be read or validated, the original whole-cache clear runs. The
existence-proof contract is unchanged: no cached block relies on a segment the
catalog no longer references, and the relink fence still covers concurrent
reference decisions.

*Alternatives rejected:* keeping every entry (a dropped last copy would still
satisfy dedup); per-entry segment provenance (state on every cached payload for
an event two index reads settle); clearing only for GC-originated drops (merges
are the common case, and caller-based provenance is weaker than the index diff).

### Measurement

`block::tests::cold_packed_read_measurement` (ignored, Linux) builds a catalog
of full-range 8,192-block segments on `TOFU_DB_COLD_READ_BENCH_DIR`, sets the
index budget so the previous quarter share held 29% of the filters (and full
indexes of about 3% of the catalog), then reads one block from each of N
distinct segments in a fixed random order. The identical test source was added
to base `db8f55c48` for the paired runs; thin-LTO release test binaries; per-read
wall time and this process's `rchar`/`syscr` from `/proc/self/io`.

Read bytes and calls per block are deterministic. Wall time depends on how much
of the test's own files the shared host keeps in page cache (its cgroup ran at
216 of 236 GB), so cells with two runs show both.

| Volume, catalog | Pass | Base us/read | Base bytes, reads per block | Fixed us/read | Fixed bytes, reads per block |
|---|---|---:|---:|---:|---:|
| local md RAID, 512 | cold | 73,244-73,547 | 89.8 MB, 686 | 725-927 | 635 KB, 5.9 |
| local md RAID, 512 | after merge drop | 73,713-75,188 | 90.7 MB, 693 | 0.5 | 0, 0 |
| dolphinfs FUSE, 256 | cold | 39,186-40,931 | 44.7 MB, 342 | 1,435-1,474 | 503 KB, 4.9 |
| dolphinfs FUSE, 256 | payload cache cleared, OS cache warm | 38,899 | 44.7 MB, 342 | 511 | 503 KB, 4.9 |
| dolphinfs FUSE, 256 | after merge drop | 37,786-38,483 | 45.3 MB, 346 | 0.5 | 0, 0 |
| local md RAID, 4,096, 40 reads | cold | 617,135-6,779,675 | 685 MB, 5,224 | 47,761-204,684 | 2.1 MB, 17.1 |
| local md RAID, 4,096, 40 reads | after merge drop | 599,573-599,919 | 687 MB, 5,242 | 0.8-0.9 | 3 B, 0.1 |

The FUSE "cache cleared" base row repeats the full probe because its 198-entry
read-hint table cannot hold a 200-read cycle, the production shape (every
publication refills the hint table with one segment's IDs). The fixed 4,096-
segment cold reads still verify about five spurious indexes per block, because
these segments sit at the 16-bits-per-block minimum (0.24% false positives per
filter); their time is disk reads of those indexes. With 512-block segments,
whose indexes stay in the OS cache, the same 4,096-filter walk costs 1.0-1.2 ms
per cold read (135 KB, 17 reads). Peak RSS of the 4,096 × 8,192-block run was
245 MiB base and 309 MiB fixed, the 64 MiB of retained filters. This is a
component measurement, not a live-service latency: the production effect needs
a rebuilt engine and shows as fewer `get_unc_ok` stage nanoseconds and lower
handler-thread `rchar`.

## Catalog location table for whole-store passes (2026-10-31)

*Problem:* online GC marks and backup-prune marks resolve every page and blob
manifest they visit by ID. On the main authority (4,091 catalogued segments of
about 29k blocks each, a 1.34 MiB verified index per segment, about 5.5 GB in
total) the mark build aborted at its 900 s budget every epoch. Sampled live on
2026-10-30/31, the collector thread ran at 87-99% CPU doing about 220-450 whole
verified index opens per second (about 4 ms each) against 431-437 marked
blocks per second: about 0.5-1 whole-index re-verifications and 0.68-1.48 MB of
reads per marked block. `get_traversal` admits nothing to the payload cache, but
its catalog path still (1) copies, sorts and witness-checks every catalogued
segment per lookup, because content-hashed IDs make every segment span the whole
ID space so prefix routes exclude nothing (about 0.3-1 ms of CPU per lookup even
with no index I/O), and (2) verifies the owning segment's whole index on every
verified-index cache miss, admitting it and evicting foreground indexes.

*Decision:* `BlockStore::traversal_reader` gives one pass its own exact location
table. Construction streams each segment index of the installed catalog once:
an index resident in the verified-index cache is reused without refreshing its
recency; any other index is read and fully verified (header, trailer, CRC32C,
BLAKE3 digest, order and coverage) exactly as `open_segment` does, and admitted
to none of the index, witness or handle caches. Each catalogued block becomes one
8-byte entry: the leading 36 bits of its ID, a 12-bit catalog ordinal and a
16-bit index position. The ordinal and position widths are the catalog and
segment-format bounds (`MAX_PAYLOAD_SEGMENTS`, `MAX_SEGMENT_BLOCKS`), so the
table is bounded by the captured catalog itself; there is no size knob.
Entries are sorted in place by an American-flag partition on the leading byte
and 256 run sorts, with the pass's stop check between steps. A lookup is
a binary search within one run. Each candidate is only a position: the reader
takes the entry from a resident verified index or one positioned 48-byte read
of the on-disk entry, bounded to the payload region, requires the full 32-byte
ID to match, and authenticates the payload by CRC32C and content hash. A prefix
collision therefore costs one extra entry read and never yields another block's
bytes. Staged and cached payloads are still reused without admission or recency
changes. A block the table does not serve (written after the table was built,
moved since, held by a segment whose index failed verification, or any failed
located read) is read from its loose file, then through the exact `get_uncached`
path, which alone may report it missing; integrity failures surface there with
their existing types. The online mark (`build_marks`) and backup prune
(`write_live_marks`) read through the reader. Other whole-store passes (segment
rewrites and consolidations, backup copy) keep `get_traversal`.

*Live-shape cost:* about 118.6M entries take 949 MB (905 MiB) for one mark and
are dropped with it; the worst-case catalog (4,096 x 65,536 blocks) is 2 GiB.
A standalone release-mode copy of the sort and lookup over 118.6M interleaved
entries sorted in 4.8 s and looked up in 565 ns, with 1.0017 candidates per
present block (36-bit prefixes). Streaming the indexes costs one verified read
per segment, about 4,091 x 4 ms (about 16 s) at the live per-open cost.

*Alternatives rejected:* a positioned interpolation search of each candidate's
on-disk index (about five probes per segment) removes the index re-reads but
keeps the per-lookup walk over every witness. A bit-sliced witness matrix makes
that walk cheap but still opens the owning index per miss and rewrites the
foreground witness store. Breadth-first waves that read each index once per wave
re-read 5.5 GB per tree level and restructure the entity visitor and its key-range
witnesses. A larger verified-index cache share is tuning, and is evicted by the
next pass anyway. Storing payload extents in the table would save the
index-entry read for 4 more bytes per entry (+475 MB live); on DolphinFS that
read follows the table build's streaming of the same index and costs a few
microseconds (below), so it was not worth the memory.

*Measurement:* `traversal_reader_resolves_catalogued_blocks_with_one_candidate_and_two_reads`
(48 segments x 512 blocks, quarter index budget, deterministic VFS) reads each
index once to build the table, then two reads and one candidate per block: 96
reads for 48 blocks versus 201 reads and 2,304 witness checks through
`get_traversal`, with no index or payload admitted. The ignored
`traversal_reader_measurement` on the DolphinFS (BeeGFS FUSE) volume, debug
build, 256 segments x 8,192 blocks, two runs: cold exact-path reads of one block
per segment cost 6,089-6,296 us, 511,644 bytes and 4.9 read calls per block;
the table build cost 7,626-7,732 us, 376,423 bytes and 2.9 calls per segment;
cold located reads of other blocks of the same segments cost 211-220 us, 61
bytes and 2.0 calls per block, and repeating them cost 7.4-12.5 us. These are
component timings, not epoch durations. Fail-closed behavior is pinned by
`traversal_reader_damage_never_yields_other_bytes_or_drops_a_block`: a payload
or index entry damaged after the build ends in `InvalidData`, a misplaced table
entry resolves through the exact path with correct bytes, and an index damaged
before the build is left out of the table and reported by the exact path.
`traversal_reader_follows_blocks_published_or_moved_after_its_table` and
`location_candidates_resolve_prefix_collisions_by_the_full_index_id` cover the
fallbacks and collisions.

## Published catalog location table (2026-11-06)

*Problem:* on 2026-11-06 the live daemon on the dolphinfs FUSE authority
(4,103 full-range segments of about 56k blocks, 1.07 TB; a 2.7 MB verified index
per segment, about 11 GB in total, of which the verified-index cache held about
29%) ran at 20-25 cores on its roughly 1,300 connection threads. A user-space
profile of every thread (10 s of `perf_event` task-clock samples) put about 16%
in sorting the per-lookup candidate copy, 15% in the witness walk, 36% in
reading and verifying whole segment indexes (BLAKE3, CRC32C, decode) and most
of a further 20% of libc time in copying candidate vectors and index buffers.
A stack scan found about 615 of 1,375 threads waiting on the segment
read-handle mutex and about 400 on the verified-index cache's write lock, which
every cache hit takes to refresh recency. The cause was the foreground read
path: every cold read of a packed block ran the exact catalog path, copying and
sorting one candidate per catalogued segment, checking every witness, and
opening (whole index read, verification, admission) its owner and each witness
false positive (four probes at 16-18.7 bits per block: 0.14-0.24% per segment,
about 6-10 per lookup). Commits run the same reads under the authority mutex:
the console recorded 5,493 `turn.event.record` holds of at least 1 s since
2026-11-02 (median 1.5 s, p95 6.8 s), dominated by `vfs_read`/`vfs_open`, and
RPC deadlines elapsed; one task never started because `turn.attempt.start`
timed out at worker entry. Writers probe dedupe the same way, under the
authority mutex: one 36 s `turn.event.record` hold was spent entirely in
`put_deferred` (`stages=put_deferred:36035`, under 5 ms of VFS time), the
committing thread queued behind the reader threads on the verified-index cache
lock. Whole-store passes already avoided all this with their own location table
("Catalog location table for whole-store passes" above), but foreground reads,
dedupe probes and the online GC's per-loose-block membership probes did not.

*Decision:* `BlockStore` publishes one catalog location table per installed
catalog publication and shares it. The online GC driver builds the first table
off the authority mutex as soon as an epoch is due, before that epoch's capture,
which defers while foreground holds contend for the mutex (after the 2026-11-07
restart two deferred captures delayed the authority's first table by four
minutes; after the next restart this rule published it 59 s after engine start,
the 30 s boot grace plus a 21.9 s build). The search index's store is resolved
under a non-blocking hold of its own mutex once that index has opened, so its
first table waits for a due epoch that finds the mutex free: after that restart
all 21 search epochs of the first 40 minutes deferred and no search table was
published. Its lookups then fall back to the exact path in 0.1 us, and the
maintenance writes that make them averaged 5.2 us. A whole-store pass (`traversal_reader`) derives a stale published
table, reuses a current one, and builds and publishes one only when none exists.
The maintenance worker's overlay poll derives each later
publication's table off the authority mutex (`advance_catalog_locations`):
entries of segments the new catalog still names with an identical reference
are kept and renumbered, a dropped segment's entries leave, and only segments
the old table did not index (added ones, or ones whose index failed
verification) have their index read, exactly as a build reads it. Kept entries
leave the old table in prefix order and merge with the new ones in one
sequential pass (median 2.6 s over 97 live derivations of the 229M-block table,
1.2 s to 82 s on the overloaded host). The poll only
starts it: the derivation runs on its own `tofu-db-locations` thread, so the
worker's overlay drain polls never wait for it; one runs at a time, and the
worker joins it before the next and when it exits. The same poll derives the
search index's table once that index is open: it is its own authority with its
own catalog and online GC, whose driver builds its first table.

One resolver (`resolve_in_published_catalog`) answers three consumers, each
before its exact path: `get_uncached_materialized` (cold reads), the dedupe
probe `probe_packed_dedupe` (on a packed-filter hit, before any read hint) and
the membership probe `catalogued_payload_segment` (when the packed filter
cannot exclude the block):

- A hit costs one candidate and a positioned index-entry read (or the resident
  index). Reads and dedupe then read the payload, which passes CRC32C and the
  content hash; membership only confirms that the entry names the full ID, as
  `PayloadSegment::contains` does. A hit records no read hint; hints serve
  reads before a table exists.
- When the table is the installed publication's, every catalogued segment is
  indexed, every candidate answered and the catalog did not change during the
  lookup, a miss proves the block absent: a read goes to the loose copy with
  that publication as its exclusion (`get_loose_after_catalog`, which
  re-checks a newer catalog after a loose miss, as for packed-filter
  exclusions), dedupe stages the payload and membership answers none.
- Anything else (a failed located read, an unindexed segment, a catalog change,
  a stale table's miss, no table) leaves the verdict to the read hints and the
  exact path, which keeps every integrity verdict and error type.

A stale table locates blocks only through segments the installed catalog still
names, and never proves absence. Every install recomputes, inside its catalog
swap, which of the published table's segments the new catalog names
(`live_segments`), so a table one publication behind keeps serving the blocks
of every segment that publication kept while its derivation waits for the
worker. The payload cache is the dedupe existence proof and drop-scoped
eviction relies on a dropped segment's blocks not returning to it, so no read
or dedupe probe resolves through a segment the installed catalog dropped;
dedupe restages content whose only copy such a segment held.
The stages `catalog_location_hit`, `catalog_location_absent`,
`catalog_location_unknown` and `catalog_location_derive` count outcomes in
`system.metrics.commitStages`, and every publication logs
`catalog_locations step=published` with its source, publication, located blocks,
unindexed segments, resident bytes and elapsed time.

*Cost:* 8 bytes per catalogued block in each of two retained allocations, the
published table and the spare it replaced (about 1.84 GB each for the live 230M
blocks, at most 2 GiB each for a full catalog of full segments; see "Derivations
write into the replaced table" below). It replaces the table the
online GC rebuilt for every mark (1.2-1.5 GB and 12-25 s of index streaming per
epoch on the live volume), which now happens once per process. Nothing is
sized by a knob: the catalog bounds the table, a derivation runs once per
installed publication, and the first build runs on the online GC driver, where
whole-store passes already ran it. Until that build (the first epoch is due
after a 30 s boot grace; streaming every index took 21.6 s for the live 229M
blocks) every lookup takes the exact path as before; between an install and its
derivation, only blocks of the segments it added and absence proofs do.

*Alternatives rejected:* deriving inside `install_payload_manifest`, because
installs from `Engine::publish_payload_manifest` hold the authority mutex and
every commit would wait for a full-table pass; deriving on the maintenance
worker itself (the first version), whose overlay drain then stopped for each
derivation; merging the table's 256 leading-byte runs on one scoped thread
each, which `location_table_derivation_measurement` measured 9-12x faster on
warm memory (186-306 ms against 1.9-2.8 s for 229M entries) but 1-18 s while
its threads first fault the new table's pages, worse than the single pass
(4.9-6.7 s), with global allocation stalls unchanged: hundreds of threads
faulting and mapping stacks at once contend on the address space every
daemon thread shares; serving stale tables
unfiltered, which returns dropped segments' blocks to the dedupe cache;
answering nothing from a stale table (the first version), which sent every
lookup to the exact path for as long as a long maintenance round delayed the
derivation; checking each candidate against the installed manifest per lookup
instead of per install, a manifest scan or a second index on every cold read;
a base table plus
per-publication delta runs, whose lookups grow with publications until a merge
threshold (a knob) folds them; a separate builder thread or an initial build on
the maintenance thread, which duplicates the index stream the first mark
already performs; more Bloom bits per segment, which removes only the
false-positive index reads and keeps the O(segments) walk and sort; sort-free
candidate lists and hash-free witness arrays, constant-factor savings on the
same walk; a sharded read-handle mutex, whose shard count is a knob, while
located reads stop taking that mutex for every false positive anyway.

*Measurement:* `foreground_cold_packed_reads_resolve_through_the_published_location_table`
(48 full-range segments x 512 blocks, deterministic VFS) reads 48 cold blocks
with 96 reads, one candidate each and no witness check or index read; the exact
path needed 201 reads, 2,304 witness checks and 51 whole-index verifications
(1.25 MB). `derived_catalog_locations_equal_a_fresh_build_of_each_publication`
shows that a derivation after an adoption-style merge plus an added segment
reads at most the two added indexes and locates exactly what a fresh build
locates, and that the dropped segment never answers in between.
`stale_location_tables_locate_kept_segments_and_never_prove_absence` serves a
kept segment's read and membership probe through a stale table with no catalog
walk, refuses the dropped segment, the added segment and absence until the
derivation, and restages dedupe of content whose only segment was dropped.
`dedupe_and_membership_probes_resolve_through_the_published_table` (16
full-range segments x 64 blocks, verified-index cache below the index bytes)
resolves 16 membership and 16 dedupe probes with 32 candidates, no witness
check and no whole-index read; the exact path took 512 witness checks and 16
whole-index verifications.
`worker_derives_the_published_location_tables_of_the_authority_and_the_search_index`
runs the derivation of both tables on the live maintenance worker after engine
publications.
The ignored `published_location_table_cold_read_measurement` (thin-LTO release
test binary; 1,024 full-range segments x 8,192 blocks, 402 MB of indexes, index
cache at the live 29%, payload cache disabled; 200 cold reads of distinct
segments per pass, disjoint blocks per pass):

| Volume | Path | us/read | bytes, read calls, witness checks per read |
|---|---|---:|---:|
| local md RAID | exact catalog path | 1,294 | 721,763, 6.5, 1,018 |
| local md RAID | located | 194 | 61, 2.0, 0 |
| local md RAID | located, warm handles | 4.8 | 23, 1.2, 0 |
| dolphinfs FUSE | exact catalog path | 2,079 | 721,763, 6.5, 1,018 |
| dolphinfs FUSE | located | 840 | 61, 2.0, 0 |
| dolphinfs FUSE | located, warm handles | 389 | 23, 1.2, 0 |

The exact pass runs second, so it inherits page-cache warmth of the shared
indexes; "warm handles" reuses read handles and indexes the exact pass left
resident. Building that table took 0.6 s locally and 1.4 s on FUSE (67 MB).
The exact path's per-read witness checks and index bytes grow with the segment
count and index size (4,103 segments of 2.7 MB indexes live); the located path's
stay at one candidate and two reads.

Live, on the dolphinfs authority after the 2026-11-07 restart onto this design:
authority holds of at least 1 s fell from 79.5 per hour (767 in the 9.7 hours
before; median 1.5 s, p95 6.9 s, max 33.5 s) to 17.3 per hour (38 in the 2.2
hours after). Foreground lookups the table answered took 200 us on average
(5,349 in the first hour; 69 us over 13,450 in the first 41 minutes after the
next restart), where the old engine's foreground uncached reads averaged
20.2 ms. None of the 38 remaining long holds (median 2.7 s, up to 23 s, while
the host ran at load average 50-93) was a catalog walk: the four longest spent
17-23 s in IR execution, command execution, one block read and one engine
commit. The holds dominated by `command_execute` coincide with the memory
cgroup at its limit ("Derivations write into the replaced table" below). History-payload blocks resolve
through `get_from_history` before any catalog lookup and are outside the table;
the online GC still performed 1.45M uncached block reads per hour at 1.3 ms on
average. `location_table_derivation_measurement` (ignored; a synthetic
229M-entry table of 4,096 full segments, one dropped) times one derivation at
1.9-2.8 s on warm memory, matching the live
`catalog_locations step=published source=derive` lines (median 2.6 s).

### Derivations write into the replaced table (2026-11-08)

*Problem:* every derivation allocated its table afresh: 1.86 GB for the live
232M entries, which the kernel maps and zero-fills page by page as the
derivation writes it, then unmaps when the next derivation replaces it. In the
first 5.5 hours after the 2026-11-07 restart that was 154 derivations, about
286 GB of fresh memory. The container's memory cgroup (v1, no swap, about
150 GB of it page cache from the FUSE mount) ran at 99.2-99.9% of its 220 GiB
limit, where every fresh page is charged through synchronous reclaim. All 13
authority holds of that period dominated by `command_execute` (2-44 s, every
one on the exclusive lane) fell in windows at 99.8-100% (35 of the 81 other
holds did; Fisher p about 6e-5); 6 of them fell inside a derivation
(time-shifted null p about 0.008), and twice its 1.5 GiB step brought the cgroup
to the limit just before the stall. Sampled daemon threads then sat in
uninterruptible sleep outside any syscall, that is, faulting. Kernel stacks
were not readable, so the stall mechanism is inferred, not observed.

*Decision:* `BlockStore` keeps the table the published one replaced
(`retired_catalog_locations`), and `advance_catalog_locations` hands it to
`derive_location_table` as the spare whose allocations the new table takes,
once no reader holds it (`Arc::try_unwrap`). The published table and the spare
then alternate between two allocations, so the derivation writes pages that
are already resident. A spare a reader still holds (a whole-store pass keeps
its table for a whole mark) is left to that reader, and that derivation
allocates as before. A spare too small for the new table is released before
its replacement is allocated, at `Vec`'s doubling capped at the format's
2^28 entries, so a growing catalog reallocates only when its table doubles.
Publication no longer frees the replaced table under the publication lock that
cold reads take. The cost is the second resident table between derivations;
the peak, two tables during a derivation, is unchanged.

*Alternatives rejected:* deriving in place, because readers keep using the
published table while the derivation reads it; one shared table updated per
segment, which needs reader exclusion; an allocator purge delay or `madvise`
hint, a knob that changes every allocation without guaranteeing this one;
exactly sized fresh tables, the previous behavior.

*Measurement:* `location_table_derivation_measurement` (thin-LTO release test
binary, the synthetic 229M-entry table, one segment dropped) on the loaded host:
deriving into fresh memory took 1.95-3.07 s and 508K-1,344K minor faults over
five runs; deriving into the previous round's table took 1.17-1.91 s and
11K-805K (medians 2.03 s fresh, 1.26 s reused). The table's own first touch
is 448K pages; the faults reused rounds
still take vary with host load (`kernel.numa_balancing` is on) and are not
the table's. `derivations_write_into_the_retired_table_once_no_reader_holds_it`
shows the third derivation writing into the first one's allocation after the
catalog shrinks, and a table a reader holds staying intact while the next
derivation allocates.

## Shared segment read handles (2026-11-07)

*Problem:* the payload-segment read-handle cache leased each idle descriptor
exclusively: a reader took the segment's handle out of the cache, read, and put
it back. A second concurrent reader of the same segment found none and opened
another descriptor; on return all but one were closed, and the cache closed
every displaced or evicted handle while holding its one global mutex, which
every read takes twice. On FUSE a close is a release round trip and an open
about 1-2 ms of metadata work. The 2026-11-06 stack scan of the live daemon
found about 760 connection threads contending on that mutex and about 90 in
`openat`, opening duplicate descriptors of segments other threads held.

*Decision:* readers share one handle per segment. `vfs::SharedReadFile` is the
one shared-reader type: positioned reads on one descriptor where the VFS offers
them (`pread` for `RealVfs`; the deterministic simulation offers them too),
otherwise one ordinary handle behind its own mutex for wrapper VFSes whose
handles keep read state. History sources already used that shape and now use
the type. A lookup takes the cache's shared lock, clones the cached `Arc` and
stores a monotonic timestamp into its own entry's recency; the read runs outside
the lock, so concurrent readers never queue on each other. A miss opens outside
the lock and admits the handle under the exclusive lock, and a racing open
shares the first admitted one; an admission beyond the bound evicts the least
recently used handle by one scan of the cached handles. Every
handle the cache lets go (a racing loser, LRU eviction, catalog retention,
segment removal, a failed read) is handed back and dropped after the lock is
released, so no descriptor closes under it; a descriptor closes when its last
reader drops it. Failure semantics are unchanged: a failed read or verification
lets go of the cached handle, so the next reader opens a fresh descriptor
(`read_through`); a handle opened before its segment's removal is never
admitted (retired IDs); verification still re-checks the path and re-verifies
framing. The bound is unchanged (descriptor-derived, 32..4,096) and now counts
one shared descriptor per segment instead of idle handles plus leased
duplicates.

*Alternatives rejected:* a sharded lease mutex (a shard-count knob that keeps
the duplicate opens and the closes under each shard's lock); a pool of idle
handles per segment (a pool-size knob, still one open per concurrent reader
beyond it); exact recency in a queue under one mutex (this change's first
version: every lookup appended to the queue, and at the live bound of 4,096
handles compacting it stalled every reader; it measured about 0.5 context
switches per read at 32-64 threads); CLOCK reference bits (an eviction hand over
a slot ring, more state than one scan per admission, and admissions open a file
anyway); keeping exclusive leases for VFSes without positioned reads (a second
cache path; those readers now wait for one read on the segment's handle instead
of opening a descriptor).

*Measurement:* `concurrent_readers_share_one_segment_handle_and_a_failed_read_releases_it`
reads 8 threads x 16 rounds x 3 blocks through one warmed handle with no open,
and a faulted read releases the handle so the next reader opens exactly one;
`real_segment_files_are_read_through_one_shared_positioned_descriptor` checks
that a real file is read with `pread` and that one `Arc` serves 8 concurrent
threads; `a_handle_opened_before_its_segment_retired_is_never_admitted` pins the
retirement rule. The ignored `concurrent_packed_read_measurement` (thin-LTO
release test binaries, each pair of the same tree with and without one step,
run in ABBA order; 256 full-range segments x 4,096 blocks, payload cache
disabled, verified-index cache at the live 29%, published location table; reads
of 16 hot segments, `getrusage` CPU and context switches per read). Step one,
leased handles to shared handles with mutex-guarded exact recency:

| Volume, threads | Leased: reads/s, CPU us, switches per read | Shared, mutex: reads/s, CPU us, switches per read |
|---|---:|---:|
| local md RAID, 1 | 353k, 2.8, 0.00 | 450k, 2.2, 0.00 |
| local md RAID, 8 | 271k, 11.1, 0.67 | 878k, 8.1, 0.13 |
| local md RAID, 32 | 178k, 13.2, 0.82 | 1,062k, 25.7, 0.51 |
| local md RAID, 64 | 153k-157k, 15.0-15.5, 0.86-0.87 | 934k-1,026k, 53.9-57.9, 0.53-0.55 |
| dolphinfs FUSE, 64 | 49k-87k, 17.7-27.7, 0.85-1.43 | 855k-1,011k, 47.5-56.7, 0.51-0.54 |

Step two, the shared-lock lookup with per-entry timestamps (a separate session):

| Volume, threads | Shared, mutex | Shared, read-locked lookups |
|---|---:|---:|
| local md RAID, 8 | 858k-905k, 7.7-8.3, 0.11-0.13 | 1,112k-1,204k, 6.4-6.7, 0.00 |
| local md RAID, 32 | 911k-943k, 28.5-29.8, 0.54-0.56 | 1,115k-1,170k, 26.1-27.6, 0.03-0.05 |
| local md RAID, 64 | 950k-953k, 57.2-57.4, 0.51-0.53 | 1,171k-1,236k, 37.0-41.2, 0.03-0.04 |
| dolphinfs FUSE, 64 | 959k-993k, 56.2-58.2, 0.51-0.55 | 1,141k-1,215k, 38.1-48.3, 0.03 |

Leased handles lost throughput as threads were added: the lease mutex convoyed,
and a contended read opened and later closed a duplicate descriptor (on FUSE
each a round trip). Shared handles read 27% faster on one thread and scale to
about 1.06M reads/s; read-locked lookups remove the remaining convoy on this
cache (context switches fall from about 0.5 to 0.03 per read) and lift the
ceiling to about 1.2M reads/s, the same on FUSE because hot segments are served
from the page cache. Past that ceiling the extra CPU is kernel and cache-line
contention time (88% kernel in the `perf_event` task-clock samples of a
64-thread pass of step one): the remaining exclusive global locks every
`BlockStore::get` takes, the payload cache's write lock twice (exact LRU
recency on every hit) and the pending-block mutex. Those are the next limit on
concurrent reads.

## Packed filter across publications (2026-10-31)

*Problem:* the packed union filter's negative answers ("in no catalogued
segment") were trusted only for the catalog publication the filter was built
for, and it was built once per process, at the first install. Every later
publication left it stale for the rest of the process: checkpoint packs,
adoption merges into a full catalog, GC rewrites and drops, history
promotion, live every few seconds. A stale filter answers "maybe" for every
block, so each cold read of a loose block, each lookup of a missing block
(every `put` of new content starts with one) and each dedupe probe of new
content (under the authority mutex) took the exact catalog path. That path
copies and sorts one candidate per catalogued segment (twice), checks every
segment witness, and verifies the whole index of each witness false positive
into the verified-index cache, evicting hot indexes. On the live catalog
(4,091 full-range segments, 1.34 MiB indexes, about three witness false
positives per walk) that is about 4,091 witness checks and three whole-index
verifications per lookup.

*Decision:* the filter tracks coverage per segment. It records each segment
whose every block ID it holds (segment ID plus the exact reference its
verified index matched), and trusts negative answers for a publication only
when every catalogued segment is covered. `verify_payload_segment`, which
every publisher already runs on each segment it adds, inserts that segment's
IDs from the index it just verified. The install reads only segments that are
still uncovered, normally none, outside the filter lock, and hands their
verified prefixes to the routing build, so one install never reads an index
twice. The first catalog of a process is still scanned whole, now without
holding the filter lock; readers stay on the exact path meanwhile instead of
waiting. The per-publication binding and the zero-publication install fence
are unchanged: an install records its covered publication before it swaps
the catalog, so no reader trusts the filter for a catalog it does not cover.
Two rules keep the answers exact:

- A negative answer is exact only for its own publication. Loose compaction
  installs its segment before it removes the loose copies, and the online
  collector removes loose copies of catalogued blocks, so a lookup whose loose
  file is missing, or vanishes between its stat and its open, consults the
  installed catalog once more unless the filter excluded the block under the
  publication still installed (`get_loose_after_catalog`; read order in
  [online GC](TOFUDB_ONLINE_GC.md#redundant-loose-copies-of-catalogued-blocks-2026-10-31)).
- A rebuild (saturation or recovery) resets coverage to the catalog it
  scanned. A segment whose bits it dropped is uncovered again and is read at
  its install, so it can never be hidden.

Coverage records hold the installed catalog plus at most one catalog
(`MAX_PAYLOAD_SEGMENTS`, the most one publication can add) of verified but
unpublished segments; forgetting the oldest keeps its bits and costs only
that segment's index read if it is ever installed. Retired segments keep
their bits as harmless positives until the existing saturation or recovery
rebuild. Saturation now counts insertions that set a bit, so re-inserting a
merged or rewritten segment, whose blocks the filter already holds, adds no
load. Covering a live-size segment (29k IDs) in a live-size filter (2^32
bits loaded with 118.6M blocks) costs 4.9-5.1 ms of CPU under the filter
write lock per published segment; an absent probe costs 92 ns (standalone
release copy of the insert and probe loops).

*Alternatives rejected:* per-publication delta filters, whose lookup cost
grows with publications and needs a compaction rule; a union of the
per-segment witnesses (different geometries, still one walk per catalog);
rebuilding the filter at every publication (about 5.5 GB of index reads
live); probing the loose file first whenever the filter is stale (an extra
network stat on every packed read, and the stale state remains); atomic
filter words so insertion needs no write lock (correct, but a larger change
for a 5 ms per-publication hold).

*Measurement:* `cold_loose_reads_skip_the_catalog_after_successive_publications`
builds 256 full-range segments of 64 blocks with a 32 KiB index budget, then
publishes a checkpoint pack, an adoption merge, a GC rewrite, a GC drop and a
two-segment history promotion. Per lookup, deterministic VFS, debug build:

| Lookup | Witness checks | Whole indexes read (bytes) | Index-cache admissions | Opens, reads |
|---|---:|---:|---:|---:|
| cold loose read, before | 254.8 | 0.56 (1,728) | 0.56 | 1.00, 2.69 |
| cold loose read, after | 0 | 0 | 0 | 1, 1 |
| missing block, before | 515.6 | 0.72 (2,208) | 0.72 | 0.62, 2.16 |
| missing block, after | 0 | 0 | 0 | 0, 0 |
| dedupe of new content, before | 256.8 | 0.50 (1,536) | 0.50 | 0.38, 1.50 |
| dedupe of new content, after | 0 | 0 | 0 | 0, 0 |
| cold packed read, both | 258.0 | 1.03 (3,168) | 1.03 | |

Before the fix only the first of the six publications was covered; after it,
all six are. Cold packed reads keep their witness walk by design ("Catalog-wide
negative witnesses" above). The ignored `cold_loose_lookup_after_publication_measurement`
ran thin-LTO release test binaries of the base (`873385739`) and the fix on
4,091 full-range segments of 1,024 blocks plus one published checkpoint pack,
with the index budget at the 2026-10-28 ratio and 200 lookups per pass. Bytes,
read calls and witness checks per lookup were identical in every run; wall
times are two runs each, the second in reversed order:

| Volume, lookup | Base us | Base bytes, calls, witness checks | Fix us | Fix bytes, calls, checks |
|---|---:|---:|---:|---:|
| local md RAID, cold loose read | 2,288-9,037 | 466,805, 30.4, 4,075 | 16.7 | 67, 2.0, 0 |
| local md RAID, missing block | 3,400-163,312 | 466,492, 28.4, 8,163 | 3.9-4.0 | 0, 0, 0 |
| local md RAID, dedupe of new content | 2,128-89,138 | 451,468, 27.5, 4,083 | 3.7-3.9 | 0, 0, 0 |
| dolphinfs FUSE, cold loose read | 3,727-3,898 | 466,805, 30.4, 4,075 | 911-955 | 67, 2.0, 0 |
| dolphinfs FUSE, missing block | 6,622-7,200 | 466,492, 28.4, 8,163 | 385-450 | 0, 0, 0 |
| dolphinfs FUSE, dedupe of new content | 4,607-4,962 | 451,468, 27.5, 4,083 | 328-336 | 0, 0, 0 |

The base local range spans page-cache state (the second run read its indexes
from disk). The fixed lookups that remain are the loose file's own stat, open
and read. Peak RSS of the measurement process was 56.0 MB base and 48.2-48.8 MB
fixed. Live projection, per cold loose read, missing-block lookup or
new-content dedupe probe on the 4,091-segment catalog: about 4,091 witness
checks (0.3-1 ms of CPU as measured for marks) and about three whole-index
verifications (about 4 MB and, at about 4 ms each on DolphinFS, about 12 ms
when they miss the verified-index cache) are removed, plus their index-cache
admissions. The share of such lookups among the 2026-10-30 foreground misses
is unknown, because marks were then misattributed to foreground. On a rebuilt
engine, `pack_filter_incomplete` stops counting between publications,
`get_unc_ok`/`get_unc_miss` nanoseconds per call fall, and `pack_filter`
gains about 5 ms per published segment.
