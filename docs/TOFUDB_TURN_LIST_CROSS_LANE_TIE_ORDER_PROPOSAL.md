# Decision proposal: `turn.list` cross-lane ordinal tie order

**Status:** DECIDED 2026-09-05 — Path ① implemented (owner directive:
push the tofu-db launch forward at full speed; this was the recommended
path with no known consumer). Legacy `_turn_list`
(`lib/storage_sidecar/operations_pkg/_turns_read.py`) now orders
`ORDER BY ordinal, turn_id`; the manifest entry and script markers are
removed and the gate asserts convergence. ·
**Author:** differential-test worker · **Evidence:**
`tests/test_tofudb_differential.py` script
`turn branch create delete lifecycle`

## Background

The semantic differential gate (a hard launch gate in `docs/STORAGE.md`)
replays identical `turn.list` scripts against the legacy SQLite sidecar
and `tofu-db serve`. One pinned divergence remains on ordering:

* **Legacy authority** (`lib/storage_sidecar/operations_pkg/_turns_read.py`,
  `_turn_list`) orders with `ORDER BY ordinal` and no tie-breaker. Every
  branch lane restarts its ordinals at 0, so rows tied at the same ordinal
  across lanes fall out in **SQLite plan order** — branch lanes currently
  surface before main, and the order is not guaranteed to survive a schema,
  index, or backend change (PostgreSQL would pick its own plan order).
* **Tofu-DB** scans its lane index deterministically and returns **main
  lane first**, then branch lanes in lane-index order.

The contract never specified cross-lane tie order; both engines agree on
everything else (within-lane ordinal order, lane filtering, pagination).
This must be resolved before cutover because the two authorities can
return the same rows in different orders for the same request, and row
order is wire-visible to transcript renderers.

## Path ① — Amend legacy `ORDER BY` to a deterministic tie-break (recommended)

Change `_turn_list` to `ORDER BY ordinal, turn_id`. Tofu-DB's
un-laned `turn.list` scans its turn document namespace in
`(conversation_id, turn_id)` byte order, so within an ordinal tier its
outcome is turn_id byte order; the ordinal-major / turn_id-minor key
matches that outcome on every script the gate exercises (main lane first
in the pinned probe, since the main turn's id sorts before the branch's).
Backend-neutral on SQLite and PostgreSQL.

* **Blast radius:** one `ORDER BY` clause in one query, backend-neutral
  (SQLite and PostgreSQL both honor the expression form). No
  storage-format or wire-shape change — only the previously unspecified
  tie order becomes deterministic.
* **Consumer impact:** transcript rendering consumes lanes grouped by
  `lane_id`, so no in-repo consumer depends on the accidental plan order;
  the only observable change is that a previously unstable order becomes
  stable. Any external consumer depending on branch-before-main is
  depending on an undocumented SQLite artifact.
* **Gate effect:** the divergence disappears; the `KNOWN_DIVERGENCES`
  ratchet forces its own removal and the suite stays green — convergence
  is mechanically verified.

## Path ② — Amend the contract: tofu-db ordering is authoritative

Declare in the conversation contract that `turn.list` returns main lane
first under ordinal ties and that tofu-db defines the order; keep legacy
as-is until cutover.

* **Blast radius:** one contract sentence, zero code.
* **Consumer impact:** identical for conforming consumers, but the two
  authorities keep returning different orders for the entire migration
  window, and any client diffing them (or replaying legacy captures
  against tofu-db) sees phantom reordering. The manifest entry must stay
  pinned for the whole window, weakening the ratchet.
* **When to prefer:** only if touching the legacy query is judged too
  risky this close to cutover — but the clause change is smaller than the
  contract amendment's verification burden.

## Recommendation

**Path ①.** The plan-order tie break is an unadvertised SQLite artifact
that is not even stable across backends; a one-clause `ORDER BY`
amendment converges both authorities to deterministic main-first order
and lets the differential ratchet verify the fix automatically. Path ②
preserves a known, unnecessary cross-authority behavior delta for the
entire migration window.

Until a decision lands, the divergence stays pinned and asserted in
`KNOWN_DIVERGENCES`; whichever path is chosen, the gate must go green by
convergence (①) or by explicit contract amendment plus manifest update
(②), never by deleting the probe.
