# Long-horizon model-context storage

This contract owns native `turn.context.read` materialization in
`packages/tofu-db/src/turn/model_context.rs` and its measurement entrypoint,
`benchmarks/tofudb_context_read.py`. Context semantics and continuation
invalidation remain in [context compaction](CONTEXT_COMPACTION.md) and
[`model_context_v1.schema.json`](../contracts/model_context_v1.schema.json).

## Product purpose and burden of proof

TofuDB earns its maintenance cost through measured improvements to durable
Agent execution: context reconstruction, incremental continuation, committed
progress, and recovery as retained history grows. General SQL superiority is
not its contract. SQLite already supplies transactions, WAL recovery and
snapshot isolation; Agent checkpoints and effect journals can also be built
on it. These features alone cannot justify a separate physical engine.
See SQLite's [WAL](https://www.sqlite.org/wal.html) and
[isolation](https://www.sqlite.org/isolation.html) documentation.

The falsifiable case for the native engine is eliminating repeated work across
the storage/application boundary while preserving the same public operation,
owner checks, exact evidence and durable acknowledgements. Compare both the
production SQLite Sidecar and direct SQL when applicable; a Sidecar win is a
whole storage-stack result, not proof that the native tree beats SQLite's
B-tree. Time saved in a storage call is not a measured increase in completed
Agent tasks. The [performance charter](STORAGE_PERFORMANCE_CHARTER.md) retains
separate correctness, capacity, recovery, resource and latency requirements.

## Owned context materialization

Problem: context reads obtained a parsed Turn through the common document
reader, serialized it, parsed it again and cloned its tool results into the
response. The target Turn was also read twice. Large tool evidence therefore
incurred repeated work even after continuation had selected the right suffix.

Decision: share the ordinary owner-scoped public-Turn constructor with `get`.
The context reader consumes its private Value and counts its exact canonical
serialized length without allocating another full JSON buffer. The existing
canonical-depth checker admits reuse only for parser fixed points. Reserved
Serde markers and excessive depth retain serialize/parse normalization and
failure. Exact byte-limit refusal still precedes parser errors. This local
evidence never substitutes for source, scope, blob or
projection-revision verification.

The target belongs to the same transaction snapshot and is moved into the
selected suffix once. Each page retains the original byte accounting and
cursor. Complete response construction moves Turns and the continuation;
`EncodedResponse` supplies producer-owned canonical/depth evidence through the
existing IR slot. Mutation, overwrite and alternate result selection retain
their existing proof invalidation. No query cache or durable index is added.
Header proof scans remain O(history headers); rebuilding uncached context
necessarily reads all selected evidence. This change makes neither path O(1).

Alternatives considered: caching parsed histories adds ownership, invalidation
and residency costs while leaving cold reconstruction unchanged; skipping
source or terminal-witness checks weakens recovery; returning fewer tool
results changes model input. None is needed to remove private copies.

Resources: no new worker, queue, timer, cache or persistent record. Parsed
objects belong to the request; canonical sizing uses one stack counter and
the existing serializer. The target allocation lives until selected or request
completion. Normalization tests prove that an ordinary large String retains
its allocation; actual process RSS remains a separate measurement.

## Verification and reproduction

- `response_json::tests` compares byte lengths, floats, Unicode, reserved
  markers and depth acceptance with the original serde_json round trip.
- `tests/model_routing/test_model_context_read.py` checks native/SQL parity,
  complete multi-page histories, owner denial and changed-proof restart.
- `tests/tofudb/test_context_semantic_revision.py` covers display/model edits,
  terminal witnesses, continuation invalidation and reopen on real authorities.
- `tests/tofudb/test_context_read_measure.py` rejects wrong content, incomplete
  pages, changed coverage and invalid proof evidence in measurement results.

Run fresh processes in old/native/SQLite/SQLite/native/old order, using matching
release executable/library pairs and the same fixture and measurement script:

```sh
python3 benchmarks/tofudb_context_read.py --engine tofudb \
  --binary /pinned/build/tofu-db --history 64 --payload-bytes 65536 \
  --count 200 --output /scratch/native.json
python3 benchmarks/tofudb_context_read.py --engine sqlite \
  --history 64 --payload-bytes 65536 --count 200 --output /scratch/sqlite.json
```

Each request returns exact ordered Turns and tool content, continuation and
independently reconstructed history proof. Attempt UUIDs are checked exactly
within each run; only the cross-run logical digest replaces them with ranks.
Both paths use their production storage contracts with strict/native or
WAL/FULL durability. Every timed call is validated outside its request clock.
The caller RSS high-water includes embedded native but excludes the SQL child;
it must not establish cross-backend memory superiority. Rebuild means a full
context read on a warm authority, not a cold process or crash recovery.

## Measured operation costs

2026-10-19, same shared Linux host with 63 affinity CPUs, Python 3.12.13 and
SQLite 3.53.4. Both native versions use optimized builds with Thin LTO,
16 codegen units and embedded mimalloc. Each row pools two fresh processes
with 200 timed requests each; order is old/new/SQLite/SQLite/new/old. All
9,600 measured responses match their exact content and independent proofs.
Results are P50 milliseconds, not tail-latency or whole-task acceptance.

| Fixture / read | Old native | New native | SQLite Sidecar |
|---|---:|---:|---:|
| XFS, 64 + 2 Turns, 1 KiB plain evidence, full rebuild | 1.513 | 0.863 | 4.517 |
| Same fixture, two-Turn continuation | 0.204 | 0.160 | 1.054 |
| XFS, 64 + 2 Turns, 64 KiB escaped evidence, full rebuild | 56.592 | 28.700 | 47.063 |
| Same fixture, two-Turn continuation | 2.567 | 1.204 | 2.363 |
| XFS, 512 + 2 Turns, 16 KiB seeded high-entropy evidence, full rebuild | 42.210 | 32.104 | 79.632 |
| Same fixture, two-Turn continuation | 0.957 | 0.870 | 3.526 |
| DolphinFS (`fuse.bgfuse`), 64 + 2 Turns, 64 KiB escaped evidence, full rebuild | 56.742 | 28.536 | 47.615 |
| Same fixture, two-Turn continuation | 2.578 | 1.206 | 2.379 |

The new path is 1.10–2.14 times as fast as its native predecessor and
1.64–6.59 times as fast as the existing SQLite Sidecar in these fixtures.
Both a long high-entropy history and small evidence remain represented;
the larger escaped-string improvement must not be generalized to every read.
There is no raw-SQLite `turn.context.read` equivalent in this measurement.
These results establish the value of this native semantic read optimization;
they do not isolate the contribution of the physical engine from Rust,
transport or the SQL adapter. A comparable Rust semantic layer backed by
SQLite remains a legitimate alternative for that separate decision.

The subsequent [four-arm SQLite controls](TOFUDB_SQLITE_CONTROLS.md) retain
the production SQL semantic implementation while separately removing its
process and JSON boundaries. They confirm a counterexample: full reads of
large escaped evidence are 34.1% slower in native than in-process SQL objects
on XFS, and 33.2% slower on DolphinFS. Long high-entropy histories and all
measured continuations remain faster in native. These controls supersede any
inference of universal superiority from the Sidecar-only table above; Rust
versus Python remains an uncontrolled variable.

The executable retains every request duration, exact fixture/normalized-Turn
digests and executable/library source identities. Old native source is
`946ed8fd858203aa72477fd98e91ad78f49983a5c04ec13dbf6dae9dedb2bb1d`; new source is
`5b4efd571420f80f04cfe570c9949c4154168de0ddae8bc0429ad07ffb155ca5`.
Old code is the parent of the isolated optimization commit; rebuild both
with the same compiler/profile and use the retained measurement entrypoint.

## Rejected direct byte-count alternative

Problem: `normalize_value_with_size` still invokes the JSON formatter while
counting into a bounded sink, before the final response is serialized. Static
inspection suggested avoidable escape-formatting work, but counting source
traversals does not establish a latency benefit.

Alternative tested: count compact punctuation, UTF-8 bytes and exact JSON escape
expansion directly; retain Serde for Number spelling, checked size accumulation,
the canonical-depth/marker gate and serialize/limit/parse fallback ordering.
All 15 experimental Rust tests passed, including every ASCII control, Unicode
boundaries, exact N/N-1 byte limits, floats, overflow and deep/marker errors.
All 43 selected SQLite/native semantic tests passed, including owner refusal,
multi-page history, continuation invalidation and reopen. These checks admitted
the experiment for measurement, not adoption.

Decision: reject the production change. Three fresh-process ABBA cohorts used
the retained `tofudb_context_controls.py` exact-content/history-proof oracle,
strict durability and graceful authority reopen. Each leg measured 200 full
and 200 continuation reads: 12 legs and 4,800 verified responses in 146 seconds.
All other coordinated compilation/testing stopped during this window. Both
builds used Thin LTO, 16 codegen units and embedded mimalloc on the same XFS host.
The pooled full-read clocks were:

| History and evidence per Turn | Existing P50 / P99, ms | Candidate P50 / P99, ms | P50 change |
|---|---:|---:|---:|
| 64 × 64 KiB escaped | 31.080 / 46.269 | 31.635 / 41.746 | +1.79% |
| 512 × 16 KiB high entropy | 36.942 / 55.483 | 38.011 / 60.509 | +2.90% |
| 64 × 1 KiB plain | 0.941 / 1.549 | 0.981 / 1.292 | +4.31% |

Each fixture additionally appended two Turns for continuation reads. Pooled
small-continuation P50 was 24.3% lower, but its P99 worsened 7.5%. Its baseline
process medians varied from 0.178 to 0.258 ms, versus 0.187 to 0.188 ms for the
candidate; this establishes no repeatable advantage. The larger fixtures'
continuation medians also worsened. Mixed observations do not override
the full-read regressions. No new SQLite ranking is claimed from historical
controls, and this experiment does not attribute costs to physical engines.
The existing Serde counter and its tests remain unchanged; candidate-only
oracle tests stay with the rejected patch because they found no baseline bug.

Baseline engine source was
`7d2a42adee860fa774f2815c17f00e7310889072f689458bb5a589f62df494f1`;
rejected candidate source was
`812ddd1953cf2b28357050da2d32d58d3f8bc8a78b265d836c57ccabc74059f6`.
The base commit `093a3e6cbdb6798f12f7f30ccdf808448a66cf15` was independently
verified to contain the same engine inputs as the frozen baseline. Raw samples,
artifact manifests, the candidate patch, exact reproduction driver and validation
logs are retained locally under `data/benchmarks/tofudb-json-size-20261019/`.

## Adverse observations and remaining gates

Caller peak RSS did not improve uniformly. In the escaped-evidence XFS runs,
old/new high-water ranges were 159.5–160.5 / 164.3–173.4 MiB; on DolphinFS,
160.3–164.2 / 172.8–186.3 MiB. These include fixture construction, native
maintenance, allocator retention and Python response decoding. They are real
adverse observations, not proof of a leak or an isolated allocation cause;
the allocation-ownership test cannot erase them. No memory-win claim is made.
A separate fresh-process ABBA phase probe over 200 full reads observed final
request RSS of 133.1–134.7 MiB old and 119.0–126.2 MiB new. Both remained at
12 threads during reads and returned to one after close; closed RSS was
72.3–72.9 MiB. This bounded lifecycle probe found no continuing growth between
reads 100 and 200, but its overlapping peak ranges neither erase the earlier
high-water regressions nor establish a sustained-soak result.
The neighboring operation smoke also retained the raw-SQLite point-read gap:
native 0.0204 ms versus direct SQLite 0.0060 ms P50, with narrower raw semantics.

A separate two-repeat, 1,000-conversation production-client comparison used
four 8 KiB stream rounds at one-second arrivals, strict durability, retained
events and actual daemon/Sidecar SIGKILL while running and after completion.
In these runs, `--native-client application` selects the production
`StorageV2Client`; the driver invokes `turns.record_task_event` and then
`client.command('turn.event.record')`. This is simulated-provider pressure
through the production executor, Turn and storage path. It does not exercise
the complete manager delivery coordinator, progress coalescing or independent
`StorageEventBatcher`. In `lib/tasks_pkg/manager/_events.py`, the standalone
append is intentionally guarded by `outcome != 'carried'`; mechanically adding
a batcher would not establish equivalent delivery semantics. The measured
`phase` event is outside the progress-coalescing set (`delta`, `program_output`,
`tool_progress`), and its atomic carried write also bypasses the standalone
batcher in production. This scope limit is not evidence of missing phase-event
batching.

Both backends recovered all 1,000 running and completed conversations, exact
content and exact event replay in both repeats; offline integrity passed.
Both nevertheless failed the existing interactive-latency budgets:

| P99 stage, seconds | Native range | SQLite range | Budget |
|---|---:|---:|---:|
| Submit to running | 0.046–0.051 | 2.398–2.553 | 5 |
| Scheduled stream acknowledgement | 1.849–1.857 | 3.564–3.893 | 1 |
| Settlement | 5.341–5.980 | 5.028–5.581 | 5 |

Native settlement was slower than SQLite in the second repeat. These results
do not certify 1,000-conversation latency, complete Harness performance,
real-task success rates, real-provider capacity, sustained soak, or
browser-visible responsiveness. The context optimization is not a
change to the measured stream-write path. Reproduce the separate capacity
comparison with `benchmarks/conversation_compare.py --native-client application
--conversations 1000 --rounds 4 --chunk-bytes 8192 --repeats 2
--carry-stream-events --binary /pinned/build/tofu-db --directory /tmp
--output /scratch/capacity.json`; its nonzero acceptance is retained.
