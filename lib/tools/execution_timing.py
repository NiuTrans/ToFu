"""Demand-scoped slow-tool evidence; no workers, extra journal or result mutation.

Entry points: tool_execution_timing and timing_phase. Shared advisory budgets
come from turn_trace_contract. The executor and streaming prefetch use the same
scope; nested scopes restore their parent. Only slow calls emit a WARNING via
lib.log, which already owns incident retention. Evidence never contains args,
results, paths or exception text. Diagnostic failures never fail tool work.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import time

from lib.log import get_logger, log_event
from lib.turn_trace_contract import TOOL_EXECUTION_BUDGETS_MS

logger = get_logger(__name__)
_current = ContextVar('tool_execution_timing', default=None)
_MAX_PHASES = 16


@contextmanager
def timing_phase(name, *, bytes_read=0, bytes_written=0, records=0):
    scope = _current.get()
    if scope is None:
        yield
        return
    # Only owner-defined names and numeric totals; callers may add confirmed
    # output bytes after an atomic write without another filesystem read.
    phase = scope['phases'].get(name)
    if phase is None and len(scope['phases']) < _MAX_PHASES:
        phase = scope['phases'][name] = {'elapsed_ms': 0, 'calls': 0,
            'bytes_read': 0, 'bytes_written': 0, 'records': 0}
    start = time.monotonic()
    try:
        yield phase
    finally:
        if phase is not None:
            phase['elapsed_ms'] += round((time.monotonic() - start) * 1000, 3)
            phase['calls'] += 1
            phase['bytes_read'] += bytes_read
            phase['bytes_written'] += bytes_written
            phase['records'] += records


@contextmanager
def tool_execution_timing(tool_name, *, task_id='', conversation_id='', tool_call_id='', state=None):
    budget = TOOL_EXECUTION_BUDGETS_MS.get(tool_name)
    if budget is None:
        yield
        return
    scope = {'phases': {}}
    token = _current.set(scope)
    start = time.monotonic()
    outcome = 'returned'
    try:
        yield
    except BaseException:
        outcome = 'exception'
        raise
    finally:
        elapsed = (time.monotonic() - start) * 1000
        _current.reset(token)
        if elapsed > budget:
            try:
                if isinstance(state, dict):
                    outcome = state.get('_runtimeState') or outcome
                log_event(logger, logging.WARNING, 'tool_execution_slow',
                    '[ToolLatency] tool=%s elapsed_ms=%d budget_ms=%d task_id=%s conv_id=%s call=%s outcome=%s',
                    tool_name, round(elapsed), budget, str(task_id)[:96],
                    str(conversation_id)[:96], str(tool_call_id)[:96], outcome,
                    tool_name=tool_name, elapsed_ms=round(elapsed), budget_ms=budget,
                    task_id=str(task_id)[:96], conversation_id=str(conversation_id)[:96],
                    tool_call_id=str(tool_call_id)[:96], outcome=outcome,
                    phases=scope['phases'])
            except Exception:
                pass  # Logging is diagnostic, never the tool acknowledgement.
