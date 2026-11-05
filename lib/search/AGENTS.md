# Search host composition

- `search_runtime.py` owns lazy dependency activation, runtime shutdown and
  explicit worker submission. It binds provider snapshots/config/task scope in
  a fresh context; each worker owns its file handoffs and its caller joins it.
- `search_bridge.py` installs host model/auth/browser providers and translates
  their results. It explicitly re-exports the existing browser-scope entry points.
- `browser_scope.py` owns explicit owner/device/profile selection, immutable
  worker bindings and one-time file response handoffs. Select once per invocation;
  activate the captured binding inside each worker. Never copy HTTP/actor context
  or infer a fresh device when that binding represents an unavailable browser.
- The standalone search pipeline remains in `packages/tofu-search/`; host
  providers implement its public seams rather than duplicating orchestration.

Read `docs/modules/browser_automation.md` and `docs/modules/tools_execution.md`.
Verify `tests/search/`, streaming search/fetch delegation, browser file-transfer,
provider and server-download contracts. Scope-only imports must not activate the
optional search dependency or load browser execution infrastructure.
