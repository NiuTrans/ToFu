# Frontend guidance

## Scope and first reads

This directory owns browser delivery. Read `docs/FRONTEND_ARCHITECTURE.md`,
`docs/RENDER_CONTRACT.md`, and the domain map for the feature being changed.

## Source boundaries

- New behavior is TypeScript under `src/`. The retained classic runtime is
  authored only under `src/runtime/sections/` and should shrink over time.
- Author application and settings CSS under `src/styles/application/` and
  `src/styles/settings/`. Never edit generated files under root `static/`.
- Files named `generated` and composed outputs such as
  `src/runtime/app-runtime.js` are generator products. Change their contract,
  section, manifest, locale, or generator owner and regenerate.
- Register actions and features through typed registries. Do not publish new
  private owners onto `window` or create a parallel event/state registry.

## Behavioral rules

- Conversation state is a projection of the v3 snapshot/event contracts, not
  browser-owned durable state.
- Preserve teardown, cancellation, navigation, cross-tab invalidation, focus,
  accessibility, reduced-motion, localization, and narrow-screen behavior.
- Use existing transport, error-envelope, identity, and feature-capability
  owners. UI code does not infer authority from cached visibility.
- Load heavy optional features lazily and keep bundles, DOM retention, timers,
  observers, media, and replay buffers bounded.

## Verification

Run the smallest focused frontend test, then `npm run check:frontend`. Add
`make test-frontend`, `make test-visual`, or `make test-e2e` only when the
change crosses serving, layout, or a user-critical browser journey.
