"""Local request-memory pressure degrades without aborting orchestration.

The cgroup guard owns admission; the LLM fallback layer owns recovery. These
tests pin the cross-layer contract: shrink the derived payload, retry the same
model, never replay local pressure across model/pool fallbacks, and surface a
typed retryable capacity envelope only after bounded recovery is exhausted.
"""

from __future__ import annotations

import threading

import pytest


pytestmark = pytest.mark.unit


def _task(task_id: str) -> dict:
    return {
        'id': task_id,
        'convId': f'conv-{task_id}',
        '_userId': 1,
        'config': {},
        'content': '',
        'thinking': '',
        'events': [],
        'events_lock': threading.Lock(),
    }


def _patch_recovery_runtime(monkeypatch, stream_impl, compact_impl):
    import lib.tasks_pkg.compaction.api as compaction_api
    import lib.tasks_pkg.llm_fallback._call as fallback

    events = []
    monkeypatch.setattr(fallback, 'stream_llm_response', stream_impl)
    monkeypatch.setattr(
        fallback, '_get_fallback_model', lambda _task: 'fallback-model')
    monkeypatch.setattr(fallback, '_emit_round_usage', lambda *a, **kw: None)
    monkeypatch.setattr(
        fallback, 'append_event', lambda _task, event: events.append(event))
    monkeypatch.setattr(fallback, 'approx_body_bytes', lambda _body: 2_300_000)
    monkeypatch.setattr(compaction_api, 'reactive_compact', compact_impl)

    def _build_body(model, messages, **kwargs):
        return {
            'model': model,
            'messages': list(messages),
            'temperature': kwargs.get('temperature', 1.0),
        }

    monkeypatch.setattr(fallback, 'build_body', _build_body)
    return fallback, events


def _call(fallback, task, messages, on_tool_call_ready=None):
    return fallback._llm_call_with_fallback(
        task,
        {'model': 'primary-model', 'messages': list(messages)},
        'primary-model', 0, 512, False, None, messages,
        'low', False, {}, [], on_tool_call_ready=on_tool_call_ready)


def test_memory_pressure_compacts_then_recovers_on_same_model(monkeypatch):
    from lib.cgroup_guard import MemoryPressureError

    calls = []
    compact_calls = []
    callback = object()

    def _stream(_task, body, tag='', on_tool_call_ready=None, **kwargs):
        calls.append((body['model'], tag, on_tool_call_ready, kwargs))
        if len(calls) == 1:
            raise MemoryPressureError('request envelope does not fit')
        return ({'role': 'assistant', 'content': 'recovered'}, 'stop', {
            'prompt_tokens': 7,
            'completion_tokens': 3,
        })

    def _compact(messages, **kwargs):
        compact_calls.append(kwargs)
        messages[:] = messages[-2:]
        return True

    fallback, events = _patch_recovery_runtime(
        monkeypatch, _stream, _compact)
    task = _task('memory-recovers')
    fallback._reactive_compact_attempts.pop(task['id'], None)
    try:
        result = _call(
            fallback, task,
            [{'role': 'user', 'content': 'old'},
             {'role': 'assistant', 'content': 'work'},
             {'role': 'user', 'content': 'continue'}],
            on_tool_call_ready=callback)
    finally:
        fallback._reactive_compact_attempts.pop(task['id'], None)

    assert result['assistant_msg']['content'] == 'recovered'
    assert result['model'] == 'primary-model'
    assert [call[:2] for call in calls] == [
        ('primary-model', 'R1'),
        ('primary-model', 'R1-REACTIVE'),
    ]
    assert calls[1][2] is callback
    assert compact_calls[0]['byte_target'] == 1_150_000
    assert '_fallback_model' not in task
    assert any(event.get('detailKey') == 'stream.phase.compactingWindow'
               for event in events)


def test_persistent_memory_pressure_never_switches_model_or_pool(monkeypatch):
    from lib.cgroup_guard import MemoryPressureError

    calls = []

    def _stream(_task, body, tag='', **kwargs):
        calls.append((body['model'], tag, kwargs))
        raise MemoryPressureError('request envelope still does not fit')

    fallback, _events = _patch_recovery_runtime(
        monkeypatch, _stream, lambda *_a, **_kw: True)
    task = _task('memory-persists')
    fallback._reactive_compact_attempts.pop(task['id'], None)
    try:
        result = _call(
            fallback, task, [{'role': 'user', 'content': 'continue'}])
    finally:
        fallback._reactive_compact_attempts.pop(task['id'], None)

    assert [model for model, _tag, _kwargs in calls] == [
        'primary-model', 'primary-model', 'primary-model']
    assert [tag for _model, tag, _kwargs in calls] == [
        'R1', 'R1-REACTIVE', 'R1-REACTIVE']
    assert not any(kwargs.get('pool_wide') for _model, _tag, kwargs in calls)
    assert '_fallback_model' not in task
    assert result['_loop_action'] == 'break'
    assert result['_loop_exit_reason'] == 'local_memory_pressure_round_0'
    assert task['error']['kind'] == 'server_busy'
    assert task['error']['retryable'] is True


