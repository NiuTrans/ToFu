# Frontend guidance

## Scope and first reads

This directory owns browser delivery. Read `docs/FRONTEND_ARCHITECTURE.md`,
`docs/RENDER_CONTRACT.md`, and the domain map for the feature being changed.

## Source boundaries

- `src/main.ts` starts parallel locale and application loading;
  `src/application.ts` owns runtime composition and readiness. Required startup
  imports remain part of the entry budget in `authoring-inputs.json`.
- That same manifest declares all host entries, including `src/plugin.ts`.
  `src/plugins/` owns explicit API/Markdown ports for optional package pages;
  plugins do not load retired classic vendor scripts or publish host globals.
- New behavior is TypeScript under `src/`. The retained classic runtime is
  authored only under `src/runtime/sections/` and should shrink over time.
- Author application and settings CSS under `src/styles/application/` and
  `src/styles/settings/`. Never edit generated files under root `static/`.
- Shared controls come from the design system: factories in `src/ui/`
  (contract `docs/DESIGN_SYSTEM.md`), tokens in
  `src/styles/application/00-design-tokens.css`, kit styles in
  `30-ui-kit.css`. New UI uses factories; it does not hand-roll one-off
  buttons/inputs.
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

Browser dependency updates run `npm run generate:vendor` before verification;
the lockfile also owns standalone artifact files and report export integrity.
Run the smallest focused frontend test, then `npm run check:frontend`. Add
`make test-frontend`, `make test-visual`, or `make test-e2e` only when the
change crosses serving, layout, or a user-critical browser journey.

The served graph `static/vite/` is built locally and never committed. Boot
checks only its integrity (`lib/vite_assets.validate_published_vite_artifact`);
`serverctl start`/`restart` and the in-app restart first run
`serverctl.prepare_source_frontend_artifact`, which in a source checkout
rebuilds a graph that is stale against `frontend/src`. A build in the shared
checkout also bundles other writers' uncommitted `frontend/src` edits.
