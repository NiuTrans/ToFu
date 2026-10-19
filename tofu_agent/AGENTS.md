# Embeddable agent runtime guidance

## Scope and first reads

`tofu_agent/` is the packageable, storage-free agent kernel and lightweight
sidecar, including its small model-routing setup page. Read
`docs/DEVELOPER_RUNTIME.md` and `docs/contracts/HEADLESS_API.md`.

## Editing rules

- Reuse shared task, agent, provider, tool, and MCP execution through explicit
  ports. Do not import full-app routes, repositories, storage-sidecar clients,
  billing, frontend runtime, or global application state.
- Every invocation receives an explicit `PrincipalContext` and owns a transient
  `TaskRuntime`. State is bounded process memory with a declared teardown and
  cannot imply durability across restart.
- Keep sync/async and CLI/server/embed entry points behaviorally aligned on
  validation, cancellation, events, results, and typed failures.
- Model-routing configuration uses the complete v2 contract, is redacted, and
  is atomically stored only in the documented developer-runtime location. The
  setup UI must remain small and independent of the full Tofu application.
- Load optional providers and tools lazily. Bound concurrency, queues, context,
  output, retries, subprocesses, and shutdown.
- Public API changes update the headless contract, capability projection,
  package exports, examples, and artifact checks together.

`native_runtime.py` owns optional trusted Rust SDK composition, borrowing the
admitted host/loop/callback executor. Request JSON cannot select these owners.
CLI run/serve/doctor may own the local archive/profile, serving loop and launch-sized
callback executor; join runtime/host before callback retirement. Never fall back
to Python after native admission fails. Native result/close waits for worker and client cleanup; same-loop sync calls
refuse. Python and native SDK results retain admission and borrowed resources
until worker completion and resource disposal. A close timeout retains ownership
for retry. Transient file-history completion runs on that existing worker;
verify held-history installed SDK and public runtime cleanup fault tests.
MCP selectors remain task-local for transient named conversations.

## Verification

Run the focused `test_tofu_agent*` and developer-runtime tests, then
`python3 scripts/check_developer_runtime_artifacts.py`. Build the package for
changes to exports, entry points, setup assets, or distribution metadata.

`native_repository.py` defines optional trusted repository configuration for
AgentRuntime/local and native application composition. Construction is lazy; task scopes use the shared execution
resource stack and runtime close joins active work before disposing sealed code.
Verify `tests/repo_map/test_native_application_runtime.py` and installed artifact
membership. This option is not a request/CLI selector or a default admission.
Its `from_launch_policy` factory reuses issued frame capacity and canonical
source-read bounds. Verify capacity refusal, SDK laziness and installed scans
in `tests/repo_map/test_native_repository_capacity.py` and the SDK runtime suite.

Installed native model/tool verification is `test_native_model_loop_installed.py`:
explicit wheel/dependency paths, `-S`, no configured-slot override, no source
imports, shared scan image and joined teardown. Shared typed failure contracts
live in `lib.agent_core.storage_errors`, never the excluded storage package.

`NativeRepositoryConfiguration.from_launch_policy` defaults its explicit native
configuration to the installed pinned grammar bundle; a supplied directory remains
a trusted external override. Construction stays lazy. Verify native grammar bundle/
stream tests and actual installed SDK owner/close before performance admission.
