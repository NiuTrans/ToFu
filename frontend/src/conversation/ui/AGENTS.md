# Conversation UI ownership

- `conversation-surface.ts` composes one design with `surface-engine.ts`, which
  owns container lifetime, intent delegation, lane/block windows and viewport
  residency. Its render plan is ephemeral.
- `surface-design-contracts.ts` defines appearance ports. `surface-design.ts`
  owns CSS loading, shell classes, control labels, renderer defaults and rich
  control construction. Reconciliation consumes those ports; it cannot import
  the design or write classes/copy. Verify `surface-design.test.ts` and
  `npm run check:renderer-boundaries` before changing this seam.
- `surface-window-preparation.ts` owns pure lane discovery, viewport budget
  allocation and stable-ID remapping. Reuse projection-owned navigation indexes
  only for their exact lane identities; serialized/external graphs remain valid.
  DOM measurements, history flights and frame scheduling stay in Surface.
- `surface-turn-renderer.ts` owns keyed Turn parts and content blocks. It
  mounts the engine's grouped reply entries (`presentation/grouped-turn-layout.ts`),
  paints the tool panel header through the design port and composes
  `RoundOutline`; navigation resolves stable identities through Surface ports
  instead of capturing old projections.
- `surface-node-lifecycle.ts` releases Surface-owned DOM references and finishes
  sibling cleanup before propagating the first required cleanup error.
- `surface-dom.ts`, `surface-presentation-equality.ts`, `surface-windowing.ts`
  and `conversation-viewport.ts` own DOM primitives, repaint equality, geometric
  policy and scroll anchoring respectively. Bind DOM reconciliation once to
  the Turn renderer retirement port; duplicate nodes use the same cleanup as
  normal removals. Rendering does not add a second
  window or transcript authority.
- Renderer-specific requests/listeners retire through `disposeBlock`; the
  renderer guides under `renderers/` own their private caches and behavior.
- `turn-context-rail/` owns the per-turn context rail's markup and keeps the
  map of every other rail owner (styles, latch, track lengths, width); the engine
  only writes the `data-turn-context-rail` latch the shell reserves from.

First verification: focused Surface, lifecycle, viewport, window, equality and
controller Vitest suites, plus `tests/conversation_surface_retention.test.cjs`.
Then use `tests/frontend/test_frontend_conversation_surface_vite.py` for the
production renderer graph and the grouped-turn-layout/scroll-history visual
journeys for focus, disclosure and residency. Current behavior lives in
`docs/FRONTEND_ARCHITECTURE.md`, `docs/RENDER_CONTRACT.md`, and retention
measurements in `docs/RESOURCE_CONTROL.md`.
