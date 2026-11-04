# LLM dispatch guidance

## Scope

This package owns provider/model resolution, request ownership, health,
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
- Local request/route counts cannot block authorized work. Bound retries,
  backoff, reconstructible health/discovery caches and diagnostic metadata.
- User-facing failures use the shared typed taxonomy and localized projection;
  never include secrets or raw provider bodies.

## Dispatch API module layout

`api.py` is a pure re-export facade (a dynamic module hook keeps `monkeypatch.setattr(api, ...)`
sites propagating into the owning shard). Implementation lives in the `_api_*.py`
shards: `_api_errors.py` (typed errors), `_api_hygiene.py` (body/key hygiene),
`_api_contention.py` (slot contention metrics), `_api_budget.py` (per-attempt
budgets), `_api_chat.py` (non-streaming dispatch), `_api_stream_state.py` (stream
body adaptation and settlement), `_api_retry_state.py` (shared retry state),
`_api_failures.py` (shared chat/sync-stream/async-stream failure decisions),
`_api_stream.py` (streaming dispatch), `_api_multi.py` (multi-key
fan-out). New code goes into the owning shard, never the facade. Shard imports stay
acyclic and may only point leftward in this layering: `_api_errors` / `_api_hygiene` /
`_api_contention` (leaf shards, no cross-imports) ← `_api_budget` ←
`_api_stream_state` / `_api_retry_state` / `_api_failures` ←
`_api_chat` / `_api_stream` ← `_api_multi`.

`DispatchFailurePolicy` settles a failed slot and updates `DispatchRetryState`;
loops own transport, cancellable waits and queue timing. `ChatFailurePolicy`
keeps whole-response transport failures request-scoped and retires stream-only
models; `ChatRetryState` makes non-stream model exclusions durable and preserves
its existing subscription-wrapper clock policy. Streaming response-read failures
rotate a pair. Keep cancellation, one OAuth refresh per request, exclusions and
attempt budgets aligned through public dispatch tests. Capacity checks must see
every exclusion that survives a reset; never create another exception ladder.
The facade's historical `_StreamRetryState` name remains a reference to the
common state owner, not another implementation.


`service.py` is the `LLMService` adapter (`DispatchLLMService` +
`resolve_llm_service`). It must not import dispatcher state or HTTP at module
level. Non-route consumers inject `lib.protocols.LLMService` and call
`resolve_llm_service(llm)` instead of dual-pathing `smart_chat`.

Public dispatcher lifecycle is `get_dispatcher` / `reset_dispatcher` from
`lib.llm_dispatch`. HTTP, local_serve, and template supply call `reset_dispatcher`
after a committed authority mutation. In-package health/autodiscover may rebuild
the live slot pool in place.

`slot_pool.py` owns live-slot membership and the selection scopes: a pinned
context reads only its provider/route group, an unpinned one only configured
slots. Selection APIs go through `LLMDispatcher._selection_scope`, never a scan
of every live slot; specs are in `tests/llm_dispatch/test_slot_pool_scopes.py`.
It also keeps the exact key-health credential index (`key_stats_key_names`)
that `lib.key_stats` reads for last-resort siblings; every pool mutation keeps
it current, so never project credentials from a walk over the pool.
Membership uses ordered occurrences plus an identity index: request withdrawal
must not scan unrelated requests. Generic sequence edits rebuild the views;
diagnostic iteration snapshots membership. Verify identity, duplicate, reload,
quiescent reclamation and retirement-work specs in `test_slot_pool_membership.py`
and `test_route_group_retirement_measure.py`.

`ephemeral.py` owns atomic request-handle registration/disposal without a count
ceiling. Each execution holds its owner-authorized candidate group: prepare
every slot without a lock, publish the group in one critical section, dispose
it by indexed identity lookup. Keep registration, rollback, cancellation,
observer-failure and one-lock-acquisition tests in `test_ephemeral_capacity.py`;
remaining candidate-selection policy is separate.

Native context admission uses NativeContextPolicy and native_context_scope.
Verify generated native tool projection, route-window/output isolation, joined
preparation, unauthorized-summary refusal and installed context application tests.
Use the existing compactor/accounting owners; never create slots or fallback routes.

## Verification

`context_policy.py` reads declared Slot windows within an explicit owner/pin;
selected-slot admission rechecks them after fallback. It never repopulates a
global model registry. See `docs/CONTEXT_BUDGET_POLICY.md` and
`tests/llm_dispatch/test_route_context_budget.py`.

Run focused dispatch, slot, pin, retry-budget, fallback, health, and cancellation
tests. Add provider registration/discovery and billing tests when resolution or
settlement crosses those boundaries.
