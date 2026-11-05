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

Request Inspector uses `inspector-session.ts` for its opening, pane selection
and scheduled resources; `inspector-payloads.ts` for snapshot/cache settlement;
and `inspector-details.ts` for trace/detached reads. `inspector-feed.ts` owns the
selected flat-feed presentation; `diagnostics-wire-controller.ts` retains read
coalescing and last-good evidence. Evidence preference stays in
`inspector-evidence.ts` and messages use the common Debug renderer. Start with
`tests/frontend/test_inspector_read_lifecycle.py`, the retained feed/inline
tests, then `test_inspector_read_lifecycle_browser.py` and Inspector reading.

`inspector-navigation.ts` owns feed selection and scroll resources;
`inspector-navigation-list.ts` projects keyed rows through the common anchored
disclosure. Start with `test_inspector_navigation.py`, then its browser controls.

`inspector-trace-projection.ts` projects the generated `TurnTimingTrace` type;
`inspector-trace-view.ts` renders it with common HTML safety. Reads and pane
identity stay in `inspector-details.ts`; Trace styles load with diagnostics.
Start with `test_inspector_trace_projection.py`, then its theme/width browser
tests. The authority is `docs/contracts/TURN_TRACE_CONTRACT.md`.

`inspector-inline.ts` owns exact tool addresses and original-mount read rights;
`inspector-inline-view.ts` uses shared controls and the common Debug renderer.
Payload selection and incremental evidence stay in `inspector-payloads.ts`.
Start with `test_inspector_inline_owner.py`, structured-body/inline contracts,
then `test_inspector_inline_browser.py` for keyboard and detached-DOM release.

`inspector-feed-view.ts` projects feed dividers and coverage notices;
`inspector-pane-view.ts` supplies shared loading, failure, retry and back controls.
All feed publication states respect the selected pane; retired controls cannot
act on a replacement view. Start with `test_inspector_feed_owner.py`, then the
flat-feed/read-lifecycle contracts and actual browser reading/control journeys.

Run the smallest feature-specific frontend test and
`npm run typecheck:modules`. Add registry/action/i18n checks when registration
or copy changes, and use visual/E2E tests only for cross-boundary user flows.
