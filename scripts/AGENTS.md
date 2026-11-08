# Repository script guidance

## Scope

This directory owns committed generators, checks, migrations, packaging, and
operator utilities. Scripts support an existing authority; they do not become
an undocumented second source of product policy.

## Editing rules

- Generators are deterministic, write only declared outputs, expose a check
  mode where practical, and identify generated files. Update source, generator,
  output, and drift test together.
- `gen_storage_operation_catalog.py` derives the JSON/Rust operation catalog
  and `generated_storage_operations.py` client schema from the sidecar registry;
  never maintain a separate handshake version in the Python wire client.
- `gen_tofudb_ir_contract.py` also consumes `conversation_search_v2.json` for
  native projection limits. Search benchmarks measure a complete logical query,
  including every continuation needed to reach its requested result limit.
  Its authored owner is `tofudb_contract_codegen/`: per-invocation input capture,
  ordered domain validation and output projection. Read that directory's guide
  before changing the generator; the CLI owns only check/write publication.
- Validation scripts are read-only and return actionable nonzero failures.
  Avoid network access and machine-specific assumptions in release gates.
- Migration and maintenance tools require explicit targets, preflight,
  dry-run/confirmation semantics appropriate to risk, idempotency or a resume
  boundary, atomic replacement, and postcondition verification.
- Resolve repository/runtime roots safely; never use broad deletion targets,
  unresolved globs, or implicit current-directory authority.
- `git_land.py` preserves committed and uncommitted source ownership separately;
  generator families and input/output scopes live in
  `lib/project_mod/generated_integration.py`. Merged-result verification lives
  in `lib/project_mod/landing_verification.py` (impact records: only the test
  files the merge can affect run) and landing notices in
  `lib/project_mod/integration_radar.py`. `lane.py` creates one branch plus a
  recycled local checkout per unit of work (`lib/project_mod/lanes.py`). Verify
  both `test_git_land_*.py` suites, `test_landing_verification.py`,
  `test_impact_selection.py`, `test_lanes.py`, `test_integration_radar.py` and
  the generated task-delta test in `test_integration_delivery.py`.
  Landing removes the worktree, so first let any swarm worker that still uses
  it finish (or stop it). In a FUSE worktree, `Entry '<path>' not uptodate.
  Cannot merge` with no visible diff is stale index stat data (tool-run git
  uses `--no-optional-locks`): run `git update-index -q --refresh` and retry;
  never reset.
- `frontend_vendor_assets.mjs` derives standalone KaTeX files, CSS font closure
  and `lib/http_transport/generated_frontend_vendor.py` from the installed lockfile version.
  Author the iframe adapter under `frontend/src/artifacts/`; verify generation
  drift, exported integrity and sandboxed math with the frontend vendor tests.
- `vite_asset_manifest.mjs` owns candidate admission and complete previous-graph
  snapshots; `vite_asset_publication.mjs` owns verified assets before the atomic
  manifest commit. Verify their Node tests and `npm run check:frontend`.
  Publication policy: `docs/FRONTEND_ARCHITECTURE.md`.
- Bound concurrency, memory, output, temporary storage, retries, and subprocess
  lifetimes. Redact credentials and user content.
- Reuse application contracts and storage maintenance ports; do not copy SQL or
  lifecycle logic into a convenience script.
- Native archives use build_native_runtime.py's named compression profiles:
  distribution for transport, local stored gzip blocks for repeated activation.
  Verify byte bounds after gzip closes and before exclusive publication.

## Verification

Run the script's focused test and both write/check paths for a generator. For
cross-cutting changes, run `npm run check:frontend` or the
specific storage/developer-runtime certification command that owns the output.
