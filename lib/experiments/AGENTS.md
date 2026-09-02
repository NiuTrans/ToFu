# Experiment framework guidance

## Scope

This package owns versioned experiment plugins, immutable resolved specs,
owner-aware assignment, metric plans, and analyzers. Read
`contracts/experiments_v1.schema.json` and `docs/modules/experiments.md`.

## Editing rules

- Registry discovery is callback-free and fail-soft; activation resolves and
  persists a complete immutable spec with provider versions and digest.
- Assignment hashes an explicit owner and assignment unit without persisting raw
  owner identity in exposure payloads. Filtering occurs before caps/aggregation.
- Keep strategy application, metric extraction, and analysis pure and pinned to
  the resolved spec. Product adapters only activate and report outcomes.
- Preserve version coexistence, mount/unmount rollback, missing-provider and
  digest-drift failures, precommitted inference horizon, and incomplete-metric
  handling.
- Bound catalog size, assignments, outcome projections, metric data, analyzer
  work, and retained reports.
- Experimental evidence cannot silently change production defaults; graduation
  updates the owning product contract and tests explicitly.

## Verification

Run `tests/test_experiment_framework.py`, the focused product adapter test, the
Sidecar outcome-projection case, and the benchmark acceptance gate named in the
experiment domain map.
