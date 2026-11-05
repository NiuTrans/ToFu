# Paper workflow guidance

## Scope

This package owns paper metadata/library behavior and grounded report, insight,
QA, recommendation, podcast, survey, and translation workflows. Parsing,
generic translation, motion video, and production substrate have separate
owners. Read `docs/modules/ingest_media.md`.

`podcast_engine/service.py` owns report-gated, cache-aware admission through
the production runtime's atomic claim. HTTP routes parse/serialize; the worker
keeps script/audio validation and publication. Verify podcast admission and API
contracts together when changing either side.
Worker-only specs construct carriers in `tests/support/paper_podcast.py`; do not
retain a second production creation entry solely for those tests.

Writing-side modules: `citations.py` is the PID-anchored citation stack
(OpenAlex search, Crossref BibTeX, verification audit — see
`docs/PAPER_CITATIONS.md`); `checks.py` is the submission-readiness gate and
`review_dossier.py` the deterministic review/scaffold text builders (see
`docs/PAPER_QUALITY.md`). All .bib/.tex mutations go through the journaled
project write tools; nothing here calls a model directly.

`citation_audit_pages.py` owns source snapshots and continuation positions for
the citation tool. A cursor carries no earlier evidence; source edits invalidate
it. Verify citation paging, coverage and stack suites for scan limits, deadline,
retry, metadata refresh and handler continuation.

## Editing rules

- Address papers, jobs, checkpoints, artifacts, and searches by explicit owner
  and stable paper/content identity. Never infer ownership from a filesystem
  path or UI selection.
- Upload and library mutations are atomic and content-hash aware. Durable state
  uses semantic storage operations; temporary extraction/render data has a
  bounded cleanup lifecycle.
- Engines remain grounded in declared source pages/evidence. Separate retrieval,
  model generation, validation, and publication so failures are attributable.
- Long workflows checkpoint only validated stages, resume idempotently, and
  invalidate dependent suffixes when inputs change. Abort propagates through
  model, parser, renderer, and artifact work.
- Use shared LLM dispatch, tools, translation, production, and artifact ports;
  do not create private provider transports or task settlement.
- Preserve explicit caller selections and execution budgets; do not impose
  private source/page/artifact admission quotas. Transport/context projections
  and reconstructible idle retention retain their declared lifecycle.
- `images/figures.py` retains all detected figures by default; versioned cache
  reuse requires matching count/width and successful extraction. Verify
  `tests/pdf_parser/test_pdf_classic_bounds.py`.

`tools.make_paper_task_tool_executor` owns full-catalog tool rows, live events
and bounded feedback for report, Q&A and Deepen. Reports explicitly include
the model-round index in live events; workflow-specific streaming, publication
and terminal cancellation/error policies stay in their workers.

`report_engine/_body.py` owns report stream text, per-round draft withdrawal
and terminal-response convergence. The worker keeps model/tool invocation,
usage and post-processing, and composes publication with task settlement. Verify
`test_paper_report_body_projection.py` together with report abort, dedup and
artifact-integrity contracts when changing either side.

`report_engine/_publication.py` confirms the report repository write and
prepares title/enrichment/completion fields through explicit ports. The worker
passes it to `TaskRuntime.finish(commit_result=...)`; no paper-local lock or
terminal state machine may replace that claim. Verify publication races,
storage refusal, request-local isolation and shared completion contracts.

`report_engine/_hooks.run_report_postpasses` owns ordered, best-effort additive
passes. The worker admits only published canonical reports; individual hooks
own their gates and artifact/usage updates. Verify postpass order, isolated
phase failures and process-control propagation in `test_paper_report_postpass_order.py`.

`report_metadata.py` is the dependency-free usage/recorded-cost projection
shared with SQL merge/accumulation operations and failed-write hook projection. It owns no pricing lookup,
identity, lock, storage or replay state. Verify metadata projection and
concurrent second-pass contracts when changing it.

`report_markup.py` owns report Markdown/math parsing through Python-Markdown's
stash lifecycle. Export routes own identity, images and response policy; math
asset versions come from `lib/http_transport/generated_frontend_vendor.py`. Verify
`test_report_markup.py`, report export confinement and standalone math browser
tests when changing this boundary.

`agent_usage.py` owns shared accounting snapshots and strict meter
restoration; its restoration guards live in
`tests/research/test_research_meter_recovery.py`. Workflow owners must separately
restore loop progress and task ownership before executing a continuation.

No Paper stage declares a default spend or dispatch envelope, and no stage may
reintroduce one as a configuration knob. A loop ends on the shared no-progress
breaker, cancellation, or a caller-frozen `logical_budget`; a spent caller
envelope raises `PaperAgentBudgetExceeded` before the next paid dispatch and
never removes the model's tools. The specification is
`tests/paper/test_paper_agent_loop_policy.py`.

## Verification

Run the focused paper engine/library test, then owner-isolation, atomic upload,
grounding, checkpoint/resume, abort, and API/frontend neighbors from the ingest
domain map.

Independent paper/research judge, terminology and podcast work uses production
`owned_results` with a copied caller context per job. No local worker window
or mandatory first-call warm-up may gate these siblings; original budget
ports, caller-owned result order and joined cleanup remain authoritative.
