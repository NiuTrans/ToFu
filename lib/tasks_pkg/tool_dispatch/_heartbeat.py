# HOT_PATH
"""Long-tool heartbeat + serial-dispatch config + pooled execution wrapper.

Houses the ``_SERIAL_BLOCKING_TOOLS`` config table, the heartbeat ticker that
keeps the SSE stream non-silent while a slow tool blocks, and the callable used
by parallel tool workers.
"""

from __future__ import annotations

import os
import threading
import time

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.executor import _execute_tool_one
from lib.tasks_pkg.manager._events import append_event

from lib.tasks_pkg.tool_dispatch._labels import tool_label

logger = get_logger(__name__)


def _execute_tool_one_in_pool(task, tc, fn_name, tc_id, fn_args, rn,
                              round_entry, cfg, project_path, project_enabled,
                              all_tools=None):
    """Stable callable boundary for parallel tool execution.

    Arms the tool's DECLARED timeout (``ToolSpec.timeout_ms_by_name``) when
    one exists. On expiry the call's runtime context is asked to cancel
    cooperatively, and — ONLY when our own timer fired, never on an outer
    abort or the pool ceiling — the late result is replaced with the
    typed timeout result, retaining any captured classified failure and its
    recovery instructions. The pool ceiling (``TOOL_PARALLEL_TIMEOUT``) remains
    the outer bound for non-cooperative handlers; the ``_SERIAL_BLOCKING``
    human-wait exemptions never arm a declared timeout.
    """
    timeout_ms = None
    if not _is_exempt_wait(fn_name, fn_args or {}):
        from lib.tools.registry import declared_tool_timeout_ms
        timeout_ms = declared_tool_timeout_ms(fn_name)
    if timeout_ms is None:
        return _execute_tool_one(task, tc, fn_name, tc_id, fn_args, rn,
                                 round_entry, cfg, project_path,
                                 project_enabled, all_tools=all_tools)
    fired = threading.Event()

    def _on_declared_timeout() -> None:
        fired.set()
        # Cooperative only: request cancellation on this call's runtime
        # context (registered inside _execute_tool_one; retry across the
        # submit→register race like _drain_cancelled_tool_workers does).
        from lib.tasks_pkg.tool_runtime import active_context_for_call
        for _attempt in range(10):
            context = active_context_for_call(
                task, round_num=rn, tool_call_id=tc_id,
                round_entry=round_entry)
            if context is not None:
                try:
                    context.request_resource_cancellation()
                except Exception as exc:  # cancel is best-effort
                    logger.debug('[tool_timeout] cancel failed for %s '
                                 '(tc_id=%s): %s', fn_name, tc_id[:8], exc)
                return
            time.sleep(0.05)

    timer = threading.Timer(timeout_ms / 1000.0, _on_declared_timeout)
    timer.daemon = True
    timer.start()
    try:
        result = _execute_tool_one(task, tc, fn_name, tc_id, fn_args, rn,
                                   round_entry, cfg, project_path,
                                   project_enabled, all_tools=all_tools)
    finally:
        timer.cancel()
    if not fired.is_set() or task.get('aborted'):
        return result
    # Attribution: ONLY our own declared-timer expiry produces this verdict —
    # an outer abort keeps its own semantics (checked above), and the pool
    # ceiling keeps its separate pipeline-level replacement.
    logger.warning('[tool_timeout] %s (tc_id=%s) exceeded its declared '
                   'timeout of %d ms; replacing the late result',
                   fn_name, tc_id[:8], timeout_ms)
    if isinstance(round_entry, dict):
        round_entry['_declaredTimeoutMs'] = timeout_ms
        round_entry['status'] = 'error'
    from lib.tasks_pkg.executor._effects import tool_timeout_result
    return tc_id, tool_timeout_result(
        task, fn_name, round_entry, content=result[1]), False


