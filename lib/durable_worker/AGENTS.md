# Durable execution ownership

This package owns typed kind admission, synchronous claimed execution, lease
supervision and the demand service. Domain handlers retain invocation, effect,
accounting and Turn settlement; this package never replays an unknown effect.

- `__init__.py` retains the public runner/context/registration imports.
- `activity.py` projects active conversation jobs for the existing task
  visibility/restart owner. It preserves explicit scope and propagates read
  failures; API-local absence or age never proves remote completion.
- `runtime.py` is the application composition and lifecycle owner for automation
  and conversation registrations; `contracts/durable_worker_runtime_v1.json`
  retains the existing timing and idle-reuse policy.
- `service.py` composes claim horizons, the shared demand executor, typed
  registrations and one injected wake source. An idle worker count is reuse
  policy, never an admission ceiling.
- `lib/storage/worker_wake.py` owns the remote hint transport. A hint only
  requests a new authority proof; it cannot cancel or authorize an effect.
- State and transport semantics live in
  `contracts/worker_job_lifecycle_v1.json`, `contracts/worker_control_v1.json`
  and `docs/STORAGE.md`.

Verify `tests/storage/test_durable_worker_foundation.py`, worker lease and
suspension suites, `test_worker_service_remote.py`, and the automation runtime
tests. Claim kinds only after their domain's crash, effect and settlement
specifications prove every registration property.
