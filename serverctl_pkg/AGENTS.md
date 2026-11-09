# Server control support guidance

## Scope

This package contains reusable support for `serverctl.py`. Server lifecycle
ownership remains in the application lifecycle modules; conversation diagnosis
begins with `debug/inspect_conversation.py`.

`native_engine.py` owns compiled source identity, bounded staging/build/download, and
pre-shutdown preparation. It never opens user databases or restarts a service.
Manifest parses are reused only within one layout/fingerprint operation; retain
child timeout/failure isolation, standard-library-only startup (`-I -S`), and
fresh reads on the next call. Verify identity,
root-input and manifest-read tests before measuring startup/recovery costs.
Source hashing streams the existing length-prefixed protocol with bounded reads;
file growth, truncation or changed metadata refuses identity. Verify
`tests/server/test_native_engine_streamed_identity.py` and compiled identity parity.
Compiler snapshots preserve optional watched roots and a stable release profile;
`local_placement.py` selects their reconstructible local cache. Verify real Cargo
freshness, transitive edits, private-cache ownership and fallback with
`test_native_engine_identity.py` and `test_restart_build_cache.py`.

`application_restart.py` runs artifact preparation and deferred manager handoff
from current disk code in bounded child interpreters; the application retains
approval and task-interruption policy.
`restart_budget.py` composes all nested preparation deadlines. Frontend and
manager preparation share native_engine's subprocess cleanup owner; verify
real descendant cancellation and manager/CLI roundtrips when changing it.

`resident_service.py` owns manager-bound PID identity, the inherited process
lease, launch and joined stop. Resource policies specialize its preparation
and identity ports. `native_storage_service.py` supplies the storage policy;
`native_storage_host.py` owns the selected `StorageRuntime` across Web
restarts. Preserve configured data roots, v2 identity admission, manager exec
retention, terminal crash budgets and drained upgrades. The full contract and
real-process verification are in `docs/NATIVE_STORAGE_LIFECYCLE.md` and
`tests/server/test_native_storage_service.py`.

Every module the manager imports unconditionally belongs to
`supervisor_protocol.SUPERVISOR_SOURCE_FILES`, its generation identity;
otherwise an update to it never reloads the manager. The real re-exec test in
`tests/server/test_supervisor.py` starts the manager from exactly that closure.
A running manager hashes disk with its own loaded list; the client reloads to
that digest, so extending the list re-execs in place. A refused reload is a
typed error, never a signal: manager exit drains its resident hosts.

`execution_service.py` / `execution_host.py` own the resident headless
application process. `execution_lifespan.py` awaits the existing BootRunner
and application shutdown; it never runs an agent loop. Both resident hosts
borrow the manager's inherited EOF channel, verified by pipe identity, and
retain the launch lease through physical cleanup. Verify
`tests/server/test_execution_service.py` and `test_execution_lifespan.py`.
`ExecutionService.recover` and `readiness_error` own executor reconciliation
and readiness; the manager injects its existing failure budget and retains the
single recovery record. Verify `test_execution_composition.py` for those ports.
`execution_plan.py` prepares a source/environment/configuration digest before
host admission; it uses the existing pinned-source discovery boundary and
`runtime_replica`'s install fingerprint, and never copies an app.
It runs the shared static storage boundary before drain. Only a source mismatch
during application construction may authorize source refresh after physical
exit; startup failures and policy/dependency changes keep the generation fence.
The manager prepares both resident generations before drain, preserves unchanged
execution across API restarts, and charges execution recovery to its existing
failure budget. Verify `test_execution_composition.py` and the real headless
`test_execution_application.py`; readiness must include execution ownership.
SQL hosts publish their existing v1 handoff; verify
`tests/server/test_sql_storage_service.py` without relaxing distributed admission.

## Editing rules

- Keep control commands thin wrappers over declared health, lifecycle,
  diagnostic, and maintenance interfaces.
- Support bundles are bounded, deterministic inventories. Redact credentials,
  cookies, request bodies, prompts, user documents, database contents, and
  machine-specific secrets before any archive is written.
- Distinguish durable user state from reconstructible logs/cache. Collection or
  cleanup must never silently remove durable state.
- Validate every output/archive path and member name; prevent traversal and
  symlink escape. Clean only temporary paths created by the current command.
- Partial collection must report omitted/failed components instead of claiming
  a complete bundle.

## Verification

Run focused `serverctl` and support-bundle tests, including redaction, size
limits, missing-service behavior, unsafe paths, and partial-failure reporting.
