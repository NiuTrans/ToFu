# Frontend core guidance

## Scope

Core modules own shared browser primitives such as conversation synchronization,
storage, capabilities, and transport-neutral lifecycle. They must not become a
grab bag for feature policy or DOM presentation.

## Editing rules

- Expose small typed interfaces with explicit start, subscribe, cancel, and
  dispose lifecycles. Avoid import-time side effects and mutable globals.
- A server contract has one reducer/projection owner. Cross-tab and push signals
  invalidate or wake; they do not become competing state authorities.
- Persist only explicitly browser-owned preferences/cache. Owner-visible server
  data remains a projection and is discarded or refreshed on identity change.
- Bound caches, replay cursors, listeners, channels, timers, and queued work.
  Remove all resources during teardown and make repeated start/stop idempotent.
- Core cannot import feature UI or retained classic sections. Depend on
  generated DTOs and inject presentation callbacks at the edge.

`entity-record.ts` owns TurnStore ID-index layout and bulk readers; its clone/
reflection boundary is documented in `docs/contracts/CONVERSATION_SYNC_V3.md`.
`observer.ts` isolates best-effort notifications. Required projection/commit
callbacks propagate failures to their owner; never pass them to `notifyObserver`.

`turn-projection.ts` owns lifecycle-bound shell invalidation from reducer receipts;
see `docs/TURN_RENDERING_PERFORMANCE.md` and `turn-projection.test.ts`.

`conversation-event-multiplexer.ts` owns the page's single conversation event
response; each coordinator gets an EventSource-shaped source from it and keeps
its own cursor and recovery ("One page stream" in `CONVERSATION_SYNC_V3.md`).
Verify with `conversation-event-multiplexer.test.ts`.

`turn-runtime.ts` composes canonical stores, sync coordinators and shell bindings.
`turn-hydration.ts` owns coalesced explicit snapshot requests and their latest
catalog-shell claimant. Binding, projection and connection demand remain in the
runtime; snapshot/replay transport remains in the sync coordinator. Verify shell
handoff and host callback reentry in `turn-runtime-ingestion.test.ts`.
`turn-runtime-contracts.ts` owns its typed ports; the existing runtime entry still
re-exports public types. `turn-command-execution.ts` owns command request/adoption
flows, using the payload builders in `turn-command.ts` and injected runtime ports.
`turn-runtime-commands.test.ts` covers command ACK/error identity, retired stores,
shell replacement and owned pre-edit reads; writes keep their server outcomes.
`turn-attempt-lifecycle.ts` owns each store's callback leases and live observations;
`turn-runtime-attempts.test.ts` covers removal, replacement, reentry and teardown.
Bindings are owned by conversation ID, independent of optional catalog lookups;
`turn-runtime-ingestion.test.ts` covers replacement, reentry and teardown faults.
`turn-render-observations.ts` owns DOM-commit witnesses and bounded receive clocks;
`perception-recorder.ts` owns paint observations, receipt queues and retries.
Verify their shared mount/revision boundary with
`tests/turn_lifecycle/test_turn_runtime_perception_commit.py`.

## Verification

Run focused core/conversation-sync/browser-storage tests (`entity-record.test.ts`
for ID-index changes), then
`npm run typecheck:modules` and the relevant generator drift check.
