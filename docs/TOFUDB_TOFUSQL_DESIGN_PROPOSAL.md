# TofuSQL design proposal (draft)

**Status:** P0 IMPLEMENTED 2026-09-09 · P1 IMPLEMENTED 2026-09-10 · P2 IMPLEMENTED 2026-09-09 · **Author:** differential-test worker ·
**Context:** `docs/STORAGE.md` lists TofuSQL as remaining release work for
the tofu-db launch; this draft frames scope, boundaries, and a phased plan
so the first implementation slice can be sized and reviewed. The P0 slice
below is now code: `packages/tofu-db/src/tofusql.rs`, the catalog contract
`contracts/tofusql_catalog_v1.json` + `scripts/gen_tofusql_catalog.py`, the
`tofusql.query` storage.v2 operation, and the `tofu-db sql` operator CLI.

## Problem

Every tofu-db domain today answers questions only through its compiled
storage.v2 operations. Anything not anticipated by an operation — ad-hoc
inspection during incidents, migration verification, differential-gate
debugging, operator forensics — requires hand-written tooling that
re-encodes physical layout knowledge (namespaces, key encodings, blob
indirection, index families). That tooling rots against the generated IR
contract and tempts bypasses around the authority's invariants
(`AGENTS.md`: never expose physical records as the application contract).

TofuSQL is the proposed **read-only query surface** over a live tofu-db
authority: a SQL-subset front that resolves the logical entity model
(documents, index namespaces, streams) so tooling and operators can ask
bounded questions without new Rust per domain.

## Non-goals

* **No writes, ever.** The write path remains storage.v2 operations
  compiled to Transaction IR with owner checks, receipts, outbox effects,
  and commit-fault certification. TofuSQL carries no receipt/outbox
  effects and cannot mutate.
* **Not an application query path.** Request handlers keep calling
  declared operations; SQL text never appears in the serving path. This
  preserves the enterprise seam (repository/ownership layer, default
  deny) and keeps `AGENTS.md`'s "no SQL dialect leaks outside the storage
  layer" intact — TofuSQL lives *inside* the storage layer, beside the
  engine.
* **Not PostgreSQL wire compatibility** in any initial phase.
* **Not a schema/DDL surface.** Physical layout stays owned by
  `contracts/tofudb_ir_v1.json` and the generated IR.

## Query model

* **Catalog derived, not declared.** Namespaces become tables from the
  generated IR contract (`generated_tofudb_ir.rs`): one table per
  document namespace (`turn_documents`, `conversation_headers`,
  `plugin_manifest_documents`, …) with key-part columns decoded by the
  same `push_text_key`/big-endian encodings the engine uses, a `version`
  column, and a `body` JSON column. Index namespaces surface as separate
  narrow tables with their key-part columns plus the target identity.
  Streams (event families) surface as append-only tables with cursor,
  timestamp, type, and payload columns.
* **Snapshot isolation.** Each statement executes against one MVCC
  snapshot — the same snapshot semantics operations already use — so a
  query never observes a half-committed transaction and never blocks the
  single writer.
* **Dialect.** SQLite-flavored expression subset: `SELECT` with
  `WHERE`/`ORDER BY`/`LIMIT`, `json_extract` family over `body`,
  equality/range predicates on key-part columns, `IN`, `LIKE` with
  declared `ESCAPE` (the record.list lesson: wildcards are never
  implicit). No `JOIN` in phase P0–P1; P2 adds joins only over documented
  identity columns with both sides key-prefixed (implemented, see
  phasing).
* **Identity is explicit.** Like operations, every query carries
  `tenant_id` + `owner_user_id`; rows are owner-scoped by construction.
  Cross-owner/tenant-global namespaces require the same internal-scope
  marker operations use (e.g. scheduler global poll feed). There is no
  anonymous "root" query.

## Bounded execution (AGENTS.md constraint 3 applies)

Every statement runs under hard ceilings, consistent with the engine's
existing style:

* entity reads per statement ≤ 1,000 (the Entity boundary page rule);
* materialized rows ≤ 2,000 and aggregate response ≤ 8 MiB
  (`MAX_TRANSACTION_IR_LITERAL_BYTES` parity);
* blob materialization only for explicitly selected `body` columns, under
  the same 64 MiB hydration ceiling used by turn compaction;
* wall-clock statement budget with cooperative cancellation; a query
  that exceeds any bound fails with resource exhaustion instead of
  returning a partial view (the established fail-closed rule);
* a small bounded worker pool (default 1–2) so inspection can never
  starve serving; queries queue rather than preempt.

## Execution engine options

