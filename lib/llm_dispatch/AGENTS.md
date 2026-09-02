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

## Verification

Run focused dispatch, slot, pin, retry-budget, fallback, health, and cancellation
tests. Add provider registration/discovery and billing tests when resolution or
settlement crosses those boundaries.
