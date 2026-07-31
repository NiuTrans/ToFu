# HOT_PATH
"""Long-tool heartbeat + serial-dispatch config + pooled execution wrapper.

Houses the ``_SERIAL_BLOCKING_TOOLS`` config table, the heartbeat ticker that
keeps the SSE stream non-silent while a slow tool blocks, and the pooled
``_execute_tool_one`` wrapper that releases the worker's thread-local DB conn.
"""

from __future__ import annotations

import os
import threading
import time

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.executor import _execute_tool_one
from lib.tasks_pkg.manager import append_event

from lib.tasks_pkg.tool_dispatch._labels import tool_label

logger = get_logger(__name__)


def _execute_tool_one_pooled(*args, **kwargs):
    """Run ``_execute_tool_one`` then release the worker's thread-local DB conn.

    Parallel tools run on a per-round ``ThreadPoolExecutor`` whose threads are
    reused across submissions and only torn down at pool shutdown. A tool that
    touches the DB (memory, conversation, mcp handlers) would otherwise pin a
    connection on its worker thread for the whole round; under high concurrency
    that compounds across tasks and exhausts the connection semaphore. Release
    at the worker boundary so the connection returns to the shared pool.
    """
    try:
        return _execute_tool_one(*args, **kwargs)
    finally:
        try:
            from lib.agent_core.store import get_conversation_store
            get_conversation_store().release_connection()
        except Exception as _ctd_err:
            logger.debug('[tool_dispatch] pooled release_connection failed: %s', _ctd_err)


# ── Serial-dispatch config for long-blocking tools ──────────────────────
# These tools must run serially (outside the thread pool) because they block
# for user input or extended periods, exceeding TOOL_PARALLEL_TIMEOUT.
# Each entry: tool_name → {match: callable(fn_args) → bool, inject: dict of extra args}
#
# Owner decision (2026-07-25, epic pt_1acd0bcdb2174566 F4, option A): while a
# task sits in one of these waits, the heartbeat below keeps refreshing
# ``_dispatch_heartbeat``, so the stuck-reaper NEVER reaps it. For
# ``ask_human`` that is the INTENDED semantics — a human may answer hours or
# days later, and there is deliberately NO human-wait timeout. The accepted
# trade-off, also ratified: a genuinely-hung non-human serial tool (dead
# socket inside await_task/timer_create) is equally immune to the reaper —
# a phantom "running" that clears only on abort/restart. Do NOT "fix" this
# by capping the heartbeat; the cap question was decided as status-quo.
_SERIAL_BLOCKING_TOOLS: dict[str, dict] = {
    'ask_human': {
        'match': lambda _args: True,
        'reason': 'blocks for user input',
    },
    'await_task': {
        'match': lambda args: args.get('action') == 'wait',
        'reason': 'long-blocking, bypasses pool timeout',
        'inject': lambda task, rn: {'_parent_task': task},
    },
    'timer_create': {
        'match': lambda _args: True,
        'reason': 'blocking poll, bypasses pool timeout',
        'inject': lambda task, rn: {'_parent_task': task, '_tool_round_num': rn},
    },
}


def _emit_tool_heartbeat(task: dict, parallel_items: list, t0: float) -> int:
    """Emit ONE heartbeat tick for the still-in-flight tools of this round.

    Refreshes the ``_dispatch_heartbeat`` positive-liveness clock and emits a
    ``tool_progress`` for each round still ``searching``/``executing`` — which
    also bumps ``_t_last_event`` (via ``append_event``) and keeps the SSE stream
    non-silent (so a buffering proxy doesn't idle-time-out and the UI shows
    "Searching… (Ns)"). Returns the number of progress events emitted (0 when
    the task is aborted or every round already settled).

    Module-level + side-effect-contained so it is directly unit-testable
    (see tests/test_tool_heartbeat.py) — the reaper false-reap protection for a
    slow-but-alive tool hinges on this refreshing both clocks.
    """
    # Resolve ``append_event`` through the FACADE so a test that patches
    # ``tool_dispatch.append_event`` on the package is honoured at call time
    # (byte-identical to the pre-split single-module behaviour).
    try:
        import lib.tasks_pkg.tool_dispatch as _facade
        _append_event = getattr(_facade, 'append_event', append_event)
    except Exception as e:
        logger.debug('[tool_dispatch] facade append_event resolve failed, using local: %s', e)
        _append_event = append_event
    task['_dispatch_heartbeat'] = time.time()
    if task.get('aborted'):
        return 0
    elapsed = int(time.time() - t0)
    emitted = 0
    for _item in parallel_items:
        round_entry = _item[5]
        fn_name = _item[1]
        if not round_entry:
            continue
        # Only ping rounds still in-flight (not yet finalized).
        if round_entry.get('status') not in ('searching', 'executing', None):
            continue
        _append_event(task, build_event(
            EventType.TOOL_PROGRESS,
            roundNum=_item[4],
            toolCallId=round_entry.get('toolCallId', ''),
            toolName=round_entry.get('toolName') or fn_name,
            detail='%s… (%ds)' % (tool_label(fn_name), elapsed),
            elapsed=elapsed,
        ))
        emitted += 1
    return emitted


def _start_tool_heartbeat(task: dict, parallel_items: list, tid: str):
    """Start a daemon ticker that calls :func:`_emit_tool_heartbeat` every
    ``TOOL_HEARTBEAT_INTERVAL`` seconds while the parallel-tool wait blocks.

    Returns ``(stop_event, thread)``. The caller MUST ``stop_event.set()`` in
    its ``finally`` so the ticker can't outlive the round. A fast tool finishes
    before the first tick, so it never emits a heartbeat.
    """
    stop = threading.Event()
    t0 = time.time()
    interval = max(2, int(os.environ.get('TOOL_HEARTBEAT_INTERVAL', '15')))

    def _loop():
        while not stop.wait(interval):
            try:
                _emit_tool_heartbeat(task, parallel_items, t0)
            except Exception as e:
                logger.debug('[Task %s] tool heartbeat tick failed: %s', tid, e)

    thread = threading.Thread(target=_loop, name=f'tool-hb-{tid}', daemon=True)
    thread.start()
    return stop, thread