**Option A — embed SQLite as the expression evaluator.** Scan the
relevant namespace pages into an in-memory SQLite temp table (bounded by
the ceilings above), then execute the SQL text with SQLite itself.

* Pros: dialect, JSON functions, and planner come free; implementation is
  a bounded scan + adapter; zero home-grown parser risk.
* Cons: plan quality irrelevant (inputs pre-bounded); ingress scan must
  be pushed down by our own front-end or every query degrades to a full
  namespace scan — so a minimal predicate-pushdown front (key-prefix
  extraction) is still required.

**Option B — hand-rolled parser/planner.** Full control of pushdown and
bounds; high ongoing cost, new parser is exactly the class of code this
project avoids owning.

**Recommendation: Option A with a key-prefix pushdown front.** Extract
equality/range predicates on leading key-part columns before evaluation,
translate them to `EntityKey::prefix_range` scans, fail statements whose
selective predicates cannot be pushed down when the unbounded scan would
exceed the read ceiling. This keeps TofuSQL honest without owning a
planner.

## Transport

A new `tofusql.query` storage.v2 operation (query kind, maintenance flag,
owner scope in the envelope) so authentication, admission, priority, and
accounting reuse the existing wire path — no new socket, no new
credential. The daemon CLI gains `tofu-db sql` for operator use, speaking
that same operation.

## Phasing

* **P0 — single-namespace scans (implemented 2026-09-09).** Derived catalog
  for document namespaces, key-prefix pushdown, `LIMIT`, statement bounds,
  CLI. Delivered as `src/tofusql.rs` + the `tofusql.query` operation +
  `tofu-db sql`, verified by owner-isolation, authorizer, range-pushdown,
  whole-namespace-scan, and no-join tests.
  First consumer: `debug/inspect_conversation.py` resolves conversation,
  turn, and event state through TofuSQL instead of layout-aware reads
  (also closing the fastpath-authority resolution gap noted 2026-08-27,
  since the daemon answers through its live front).
* **P1 — index and stream tables (implemented 2026-09-10).** The
  conversation updated/trash-age/activity-candidate indexes, the Turn lane
  ordinal/lane compaction/activity/updated indexes and turn tombstones, and
  the task-event stream family's physical index namespace are queryable as
  catalog tables (`contracts/tofusql_catalog_v1.json` catalog version 2).
  Trailing integer-suffixed and order-inverted key parts use the new
  `orderedBinary` key encoding (opaque bytes, BLOB column, hex in JSON
  responses) with equality pushdown through the `tofusql_key()` hex table
  function; range predicates on `orderedBinary` parts fail closed. Index and
  stream rows surface under the unified value codec: raw stored values decode
  as the JSON `body` when they parse, while versioned-document envelopes
  hydrate through the same bounded path P0 document tables use. Migration
  verification reports (count/digest per domain) become SQL instead of
  bespoke code in the tofu-db migrator (currently on the
  `tofudb/integrate` branch).
* **P2 — constrained joins (implemented 2026-09-09).** Catalog version 3
  declares `identityColumns` per table (a documented single-field JSON
  path out of the decoded body, surfaced as a nullable TEXT column) and
  `joinEdges` between tables; both are validated and rendered by the
  generator (`scripts/gen_tofusql_catalog.py`). A join-aware statement
  analyzer accepts exactly one bare or `INNER JOIN` between two distinct
  catalog tables when every top-level `ON` term is an equality between
  qualified columns matching a declared edge in either orientation;
  self-joins, a third table, comma `FROM` lists, set operations, outer
  joins, subqueries, and CTEs fail closed, and the read-only authorizer
  still pins at most two catalog tables per statement. Execution enforces
  the constraint: every cursor of a join statement must scan one of the
  two validated tables with an equality-pinned leading key part, and the
  1,000-entity read ceiling is charged statement-wide across the outer
  scan and all per-row inner scans. Declared edges cover
  `conversation_turns ⋈ generation_attempts` on `conversation_id` and on
  the attempt document's `turnId` identity, and
  `conversation_turns ⋈ turn_tombstones` on `conversation_id`.

Each phase lands behind the differential/certification gates already
required for launch work: deterministic fault injection for the scan
front, owner-isolation tests, and a resource-budget test per
`AGENTS.md`'s personal-computer constraint.

## Open questions for review

1. Does any *serving-path* consumer have a legitimate need (e.g. the
   tool-search catalog) that would justify promoting TofuSQL beyond
   tooling? Default answer is no; a yes must name the consumer and the
   bound.
2. Cross-owner inspection for support: separate break-glass scope with
   audit receipt, or out of scope entirely for the personal deployment?
3. Should query text/results be archived to `raw_archive` for incident
   forensics, or is the access log sufficient?
