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
Report-runtime releases those scopes on Paper exit.

`push-transport.ts` also owns Podcast/Video task observation: replay cursor,
one read per lifetime, push wakeups, retry sequence and poll/activity timers.
Media runtimes inject only event/snapshot/terminal projection; the retained
presenters own no scheduling fields. Library and session teardown import the
media disposers directly. Start with `task-observation.test.ts` and the compiled
media runtime/UX specs; keep the UX fault-injection probes on this shared owner.

`report-events.ts` owns typed stream initialization and event projection through
explicit view/publication ports. `report-runtime.ts` owns start, polling, push
attachment and cleanup through the shared `push-transport.ts` sequence/lifecycle
owner. The retained presenter only supplies the view adapter; remove that bridge
when its view/publication owners migrate. Audit cards and reading-time estimates
stay in the presenter. Event specs are co-located; compiled lifecycle specs live
in `tests/frontend/test_frontend_paper_report_runtime_vite.py`, and
`tests/paper/test_paper_report_inline_timeline.py` verifies both reading views.
Primary completion can precede worker exit: follow the shared `workerActive`
observation and reconcile body snapshots through `report-events.ts` so late
addenda survive polling. Teardown still releases the exact view's subscription.
Report/title adoption projects confirmed server state locally without writing
unrelated library fields. Verify stale-entry protection and retained
reader title adoption in `tests/frontend/test_frontend_paper_report_publication.py`.
Completion metadata belongs to `report-article/metadata.ts`. Event projection
keeps active-view identity; article painting reconciles both event and snapshot
updates. Deepen imports the same owner after its existing source/lifecycle fence.
Verify the browser timeline with metadata events and snapshot-only delivery.

## Verification

Run style composition/token checks and `npm run typecheck:modules`, then the
real PDF tests in `tests/frontend/test_pdf_reader_browser.py`, the split preview
journey in `tests/paper/test_paper_preview_browser.py`, and Vite preload tests
in `tests/visual/test_visual_lazy_paper_styles.py`. IIFE fixtures explicitly
link component CSS and shared tokens; production Vite owns CSS delivery.
Rendering/style changes need a real browser; finish with `npm run check:frontend`.
