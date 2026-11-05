# Process services

- `server_background_services.py` owns role-scoped worker start/stop and
  readiness-gated catalog/plugin work. Keep storage-before-start order,
  reentrant role latches, shutdown-before-ready suppression and incomplete-join
  latches intact. HTTP registration has no lifecycle role.
- `server_reexec.py` owns restart preparation, Supervisor delegation and the
  in-place drain/exec boundary. HTTP callers retain approval and response
  flushing; auto-restart retains its idle checks. Every caller uses this owner.
- `server_shutdown.py` joins producers before dependencies; storage release is
  an explicit certificate required by re-exec. Follow `docs/DEFENSIVE_PATTERNS.md`.
- Authority: `docs/modules/infra_runtime.md`. Verify the focused `tests/server/`
  specifications, `tests/auto_restart/`, `tests/server_reexec/`, lifecycle
  approval tests and `scripts/check_architecture.py`.
