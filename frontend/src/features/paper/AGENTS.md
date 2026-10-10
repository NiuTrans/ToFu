# Paper reader ownership

## Map

`pdf-reader.ts` owns PDF resources, viewport geometry and `03-pdf-reader.css`.
`reader-controls.ts` owns navigation/search/outline and the three shared
`24-paper-preview-{shell,controls,responsive}.css` sheets.
`preview-panel.ts` owns chat split/focus/drawers and the seven preview detail
sheets. Paper, chat preview and Research import these same component owners.
The style manifest names the direct importer of every deferred sheet.

Keep optional CSS on this actual module graph; an outer Paper-only entry would
omit Research. Feature activation awaits the existing cached module transaction
so CSS failure cannot construct an unstyled component on a later attempt.
Frozen legacy sheets are exact copies; migrate through tokens when editing.

The report presenter imports `report-article/index.ts`: typed final-article
decoration and host-owned interactions; its child guide names each owner.
Report-runtime releases those scopes on Paper exit. Generation, reading
anchors, audit cards and reading-time estimates stay in their existing owners.

## Verification

Run style composition/token checks and `npm run typecheck:modules`, then the
real PDF tests in `tests/frontend/test_pdf_reader_browser.py`, the split preview
journey in `tests/paper/test_paper_preview_browser.py`, and Vite preload tests
in `tests/visual/test_visual_lazy_paper_styles.py`. IIFE fixtures explicitly
link component CSS and shared tokens; production Vite owns CSS delivery.
Rendering/style changes need a real browser; finish with `npm run check:frontend`.
