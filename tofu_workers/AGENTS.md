# Demand worker kernel

`executor.py` owns the standard-library-only physical thread/Future lifecycle.
`lib/agent_core/worker_executor.py` supplies app observations; standalone helpers
import `DemandExecutor` directly without booting `lib`. Keep one implementation
of publication, cancellation, initializer failure, quarantine and teardown.
The admitting hold commits an admitted job to one parked carrier's private
inbox, which a carrier already holding the lock may take back before it is
picked up. Whichever carrier takes the job enters its Future, so cancellation,
`cancel_task` and `shutdown(cancel_futures=True)` withdraw it until then.
The robbed carrier, woken anyway, finds a notice and claims like a finished
one; admitted jobs wait for such claimers instead of waking parked carriers.
Surplus retires at completion boundaries and capacity or terminal transitions,
never on admission; only terminal transitions release every parked carrier.
Finished jobs settle lock-free before their Future resolves, and no job
publishes metrics (`metric_snapshot()` is read per scrape).
`test_worker_targeted_wakeup.py` pins the wake-ups and the per-job lock budget.
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

`resources.py` owns stdlib CPU-affinity/cgroup, memory/headroom and volume
observations. `runtime_guards.py` retains app snapshot caching, launch policy
and data-volume selection. `scripts/gen_packaged_search_runtime.py` exports
the same probe into standalone search; edit the source and regenerate.
Verify deployment resource fixtures, resource-control policy and packaged
generation tests; local-model resource policy lives in `docs/RESOURCE_CONTROL.md`.
