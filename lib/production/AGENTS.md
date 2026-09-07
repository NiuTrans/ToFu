# Production substrate guidance

## Scope

This package owns durable, resumable stage execution shared by long-running
deliverable recipes. Read `docs/modules/production.md`.

## Editing rules

- Keep job identity, stage inputs/outputs, versions/digests, checkpoints,
  quality gates, artifacts, and terminal outcomes explicit and durable.
- A retry/resume reuses validated prefixes and invalidates every dependent
  suffix after an input or implementation-version change.
- Concurrent claims, deduplication, restart scans, pruning, abort, and task
  settlement are atomic/idempotent and observable.
- Recipes provide bounded stage implementations through the substrate; they do
  not fork job manifests, retention policy, or publication semantics.
- Separate durable user deliverables/checkpoints from reconstructible temporary
  work. Capacity reclamation never silently deletes durable artifacts.
- Bound active jobs, workers, stage time, retries, manifest/history growth,
  temporary disk, and subprocess/model work.

## Verification

Run `test_production_substrate.py`, `test_production_runtime.py`, and the focused
restart/lifecycle cases, then the consuming recipe's checkpoint, abort, quality,
and publication tests.
