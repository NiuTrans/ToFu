# Frontend style guidance

## Scope

Authored CSS lives under `application/` and `settings/`; manifests define
deterministic composition. Root `static/` CSS is generated delivery output.

## Editing rules

- Edit the smallest semantic source file and preserve manifest ordering.
  Never patch generated files under `static/`.
- Reuse established tokens, typography, spacing, color, elevation, and motion
  semantics. Avoid one-off overrides whose only purpose is to beat cascade
  order.
- Scope selectors to their component/feature and remove obsolete rules with the
  markup they served. Do not rely on generated hashes or incidental DOM depth.
- Preserve keyboard focus, contrast, zoom, reduced motion, touch targets,
  safe-area behavior, long localized text, and narrow/mobile layouts.
- Treat large images, fonts, animations, and CSS growth as bundle/resource
  budgets. Keep optional feature styles lazy where the architecture supports it.
- A visual change updates source markup/styles together; do not encode behavior
  solely through fragile CSS state.

## Verification

Run `npm run check:styles` and the focused style/frontend test. Use targeted
desktop and narrow-screen screenshots for layout changes, then the visual gate
when the affected behavior cannot be proven structurally.
