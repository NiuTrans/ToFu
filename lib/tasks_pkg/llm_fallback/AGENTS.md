# Round invocation and fallback

`_call._llm_call_with_fallback` is the orchestrator entry. Read
`docs/modules/llm_io.md` and `docs/contracts/TURN_SETTLEMENT.md` for model
selection, refusal and failure semantics.

## Owners

- `_requests`: configured destination bodies, using the shared
  turn-requirements and wire-tool owners.
- `_transport`: credential recovery around the stream manager, physical-input
  receipts and factual stream-outcome logging.
- `_usage`: one paid-response ledger for ordinary, reactive, configured, pool,
  refused and post-loop synthesis attempts. Scope-denied work records known
  charges without publishing context or serving evidence.
- `_recovery` / `_state`: same-model context-overflow recovery and its existing
  per-task budget; `_retry`: configured selection and empty-stop classification.
- `_configured` / `_pool`: admitted recovery attempts; `_errors`: non-rotatable
  primary errors and failed-fallback settlement.
- `_refusal`: typed content-filter evidence, partial-output retirement and
  refusal settlement.

Import the concrete owner when injecting a fault. Do not add patch facades or
another model loop. Request preparation remains outside best-effort transport
recovery; only the existing policy can authorize another model.

## Verification

Start with `test_fallback_accounting`, `test_reactive_payload_recovery` and
`test_model_refusal_fallback` under `tests/tasks_pkg/`. Then cover continuity,
gateway strictness, pool rescue, delivery acknowledgement, input receipts,
credential recovery, compaction and turn settlement. The gateway-ledger parity
suite keeps real FloorRetry transport and cost arithmetic.
