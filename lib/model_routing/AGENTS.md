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
- Preserve all admitted owner entities and grants; entity counts and aggregate bytes
  do not reject authority commits. Reconstructible health/snapshots and cleanup
  retain their declared lifecycle. Default dispatch retains every eligible
  candidate; an explicit caller shortlist remains a positive size hint.

## Verification

Run `pytest -q tests/model_routing/test_model_routing_contract.py` first, then the focused API,
dispatch, migration, storage, and frontend model-routing tests.

`native_route.py` materializes one canonical primary into a bounded single-use
owner route for the native host. Verify native-route tests and real archive
probe; preserve pre-secret owner/protocol refusal, no endpoint/header override,
trusted inference preparation, output caps and owned credential lifetime.
RouteCandidateCompiler.resolve_model_id reuses its normalized request indexes;
only exact unique logical IDs resolve. Display/wire IDs and creator ambiguity
grant no route. Verify native child-model and canonical routing tests.

Codex OAuth native routes require an explicit owner/destination credential resolver.
Verify tenant fencing, private callback disposal, joined model binding, canonical
cache/replay capabilities and real installed OAuth-route fixtures. Never select
provider-global credentials or infer missing-login from source-read failure.

Explicit priced native routes bind the model policy resolver to issued metadata
including the selected offering's actual_pricing, and dispose it with joined execution.
Verify route pricing ownership, template failures, installed alias/global-price
isolation and full production restart; never infer prices from task config.

Native counter preparation receives detached metadata/template/profile after
selected output clamping. Failure refuses before client launch; it may not edit
caller configuration, messages, catalog, endpoint or credential bindings.
