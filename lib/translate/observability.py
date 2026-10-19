"""Translation-only diagnostic scopes; never alter output or retry policy.

Public: translation_operation, observed_engine, observed_submission. Generic
HTTP/dispatch observers depend only on lib.llm.diagnostic_timing. Cross-thread
binding carries only these diagnostics, not authentication or arbitrary context.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps

from lib.llm.diagnostic_timing import (
    DiagnosticTiming, bind_timing, current_parent, current_timing, timing_stage,
)

from lib.log import get_logger

logger = get_logger(__name__)


@contextmanager
def translation_operation(kind, **identity):
    existing = current_timing()
    timing = existing or DiagnosticTiming(kind=kind, identity=identity)
    outcome = 'completed'
    try:
        with bind_timing(timing, current_parent() if existing else None):
            yield timing
    except BaseException as error:
        outcome = type(error).__name__
        raise
    finally:
        if existing is None:
            timing.complete(outcome)


def observed_engine(function):
    @wraps(function)
    def run(*args, **kwargs):
        with translation_operation('engine') as timing:
            with timing_stage('engine_total') as phase:
                try:
                    result = function(*args, **kwargs)
                except BaseException as error:
                    timing.finish_open_children(phase.ordinal, error)
                    raise
            if (isinstance(result, tuple) and len(result) == 2
                    and isinstance(result[1], dict)):
                trace = result[1].get('_translate_trace')
                if isinstance(trace, dict):
                    trace['timing'] = timing.snapshot()
            return result
    return run


def observed_submission(submit, job_id, function, *, kind, identity=None):
    """Queue + worker evidence, including unstarted cancellation/admission errors.

    The Future remains the original lifecycle authority. An independently opened
    scope ends at worker completion, NOT at durable task settlement. If a send
    times out first, its summary names outstanding spans; late completions are
    separate phase records and never retroactively claim caller success.
    """
    existing = current_timing()
    timing = existing or DiagnosticTiming(kind=kind, identity={'job_id': job_id, **(identity or {})})
    parent = current_parent() if existing else None
    with bind_timing(timing, parent):
        queued = timing.start('lane_queue')

    def worker():
        with bind_timing(timing, parent):
            queued.finish()
            with timing_stage('lane_worker'):
                return function()

    try:
        future = submit(worker)
    except BaseException as error:
        queued.finish(error)
        if existing is None:
            timing.complete('admission_failed')
        raise

    def settled(done):
        # Done callbacks are diagnostic only and cannot replace Future results.
        try:
            if done.cancelled():
                queued.annotate(queue_cancelled=True)
                queued.finish()
                outcome = 'cancelled_before_worker'
            else:
                error = done.exception()
                queued.finish(error)
                outcome = type(error).__name__ if error else 'worker_completed'
            if existing is None:
                timing.complete(outcome)
        except Exception as diagnostic_error:
            # Diagnostic Future inspection/publication failed. The Future and
            # its result/exception remain the untouched lifecycle authority.
            logger.debug('settled failure handled: %s', type(diagnostic_error).__name__)
            pass

    future.add_done_callback(settled)
    return future


def observed_turn_command(function):
    """Observe translation-enabled create commands without changing their DTOs."""
    @wraps(function)
    def run(*args, **kwargs):
        from collections.abc import Mapping
        from lib.conv_config import resolve_auto_translate
        body = kwargs.get('body', args[3] if len(args) > 3 else None)
        config = body.get('config') if isinstance(body, Mapping) else None
        if not isinstance(config, Mapping) or not resolve_auto_translate(config):
            return function(*args, **kwargs)
        conversation_id = kwargs.get('conversation_id', args[1] if len(args) > 1 else None)
        existing = current_timing()
        with translation_operation('turn_command', conversation_id=conversation_id) as timing:
            with timing_stage('turn_command_total'):
                result = function(*args, **kwargs)
            if existing is None:
                timing.complete('command_aborted' if result.value.get('aborted') else 'command_accepted')
            return result
    return run
