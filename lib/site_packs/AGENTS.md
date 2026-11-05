# Site packs engine guidance

## Scope and first read

This package owns the site-pack engine: manifest validation, three-layer
loading, the step-pipeline interpreter, and the adapter-registry projection.
Read `docs/SITE_PACKS.md` and `contracts/site_pack_v1.schema.json` first.
Auth catalog, tool descriptions, and Settings rows are projections of this
registry — never hand-maintain a copy elsewhere.

- `_contract.py` owns cached schema access. `_inputs.py` derives numeric input
  constraints/defaults and traverses nested steps; `_templates.py` owns the
  parameter grammar shared by admission and rendering.
- `_manifest.py` admits a copied document and builds operation schemas;
  `_interpreter.py` captures executable steps for already-admitted adapter calls.
  `_projection.py` keeps the public and executed input schemas identical.
- JSON payload source/status/byte-budget admission lives in
  `lib/browser/read_pipeline.py`; the interpreter carries the observed page URL
  from the capture envelope and does not duplicate acceptance or invent sources.

## Editing rules

- The step set is closed: new step types start in the contract, then the
  interpreter, then capability derivation — never interpreter-first.
- Every step carries its budget in the schema; the interpreter rejects
  out-of-range values rather than clamping silently.
- `__init__.py` and the loader stay free of `lib.browser` imports so
  lightweight consumers (auth_sources, tools) skip the browser stack; only
  `_interpreter`/`_projection` may import browser modules, and `_projection`
  is imported lazily by the adapter registry.
- Pack sources are data, hot-reloaded: a malformed drop-in is skipped with a
  warning and never takes the registry down.
- Mutation steps (`fill`/`press`/`click`) run trusted only inside read
  operations' form-fill semantics; `click` belongs to `access="write"`.

## Verification

Run `tests/site_packs/`, adapter schema ownership, and the
adapter/auth suites (`tests/browser/test_browser_native_runtime.py`,
`tests/auth/test_auth_sources_*.py`); bundle behavior lives in
`bundles/*/tests/` (`make test-bundles`).
