# Report article enhancements

`index.ts` composes final/cached sanitized Markdown DOM only. `glossary.ts`
extracts table definitions; `aliases.ts` owns matching names; `terms.ts` decorates
one mention per row/section. `callouts.ts` and `figures.ts` decorate content.
`headings.ts` owns stable local IDs, escaped TOC and inner-scroller navigation.
`lifecycle.ts` owns one host scope, figure/glossary listeners and scroll-spy.
`metadata.ts` renders recorded model, cost breakdown and token usage and
reconciles the article's completion tag without rebuilding its body. Initial
paint, later snapshots and Deepen use the same owner; identical output preserves
the tag node. Verify its co-located tests plus late report/review browser updates.

Tasks, audits, reading-time estimates/anchors and generation remain in their
existing Paper owners. No paid work starts from enhancement. Repaint, skeleton,
ready state and Paper exit must release interactions; failed observer setup
rolls back listeners/observers. Imports stay on the lazy Paper presenter graph.

Verify compiled outcomes with `tests/frontend/test_frontend_report_article.py`,
real reading interactions with `tests/visual/test_visual_report_article.py`,
neighboring report-runtime/reader tests, then `npm run check:frontend`.
