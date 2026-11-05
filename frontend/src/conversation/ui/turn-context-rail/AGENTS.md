# Per-turn context rail

The rail beside the reading column ("what this round ran with": model,
effort, run modes, tools, workspace roots with their branches, checklist) and its
one-line fold for panes that cannot grant it a track. This directory is the
single owner of its markup; every other piece is listed here so the rail can
be changed from one map.

| Concern | Owner |
|---|---|
| Snapshot normalization (legacy tool-modes, string items, bounds) | `projection.ts` |
| Rail, fold and live-part markup, glyphs, preview sizes | `markup.ts` |
| Public entry points and host contract | `index.ts`, `contracts.ts` |
| Styles, section vocabulary (`--tctx-*`), track reservation, the sticky card and its height bound | `styles/application/23-reading-context-rail.css` |
| Snapshot capture, done-frame reconcile, live root branches/checklist row, `+N` toggle, track lengths and pane height | `runtime/sections/info-rail.js` (retained; host services via `renderTurnCtxNote`) |
| Placement in the Turn (rail track part › card part › content part, fold block) | `renderers/context-blocks.ts`, `surface-turn-renderer.ts` |
| Track latch (`data-turn-context-rail`: present / pending / absent) | `surface-engine.ts` |
| Pane grid track and adaptive width | `styles/application/01-shell-navigation.css`, `runtime/sections/main.js` |
| Background agents section inside the rail's card | `agent-sidebar-dock.ts`, `styles/application/32-agent-sidebar-dock.css` |

Rules:

- One section anatomy: a heading (label, count, optional disclosure) above
  its facts. New facts join that anatomy and the `--tctx-*` tokens instead of
  adding a new chip or line style.
- The disclosure stays on its heading; facts expand below it. Expansion is a
  click, never hover (the rail grows in flow, no overlay).
- The rail root is an out-of-flow track that never sizes its Turn;
  `info-rail.js` measures its length on layout changes, never on scroll.
  The card inside it travels by `position: sticky` and is bounded only by
  its track and the pane. Do not move travel back into script, place the
  rail in flow, give the track an overflow clip (the card would stick to
  it), or add a fixed height cap.
- Markup is escaped through `safeHtml`; only registry glyphs and the host's
  brand mark pass as trusted markup. Persisted tones become classes only
  when they match the token grammar.
- A branch is a fact of its root: the root's line reads `root:branch`, and
  capture records each root's own branch. Each name is one line-breaking
  unit (`.tctx-path-name`), so a pair too long for its line breaks only
  after the colon. A turn without roots shows no branch.
- Live parts keep their keys (each root line `[data-tctx-root]` and its
  branch suffix `[data-tctx-git]`, the checklist row `[data-tctx-todo]`):
  the live patch replaces them in place.

Verify `turn-context-rail.test.ts`, the Surface bundle cases in
`tests/frontend/test_frontend_conversation_surface_vite.py`, the rail specs in
`tests/turn_lifecycle/test_turn_ctx_rail_*.py`,
`tests/visual/test_visual_context_rail_*.py`, then `npm run check:styles`.
