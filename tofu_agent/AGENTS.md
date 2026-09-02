# Embeddable agent runtime guidance

## Scope and first reads

`tofu_agent/` is the packageable, storage-free agent kernel and lightweight
sidecar, including its small provider setup page. Read
`docs/DEVELOPER_RUNTIME.md` and `docs/HEADLESS_API.md`.

## Editing rules

- Reuse shared task, agent, provider, tool, and MCP execution through explicit
  ports. Do not import full-app routes, repositories, storage-sidecar clients,
  billing, frontend runtime, or global application state.
- Every invocation receives an explicit `PrincipalContext` and owns a transient
  `TaskRuntime`. State is bounded process memory with a declared teardown and
  cannot imply durability across restart.
- Keep sync/async and CLI/server/embed entry points behaviorally aligned on
  validation, cancellation, events, results, and typed failures.
- Provider configuration is explicit, redacted, and atomically stored only in
  the documented developer-runtime location. The setup UI must remain small and
  independent of the full ChatUI.
- Load optional providers and tools lazily. Bound concurrency, queues, context,
  output, retries, subprocesses, and shutdown.
- Public API changes update the headless contract, capability projection,
  package exports, examples, and artifact checks together.

## Verification

Run the focused `test_tofu_agent*` and developer-runtime tests, then
`python3 scripts/check_developer_runtime_artifacts.py`. Build the package for
changes to exports, entry points, setup assets, or distribution metadata.
