"""Storage authority warm-up keeper.

Problem: on the native TofuDB authority, the first read against a cold
entity table can take tens of seconds when the data dir sits on a
high-latency volume (network/FUSE mount), far beyond the per-call client
deadline. Abandoned calls discard their warm-up progress, so after every
sidecar (re)start the host can enter a self-sustaining timeout storm:
every reader gives up before any read completes and the verified block
cache never stays resident (observed 2026-09-16/18: 2.3G authority on
DolphinFS, cold ``record.list`` 37-60s+ against a 5s client deadline).

Decision: run one bounded pass of representative reads with generous
deadlines after readiness, then re-touch the same set on a modest interval
to counter block-cache eviction. The lifecycle-owned keeper runs the first
pass without blocking startup. Warm-up never raises into request paths,
and every pass is time-capped.

Alternatives considered: engine-side eager index load (requires rebuilding
and re-certifying the tofu-db binary; deferred), relocating the data dir
off the network volume (rejected: silently changes the durability posture
of the configured authority), raising per-call client deadlines (rejected:
pays the cold cost on every restart inside user-facing requests instead of
once inside a bounded boot pass).
"""

from __future__ import annotations

from concurrent.futures import CancelledError, Future
from concurrent.futures import wait as wait_futures
import json
import os
import sys
import threading
import time
from typing import Any, Callable, Mapping

from lib.identity import PERSONAL_USER_ID
from lib.log import get_logger
from lib.observability import InstrumentedThreadPoolExecutor

logger = get_logger(__name__)

# Representative replay-safe reads, one per hot entity family. Payloads are
# validated against the live authority; keep them minimal so a warm pass is
# millisecond-cheap once the block cache is resident.
def _operations(owner_user_id: int) -> list[tuple[str, Mapping[str, Any]]]:
    owner_prefix = f'{owner_user_id}:'
    return [
        ('conversation.count', {}),
        ('conversation.list', {
            'catalog_page': True, 'limit': 1,
            'include_messages': False, 'settings_keys': [],
        }),
        ('record.list', {
            'namespace': 'evolution.runs.v1', 'prefix': owner_prefix,
            'limit': 1,
        }),
        ('task_results.recovery_pending', {'limit': 1}),
        ('timer.active.count', {}),
        ('credential.list', {'owner_user_id': owner_user_id, 'tenant_id': ''}),
    ]


def _env_seconds(name: str, default: float, *, floor: float) -> float:
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        return max(floor, float(raw))
    except ValueError:
        logger.warning('[StorageWarmup] invalid %s=%r; using %.0fs', name, raw, default)
        return default


def _owner_user_id() -> int:
    return int(os.environ.get('TOFUDB_OWNER_ID', str(PERSONAL_USER_ID)))


def warmup_deadline_seconds() -> float:
    return _env_seconds('TOFU_STORAGE_WARMUP_DEADLINE_SECONDS', 180.0, floor=1.0)


def warmup_boot_budget_seconds() -> float:
    return _env_seconds('TOFU_STORAGE_WARMUP_BOOT_BUDGET_SECONDS', 600.0, floor=10.0)


def keep_warm_interval_seconds() -> float:
    # 0 disables the periodic keeper; the boot pass still runs.
    return _env_seconds('TOFU_STORAGE_KEEP_WARM_INTERVAL_SECONDS', 300.0, floor=0.0)


# One lifecycle-owned demand executor serves all passes. Independent probes
# start without a count window. Only an identical read already owned by this
# client is merged across passes; its physical worker keeps its deadline.
_pool_lock = threading.Lock()
_pool: InstrumentedThreadPoolExecutor | None = None
_inflight: dict[tuple[int, str, str], Future] = {}


def _warmup_pool() -> InstrumentedThreadPoolExecutor:
    global _pool
    from runtime_guards import resolve_resource_budget
    with _pool_lock:
        if _pool is None:
            _pool = InstrumentedThreadPoolExecutor(
                max_workers=resolve_resource_budget('TOFU_AGENT_WORKERS', maximum=sys.maxsize),
                metric_pool='storage-warmup', thread_name_prefix='storage-warmup')
        return _pool


def _submit_tracked(
    pool: InstrumentedThreadPoolExecutor,
    fn: Callable[[], str],
    key: tuple[int, str, str],
) -> Future | None:
    """Merge only the same client/operation/payload while its read is live."""
    with _pool_lock:
        current = _inflight.get(key)
        if current is not None and not current.done():
            return None
        future = pool.submit(fn)
        _inflight[key] = future

    def forget(completed):
        with _pool_lock:
            if _inflight.get(key) is completed:
                _inflight.pop(key, None)

    # Registration must be outside the lock: an already-finished future calls
    # its callback inline. A replaced entry remains owned by its new future.
    future.add_done_callback(forget)
    return future


