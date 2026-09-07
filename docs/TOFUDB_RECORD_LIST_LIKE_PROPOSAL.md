# Decision proposal: `record.list` prefix semantics (SQL LIKE wildcards)

**Status:** DECIDED 2026-09-04 — Path ① implemented (owner directive:
push the tofu-db launch forward at full speed; this was the recommended
path with no known wildcard consumer). Legacy `_record_list` now escapes
LIKE metacharacters and declares `ESCAPE '\'`, so `prefix` is a literal
byte prefix on both authorities; the manifest entry is removed and the
probe asserts convergence. · **Author:** differential-test worker ·
**Evidence:** `tests/test_tofudb_differential.py` script
`record list prefix treats wildcards literally`

## Background

The semantic differential gate (a hard launch gate in `docs/STORAGE.md`)
replays identical `record.list` scripts against the legacy SQLite sidecar
and `tofu-db serve`. One pinned divergence remains in the `record.*`
domain:

* **Legacy authority** (`lib/storage_sidecar/operations_pkg/_records.py`,
  `_record_list`) filters with `record_key LIKE prefix || '%'` **without an
  `ESCAPE` clause**. A prefix containing `_` or `%` is therefore a SQL
  pattern, not a literal byte prefix: prefix `p_a` also matches key
  `pxa/1`.
* **Tofu-DB** (`packages/tofu-db/src/semantic_executor.rs`, `record.list`)
  scans a literal byte-prefix range; `_`/`%` carry no special meaning.

The probe seeds `p_a/1` and `pxa/1` and lists with prefix `p_a`: legacy
returns both rows, tofu-db returns one. Both engines agree in every other
tested respect (ordering, limit bounds, CAS, idempotent delete, Unicode).

This is a **contract incompatibility**, not an implementation bug on either
side: `contracts/storage_v2.json` does not state whether `prefix` is a
literal byte prefix or a SQL pattern. It must be resolved before tofu-db
may serve `record.list` in production, because the two authorities can
return different row sets for the same request.

## Path ① — Fix legacy to literal-prefix semantics (recommended)

Change `_record_list` to escape LIKE metacharacters and declare the escape:

```python
escaped = prefix.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
# ... AND record_key LIKE ? ESCAPE '\'  -- param: escaped + "%"
```

* **Blast radius:** one handler, ~3 lines, backend-neutral (SQLite and
  PostgreSQL both honor `ESCAPE '\'`). No storage-format or wire change.
* **Consumer impact:** the only in-repo `record.list` callers are test
  seeds/contracts; production domains (`task_results`, cost experiments)
  explicitly avoid raw `record.list` (see the warning comments in
  `_records.py`). No consumer is known to rely on `_`/`%` acting as
  wildcards — that behavior is an accident of the SQL formulation, not an
  advertised feature. Anyone pattern-matching today is depending on
  undocumented behavior that already breaks under a backend swap.
* **Gate effect:** the divergence disappears; the ratchet in
  `KNOWN_DIVERGENCES` forces its own removal and the suite stays green —
  convergence is mechanically verified.

## Path ② — Amend the contract: prefix is literal bytes, tofu-db is authority

Declare in `contracts/storage_v2.json` that `record.list.prefix` is a
literal byte prefix and that tofu-db's behavior is correct by definition;
keep legacy as-is until cutover.

* **Blast radius:** one contract sentence, zero code.
* **Consumer impact:** identical to ① for conforming consumers — but any
  client accidentally relying on wildcard matching keeps working against
  legacy and **silently changes behavior at cutover**, exactly the class of
  drift the differential gate exists to catch. The manifest entry would
  have to stay pinned (or the probe deleted) for the entire migration
  window, weakening the ratchet.
* **When to prefer:** only if a real consumer is discovered that depends
  on wildcard prefixes and cannot be migrated before launch. None is known
  today.

## Recommendation

**Path ①.** The wildcard behavior is an unadvertised SQL artifact with no
known consumer; a 3-line `ESCAPE` fix converges both authorities to the
stricter, portable, backend-neutral semantics and lets the differential
ratchet verify the fix automatically. Path ② preserves a silent
cutover-behavior-change risk for zero engineering savings.

Until a decision lands, the divergence stays pinned and asserted in
`KNOWN_DIVERGENCES`; whichever path is chosen, the gate must go green by
convergence (①) or by explicit contract amendment plus manifest update
(②), never by deleting the probe.
