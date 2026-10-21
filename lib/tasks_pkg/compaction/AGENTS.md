# Context compaction guidance

## Scope

This package owns the staged compaction pipeline and durable compaction receipts.
Read `docs/modules/context_engineering.md` and
`docs/CONTEXT_COMPACTION.md` and `docs/LLM_COST_OPTIMIZATION.md`.
`_handoff.py` owns automatic continuation identity and phase-aware ordering;
`_receipt.py` owns archive metadata, not model authority. Read these owners
before changing composer/replay/recovery boundaries; handoff and provider-wire
tests are the executable compatibility matrix.
`_resume_projection.py` validates accepted archive snapshots against fresh
history/policy/tool-prefix evidence for Continue; its focused regressions are
`test_dispatch_context_recovery.py` (including isolated storage restart).
`_admission_budget.py` owns mandatory limits; `_cache_economics.py` owns shared
optional rewrite ROI, and `_pressure.py` tries one detached L1 candidate before
L2. Their contract is `docs/CONTEXT_BUDGET_POLICY.md`; verify with
`test_compaction_pressure_policy.py` and `test_route_context_budget.py`.
`_budget.py` fits the final tool view, including host outcomes and recovery
metadata; candidate finalizers are pure and retention stays outside the search.
Verify final-result budget facts/backends and aggregate recovery tests.
`_context_lifecycle.py` validates manual admission through the captured
ConversationStore. The host policy lives in `lib/conversations/compaction_policy.py`;
verify manual-engine, coordinator-lifecycle and core-boundary tests.

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
