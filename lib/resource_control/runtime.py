"""Serving-loop feedback lifecycle, sharing the cached launch probe and policy.

Sampling uses one awaited job in the existing asyncio worker pool; no dedicated
thread, provider connection, DB scan or per-task hardware probe.
Failure leaves the last acknowledged budgets in place and is retried next tick.
"""
from __future__ import annotations

import asyncio
import threading

from runtime_guards import (
    observe_host_memory_available_bytes, resource_budget_manifest,
    resolve_resource_budget, task_concurrency_hard_ceiling,
)

from . import ResourceController, install_controller
from .adapters import bind_root
from .contract import catalog_document


def launch_policy(environ) -> dict:
    manifest = resource_budget_manifest(environ)
    overrides = manifest['overrides']
    def budget(name, maximum, minimum=1):
        return resolve_resource_budget(name, environ, minimum=minimum, maximum=maximum)

    from runtime_guards import MAX_TASK_CONCURRENCY
    workers = budget('TOFU_AGENT_WORKERS', MAX_TASK_CONCURRENCY)
    admission = budget('TOFU_MAX_INFLIGHT_TASKS', MAX_TASK_CONCURRENCY, 0)
    maximum = task_concurrency_hard_ceiling(environ)
    if 'TOFU_AGENT_WORKERS' in overrides:
        maximum = min(maximum, workers)
    if 'TOFU_MAX_INFLIGHT_TASKS' in overrides and admission:
        maximum = min(maximum, admission)
    maximum = max(1, maximum)
    children = budget('TOFU_SWARM_GLOBAL_WORKERS', 64, 0)
    if not children:
        children = budget('TOFU_SWARM_MAX_PARALLEL', 64) * budget('TOFU_SWARM_SESSION_CAPACITY', 128)
    task_bytes = budget('TOFU_TASK_RSS_RESERVE_MB', 16384, 64) * (1 << 20)
    available = manifest['probe']['effective_memory_available_mb']
    return {'automatic': bool(manifest['adaptive']), 'root_maximum': maximum,
            'child_maximum': children, 'task_bytes': task_bytes,
            'frame_maximum': max(128 << 20, int(available or 128) * (1 << 20) // 8)}


def start_controller(runtime):
    from lib.agent_core.admission import controller as admission
    controller = ResourceController()
    controller.launch_policy = launch_policy(runtime.environ)
    bind_root(controller, runtime, admission)
    install_controller(controller)
    return controller


def memory_observation() -> tuple[bool, int | None]:
    from lib.cgroup_guard import process_rss_admission_snapshot, pressure as cgroup_pressure
    # These are local kernel counters. The full launch probe stays cached.
    process = process_rss_admission_snapshot()
    headroom = max(0, process['headroom_bytes'] - process['reserve_bytes']) if process else None
    pressure = bool(process and not process['allowed'])
    snapshot = cgroup_pressure()
    if snapshot and snapshot.get('limit'):
        reclaimable = snapshot.get('reclaimable', 0)
        available = max(0, snapshot['limit'] - snapshot['usage'] + reclaimable)
        headroom = min(headroom, available) if headroom is not None else available
        pressure = pressure or available < (process['reserve_bytes'] if process else 64 << 20)
    host_available = observe_host_memory_available_bytes()
    if host_available is not None:
        headroom = min(headroom, host_available) if headroom is not None else host_available
        pressure = pressure or host_available < (process['reserve_bytes'] if process else 64 << 20)
    return pressure, headroom


def _feedback_sample(runtime, cancelled):
    """Keep synchronous owner/kernel reads off the HTTP serving loop."""
    pressure, headroom = memory_observation()
    controller = runtime.resource_controller
    before = controller.snapshot()
    if cancelled.is_set() or runtime.shutdown_requested.is_set():
        return before, before
    after = controller.tick(memory_pressure=pressure, headroom_bytes=headroom,
                            allow_growth=headroom is not None)
    return before, after


async def run_controller(runtime) -> None:
    interval = catalog_document()['control']['sample_seconds']
    cancelled = threading.Event()
    try:
        while not runtime.shutdown_requested.is_set():
            await asyncio.sleep(interval)
            try:
                if runtime.shutdown_requested.is_set():
                    break
                before, after = await asyncio.to_thread(_feedback_sample, runtime, cancelled)
                if after['revision'] != before['revision']:
                    changes = {name: {'desired': value['desired'], 'effective': value['effective'],
                                      'reason': value['reason'], 'status': value['status']}
                               for name, value in after['resources'].items()
                               if value != before['resources'].get(name)}
                    runtime.logger.info('[Resources] revision=%d changes=%s', after['revision'], changes)
            except Exception as exc:
                runtime.logger.warning('[Resources] adjustment deferred (%s)', type(exc).__name__)
    finally:
        # Cancelling an await cannot kill a synchronous kernel/owner read.
        # Fence its next adjustment without spawning a replacement sampler.
        cancelled.set()
