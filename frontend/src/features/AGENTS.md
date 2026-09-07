# Frontend feature guidance

## Scope

Feature modules implement user-facing behavior on top of typed core/API owners.
Read the matching `docs/modules/*.md` map before changing a feature.

## Editing rules

- Keep each feature's registration, state, commands, view, and teardown
  discoverable from one entry point. Split by responsibility before growing a
  multipurpose runtime file.
- Use the shared action/feature registries and lifecycle. Do not add parallel
  registries, hidden boot hooks, or new globals on `window`.
- Server-owned data is fetched and mutated through typed clients. Keep
  authorization, storage, provider, scheduling, and orchestration policy in
  their backend owners.
- Capability absence and partial failure are explicit UI states. Preserve
  cancellation, retry ownership, optimistic rollback, and actionable canonical
  errors.
- Lazy-load optional media, paper, orchestration, settings, and admin surfaces.
  Bound observers, timers, subscriptions, previews, cached results, and DOM.
- New text goes through the i18n contract; new styling goes through the source
  style manifests, not inline proliferation or generated static CSS.

## Verification

Run the smallest feature-specific frontend test and
`npm run typecheck:modules`. Add registry/action/i18n checks when registration
or copy changes, and use visual/E2E tests only for cross-boundary user flows.
