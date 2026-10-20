# Demand worker kernel

`executor.py` owns the standard-library-only physical thread/Future lifecycle.
`lib/agent_core/worker_executor.py` supplies app observations; standalone helpers
import `DemandExecutor` directly without booting `lib`. Keep one implementation
of publication, cancellation, initializer failure, quarantine and teardown.
Verify `tests/agent_core/test_agent_worker_executor.py`, server executor lifecycle
and copy-helper tests, then a wheel import in a fresh process.

`DemandThreadPoolExecutor` supplies standalone per-item failed Futures and
joined non-daemon fan-out. `scripts/gen_packaged_demand_workers.py` emits its
private installed-package copy; verify drift and fresh wheel execution after
kernel changes. Package copies are generated, never separate implementations.
`test_worker_start_recovery.py` covers unpublished startup recovery, bounded
refusal, owner withdrawal and observation isolation. Native approval/parallel
tests prove context and occurrence ownership across the application adapter.

`json_frames.py` owns shared immutable JSON file ranges and demand encoding/read
workers and associated blocking transport I/O. Callers inject reuse hints and transport identity; no app imports or
admission quotas belong here. Verify Push frame-spool and webhook-delivery tests,
including actual joined close after an observation timeout.
