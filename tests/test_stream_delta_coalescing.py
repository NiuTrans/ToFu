"""Lossless, ordered, bounded coalescing for provider text chunks."""

from __future__ import annotations

import threading

import pytest

from lib.agent_core.events import EventType
from lib.tasks_pkg.manager._delta_coalescer import (
    BoundedTextDeltaCoalescer,
    TaskTextDeltaCoalescer,
)


pytestmark = pytest.mark.unit


def test_first_delta_is_immediate_and_worker_flushes_lossless_tail():
    emitted = []
    tail_flushed = threading.Event()

    def _emit(content, thinking):
        emitted.append((content, thinking))
        if len(emitted) == 2:
            tail_flushed.set()

    coalescer = BoundedTextDeltaCoalescer(
        _emit, delay_s=0.01,
    )

    coalescer.add(content='first')
    coalescer.add(thinking='think-1')
    coalescer.add(content='second')

    assert emitted == [('first', '')]
    assert tail_flushed.wait(1.0)

    assert emitted == [('first', ''), ('second', 'think-1')]
    assert coalescer.stats == {
        'raw_chunks': 3,
        'raw_chars': 18,
        'emitted_events': 2,
        'failed_emits': 0,
        'pending_chars': 0,
        'worker_started': 1,
        'worker_alive': 1,
    }
    coalescer.close()
    assert coalescer.stats['worker_alive'] == 0


def test_character_bound_flushes_and_close_stops_worker():
    emitted = []
    coalescer = BoundedTextDeltaCoalescer(
        lambda content, thinking: emitted.append((content, thinking)),
        max_chars=4,
        delay_s=60,
    )

    coalescer.add(content='A')
    coalescer.add(content='bc')
    coalescer.add(content='de')

    assert emitted == [('A', ''), ('bcde', '')]
    coalescer.add(thinking='z')
    assert coalescer.close() is True
    assert emitted[-1] == ('', 'z')
    assert coalescer.stats['worker_alive'] == 0
    with pytest.raises(RuntimeError, match='closed'):
        coalescer.add(content='late')


def test_background_emit_failure_keeps_pending_text_for_boundary_retry():
    emitted = []
    failure_seen = threading.Event()
    fail_tail = {'enabled': True}

    def _emit(content, thinking):
        if content == 'tail' and fail_tail['enabled']:
            fail_tail['enabled'] = False
            failure_seen.set()
            raise RuntimeError('injected emit failure')
        emitted.append((content, thinking))

    coalescer = BoundedTextDeltaCoalescer(
        _emit, delay_s=0.01)
    coalescer.add(content='first')
    coalescer.add(content='tail')

    assert failure_seen.wait(1.0)
    assert coalescer.stats['pending_chars'] == 4
    assert coalescer.stats['failed_emits'] == 1
    assert coalescer.flush() is True
    assert emitted == [('first', ''), ('tail', '')]
    coalescer.close()


def test_leading_emit_failure_keeps_text_for_close_retry():
    emitted = []
    fail_once = {'enabled': True}

    def _emit(content, thinking):
        if fail_once['enabled']:
            fail_once['enabled'] = False
            raise RuntimeError('injected leading emit failure')
        emitted.append((content, thinking))

    coalescer = BoundedTextDeltaCoalescer(_emit, delay_s=60)

    with pytest.raises(RuntimeError, match='leading emit failure'):
        coalescer.add(content='first')

    assert coalescer.stats['pending_chars'] == 5
    assert coalescer.stats['emitted_events'] == 0
    assert coalescer.stats['failed_emits'] == 1
    assert coalescer.close() is True
    assert emitted == [('first', '')]


def test_cumulative_projection_is_updated_inside_the_emit_ordering_boundary():
    state = {'content': '', 'thinking': ''}
    observed = []

    def _accumulate(content, thinking):
        state['content'] += content
        state['thinking'] += thinking

    def _emit(content, thinking):
        observed.append((content, thinking, dict(state)))

    coalescer = BoundedTextDeltaCoalescer(
        _emit, accumulate=_accumulate, delay_s=60)
    coalescer.add(content='A')
    coalescer.add(thinking='T')
    coalescer.add(content='B')
    coalescer.close()

    assert observed == [
        ('A', '', {'content': 'A', 'thinking': ''}),
        ('B', 'T', {'content': 'AB', 'thinking': 'T'}),
    ]


def test_resource_budget_bounds_buffer_worker_and_burst_event_count():
    emitted = []
    coalescer = BoundedTextDeltaCoalescer(
        lambda content, thinking: emitted.append((content, thinking)),
        delay_s=60,
        max_chars=256,
    )

    for _ in range(100):
        coalescer.add(content='abcd')
        assert coalescer.stats['pending_chars'] <= 256
        assert coalescer.stats['worker_started'] <= 1
    coalescer.close()

    assert len(emitted) == 3
    assert ''.join(content for content, _thinking in emitted) == 'abcd' * 100
    assert coalescer.stats['worker_alive'] == 0


