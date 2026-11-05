"""Serving-loop feedback lifecycle over one fixed, hardware-independent policy.

Sampling uses one awaited job in the existing asyncio worker pool; no dedicated
thread, provider connection, DB scan, hardware probe or memory observation.
Failure leaves the last acknowledged budgets in place and is retried next tick.
"""
from __future__ import annotations

import asyncio
import threading

from runtime_guards import MAX_TASK_CONCURRENCY, resolve_deployment_mode

from . import ResourceController, install_controller
from .adapters import bind_root
from .contract import catalog_document

# native/storage frame ceiling; fixed, never probe-derived
FRAME_MAXIMUM_BYTES = 1 << 32


def launch_policy(environ) -> dict:
    return {'automatic': resolve_deployment_mode(environ) != 'distributed',
            'root_maximum': MAX_TASK_CONCURRENCY,
            'child_maximum': MAX_TASK_CONCURRENCY,
            'frame_maximum': FRAME_MAXIMUM_BYTES}


def start_controller(runtime):
    from lib.agent_core.admission import controller as admission
    controller = ResourceController()
    controller.launch_policy = launch_policy(runtime.environ)
    bind_root(controller, runtime, admission)
    install_controller(controller)
    return controller


def _feedback_sample(runtime, cancelled):
    """Keep synchronous owner reads off the HTTP serving loop."""
    controller = runtime.resource_controller
    before = controller.snapshot()
    if cancelled.is_set() or runtime.shutdown_requested.is_set():
        return before, before
    return before, controller.tick()


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
