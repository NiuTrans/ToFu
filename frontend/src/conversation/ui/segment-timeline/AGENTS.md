# Settled segment timeline

`index.ts` composes pure display projection and HTML from explicit ports.
`checklist-history.ts` owns accepted history and result-less superseded identity;
all audit occurrences survive. `injections.ts` extracts synthetic rows and
normalizes anchors. `occurrences.ts` matches repeated call IDs by cursor and
keeps legacy no-ID results separate. `batches.ts` attaches results to canonical
execution batches; `context-batches.ts` folds adjacent context display only.
`contracts.ts` defines immutable inputs and presentation ports.

Use canonical `computeExecutionBatches`; do not introduce another attempt
policy. An injection anchor renders once, before its first visible batch;
unmatched anchors render at the tail. Context folding cannot cross an anchored
injection slot. One tool-group session lasts one render; no durable state,
cache, DOM, timer, request or document listener belongs here.

Verify native outcomes with `test_frontend_segment_timeline_owner.py`, public
adapter/translation/negative controls with `test_frontend_segment_timeline.py`
and neighboring tool tests, actual Chromium with
`tests/visual/test_visual_segment_timeline.py`, then `npm run check:frontend`.
