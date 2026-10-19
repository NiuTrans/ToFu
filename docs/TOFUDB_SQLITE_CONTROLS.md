# Model-context SQLite transport controls

`benchmarks/tofudb_context_controls.py` owns controlled comparisons of the same
`turn.context.read` operation. It reuses the exact fixtures, complete Turn
equality and independent history-proof reconstruction from
[`tofudb_context_read.py`](../benchmarks/tofudb_context_read.py). These controls
extend the [native context-read measurements](TOFUDB_CONTEXT_READ.md).

## Problem, decision and alternatives

Problem: comparing embedded Rust TofuDB with a Python SQLite Sidecar combines
the database, implementation language, JSON conversion, process scheduling and
loopback transport. That comparison measures the deployed stacks; it cannot
attribute a win to the physical database.

Decision: retain four independently measured arms, all with the exact same
synthetic Agent history and continuation:

| Arm | Semantic implementation | Boundary in the timed call |
|---|---|---|
| `native-embedded` | Production Rust TofuDB | Python adapter, JSON/FFI, strict embedded authority |
| `sqlite-sidecar` | Production Python SQL authority | Production client, JSON, loopback RPC and child process |
| `sqlite-json` | The same Python SQL authority | In-process production dispatch, real wire JSON encode/decode |
| `sqlite-object` | The same Python SQL authority | In-process production dispatch returning Python objects |

The two additional arms instantiate the production `SQLiteBackend` and invoke
the production `_StorageHandler._dispatch` entry. They retain token checking,
operation registration, owner predicates, read transactions, command receipts,
the ordered writer, demand-created query-only readers and production caches.
They do not duplicate SQL, Turn projection, context selection or proof policy.
The JSON arm uses `storage.v1`'s actual frame encoder and its `orjson` decoder
for both request and response; sockets, frame-admission accounting and RPC
handler scheduling are omitted. The object arm deliberately omits the final
serialization boundary and provides a useful lower-cost SQL control.

Alternatives considered: a raw `SELECT` omits the semantic operation and is
not equivalent work; rewriting the operation in Rust on SQLite changes both
the implementation and its maintenance surface. That remains a valid future
experiment to isolate the language difference, but is not implemented or
claimed by this control. A new production in-process SQL backend is also not
introduced by this disposable benchmark adapter.

The difference between Sidecar and in-process results includes scheduling,
serialization, process-local allocator/cache behavior and client adaptation;
it is not a pure socket cost obtained by subtraction. The object/JSON pair
controls the added JSON boundary more narrowly. Rust versus Python remains
uncontrolled even when both databases execute inside the benchmark process.

## Evidence and persistence boundaries

Each process owns a fresh temporary authority on the requested filesystem.
SQLite fastpath relocation is disabled in all SQLite arms. Both in-process
controls use the same launch-time resource defaults as the production SQL
supervisor; the writer's actual `journal_mode=wal`, `synchronous=2` (FULL) and
`foreign_keys=1` are checked through a joined writer operation. Native uses
strict durability and matching executable/library build identities.

Every fixture is written, gracefully closed and reopened on the same files
before warmup or timing. Full Turns, evidence content, continuation, coverage,
target order and independently rebuilt history proof must survive reopening.
Every timed response is checked outside its request clock. A foreign owner
must be denied; native's scoped ABI returns `authority_scope_mismatch`, while
the SQL semantic read returns not-found. Neither path returns foreign data.
Graceful reopen proves persistence across that lifecycle only; this benchmark
does not prove abrupt-process or power-loss recovery.

Each eight-process cohort runs four arms forward then in reverse order.
The summary refuses differing fixtures, logical Turn digests, source witnesses,
durability, missing controls, overlapping measurement processes, reused PIDs,
incomplete samples, missing owner/reopen/cleanup evidence and differing native
artifacts. Attempt UUIDs are compared exactly within each run; only cross-run
logical digests substitute deterministic ranks for generated Attempt IDs.
All raw nanosecond samples and source-file hashes are retained.

The measured interval is a single warm storage operation. Full rebuild means
returning the complete selected history from a warm, reopened authority; it
does not mean cold-process startup. Caller RSS high-water excludes the Sidecar
child and is diagnostic only. This comparison grants no whole-Agent speed,
capacity-SLO, memory, power-loss or universal physical-engine claim.

## Running and verification

