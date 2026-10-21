#!/usr/bin/env python3
"""A spent shared budget is not a translation incident; a cancellation is not a failure.

Root cause this guards (2026-10-21 boot): the boot sweep starts ~125
translations that all share one turn budget. ``_TranslationDeadline.exceeded()``
folds "owner cancelled" and "budget spent" into the same True, and the loop top
checked that one predicate first. Two consequences, both wrong:

  * a cancellation was reported as ``deadline exhausted`` at ERROR;
  * a chunk whose budget was already spent before ANY dispatch (``attempts=0``,
    ``dispatch_fails=0``) — i.e. nothing failed here — was also an ERROR.

Together they produced ~95 ERROR lines plus coalescing noise in one boot while
the actual translation work was healthy.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
        tests/translate/test_translate_give_up_log_level.py -v
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

pytestmark = pytest.mark.unit


def _run(monkeypatch, *, abort=False, spent_budget=False, dispatch_fails=False):
    """Drive _translate_one_chunk to its give-up path and collect log rows."""
    from lib.translate.engine import _engine

    monkeypatch.setattr(_engine, '_try_translation_fast_paths',
                        lambda *a, **k: None)
    monkeypatch.setattr(_engine, '_translation_chunk_attempt_limits',
                        lambda chars: (30, 1, 1))
    if dispatch_fails:
        from lib.llm.llm_errors import RateLimitError
        monkeypatch.setattr(
            _engine, '_dispatch_translation_candidate',
            lambda *a, **k: (_ for _ in ()).throw(RateLimitError('all keys busy')))

    rows = []
    errors = []

    class _Grab(logging.Handler):
        def emit(self, record):
            rows.append((record.levelno, record.getMessage()))

    handler = _Grab()
    log = _engine.logger
    log.addHandler(handler)
    previous = log.level
    log.setLevel(logging.DEBUG)
    try:
        try:
            _engine._translate_one_chunk(
                'hello world', 'translate to chinese', chunk_label='[t]',
                source='en', target='zh', use_cache=False, allow_mt=False,
                overall_deadline=(0.0 if spent_budget else 0.4),
                abort_check=(lambda: True) if abort else (lambda: False),
            )
        except BaseException as exc:  # noqa: BLE001 — reporting is under test
            errors.append(exc)
    finally:
        log.removeHandler(handler)
        log.setLevel(previous)
    return rows, errors


def _giving_up(rows):
    return [row for row in rows if 'Giving up after' in row[1]]


def test_spent_budget_without_a_failed_dispatch_is_not_an_error(monkeypatch):
    rows, _ = _run(monkeypatch, spent_budget=True)
    give_ups = _giving_up(rows)
    assert give_ups, 'expected a give-up row, got %r' % (rows,)
    level, message = give_ups[-1]
    assert 'attempts=0' in message and 'dispatch_fails=0' in message
    assert level < logging.ERROR, (
        'a budget already spent by the caller was logged as an incident: %r'
        % (give_ups,))
    assert level >= logging.WARNING, 'the give-up must stay visible'


def test_owner_cancellation_raises_abort_rather_than_giving_up(monkeypatch):
    from lib.llm.llm_errors import AbortedError

    rows, errors = _run(monkeypatch, abort=True)
    assert any(isinstance(exc, AbortedError) for exc in errors), (
        'cancellation must surface as AbortedError, got %r' % (errors,))
    assert not _giving_up(rows), 'cancellation reported as a give-up: %r' % (rows,)


def test_cancellation_does_not_emit_a_deadline_error(monkeypatch):
    rows, _ = _run(monkeypatch, abort=True)
    elevated = [row for row in rows if row[0] >= logging.ERROR]
    assert not elevated, 'cancellation produced ERROR rows: %r' % (elevated,)


def test_a_real_dispatch_failure_still_logs_at_error(monkeypatch):
    """Regression guard: the downgrade must not swallow genuine failures."""
    rows, errors = _run(monkeypatch, dispatch_fails=True)
    give_ups = _giving_up(rows)
    assert give_ups, 'expected a give-up row, got %r' % (rows,)
    assert 'dispatch_fails=1' in give_ups[-1][1]
    assert give_ups[-1][0] == logging.ERROR, (
        'a failed dispatch lost its ERROR level: %r' % (give_ups,))


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
