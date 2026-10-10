# Slide editor owners

`../slides.ts` is the demand-loaded entry and runtime-port composition. No
listener, request, timer or overlay is created at import time.

- `contracts.ts`: API ports, deterministic patch shapes, unique page-visit identity.
- `controller.ts`: the single mounted session, write ownership and teardown.
- `element-reader.ts`: one active read and the latest trailing page demand.
- `view.ts` / `properties.ts`: DOM projections and field-to-patch conversion.
- `canvas.ts`: one iframe document's selection style and listeners.

Never retain detached editor DOM in request callbacks. All read/patch receipts
must match the current visit, including when the same task/page is reopened.
Closing does not cancel a server mutation or pretend it rolled back; its late
receipt cannot repaint a new editor. Shared controls come from `src/ui/`.

Verify with `node --test tests/slides-editor.test.cjs`, the slide-editor visual
tests, then `npm run check:frontend`.
