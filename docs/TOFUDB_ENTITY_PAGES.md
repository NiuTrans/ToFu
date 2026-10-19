# Immutable entity-page storage

Owner: `packages/tofu-db/src/entity_page_codec.rs`. Tree traversal, OCC,
publication and snapshot ownership remain in `entity.rs`. Format and packing
policy are declared once in `contracts/tofudb_ir_v1.json` under
`entityPageCodec` and `entityPagePacking`.

## Problem, decision and alternatives

Problem: version-1 leaves and internal pages repeat complete sorted entity
keys, including tenant, owner, namespace and business prefixes. Every copied
page carries those bytes through staging, WAL, retained history and backup.
Packing by full key length also limits fanout and creates more physical pages.

Decision: version 2 stores each key as a little-endian u16 shared-prefix length
and u16 suffix length relative to the immediately preceding reconstructed key.
The first prefix is zero. Leaves retain value length, original commit version,
tombstone flag and exact value bytes; internal pages retain exact child IDs.
New pages use version 2. Version-1 pages remain readable and are replaced only
when ordinary copy-on-write changes their branch. Open performs no rewrite.

The 16-byte header retains `TDBENT01`, the explicit page version, kind, level
and entry count. Version-2 leaf entries contain prefix length, suffix length,
value length, u64 commit version, tombstone byte, key suffix and value. Internal
entries contain prefix length, suffix length, key suffix and 32-byte child ID.
Keys reconstruct before existing ordering, owner, range and level validation.

Alternatives considered: larger fixed pages increase unrelated rewrite work;
general compression introduces per-page compression/decompression cost before
removing known duplicated keys; mutating existing pages breaks immutable
snapshots and content IDs. Prefix coding changes page density while retaining
the existing immutable graph and publication protocol. It does not compress
values, change overlay replay framing or create another durable authority.

## Packing, decoding and residency

Both leaf and internal packing target 4,096 encoded bytes. A legal indivisible
entry and minimum internal-child groups may exceed the target, but never the
16,384-byte physical ceiling. The expanded equivalent version-1 representation
has an independent 16,384-byte ceiling, including its header and entry framing.
Leaf frames split on either limit; internal groups recompute their first key's
full length at each split and preserve minimum-child/tail balancing.

The decoder rejects unknown versions, impossible counts before vector
allocation, a prefix longer than the preceding key, oversized reconstructed
keys/values, invalid tombstones, expanded-size overflow, truncated/trailing
bytes and non-increasing keys. Legacy input follows the same value, ordering
and original version checks. Compression cannot admit an unbounded expanded
key set into the decoded cache.

Decoded-cache charge is the maximum of the preceding encoded-size estimate
and the actual owned node, entry-vector, key, value and directory allocations,
plus the existing entry overhead. Zero-capacity eviction still releases roots.
No resident worker, queue, background sampler or durable namespace is added.

## Durability, compatibility and lifecycle

New page bytes are content-addressed and published through ordinary fresh-page
staging and generation-bound WAL/CONTROL acknowledgements. Overlay preparation
remains private until its root fence succeeds. Original per-key commit versions,
old-reader roots, range mounts, tombstone masks, replay coverage, backup and GC
reachability remain authoritative. Untouched branches retain their original IDs.

Older readers reject version-2 pages when encountered; an upgraded authority
cannot be assumed safe for in-place binary downgrade. A legacy root or directory
can initially remain readable while a descendant already uses the new format.
Publication and recovery fault matrices must run against the actual new build;
successful codec tests alone do not certify acknowledged recovery.

## Verification and measurement

Conversation trash retirement probes an attempt's event prefix before reserving
a range-retirement slot. Problem: completed imported attempts have no event rows,
but empty ranges exhausted the transaction's 64-slot codec bound during deletion.
Decision: `turn::retire_conversation_attempt_events` skips empty ranges inside
the same owned transaction. Nonempty retirement, event-head removal and complete
trash snapshots retain their existing semantics. Alternatives rejected: raising
the engine-wide bound or splitting deletion across commits changes resource or
atomicity guarantees. The regression deletes/restores 97 attempts, including
one with live evidence. This fixes sparse histories; the existing bound for many
nonempty ranges remains a separate limitation.

`cargo test --lib entity::page_codec` covers both format generations, fixed
legacy/golden pages, malformed inputs, expansion/cache bounds, mixed branches
and deterministic legacy-authority upgrade across simulated power loss.
The neighboring entity and overlay tests cover stale snapshots/OCC, range
mounts, splits, I/O faults, short writes and lost-sync publication. Full native
tests additionally cover backup, restore, integrity and GC.

`cargo run --release --example entity_page_measure -- <existing-volume-parent>
[rows] [value-bytes]` uses disposable real authorities for seed, update/delete,
full overlay merge, pinned-old-reader verification, checkpoint, reopen and exact
current readback. It reports staging bytes/blocks, not physical device writes.
Compare source-pinned baseline/candidate executables with identical rows/values
and alternating order on the same volume. Complete public-client workloads and
active-conversation tests remain required before claiming an application win.

## Measured result and adoption scope

`tofudb_prefix_pages_20261010.json` compares
legacy/prefix pages on the same FUSE volume in A/B/B/A order. Three independent
12,000-row lifecycle shapes use 0/64/512-byte values. Staging byte counts fall
86.07%/60.35%/19.72%; staging block counts fall 72.27%/59.28%/20.55%. Every
shape and leg has identical exact-output digests. This establishes reduced
physical representation work for those keys, not device writes or application
speed. Checkpoint/close medians fall 43.06%/30.45%/13.42%, while reads without
the decoded-page cache worsen 51.83%/24.42% for small values and improve 31.20%
for 512-byte values. Dense leaf decoding is a real tradeoff, not a discarded leg.

Eight 1,000-conversation legs use strict durability, four unique 8-KiB content
chunks, fixed arrivals, running/final SIGKILL and exact readback/integrity. All
legs have zero errors and 1,000 exact recovered contents. Three adjacent pairs
improve scheduled-stream P99; the first reverse pair regresses (7.57 vs 5.66 s).
All legs miss the 1 s stream budget. Admission, RSS and the complete 16-workload
corpus have mixed outcomes. A medium four-workload writer/read/history followup
and a 16-workload native/SQLite/raw-SQLite control retain their own sample and
crash-method limits; raw SQLite connection close is not process-crash proof.

Decision: adopt the page-format capability and its deterministic physical-work
reduction, preserving legacy reads and existing publication/recovery semantics.
Do not claim stable end-to-end acceleration, universal SQLite dominance,
smaller total authority footprint or capacity-SLO completion. Choosing only
positive latency legs or treating staging bytes as device amplification would
misstate the result. Shared-source integration preserves concurrent changes;
full native results refer to the frozen tested source. Final native unit checks:
1,714 pass, seven existing ignores. The native process suite passes 38 tests;
its pre-fixture-repair build identity and final-release crash coverage are
separately retained. Existing whole-repository documentation/architecture and
unrelated model-routing contract failures add no page-module findings.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
