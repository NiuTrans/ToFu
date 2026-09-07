# Decision proposal: `conversation.search` snippet & case-folding semantics

**Status:** DECIDED 2026-09-04 — Path ① implemented (owner directive:
push the tofu-db launch forward at full speed; this was the recommended
path with verified zero/additive consumer impact). Legacy
`_conversation_search_op` now folds in Python (`str.lower`, the exact
`to_lowercase` analog), re-clamps the snippet window start, and sizes the
window by the located term; the three manifest entries are removed and
the probes assert convergence. · **Author:** differential-test worker ·
**Evidence:** `tests/test_tofudb_differential.py` scripts
`search snippet truncation boundaries`,
`search multi-word AND and snippet width`, and
`search case folding ascii and unicode converge`

## Background

The semantic differential gate (a hard launch gate in `docs/STORAGE.md`)
replays identical `conversation.search` scripts against the legacy SQLite
sidecar and `tofu-db serve`, waiting for each side's asynchronous search
projection to settle before comparing (`Step.eventual`). The two engines
agree on everything structurally important — hit selection, ranking
(updated_at DESC, id DESC tie-break), edit/delete visibility, multi-word
AND fallback — but three pinned divergences remain, **all on the legacy
side** (`lib/storage_sidecar/operations_pkg/_conversations.py`,
`_conversation_search_op`):

1. **Snippet start truncation.** Line 1408 slices
   `head[max(0, pos-radius) : pos-radius+width]`. When the match starts at
   `pos < radius`, the start is clamped to 0 but the end index
   `pos-radius+width` is never re-clamped, so the snippet is short by
   exactly `radius - pos` characters. Tofu-DB takes the full `width`
   window from offset 0.
2. **Multi-word snippet width.** Line 1380 computes
   `width = 2*radius + len(query)` once, using the **full query length**
   even on the multi-word AND fallback where the located term is
   `words[0]` (lines 1403–1405). Tofu-DB sizes the window as
   `2*radius + len(term)`, i.e. the term actually located.
3. **Non-ASCII case folding.** Legacy matches through SQLite
   `lower()`/`LIKE`, which fold **ASCII only**: a query for `café` never
   matches stored `CAFÉ`. Tofu-DB applies full Unicode lowercase to both
   sides and matches. Unlike ① and ② this changes the **hit set** (which
   conversation IDs are returned), not just snippet cosmetics.

Divergences ① and ② are implementation slips in legacy; tofu-db's window
is the coherent, documented intent. Divergence ③ is a genuine semantics
gap (SQLite limitation vs. Unicode-aware matching) where tofu-db's
behavior is also the better contract.

## Consumer impact (verified by grep, this revision)

The only production caller of `conversation.search` is
`lib/conversations/repository.py::search_ids`, consumed solely by
`lib/conv_ref/_query.py` (keyword search for conversation references).
That path **reads only `hit["id"]` and discards `snippet` entirely**;
no in-repo consumer reads the snippet field. All other `search_ids`
callers are tests.

* Divergences ①/②: **zero consumer impact** — snippets are wire-visible
  but unread in-repo. Fixing legacy changes bytes nothing depends on.
* Divergence ③: fixing legacy widens the hit set for non-ASCII queries
  (a `café` query starts returning conversations containing `CAFÉ`).
  This is strictly additive recall for the conv_ref keyword path, only
  for queries/text with non-ASCII case pairs; ASCII behavior is
  unchanged. No consumer can regress.

## Path ① — Fix legacy to tofu-db semantics (recommended)

Three independent, backend-neutral fixes in `_conversation_search_op`:

```python
# ① line 1408 — re-clamp the end against the clamped start (~1 line):
start = max(0, pos - radius)
snippet = head[start : start + width]

# ② lines 1380/1403-1408 — size the window by the located term (~2 lines):
term = query if lowered.find(query) >= 0 else words[0]
width = 2 * radius + len(term)   # move inside the per-hit loop

# ③ — Unicode-aware folding (~10-15 lines). SQLite lower() is ASCII-only,
# so fold in Python instead of SQL: fetch candidate heads for owner/lane
# bounded by the existing budget, fold with str.casefold(), and locate
# terms in Python. (PostgreSQL lower() is already Unicode-aware, so the
# gap is SQLite-specific; a create_function-registered Unicode lower is
# an equally small alternative but adds a connection-setup hook.)
```

* **Blast radius:** one handler, ~15 lines total, no storage-format or
  wire-shape change (only snippet contents and non-ASCII hit sets).
* **Consumer impact:** as verified above — ①/② none, ③ additive recall
  on the single conv_ref consumer.
* **Gate effect:** all three divergences disappear; the ratchet in
  `KNOWN_DIVERGENCES` forces their own removal and the suite stays
  green — convergence is mechanically verified.

## Path ② — Amend the contract: tofu-db behavior authoritative

Declare in `contracts/storage_v2.json` that snippet windows are
term-width, start-clamped, and that matching is full-Unicode
case-insensitive, with tofu-db correct by definition; keep legacy as-is
until cutover.

* **Blast radius:** contract sentences, zero code.
* **Consumer impact:** for ③, legacy keeps strictly worse recall for
  non-ASCII queries through the entire migration window, then
  **silently changes behavior at cutover** — the class of drift the
  differential gate exists to catch. The manifest entries would have to
  stay pinned (or the probes deleted) for the whole window, weakening
  the ratchet.
* **When to prefer:** only if touching the legacy authority is ruled
  out of scope for the launch window. There is no technical or consumer
  reason to prefer it.

## Recommendation

**Path ①, all three fixes.** ① and ② are one-to-two-line corrections of
unambiguous slips with zero consumers; ③ converges legacy to the better
recall contract with additive-only impact on the single consumer. All
three are verified automatically by the existing differential probes the
moment they land. Path ② preserves silent cutover-behavior-change risk
for zero engineering savings.

Until a decision lands, the divergences stay pinned and asserted in
`KNOWN_DIVERGENCES`; whichever path is chosen, the gate must go green by
convergence (①) or by explicit contract amendment plus manifest update
(②), never by deleting the probes.
