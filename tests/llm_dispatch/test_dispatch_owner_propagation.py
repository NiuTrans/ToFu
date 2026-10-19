"""Owner and request-authority propagation across multi-dispatch fallbacks."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_smart_chat_preserves_authority_and_dispatch_failure(monkeypatch):
    from lib.llm_dispatch import api

    captured = {}
    failure = RuntimeError('dispatch unavailable')

    def dispatch(messages, **kwargs):
        captured.update(kwargs)
        raise failure

    monkeypatch.setattr('lib.llm_dispatch._api_multi.dispatch_chat', dispatch)
    monkeypatch.setattr('lib.llm.chat', lambda **kwargs: pytest.fail('unauthorized direct fallback'))
    tools = [{'type': 'function', 'function': {
        'name': 'lookup', 'parameters': {'type': 'object'}}}]
    with pytest.raises(RuntimeError) as caught:
        api.smart_chat(
            [{'role': 'user', 'content': 'use the tool'}], tools=tools,
            extra={'response_format': {'type': 'json_object'}}, owner_user_id=41)
    assert caught.value is failure
    assert captured['owner_user_id'] == 41
    assert captured['tools'] == tools
    assert captured['extra']['response_format'] == {'type': 'json_object'}


def test_parallel_task_owner_override_does_not_treat_zero_as_missing(
        monkeypatch):
    import lib.llm_dispatch._api_multi as multi

    captured = []

    def _dispatch(_messages, **kwargs):
        captured.append((_messages[0]['content'], kwargs['owner_user_id']))
        return 'ok', {}

    monkeypatch.setattr(multi, 'dispatch_chat', _dispatch)
    results = multi.dispatch_parallel(
        [
            {'messages': [{'role': 'user', 'content': 'outer'}]},
            {'messages': [{'role': 'user', 'content': 'invalid'}],
             'owner_user_id': 0},
        ],
        owner_user_id=41,
        max_workers=1,
    )

    assert results == [('ok', {}), ('ok', {})]
    assert sorted(captured) == [('invalid', 0), ('outer', 41)]


@pytest.mark.parametrize('operation', ['parallel', 'batch'])
def test_independent_requests_start_without_worker_count_window(monkeypatch, operation):
    import threading
    import time
    import lib.llm_dispatch._api_multi as multi

    barrier = threading.Barrier(32, timeout=10)
    captured = []
    lock = threading.Lock()
    tools = [{'type': 'function', 'function': {'name': 'lookup'}}]

    def dispatch(messages, **kwargs):
        with lock:
            captured.append((messages[0]['content'], kwargs))
        barrier.wait()
        return messages[0]['content'], {'owner': kwargs['owner_user_id']}

    monkeypatch.setattr(multi, 'dispatch_chat', dispatch)
    started = time.monotonic()
    if operation == 'parallel':
        results = multi.dispatch_parallel([
            {'messages': [{'role': 'user', 'content': str(i)}],
             'owner_user_id': i, 'prefer_model': 'pinned', 'extra': {'tools': tools}}
            for i in range(32)], max_workers=1, owner_user_id=41)
        assert results == [(str(i), {'owner': i}) for i in range(32)]
        assert all(kwargs['extra']['tools'] == tools for _, kwargs in captured)
    else:
        results = multi.smart_chat_batch([str(i) for i in range(32)],
            max_concurrent=1, owner_user_id=41, prefer_model='pinned', tools=tools)
        assert results == [(str(i), {'owner': 41}) for i in range(32)]
        assert all(kwargs['tools'] == tools for _, kwargs in captured)
    assert len(captured) == 32
    assert all(kwargs['prefer_model'] == 'pinned' for _, kwargs in captured)
    prefix = 'llm-' + operation
    assert not any(t.name.startswith(prefix) for t in threading.enumerate())
    print(f'{operation}: 32 requests {(time.monotonic() - started) * 1000:.2f} ms; 0 workers')


@pytest.mark.parametrize('operation', ['parallel', 'batch'])
def test_unstarted_request_failure_settles_once_and_preserves_siblings(monkeypatch, operation):
    import threading
    import lib.llm_dispatch._api_multi as multi

    barrier = threading.Barrier(3, timeout=10)
    prefix = 'llm-' + operation
    original = threading.Thread.start
    starts = 0
    called = []
    lock = threading.Lock()

    def start(thread):
        nonlocal starts
        if thread.name.startswith(prefix):
            starts += 1
            if starts == 2:
                raise RuntimeError('injected unstarted request')
        return original(thread)

    def dispatch(messages, **kwargs):
        with lock:
            called.append(messages[0]['content'])
        barrier.wait()
        return messages[0]['content'], {}

    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(multi, 'dispatch_chat', dispatch)
    if operation == 'parallel':
        results = multi.dispatch_parallel([
            {'messages': [{'role': 'user', 'content': str(i)}]}
            for i in range(4)], max_workers=1)
        assert results[1] == (None, {'error': 'injected unstarted request'})
    else:
        results = multi.smart_chat_batch([str(i) for i in range(4)], max_concurrent=1)
        assert results[1] == ('[Error] injected unstarted request', {})
    assert sorted(called) == ['0', '2', '3']
    assert [results[i] for i in (0, 2, 3)] == [(str(i), {}) for i in (0, 2, 3)]
    assert not any(t.name.startswith(prefix) for t in threading.enumerate())


def test_empty_prompt_batch_has_no_work():
    import lib.llm_dispatch._api_multi as multi
    assert multi.smart_chat_batch([]) == []
