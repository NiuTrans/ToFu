# Paper workflow guidance

## Scope

This package owns paper metadata/library behavior and grounded report, insight,
QA, recommendation, podcast, survey, and translation workflows. Parsing,
generic translation, motion video, and production substrate have separate
owners. Read `docs/modules/ingest_media.md`.

Writing-side modules: `citations.py` is the PID-anchored citation stack
(OpenAlex search, Crossref BibTeX, verification audit — see
`docs/PAPER_CITATIONS.md`); `checks.py` is the submission-readiness gate and
`review_dossier.py` the deterministic review/scaffold text builders (see
`docs/PAPER_QUALITY.md`). All .bib/.tex mutations go through the journaled
project write tools; nothing here calls a model directly.

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

`agent_usage.py` owns shared budget/accounting snapshots and strict meter
restoration; its restoration guards live in
`tests/research/test_research_meter_recovery.py`. Workflow owners must separately
restore loop progress and task ownership before executing a continuation.

## Verification

Run the focused paper engine/library test, then owner-isolation, atomic upload,
grounding, checkpoint/resume, abort, and API/frontend neighbors from the ingest
domain map.

Independent paper/research judge, terminology and podcast work uses production
`owned_results` with a copied caller context per job. No local worker window
or mandatory first-call warm-up may gate these siblings; original budget
ports, caller-owned result order and joined cleanup remain authoritative.
