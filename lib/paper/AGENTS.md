# Paper workflow guidance

## Scope

This package owns paper metadata/library behavior and grounded report, insight,
QA, recommendation, podcast, survey, and translation workflows. Parsing,
generic translation, motion video, and production substrate have separate
owners. Read `docs/modules/ingest_media.md`.

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
- Bound pages, chunks, context, concurrent models, media duration, retries,
  artifacts, and retained intermediate data.

## Verification

Run the focused paper engine/library test, then owner-isolation, atomic upload,
grounding, checkpoint/resume, abort, and API/frontend neighbors from the ingest
domain map.
