#!/usr/bin/env python3
"""Attempt-restart truncation — transport/dispatch retries must not leave the
abandoned attempt's partial text stacked inside task['content']/['thinking']
(pt_6e12b1ffd95a453e).

THE LATENT CLASS: a transport retry (``lib/llm/stream.py``'s inner loop) or a
dispatch retry (429/key-rotation in ``lib/llm_dispatch/api.py``) restarts the
request FROM SCRATCH, but the abandoned attempt's deltas already landed in the
task accumulators (and were checkpointed into conversations.messages). The
re-streamed attempt then STACKS on the abandoned tail → duplicated text
persisted / rendered.

THE FIX:
  * ``lib/llm/stream.py::stream_chat`` fires ``on_attempt_restart(reason=…)``
    whenever an in-flight attempt is discarded (transport retry with another
    attempt remaining, or a model-limit clamp retry) — never on success and
    never on the final exhausted failure.
  * ``lib/llm_dispatch/api.py::dispatch_stream`` passes it through to
    stream_chat AND fires it at its own hard-discard points (429 rotation,
    quota/auto key exhaustion).
  * ``lib/tasks_pkg/manager/_stream.py::stream_llm_response`` captures the
    per-round base at entry and truncates back to it on restart; the
    shrink-convergent checkpoint (FloorRetry-residue fix) settles the row.

Failing-first / NEUTER:
  * test_stream_chat_fires_on_transport_retry_never_on_success is RED without
    the stream.py wiring.
  * test_task_truncates_to_round_base_on_restart and
    test_truncation_preserves_prior_round_base are RED without the
    _stream.py pass-through (NEUTER: drop the on_attempt_restart kwarg →
    the partial attempt text stacks).
"""
from __future__ import annotations

import os
from tests.support.chat_tasks import (
    chat_task_fixture_guard,
    chat_task_registry,
)
import sys
import threading as _thr

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

pytestmark = pytest.mark.unit


_HEALTHY_USAGE = {'prompt_tokens': 10, 'cache_read_tokens': 150000,
                  'cache_creation_input_tokens': 1200}


# ── stream.py: the retry loop fires on_attempt_restart exactly right ────────

def test_stream_chat_fires_on_transport_retry_never_on_success(monkeypatch):
    """stream_chat must fire on_attempt_restart once for the discarded first
    attempt (partial content had already streamed), then NOT again when the
    second attempt succeeds."""
    import lib.llm.stream as _stream_mod
    from lib.llm_errors import RetryableAPIError

    monkeypatch.setattr(_stream_mod, 'abortable_sleep', lambda *a, **k: None)
    fires = []
    calls = {'n': 0}

    def _fake_once(body, **kwargs):
        calls['n'] += 1
        oc = kwargs.get('on_content')
        if calls['n'] == 1:
            if oc:
                oc('PARTIAL-')
            raise RetryableAPIError('boom', status_code=503)
        if oc:
            oc('FULL-ANSWER')
        return ({'role': 'assistant', 'content': 'FULL-ANSWER',
                 'reasoning_content': ''}, 'stop', dict(_HEALTHY_USAGE))

    monkeypatch.setattr(_stream_mod, '_stream_chat_once', _fake_once)
    msg, finish, usage = _stream_mod.stream_chat(
        {'model': 'm', 'messages': []},
        on_content=lambda c: None,
        on_attempt_restart=lambda reason='': fires.append(reason))
    assert calls['n'] == 2
    assert len(fires) == 1, f'exactly one discarded attempt → one fire; got {fires}'
    assert 'transport retry' in fires[0]
    assert finish == 'stop'


def test_stream_chat_never_fires_on_clean_success(monkeypatch):
    """A first-attempt success must NOT fire — nothing was discarded."""
    import lib.llm.stream as _stream_mod

    fires = []
    monkeypatch.setattr(_stream_mod, '_stream_chat_once', lambda body, **k: (
        {'role': 'assistant', 'content': 'OK', 'reasoning_content': ''},
        'stop', dict(_HEALTHY_USAGE)))
    _stream_mod.stream_chat(
        {'model': 'm', 'messages': []},
        on_attempt_restart=lambda reason='': fires.append(reason))
    assert fires == [], f'clean success must not fire; got {fires}'


