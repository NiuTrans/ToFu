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
- Every repository CAS uses `registration.prepare_registration_document`; new
  incomplete Models are drafts, never executable candidates. Research belongs
  to `lib/model_catalog/registration_research.py`, not request preparation.
- Bound aggregate size, candidate work, health memory, snapshots, and cleanup.

## Verification

Run `pytest -q tests/model_routing/test_model_routing_contract.py` first, then the focused API,
dispatch, migration, storage, and frontend model-routing tests.

`native_route.py` materializes one canonical primary into a bounded single-use
owner route for the native host. Verify native-route tests and real archive
probe; preserve pre-secret owner/protocol refusal, no endpoint/header override,
trusted inference preparation, output caps and owned credential lifetime.
