#!/usr/bin/env python3
"""Test #2 of the sync-robustness pass (2026-06-25): the FIRST streaming delta
checkpoints to DB immediately, closing the pre-first-checkpoint crash-loss window.

Before: ``stream_llm_response`` initialised ``_last_stream_ckpt = time.time()``,
so the throttled ``_maybe_checkpoint_during_stream`` could not fire until 5s
into streaming. A server crash after the first tokens but before the 5s tick
lost the whole turn. Now it inits to 0.0 → the first content/thinking delta
triggers a checkpoint, then the 5s cadence resumes.

We drive ``stream_llm_response`` with a stubbed ``dispatch_stream`` that emits
two content deltas, and a stubbed ``checkpoint_task_partial`` recorder; we
assert the checkpoint fired on the first delta (not deferred 5s).
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _make_task():
    import threading
    return {
        'id': 'ckpt-test-1234',
        'convId': 'cv-ckpt',
        'content': '',
        'thinking': '',
        'toolRounds': [],
        'aborted': False,
        'status': 'running',
        '_userId': 1,
        'created_at': time.time(),
        'content_lock': threading.Lock(),
        'events': [],
        'events_lock': threading.Lock(),
    }


def test_first_delta_checkpoints_immediately(monkeypatch):
    import lib.tasks_pkg.manager as m
    import lib.tasks_pkg.manager._stream as st

    calls = []
    # ★ Package-split binding: ``stream_llm_response`` lives in the ``_stream``
    #   submodule, which imports ``checkpoint_task_partial`` (from ._sync) and
    #   ``append_event`` (from ._events) into ITS OWN namespace at import and
    #   calls them directly. Patching the ``manager`` facade attribute rebinds
    #   only the facade's re-export, NOT ``_stream``'s binding, so the stub
    #   never installs (the pre-split monolith made these patchable on the
    #   facade; the split moved the true binding site). Patch ``_stream``.
    #   ``dispatch_stream`` is also owned by ``_stream`` as an imported
    #   dependency, so patch the same concrete binding.
    monkeypatch.setattr(st, 'checkpoint_task_partial', lambda task: calls.append(time.time()))
    # Don't actually persist events to DB during the test.
    monkeypatch.setattr(st, 'append_event', lambda task, ev: task['events'].append(ev))

    def _fake_dispatch_stream(body, *, on_content=None, on_thinking=None, **kwargs):
        # Emit a content delta on the very first token — must checkpoint NOW.
        on_content('hello ')
        on_content('world')
        # dispatch_stream returns (message_dict, finish_reason, usage).
        return {'content': 'hello world', 'tool_calls': []}, 'stop', {'completion_tokens': 2}

    monkeypatch.setattr(st, 'dispatch_stream', _fake_dispatch_stream)

    task = _make_task()
    msg, finish, usage = m.stream_llm_response(task, {'model': 'test-model'})

    assert task['content'] == 'hello world'
    # The FIRST delta must have triggered a checkpoint (init=0.0 → fires
    # immediately). Before the fix, _last_stream_ckpt=time.time() meant zero
    # checkpoints within the first 5s, so calls would be empty here.
    assert len(calls) >= 1, 'first streaming delta did not checkpoint immediately'


def test_checkpoint_throttled_after_first(monkeypatch):
    """The first delta checkpoints; rapid subsequent deltas within 5s do NOT
    re-checkpoint (cadence preserved — we didn't turn it into per-delta writes)."""
    import lib.tasks_pkg.manager as m
    import lib.tasks_pkg.manager._stream as st

    calls = []
    # See test_first_delta_checkpoints_immediately for why these patch _stream
    # (real binding site), including its dispatcher dependency.
    monkeypatch.setattr(st, 'checkpoint_task_partial', lambda task: calls.append(time.time()))
    monkeypatch.setattr(st, 'append_event', lambda task, ev: task['events'].append(ev))

    def _fake_dispatch_stream(body, *, on_content=None, on_thinking=None, **kwargs):
        for _ in range(10):
            on_content('x')   # 10 rapid deltas, all within the same <5s window
        return {'content': 'x' * 10, 'tool_calls': []}, 'stop', {}

    monkeypatch.setattr(st, 'dispatch_stream', _fake_dispatch_stream)

    task = _make_task()
    m.stream_llm_response(task, {'model': 'test-model'})

    # Exactly one checkpoint: the first delta. The other 9 are throttled by
    # the 5s interval — we did NOT regress into a per-delta-write storm.
    assert len(calls) == 1, f'expected 1 throttled checkpoint, got {len(calls)}'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
