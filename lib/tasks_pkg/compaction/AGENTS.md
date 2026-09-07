# Context compaction guidance

## Scope

This package owns the staged compaction pipeline and durable compaction receipts.
Read `docs/modules/context_engineering.md` and
`docs/LLM_COST_OPTIMIZATION.md`.

## Editing rules

- Operate on immutable/frozen input and return a new projection plus explicit
  accounting. Do not mutate the authoritative conversation or tool history.
- Preserve required system/task blocks, user intent, recent anchors, causal
  order, tool-call/result pairing, identifiers, provenance, and terminal facts.
- Each stage declares eligibility, budget, deterministic fallback, and receipt
  data. A failure cannot leave a half-persisted compacted projection.
- Token/byte accounting uses canonical counters and counts all inserted
  summaries, images, tool results, and metadata. Never optimize against an
  estimate while reporting exact savings.
- Manual and automatic compaction share invariants but keep their concurrency
  and persistence commands explicit.
- Bound stage count, model calls, source material, summaries, images, artifacts,
  retries, and retained receipts; propagate cancellation.

## Verification

Run `test_compaction_invariants.py`, anchor/tool-pair/receipt/token tests, and the
focused changed stage tests. Add manual-compaction route/reload or long-agent
context-plan tests when those boundaries change.