def run_warmup_pass(
    client,
    *,
    deadline_seconds: float | None = None,
    budget_seconds: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Touch every hot entity family once; fail-soft per operation.

    Returns a small stats mapping for logs/tests. A total budget caps the
    pass so a wedged authority cannot stall the caller forever; operations
    skipped by the budget are reported, never faked as warm.

    The reads are independent cold-cache probes, so they run concurrently
    through one demand executor instead of serially: on a high-latency
    volume the sequential pass paid six round trips of latency where one
    suffices. No local worker count delays accepted independent probes.
    Per-operation deadlines and the total
    budget are unchanged from the sequential contract. Operations still in
    flight from a previous pass are reported under ``deferred`` — merged,
    never duplicated.
    """
    deadline = deadline_seconds if deadline_seconds is not None else warmup_deadline_seconds()
    budget = budget_seconds if budget_seconds is not None else warmup_boot_budget_seconds()
    started = monotonic()
    stats: dict[str, Any] = {
        'warmed': [], 'failed': [], 'skipped': [], 'deferred': [],
        'elapsed_seconds': 0.0,
    }
    operations = _operations(_owner_user_id())

    def _run_one(operation: str, payload: Mapping[str, Any]) -> str:
        remaining = max(1.0, budget - (monotonic() - started))
        try:
            client.query(operation, payload, deadline=min(deadline, remaining))
            return 'warmed'
        except Exception as exc:  # fail-soft: warm-up never raises into boot/request paths
            logger.warning('[StorageWarmup] %s warm-up read failed: %s', operation, exc)
            return 'failed'

    if not operations:
        stats['elapsed_seconds'] = round(monotonic() - started, 3)
        return stats
    pool = _warmup_pool()
    futures: dict[str, Future] = {}
    for operation, payload in operations:
        key = (id(client), operation, json.dumps(payload, sort_keys=True))
        try:
            future = _submit_tracked(
                pool, lambda op=operation, pl=payload: _run_one(op, pl), key)
        except Exception as exc:
            # A genuine unstarted worker failure affects this probe only. Prior
            # published reads remain tracked; later probes still get a chance.
            logger.warning('[StorageWarmup] %s could not start: %s', operation, exc)
            stats['failed'].append(operation)
            continue
        if future is None:
            # A previous pass owns this identical read. Independent clients
            # and payloads keep their own work.
            stats['deferred'].append(operation)
            continue
        futures[operation] = future
    _done, not_done = wait_futures(
        tuple(futures.values()), timeout=budget)
    for operation, future in futures.items():
        if future in not_done:
            # Over budget: honest skip, never faked as warm. The
            # operation's own deadline still bounds its thread, and the
            # next pass defers to it instead of stacking a duplicate.
            stats['skipped'].append(operation)
            continue
        try:
            outcome = future.result()
        except CancelledError:
            outcome = 'skipped'
        except Exception as exc:
            logger.warning('[StorageWarmup] %s worker failed: %s', operation, exc)
            outcome = 'failed'
        stats[outcome].append(operation)
    stats['elapsed_seconds'] = round(monotonic() - started, 3)
    return stats


_keep_warm_lock = threading.Lock()
_keep_warm_stop: threading.Event | None = None
_keep_warm_thread: threading.Thread | None = None


def start_keep_warm(
    *,
    client_factory: Callable[..., Any] | None = None,
    interval_seconds: float | None = None,
    deadline_seconds: float | None = None,
    initial_pass: bool = False,
) -> threading.Thread | None:
    """Start the bounded keeper, optionally with an immediate initial pass.

    Initial warming runs off the readiness path and still runs when interval
    zero disables periodic warming. One lifecycle-owned thread per process;
    repeated calls return the live thread. Every pass is bounded and fail-soft.
    """
    global _keep_warm_stop, _keep_warm_thread
    interval = interval_seconds if interval_seconds is not None else keep_warm_interval_seconds()
    if interval <= 0 and not initial_pass:
        logger.info('[StorageWarmup] periodic keep-warm disabled')
        return None
    if client_factory is None:
        from lib.storage import get_storage_client
        client_factory = get_storage_client
    deadline = deadline_seconds if deadline_seconds is not None else warmup_deadline_seconds()
    with _keep_warm_lock:
        if _keep_warm_thread is not None and _keep_warm_thread.is_alive():
            return _keep_warm_thread
        stop = threading.Event()

        def _loop() -> None:
            first = initial_pass
            while not stop.is_set():
                if not first and (interval <= 0 or stop.wait(interval)):
                    break
                budget = warmup_boot_budget_seconds() if first else max(deadline, interval)
                first = False
                try:
                    stats = run_warmup_pass(
                        client_factory(),
                        deadline_seconds=deadline,
                        budget_seconds=budget,
                    )
                    if stats['failed']:
                        logger.warning('[StorageWarmup] keep-warm pass degraded: %s', stats)
                    else:
                        logger.info('[StorageWarmup] keep-warm pass ok in %.1fs',
                                    stats['elapsed_seconds'])
                except Exception as exc:  # client factory can raise while sidecar restarts
                    logger.warning('[StorageWarmup] keep-warm pass aborted: %s', exc)

        thread = threading.Thread(
            target=_loop, name='tofu-storage-keep-warm', daemon=True)
        thread.start()
        _keep_warm_stop, _keep_warm_thread = stop, thread
        logger.info('[StorageWarmup] keeper initial=%s interval=%.0fs (per-op deadline %.0fs)',
                    initial_pass, interval, deadline)
        return thread


def stop_keep_warm(*, timeout: float = 5.0) -> None:
    """Signal the keeper to stop; tests and shutdown own the lifecycle."""
    global _keep_warm_stop, _keep_warm_thread, _pool
    with _keep_warm_lock:
        stop, thread = _keep_warm_stop, _keep_warm_thread
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        # A timed-out join is not a stopped owner. Keep the handle so another
        # start cannot create a second keeper while the bounded pass drains.
        if thread is None or not thread.is_alive():
            _keep_warm_stop, _keep_warm_thread = None, None
            with _pool_lock:
                pool, _pool = _pool, None
            if pool is not None:
                # No keeper can submit again. Unstarted probes cancel; running
                # reads remain physically owned until their deadline/completion.
                pool.shutdown(wait=False, cancel_futures=True)
