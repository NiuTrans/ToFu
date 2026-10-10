# Tofu-DB recency directory (sidebar cardinality path)

**Status:** IMPLEMENTED 2026-09-19 · **Context:** finding S3 in
`STORAGE_PERFORMANCE_CHARTER.md` — `conversation.list` (updated-desc) is the
last workload family that loses to the SQLite sidecar (0.55×/0.45×).

## Problem (quantified on this checkout)

For owners with ≤1,000 live conversations, `conversation.list`
(updated-desc) serves every sidebar page by reading **all** live headers
(several KB of JSON each) and sorting them in memory — 3.97 ms at the
suite's ~250-conversation authority vs SQLite's indexed `LIMIT 50` at
1.60 ms. The read-all+sort exists as a deliberate defense against
updated-index OCC tombstone churn (every turn append rekeys the index).
Sidebar cost therefore follows live cardinality, not page size, and every
turn append makes the next sidebar page slower.

## Decision

One **recency directory** per owner: a single derived document holding the
newest 128 `(updated_at_ms, conversation_id)` entries, sorted exactly like
the legacy comparator (`updated_at` desc, `id` desc), maintained **in the
same transaction** as every recency-affecting mutation.

* **Coverage-gated read path.** `conversation.list` uses the directory only
  when the request is updated-descending, carries no filters (ids /
  project / title / time bounds), the directory state is `Complete`, its
  recorded live count equals the count key, and `limit ≤ 128`. The page's
  headers are then point-hydrated and every hydrated `updated_at` must
  equal the directory's claim — any disagreement returns the exact legacy
  answer via the read-all+sort path. The directory is a derived index,
  never an authority.
* **Bounded inline value.** Entries are `(u64 timestamp)(u8 len)(id bytes)`
  ≈24 B each; 128 entries + 15 B header ≈ 3.1 KB — under the 8 KiB entity
  inline bound, so maintenance rides the write overlay's per-commit delta
  like any other mutation (WAL/append grows from ≈6.1 KiB to ≈9 KiB, still
  ≥5× under the legacy ≈48 KiB).
* **Maintenance funnels, not call sites.** Upserts hook `store_header`
  (every header write: turn settlement, settings, relink, clone parent,
  create) and `restore`; removals hook `delete`/`purge`. All four funnels
  already maintain the count key and the updated index in the same
  transaction, so the directory cannot drift from the paths that matter.
* **Lazy bounded backfill.** State machine `Absent → Backfilling(cursor) →
  Complete` (mirroring the activity-candidate index): the first
  recency-affecting write on an old authority starts a backfill that
  ingests the document namespace in key order, ≤64 rows per commit, never
  in the foreground read path. Until `Complete`, lists take the legacy
  path at all times.
* **Eviction keeps recency.** An upsert re-sorts and truncates at 128; a
  conversation jumping from beyond the tail enters at its sorted position.
  Owners above 128 live conversations keep exact behavior for deep pages
  through the fallback.

## Parallel preparation (2026-09-30)

Problem: preparing the full shared directory value on each snapshot makes
independent conversation writes conflict on one physical index record.
Decision: preparation retains bounded, typed upsert/remove mutations. After
business OCC validation, the exclusive commit stage applies them to the current
directory, in the same WAL transaction as their headers. Reading the index
inside a prepared transaction first applies its pending updates on that snapshot;
the resulting index read witnesses still conflict if another writer changes it.
Creation also defers the owner count and activity-candidate effects. Counts and
activity-index reads flush pending effects against their original snapshot;
commit applies all creation metadata before recency backfill, so independent
creates do not conflict on a shared count and multi-create batches stay exact.
The claim/header/count/activity/directory changes remain one atomic transaction.
Private blob headers publish only after business validation and before index
backfill can read them, under the same exclusive commit hold. Mutation buffers
share the transaction memory budget and drop on cancellation or conflict.

Alternatives rejected: ignoring the directory version loses independent updates;
asynchronous index maintenance weakens same-transaction visibility. Tests cover
concurrent disjoint headers, same-header conflicts, read-your-writes predicates,
and old-or-new header/index recovery at every injected commit I/O fault.

## Invariants (certification surface)

1. A list served from the directory is byte-identical to the legacy
   answer for the same request, or it is not served at all (fallback).
2. Directory maintenance commits atomically with the mutation it indexes;
   a crash exposes old-or-new for both, never a half-maintained directory.
3. Backfill is resumable, bounded per commit, and idempotent; it never
   blocks or fails a foreground write.
4. The fallback detects count drift, state regressions, malformed values,
   and per-page timestamp disagreement without operator action.

## Alternatives considered and rejected

* **Reverse-scan the updated index.** Still reads one stored envelope per
  row to hydrate the page, pays the tombstone churn the read-all+sort
  defends against, and the entity scan is forward-only.
* **Per-turn namespace consolidation** (charter alternative (b) for the
  write path). A physical-schema migration of every conversation domain;
  orthogonal, and no longer blocks this fix now that the overlay makes
  per-commit extra writes cheap.
* **Segmented overflow directory.** Covers pages beyond 128 without the
  fallback, at the cost of a segment chain and its consistency surface.
  Deep pages are a cold path; the exact legacy fallback covers them.

## Measured (2026-09-19, this checkout)

`scripts/tofudb_perf_compare.py --scale small --retry-backpressure`,
`TOFU_DB_WRITE_OVERLAY=1`, same binary on both volume classes. Reports:
`data/benchmarks/recency-s3-2026-09-19-fuse-on.json` (+ local twin).

| workload | before (read-all+sort) | after (directory) | SQLite sidecar |
|---|---|---|---|
| sidebar_list_page p50/p99 | 2.35–2.42 / 3.3–4.4 ms | **0.72–0.73 / ~1.0 ms** | 1.27 ms |
| sidebar_with_history p50/p99 | 3.97–4.07 / 5.9–7.3 ms | **0.75–0.76 / 1.2–1.9 ms** | 1.60 ms |
| turn_append_burst p50 (hook cost) | 0.58 local / 0.77 FUSE | 0.60 local / 0.84 FUSE | — |

The sidebar family moved from 0.55×/0.45× against the sidecar to **1.7×/2.1×
faster**, identical on FUSE and local volumes; the per-append maintenance
cost stays inside noise (≤0.07 ms on FUSE). Certification: 7 recency tests
(codec, caps, multi-batch backfill, mutation-script equivalence, count-drift
and deep-owner fallbacks, purge/recreate), the full lib matrix green except
one unrelated in-flight engine-adoption test owned by a concurrent worker,
and the native differential gate 178/178 with the overlay both off and on.
