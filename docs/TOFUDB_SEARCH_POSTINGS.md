# Native search posting representation

The authority is `contracts/conversation_search_v2.json::nativeProjection`.
`turn_search_projection.rs` owns generation publication and owner accounting;
`turn_search_generation_dictionary.rs` owns generation numbering;
`turn_search_posting_codec.rs` owns canonical delta-varint values;
`turn_search_query.rs` owns candidate selection and exact verification.

## Problem

Format 2 rewrote a sorted list of 32-byte generation digests for every gram.
The measured format-2 regression fixture appends 32 KiB seeded hex text per
conversation using real 4096-character chunks and eight-document pages.
The 40th conversation failed with `transaction references too many blocks`:
2319 new-page stores exceeded the unchanged 2048-reference transaction bound.
The selected physical tree before failure contained 4382 posting rows and
5,475,516 posting-value bytes. This is a capacity failure, not a latency rank.

## Decision

Format 4 maps full generation digests to monotonic, owner-scoped 64-bit IDs.
Assignment and allocator state commit with the invisible build. Posting values
hold sorted IDs as canonical positive delta varints; invalid ordering,
overflow, noncanonical values and truncated lists are errors, never partial
candidate sets. Candidate selection resolves published generations through
the dictionary; exact title, identity and text verification still determines
matches. Exclusion hints retain their conservative fingerprint semantics.

Generation retirement removes its assignment in the same transaction as the
existing content and posting retirement. Committed IDs are never reused:
bounded retraction can leave charged postings containing an old ID, which
must never identify a future generation. Dictionary rows and allocator bytes
are charged to owner usage. After all ordinary generation state is removed,
the owner retains the exactly charged 12-byte allocator record. Owner-wide
format/source replacement retires both allocator and all posting ranges.

Versioned replacement accepts known old format markers, retires only the
selected owner's projection ranges, and retains the existing pending
activation until source backfill acknowledgement. The source authority and
its dirty-token protocol remain the sole durable source of conversation data.

## Alternatives considered

Increasing transaction references would hide amplification and enlarge every
publication and recovery envelope. Truncating digests in postings would require
collision-safe deletion; query-only false-positive fingerprints do not supply
that proof. Independent posting rows avoid growing values but repeat keys and
can touch many scattered leaves. Splitting posting publication introduces
additional durable progress and rollback states. Stable numbering first removes
most repeated identity bytes without adding a multi-transaction publisher.

## Verification and limits

On the same fixture before conversation 40, format 4 stored 183,982 posting
value bytes, plus dictionary metadata, and the append stored 228 new pages.
The regression reaches 128 conversations and checks READY-index queries,
generation removal and reopen. Neighboring tests exercise owner isolation,
old-format replacement, full-digest identities with colliding 64-bit query
fingerprints, budget rejection, aborted allocation, ID exhaustion and
publication fault recovery. These measurements cover this native fixture;
full harness latency, RSS, reopen cost and SQLite comparison require separate
build-attested measurements. The existing common-gram universal marker,
emission/removal bounds and owner byte budget still apply.


Current prefix-codec validation: the format-2 reference remained indexing
after 600 seconds with 32 rebuilt conversations and 17 failures. The
search-only format-4 build completed all 1,000 identities, three exact
body queries, content/events and idle/reopen oracles without errors. Its
independent native suite passed 1,762 tests (7 ignored); real process
SIGKILL/reopen also passed. The approximately 34-second index check is
a functional workload observation, not a universal or paired speed claim.
Compact decoded leaves remain an independent, unadmitted experiment.

## Physical capacity and reclamation

Problem: logical owner accounting does not bound unreachable physical pages or
filesystem inode consumption. Decision: `turn_search_runtime.rs` owns the public
index lifecycle; the opened codec remains private to its implementation and
query module. Before each write, `search_volume.rs` checks independently known
byte and inode capacity through the VFS. Geometry derives the shared reserve
(fraction, floors and caps from `nativeProjection.volumeGuard`), capped at ten
percent of small volumes; unknown inode accounting never hides known byte
exhaustion. The reserve is an admission margin, not a corpus-size cap.

StorageFull defers index creation/open while source authority stays available.
Queries report the existing retryable storage-unavailable error; maintenance
retries opening and indexing without losing exact dirty tokens. Existing
readable indexes remain readable under pressure. Native corruption recovery,
source UUID/owner admission and publication fences remain the original owners.
Native path validation resolves the nearest existing ancestor without creating
parents and retains authority-overlap and dangling-symlink fences. A fresh
default directory is exercised through the complete supervised launch.
Launch readiness distinguishes configured from actually opened. Python settings
validate a proposed base with the same generated policy; Python launchers do
not impose a search-capacity gate on source availability.

The index mounts the [shared online collector](TOFUDB_ONLINE_GC.md), with its
own marks, pins, relink fences and capacity-bounded scratch. Alternatives
rejected: a startup-only floor misses subsequent growth; stopping the database
for an optional index broadens the outage; moving indexes implicitly changes
operator intent; deleting files without reachability proof risks live results.
