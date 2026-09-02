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

## Verification

Run focused core/conversation-sync/browser-storage tests, then
`npm run typecheck:modules` and the relevant generator drift check.