def test_stream_chat_does_not_fire_after_final_exhausted_failure(monkeypatch):
    """When retries are EXHAUSTED the loop raises — no further attempt will
    run, so firing on the last failure would be a lie (nothing restarts)."""
    import lib.llm.stream as _stream_mod
    from lib.llm_errors import RetryableAPIError

    monkeypatch.setattr(_stream_mod, 'abortable_sleep', lambda *a, **k: None)
    fires = []
    calls = {'n': 0}

    def _always_fail(body, **kwargs):
        calls['n'] += 1
        raise RetryableAPIError('boom-%d' % calls['n'], status_code=503)

    monkeypatch.setattr(_stream_mod, '_stream_chat_once', _always_fail)
    with pytest.raises(RetryableAPIError):
        _stream_mod.stream_chat(
            {'model': 'm', 'messages': []},
            on_attempt_restart=lambda reason='': fires.append(reason))
    # attempts = 1 + MAX_STREAM_RETRIES; fires happen only when another
    # attempt WILL run → attempts-1 fires, never on the last failure.
    from lib.llm._transport import MAX_STREAM_RETRIES
    assert calls['n'] == 1 + MAX_STREAM_RETRIES
    assert len(fires) == MAX_STREAM_RETRIES, (
        f'must fire only when another attempt follows; got {fires}')


# ── _stream.py: task accumulators truncate to the per-round base ────────────

def _mk_task(content='', thinking=''):
    return {'id': 'task-ar-1', 'convId': 'ar-conv', 'content': content,
            '_userId': 1, 'status': 'running',
            'thinking': thinking, 'config': {'userId': 1},
            'events': [], 'toolRounds': [],
            'content_lock': _thr.Lock(), 'events_lock': _thr.Lock()}


def _drive_with_restart(task, monkeypatch, partial, restored):
    """Scripted dispatch: attempt 1 streams ``partial``, a restart is
    announced, attempt 2 streams ``restored``. Tolerates a MISSING
    on_attempt_restart kwarg (the NEUTER state) by simply not firing."""
    import lib.tasks_pkg.manager._stream as _mgr
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '0')

    def _fake_dispatch(body, **kwargs):
        oc = kwargs.get('on_content')
        if oc:
            oc(partial)
        restart = kwargs.get('on_attempt_restart')
        if restart:
            restart(reason='simulated transport retry')
        if oc:
            oc(restored)
        return ({'role': 'assistant', 'content': restored,
                 'reasoning_content': ''}, 'stop', dict(_HEALTHY_USAGE))

    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = _fake_dispatch
    with chat_task_fixture_guard:
        chat_task_registry[task['id']] = task
    try:
        return _mgr.stream_llm_response(
            task, {'model': 'm',
                   'messages': [{'role': 'user', 'content': 'go'}]}, tag='R1')
    finally:
        _mgr.dispatch_stream = _orig
        with chat_task_fixture_guard:
            chat_task_registry.pop(task['id'], None)


def test_task_truncates_to_round_base_on_restart(monkeypatch):
    """NEUTER-verifiable: with the pass-through wired, the abandoned attempt's
    partial text is dropped before the restored attempt restreams — the task
    ends with EXACTLY the restored text, not partial+restored."""
    task = _mk_task()
    _drive_with_restart(task, monkeypatch, 'PARTIAL-attempt-', 'RESTORED-full-answer')
    assert task['content'] == 'RESTORED-full-answer', (
        f'partial attempt text must be truncated, got {task["content"]!r}')


def test_truncation_preserves_prior_round_base(monkeypatch):
    """The base captured at stream entry (prior rounds' committed text) must
    SURVIVE the truncation — only this attempt's partial tail is dropped."""
    task = _mk_task(content='PRIOR-ROUND|', thinking='prior-think|')
    _drive_with_restart(task, monkeypatch, 'partial-tail', 'FULL')
    assert task['content'] == 'PRIOR-ROUND|FULL', (
        f'base must be preserved, partial dropped; got {task["content"]!r}')
    assert task['thinking'] == 'prior-think|', (
        f'thinking base must be preserved; got {task["thinking"]!r}')


def test_no_restart_no_truncation(monkeypatch):
    """Without a restart fire, streaming appends normally (the truncation
    closure must not eat content on the happy path)."""
    import lib.tasks_pkg.manager._stream as _mgr
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '0')

    def _fake_dispatch(body, **kwargs):
        oc = kwargs.get('on_content')
        if oc:
            oc('hello ')
            oc('world')
        return ({'role': 'assistant', 'content': 'hello world',
                 'reasoning_content': ''}, 'stop', dict(_HEALTHY_USAGE))

    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = _fake_dispatch
    task = _mk_task()
    with chat_task_fixture_guard:
        chat_task_registry[task['id']] = task
    try:
        _mgr.stream_llm_response(
            task, {'model': 'm',
                   'messages': [{'role': 'user', 'content': 'go'}]}, tag='R1')
    finally:
        _mgr.dispatch_stream = _orig
        with chat_task_fixture_guard:
            chat_task_registry.pop(task['id'], None)
    assert task['content'] == 'hello world'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