# ── Serial-dispatch config for long-blocking tools ──────────────────────
# These tools must run serially (outside the thread pool) because they block
# for user input or extended periods, exceeding TOOL_PARALLEL_TIMEOUT.
# Each entry: tool_name → {match: callable(fn_args) → bool, inject: dict of extra args}
#
# Owner decision (2026-07-25,  F4, option A): while a
# task sits in one of these waits, the heartbeat below keeps refreshing
# ``_dispatch_heartbeat``, so the stuck-reaper NEVER reaps it. For
# ``ask_human`` that is the INTENDED semantics — a human may answer hours or
# days later, and there is deliberately NO human-wait timeout.
#
# SCOPE (owner ruling 2026-07-31, ): the exemption covers ONLY
#   the tools in THIS table. The heartbeat used to refresh the reaper clocks
#   for EVERY tool — including run_command, which is why a hung
#   ``grep -rn … ../`` (2.5h, zero output, task 96c56840) was never reaped:
#   the tick proved "the dispatcher thread is alive", not "the tool is
#   producing". Non-exempt tools now get graded ticks (see _emit_tool_heartbeat).
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
}


def _is_exempt_wait(fn_name: str, fn_args: dict) -> bool:
    """True when (fn_name, fn_args) is a ratified human-wait exemption.

    Delegates to the SAME table the serial dispatcher consults, so the
    heartbeat's immunity set and the pipeline's serial set can never drift
    (await_task is exempt only when action='wait'; a status/list call is an
    ordinary tool and gets graded ticks like everything else).
    """
    cfg = _SERIAL_BLOCKING_TOOLS.get(fn_name)
    if not cfg:
        return False
    try:
        return bool(cfg['match'](fn_args or {}))
    except Exception as e:
        logger.debug('[tool_dispatch] exempt-wait match failed for %s: %s', fn_name, e)
        return False


def _emit_tool_heartbeat(task: dict, parallel_items: list, t0: float) -> int:
    """Emit ONE heartbeat tick for the still-in-flight tools of this round.

    Two jobs, and since 2026-07-31 () they are GRADED by whether
    the in-flight tool is a ratified human-wait exemption:

      1. TRANSPORT (every tool): emit a ``tool_progress`` per still-in-flight
         round so the SSE stream stays non-silent (a buffering proxy doesn't
         idle-time-out) and the UI shows "Searching… (Ns)".
      2. REAPER LIVENESS (exempt human-wait tools ONLY): refresh
         ``_dispatch_heartbeat`` and let the tick bump ``_t_last_event``.
         For every other tool the tick is marked ``_selfTick: True`` — it
         keeps the transport alive but is NOT evidence of tool life, so a
         genuinely-hung ordinary tool (zero output >30min) is reaped.
         Real liveness for ordinary tools comes from real events: stdout
         chunks, tool results, deltas, retry phases.

    A round containing a LIVE human-wait tool stays alive on that member's
    unmarked ticks (the round's fate belongs to the human wait). Returns the
    number of progress events emitted (0 when the task is aborted or every
    round already settled).

    Module-level + side-effect-contained so it is directly unit-testable
    (see tests/tools/test_tool_heartbeat.py +
    tests/tools/test_tool_heartbeat_liveness_grading.py).
    """
    in_flight = [
        _it for _it in parallel_items
        if _it[5] and _it[5].get('status') in ('searching', 'executing', None)
    ]
    all_exempt = bool(in_flight) and all(
        _is_exempt_wait(_it[1], _it[3]) for _it in in_flight)
    if all_exempt:
        # Ratified human-wait immunity: a pure human-wait round may sit for
        # days with zero output by design — keep the reaper's positive-
        # liveness clock warm (owner ruling 2026-07-25, scope 2026-07-31).
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
        ev = build_event(
            EventType.TOOL_PROGRESS,
            roundNum=_item[4],
            toolCallId=round_entry.get('toolCallId', ''),
            toolName=round_entry.get('toolName') or fn_name,
            detail='%s… (%ds)' % (tool_label(fn_name), elapsed),
            elapsed=elapsed,
        )
        if not _is_exempt_wait(fn_name, _item[3]):
            # Self-tick: transport keepalive, NOT evidence the tool is alive.
            # append_event skips the _t_last_event bump for marked events;
            # the frontend stalled-card reads the same marker to tell
            # "system pinging itself" apart from "tool actually producing".
            ev['_selfTick'] = True
        append_event(task, ev)
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