```sh
python benchmarks/tofudb_context_controls.py --arm sqlite-object \
  --history 64 --payload-bytes 65536 --count 200 --output /scratch/object.json
python benchmarks/tofudb_context_controls.py --arm sqlite-json \
  --history 64 --payload-bytes 65536 --count 200 --output /scratch/json.json
python benchmarks/tofudb_context_controls.py --arm sqlite-sidecar \
  --history 64 --payload-bytes 65536 --count 200 --output /scratch/sidecar.json
python benchmarks/tofudb_context_controls.py --arm native-embedded \
  --binary /pinned/build/tofu-db --history 64 --payload-bytes 65536 \
  --count 200 --output /scratch/native.json
```

Execute these four arms in forward/reverse order in eight fresh processes with
distinct output paths, then pass all eight JSON paths to `--summarize` and an
`--output` path. Optional `--directory` keeps each disposable authority on the
selected volume. `--pattern unique` uses fixed per-Turn seeds and high-entropy
evidence. Native executable and library must be a matching source/schema pair;
a newer SDK must not silently load an older schema's frozen binary.

Verification: `tests/tofudb/test_context_controls.py` exercises actual SQL
authentication, owner denial, exact-once receipts, WAL/FULL, cleanup and reopen
for both new arms, and rejects mutated comparison evidence.
`tests/tofudb/test_context_read_measure.py` fences changed or truncated content,
wrong coverage, proof changes and incomplete pages.

## Measured controls on 2026-10-19

The native comparison source is
`7d2a42adee860fa774f2815c17f00e7310889072f689458bb5a589f62df494f1`
(repository baseline `ac1df53948c486b18e1d3645b44b42927282f937`, schema 68),
built with Thin LTO, 16 codegen units and embedded mimalloc. Python is 3.12.13
and SQLite is 3.53.4, on the same shared Linux host with 63 affinity CPUs.
Sibling optimization agents paused compilation and tests during the formal
measurement interval. This does not assert that the whole host was otherwise
idle. The two in-process SQL controls both observed 12 read-pool slots,
64 MiB writer cache and 128 MiB projection cache from production defaults.

Four fixtures, eight sequential fresh processes per fixture, and 200 samples
per read mode per process produced **12,800 exactly validated responses**.
All 32 processes exited successfully and all four cohort summaries passed
their comparability checks. Each cell below pools two fresh runs (400 samples)
and reports P50 milliseconds; lower is better.

| Fixture / read | Native embedded | SQL Sidecar | SQL object | SQL JSON |
|---|---:|---:|---:|---:|
| XFS, 64 + 2 Turns, 1 KiB plain, full | 0.904 | 4.379 | 3.400 | 4.241 |
| Same fixture, two-Turn continuation | 0.173 | 1.032 | 0.675 | 0.775 |
| XFS, 64 + 2 Turns, 64 KiB escaped, full | 29.799 | 47.671 | **22.220** | 40.603 |
| Same fixture, two-Turn continuation | 1.226 | 2.418 | 1.597 | 2.096 |
| XFS, 512 + 2 Turns, 16 KiB high-entropy, full | 33.636 | 84.643 | 60.755 | 80.356 |
| Same fixture, two-Turn continuation | 0.868 | 3.595 | 3.074 | 3.293 |
| DolphinFS, 64 + 2 Turns, 64 KiB escaped, full | 29.623 | 47.922 | **22.247** | 41.004 |
| Same fixture, two-Turn continuation | 1.221 | 2.421 | 1.587 | 2.089 |

The negative control matters: on large escaped evidence, native full reads are
34.1% slower than in-process SQL objects on XFS and 33.2% slower on DolphinFS.
That counterexample also holds at P99: native/SQL object are 38.182/24.083 ms
on XFS and 36.722/23.242 ms on DolphinFS. Universal superiority over SQLite is
therefore contradicted by this measured semantic workload.

Native remains 1.36–4.69 times as fast as in-process SQL with its JSON boundary
across these eight read cases, and 1.60–5.95 times as fast as the deployed SQL
Sidecar. Small evidence, high-entropy long histories and every measured
continuation also beat the SQL object control. These are workload-specific
storage-path results. They support retaining and improving the native read
path, while leaving the necessity of its physical engine unproven because
the Rust/Python implementation difference remains uncontrolled.

Raw samples, each cohort's source manifests and summaries, and an index of
their hashes are retained under ignored
`data/benchmarks/tofudb-sqlite-controls-20261019/formal/`; `matrix.json` is the
entrypoint and `../evidence.json` distinguishes formal results from diagnostics.
Reproduction scripts and the baseline build manifest are in `../reproduction/`.
The first partial matrix recorded the initial Sidecar PID before reopening;
it was excluded and rerun in full. The final collector records both process
generations, verifies the initial child is retired at reopen and verifies the
actual timed child is retired at final close. Its real Sidecar lifecycle test
and the other measurement tests all pass (six total).
