# LLM dispatch guidance

## Scope

This package owns provider/model resolution, concurrency slots, health,
fallback, and retry dispatch around `lib/llm/`. Read
`docs/modules/llm_io.md` and `docs/modules/auth_providers_billing.md`.

## Editing rules

- Resolve provider configuration inside the explicit owner boundary. Credential
  IDs, account IDs, model aliases, and owner IDs are distinct types.
- Keep selection, admission/slot acquisition, one attempt, health accounting,
  retry classification, and fallback as separate observable stages.
- Respect explicit provider/model pins. A retry or fallback may not silently
  cross an authority, billing, capability, or data-egress boundary.
- Propagate cancellation through queued slots, backoff, transport, and streams.
  Release every slot exactly once on success, error, timeout, or cancellation.
- Bound concurrent calls, waiters, retries, backoff, health history, discovery
  caches, and diagnostic metadata using the canonical resource profile.
- User-facing failures use the shared typed taxonomy and localized projection;
  never include secrets or raw provider bodies.

## Dispatch API module layout

`api.py` is a pure re-export facade (a dynamic module hook keeps `monkeypatch.setattr(api, ...)`
sites propagating into the owning shard). Implementation lives in the `_api_*.py`
shards: `_api_errors.py` (typed errors), `_api_hygiene.py` (body/key hygiene),
`_api_contention.py` (slot contention metrics), `_api_budget.py` (per-attempt
budgets), `_api_chat.py` (non-streaming dispatch), `_api_stream_state.py` (stream
attempt state), `_api_stream.py` (streaming dispatch), `_api_multi.py` (multi-key
fan-out). New code goes into the owning shard, never the facade. Shard imports stay
acyclic and may only point leftward in this layering: `_api_errors` / `_api_hygiene` /
`_api_contention` (leaf shards, no cross-imports) ← `_api_budget` ← `_api_stream_state` ←
`_api_chat` / `_api_stream` ← `_api_multi`.
## Verification

Run focused dispatch, slot, pin, retry-budget, fallback, health, and cancellation
tests. Add provider registration/discovery and billing tests when resolution or
settlement crosses those boundaries.