def test_compaction_defect_preserves_original_typed_pressure(monkeypatch):
    from lib.cgroup_guard import MemoryPressureError

    calls = []

    def _stream(_task, body, tag='', **kwargs):
        calls.append((body['model'], tag, kwargs))
        raise MemoryPressureError('request envelope does not fit')

    def _broken_compaction(*_args, **_kwargs):
        raise ValueError('derived compaction bug')

    fallback, _events = _patch_recovery_runtime(
        monkeypatch, _stream, _broken_compaction)
    task = _task('memory-prep-defect')
    fallback._reactive_compact_attempts.pop(task['id'], None)
    try:
        result = _call(
            fallback, task, [{'role': 'user', 'content': 'continue'}])
    finally:
        fallback._reactive_compact_attempts.pop(task['id'], None)

    assert len(calls) == 1
    assert result['_loop_action'] == 'break'
    assert task['error']['kind'] == 'server_busy'
    assert 'request envelope does not fit' in task['error']['detail']
    assert 'derived compaction bug' not in task['error']['detail']


def test_memory_pressure_on_configured_fallback_recovers_in_place(
        monkeypatch):
    from lib.cgroup_guard import MemoryPressureError
    from lib.llm import PermissionError_

    calls = []

    def _stream(_task, body, tag='', **kwargs):
        calls.append((body['model'], tag, kwargs))
        if len(calls) == 1:
            raise PermissionError_('primary key rejected')
        if len(calls) == 2:
            raise MemoryPressureError('fallback request envelope does not fit')
        return ({'role': 'assistant', 'content': 'fallback recovered'},
                'stop', {'prompt_tokens': 4, 'completion_tokens': 2})

    fallback, _events = _patch_recovery_runtime(
        monkeypatch, _stream, lambda *_a, **_kw: True)
    task = _task('fallback-memory')
    fallback._reactive_compact_attempts.pop(task['id'], None)
    try:
        result = _call(
            fallback, task, [{'role': 'user', 'content': 'continue'}])
    finally:
        fallback._reactive_compact_attempts.pop(task['id'], None)

    assert [(model, tag) for model, tag, _kwargs in calls] == [
        ('primary-model', 'R1'),
        ('fallback-model', 'R1-FALLBACK'),
        ('fallback-model', 'R1-REACTIVE'),
    ]
    assert not any(kwargs.get('pool_wide') for _model, _tag, kwargs in calls)
    assert result['assistant_msg']['content'] == 'fallback recovered'
    assert result['model'] == 'fallback-model'
    assert task['_fallback_model'] == 'fallback-model'


def test_persistent_fallback_memory_pressure_clears_unrealized_switch(
        monkeypatch):
    from lib.cgroup_guard import MemoryPressureError
    from lib.llm import PermissionError_

    calls = []

    def _stream(_task, body, tag='', **kwargs):
        calls.append((body['model'], tag, kwargs))
        if len(calls) == 1:
            raise PermissionError_('primary key rejected')
        raise MemoryPressureError('fallback request envelope does not fit')

    fallback, _events = _patch_recovery_runtime(
        monkeypatch, _stream, lambda *_a, **_kw: True)

    def _pool_must_not_run(*_args, **_kwargs):
        pytest.fail('local memory pressure must not enter pool rescue')

    monkeypatch.setattr(fallback, '_attempt_pool_rescue', _pool_must_not_run)
    task = _task('fallback-memory-persists')
    fallback._reactive_compact_attempts.pop(task['id'], None)
    try:
        result = _call(
            fallback, task, [{'role': 'user', 'content': 'continue'}])
    finally:
        fallback._reactive_compact_attempts.pop(task['id'], None)

    assert [(model, tag) for model, tag, _kwargs in calls] == [
        ('primary-model', 'R1'),
        ('fallback-model', 'R1-FALLBACK'),
        ('fallback-model', 'R1-REACTIVE'),
        ('fallback-model', 'R1-REACTIVE'),
    ]
    assert result['_loop_action'] == 'break'
    assert task['error']['kind'] == 'server_busy'
    assert '_fallback_model' not in task
    assert '_fallback_from' not in task


def test_memory_reactive_compaction_is_llm_free_and_hits_byte_target(
        monkeypatch):
    import lib.tasks_pkg.compaction._reactive as reactive
    import lib.tasks_pkg.compaction._reactive._headtrunc as headtrunc

    messages = [
        {'role': 'system', 'content': 'system'},
        {'role': 'user', 'content': 'objective'},
    ]
    for index in range(12):
        messages.append({
            'role': 'assistant' if index % 2 else 'user',
            'content': chr(65 + index) * 180_000,
        })
    target = 1 << 20
    before = reactive._estimate_wire_bytes(messages)

    monkeypatch.setattr(reactive, '_archive_transcript', lambda *a, **kw: None)
    monkeypatch.setattr(reactive, 'micro_compact', lambda *a, **kw: None)
    monkeypatch.setattr(
        reactive, '_truncate_largest_message', lambda *a, **kw: (-1, 0))

    def _summary_must_not_run(*_args, **_kwargs):
        pytest.fail('local-memory recovery must not dispatch a summary LLM')

    monkeypatch.setattr(
        reactive, 'force_compact_if_needed', _summary_must_not_run)
    monkeypatch.setattr(headtrunc, 'audit_log', lambda *a, **kw: None)

    compacted = reactive.reactive_compact(
        messages,
        task={'id': 'memory-byte-trim', 'convId': '', '_userId': 1,
              'config': {'model': 'primary-model'}},
        error_text='local memory headroom is insufficient',
        byte_target=target,
    )

    assert compacted is True
    assert reactive._estimate_wire_bytes(messages) <= target
    assert reactive._estimate_wire_bytes(messages) < before


def test_memory_pressure_classifies_as_retryable_server_capacity():
    from lib.cgroup_guard import MemoryPressureError
    from lib.error_envelope import from_exception

    envelope = from_exception(MemoryPressureError('request does not fit'))

    assert envelope['kind'] == 'server_busy'
    assert envelope['severity'] == 'warning'
    assert envelope['retryable'] is True
