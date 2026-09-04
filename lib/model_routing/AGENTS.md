# Model-routing guidance

## Scope

This package owns the `tofu.model-routing/v2` aggregate, owner-aware repository,
legacy import, runtime candidate compilation, health scopes, and route snapshots.
`contracts/model_routing_v2.schema.json` is the field-level authority.

## Boundaries

- Persist one revisioned aggregate per explicit owner through semantic Sidecar
  operations. Never read SQL or server-config files here.
- Store only encrypted secret references in the aggregate. Plaintext may cross
  the repository only for one outbound credential resolution.
- `Route` is computed, never configured. One provider-scoped wire ID maps to
  exactly one Deployment.
- Legacy provider/catalog shapes are migration input only; runtime modules may
  not project v2 state back into them.
- Bound aggregate size, candidate work, health memory, snapshots, and cleanup.

## Verification

Run `pytest -q tests/test_model_routing_v2.py` first, then the focused API,
dispatch, migration, storage, and frontend model-routing tests.
