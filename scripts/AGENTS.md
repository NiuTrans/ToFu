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
- Validation scripts are read-only and return actionable nonzero failures.
  Avoid network access and machine-specific assumptions in release gates.
- Migration and maintenance tools require explicit targets, preflight,
  dry-run/confirmation semantics appropriate to risk, idempotency or a resume
  boundary, atomic replacement, and postcondition verification.
- Resolve repository/runtime roots safely; never use broad deletion targets,
  unresolved globs, or implicit current-directory authority.
- Bound concurrency, memory, output, temporary storage, retries, and subprocess
  lifetimes. Redact credentials and user content.
- Reuse application contracts and storage maintenance ports; do not copy SQL or
  lifecycle logic into a convenience script.

## Verification

Run the script's focused test and both write/check paths for a generator. For
cross-cutting changes, run `make docs-check`, `npm run check:frontend`, or the
specific storage/developer-runtime certification command that owns the output.
