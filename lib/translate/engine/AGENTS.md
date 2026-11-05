# Translation engine ownership

Read `docs/modules/ingest_media.md` (Translation) for the product contract.

- `__init__.py` is the explicit facade used by general text and paper callers.
  `_translate_freetext` translates the whole document in one call per attempt;
  paper slicing and artifact publication stay in `lib/paper/translate_engine.py`.
- `_engine.py` owns fast paths, dispatch, cancellation/deadline checks, retry
  counters and final positive-cache commit. Keep physical requests on the shared
  dispatcher/billing/model-budget path.
- `_candidate.py` normalizes and assesses one returned candidate. All content
  failures share feedback and the engine's content-failure counter. Empty,
  partial and typed-refusal outcomes intentionally have different cache policy.
  It never dispatches, waits or starts another retry loop.
- `_quality_checks.py` and `_split.py` are pure content policy. Check order is
  significant: truncation precedes no-op, wrong-language and over-generation.
  Engine-level over-generation thresholds remain injectable for negative controls.
- Quality assessment owns its diagnostic span; execution lifecycle still belongs
  to `lib/translate/observability.py` and `lib/translate/execution.py`.

Run `tests/translate/test_translation_quality_boundaries.py` first, then the
translation suite and paper translation budget/storage tests. For ownership
changes also run `scripts/check_architecture.py` and the function-size ratchet.
