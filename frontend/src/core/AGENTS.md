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

## Verification

Run focused core/conversation-sync/browser-storage tests (`entity-record.test.ts`
for ID-index changes), then
`npm run typecheck:modules` and the relevant generator drift check.
