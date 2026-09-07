#!/usr/bin/env python3
"""Recovery checkpoint sampling at the provider-ingress isolation boundary.

The first content/thinking delta requests a checkpoint immediately, but the DB
write must not run on the callback stack that drains the upstream model stream.
``stream_llm_response`` therefore coalesces all in-flight requests and performs
one checkpoint as soon as that provider dispatch has returned.
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


def test_first_delta_requests_checkpoint_settled_at_provider_boundary(
        monkeypatch):
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
        # The first delta samples recovery immediately, but DB work is isolated
        # until this provider function has returned.
        on_content('hello ')
        assert calls == []
        on_content('world')
        assert calls == []
        # dispatch_stream returns (message_dict, finish_reason, usage).
        return {'content': 'hello world', 'tool_calls': []}, 'stop', {'completion_tokens': 2}

    monkeypatch.setattr(st, 'dispatch_stream', _fake_dispatch_stream)

    task = _make_task()
    msg, finish, usage = m.stream_llm_response(task, {'model': 'test-model'})

    assert task['content'] == 'hello world'
    assert len(calls) == 1, 'sampled checkpoint did not settle at provider boundary'


def test_checkpoint_throttled_after_first(monkeypatch):
    """Rapid deltas coalesce into one post-provider recovery checkpoint."""
    import lib.tasks_pkg.manager as m
    import lib.tasks_pkg.manager._stream as st

    calls = []
    # See test_first_delta_checkpoints_immediately for why these patch _stream
    # (real binding site), including its dispatcher dependency.
    monkeypatch.setattr(st, 'checkpoint_task_partial', lambda task: calls.append(time.time()))
    monkeypatch.setattr(st, 'append_event', lambda task, ev: task['events'].append(ev))

    def _fake_dispatch_stream(body, *, on_content=None, on_thinking=None, **kwargs):
        for _ in range(10):
            on_content('x')
            assert calls == []
        return {'content': 'x' * 10, 'tool_calls': []}, 'stop', {}

    monkeypatch.setattr(st, 'dispatch_stream', _fake_dispatch_stream)

    task = _make_task()
    m.stream_llm_response(task, {'model': 'test-model'})

    # Exactly one checkpoint after provider return; no callback did DB work.
    assert len(calls) == 1, f'expected 1 throttled checkpoint, got {len(calls)}'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