def _stream_task():
    return {
        'id': 'delta-coalesce-task',
        '_attemptId': 'delta-coalesce-attempt',
        '_userId': 1,
        'convId': 'delta-coalesce-conv',
        'status': 'running',
        'content': '',
        'thinking': '',
        'content_lock': threading.Lock(),
        'events_lock': threading.Lock(),
        'model': 'test-model',
        'config': {},
    }


def test_stream_flushes_text_before_tool_boundary_and_provider_return(monkeypatch):
    import lib.tasks_pkg.manager._stream as stream_module

    emitted = []
    instances = []
    real_class = TaskTextDeltaCoalescer

    def _coalescer(*args, **kwargs):
        instance = real_class(*args, delay_s=60, **kwargs)
        instances.append(instance)
        return instance

    monkeypatch.setattr(stream_module, 'TaskTextDeltaCoalescer', _coalescer)
    monkeypatch.setattr(
        stream_module, 'append_event',
        lambda _task, event: emitted.append(event),
    )
    monkeypatch.setattr(
        stream_module, 'checkpoint_task_partial', lambda _task: None)

    def _dispatch(_body, *, on_content, on_thinking,
                  on_tool_call_ready, on_before_tool_call_ready, **_kwargs):
        on_content('A')
        on_content('B')
        on_thinking('T')
        on_before_tool_call_ready()
        on_tool_call_ready({'id': 'tool-1'})
        on_content('C')
        return ({'role': 'assistant', 'content': 'ABC',
                 'reasoning_content': 'T', 'tool_calls': []},
                'stop', {})

    monkeypatch.setattr(stream_module, 'dispatch_stream', _dispatch)

    def _tool_ready(tool_call):
        emitted.append({'type': 'test_tool_boundary', 'id': tool_call['id']})

    task = _stream_task()
    body = {'model': 'test-model', 'messages': []}
    result = stream_module.stream_llm_response(
        task, body, on_tool_call_ready=_tool_ready)

    delta_events = [event for event in emitted
                    if event.get('type') == EventType.DELTA]
    assert ''.join(event.get('content') or '' for event in delta_events) == 'ABC'
    assert ''.join(event.get('thinking') or '' for event in delta_events) == 'T'
    types = [event['type'] for event in emitted]
    boundary = types.index('test_tool_boundary')
    assert [event.get('content') for event in emitted[:boundary]
            if event.get('type') == EventType.DELTA] == ['A', 'B']
    assert emitted[boundary - 1].get('thinking') == 'T'
    assert delta_events[-1].get('content') == 'C'
    assert types.index(EventType.MODEL_REQUEST_COMPLETE) > types.index(
        EventType.DELTA, boundary)
    assert task['content'] == 'ABC'
    assert task['thinking'] == 'T'
    assert result.message['content'] == 'ABC'
    assert instances[0].stats['raw_chunks'] == 4
    assert instances[0].stats['emitted_events'] == 3
    assert '_request_activity_sink' not in body


def test_final_delta_emit_failure_closes_request_span_and_sink(monkeypatch):
    import lib.tasks_pkg.manager._stream as stream_module

    emitted = []
    instances = []
    fail_tail_once = {'enabled': True}
    real_class = TaskTextDeltaCoalescer

    def _coalescer(*args, **kwargs):
        instance = real_class(*args, delay_s=60, **kwargs)
        instances.append(instance)
        return instance

    def _append_event(_task, event):
        if (event.get('type') == EventType.DELTA
                and event.get('content') == 'B'
                and fail_tail_once['enabled']):
            fail_tail_once['enabled'] = False
            raise RuntimeError('injected final delta failure')
        emitted.append(event)

    monkeypatch.setattr(stream_module, 'TaskTextDeltaCoalescer', _coalescer)
    monkeypatch.setattr(stream_module, 'append_event', _append_event)
    monkeypatch.setattr(
        stream_module, 'checkpoint_task_partial', lambda _task: None)

    def _dispatch(_body, *, on_content, **_kwargs):
        on_content('A')
        on_content('B')
        return ({'role': 'assistant', 'content': 'AB', 'tool_calls': []},
                'stop', {})

    monkeypatch.setattr(stream_module, 'dispatch_stream', _dispatch)
    task = _stream_task()
    body = {'model': 'test-model', 'messages': []}

    with pytest.raises(RuntimeError, match='final delta failure'):
        stream_module.stream_llm_response(task, body)

    completed = [event for event in emitted
                 if event.get('type') == EventType.MODEL_REQUEST_COMPLETE]
    assert completed[-1]['status'] == 'failed'
    assert '_request_activity_sink' not in body
    assert '_activeModelRequestSpan' not in task
    assert instances[0].stats['worker_alive'] == 0


def test_dispatch_tool_pre_hook_preserves_original_callback_identity_and_order():
    from lib.llm_dispatch.api import _first_output_callbacks

    order = []
    original = lambda value: order.append(('tool', value))  # noqa: E731
    before = lambda: order.append(('before', None))  # noqa: E731
    _ttft, _thinking, _content, tool = _first_output_callbacks(
        0, None, None, original, before)

    tool('call-1')

    assert order == [('before', None), ('tool', 'call-1')]
