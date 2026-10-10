# Research workbench

The auto-research workbench is a page inside the Evolution workspace
(`#/evolution/research`), mounted lazily by `../workspace.ts`. `page.ts` owns
the page subtree (direction composer, live console, record archive rail) and
client-only teardown; there is no shell mode or topbar entry any more.
`api/` declares shared API/reader ports; `state/` owns ephemeral projections;
`controllers/` owns task subscriptions and workspace CAS; `components/` owns
stable presentation; `testing/` owns scenarios.
`components/manuscript-preview.ts` composes the shared Paper reader/lease owners.
The workspace owns its stable DOM island and releases it when the page unmounts.
`components/history.ts` owns one demand-loaded, read-only archive page and aborts
its transport on workspace identity changes/disposal; verify through
`testing/history.test.cjs` and `tests/research/test_research_history_browser.py`.
`components/observation-recovery.ts` owns task-ID observation inspection and
abort/stale-response fences; verify in `test_research_observation_browser.py`.
`components/submission-gates.ts` presents server decisions; local edits invalidate
them until a matching saved-workspace response arrives.

Contracts: `contracts/research_progress_v1.schema.json` generates
`frontend/src/api/research-progress.generated.ts`. Generic task status/replay
is execution authority; connection failure never changes that status.
Workspace and artifacts stay owner-scoped server state. Explicit experiment,
analysis, compile and publish confirmation is not granted by rendering a button.

Numerical protocol controls use the shared field factory; verify saved rules,
confirmation and draft retention in `tests/research/test_research_numeric_rule_browser.py`.

Controlled task completion with inconclusive science and blocked submission is
verified by `tests/research/test_research_inconclusive_browser.py`.

Verify with `npm run check:research`, research pytest tests, and
`tests/research/test_research_reading_browser.py`. Manuscript faults/lifecycles use
`tests/research/test_research_manuscript_reader.py`; the real compile/read/failure flow is
`tests/research/test_research_compile_browser.py`. Finish with the frontend build/check gate.
Do not restore the deleted Paper research implementations or add a second
subscription, persistence store, or transport.

Protocol drafting carries the opened task ID to the server; historical records
never borrow latest per-direction evidence. Verify
`tests/research/test_research_draft_source_browser.py`.

The Programme entry seeds owner-scoped source-specific drafts in the existing
creator; it never launches work. Verify Programme frontend scenarios and the
live draft-source browser journey before changing navigation or retention.
