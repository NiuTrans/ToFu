"""Dispatcher attempts produce canonical, bounded timing facts without providers."""
import asyncio
from types import SimpleNamespace

import pytest

from lib.agent_core.events import EventType, build_event
from lib.llm_dispatch._attempt_timing import AttemptObserver
from lib.secret_envelope import secret_hint
from tests.llm_dispatch.test_dispatch_stream import _FakeDispatcher, _make_slot

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'abort', 'callback_error', 'cancel'])
def test_dispatch_attempt_boundaries(monkeypatch, asynchronous, outcome):
    from lib.llm_dispatch import api
    from lib.llm import AbortedError
    import lib.llm
    import lib.llm.astream

    slots = [_make_slot(key=f'k{i}') for i in range(3)]
    dispatcher = _FakeDispatcher(slots)
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    monkeypatch.setenv('TOFU_CACHE_SETTLE', '0')
    monkeypatch.setenv('TOFU_CONV_STICKY_HOLD', '0')
    events = []
    order = []
    calls = 0

    def observe(*, kind, **fields):
        order.append(kind)
        fields.update(parentSpanId='logical:1', requestTag='R1', roundNum=1)
        events.append(build_event(EventType.MODEL_REQUEST_START if kind == 'start'
                                  else EventType.MODEL_REQUEST_COMPLETE, **fields))
        if outcome == 'callback_error':
            raise RuntimeError('observer unavailable')

    def transport(*args, **kwargs):
        nonlocal calls
        calls += 1
        if outcome == 'abort':
            raise AbortedError('user abort')
        if outcome == 'cancel':
            raise asyncio.CancelledError()
        if calls < 3:
            raise RuntimeError('temporary failure')
        return {'role': 'assistant', 'content': 'ok'}, 'stop', {}

    async def atransport(*args, **kwargs):
        return transport(*args, **kwargs)

    monkeypatch.setattr(lib.llm, 'stream_chat', transport)
    monkeypatch.setattr(lib.llm.astream, 'async_stream_chat', atransport)
    kwargs = dict(on_provider_attempt=observe,
                  on_retry=lambda **kw: order.append('retry'))
    def run():
        body = {'model': 'qwen-plus', 'messages': []}
        if asynchronous:
            return asyncio.run(api.async_dispatch_stream(body, **kwargs))
        return api.dispatch_stream(body, **kwargs)

    if outcome in ('abort', 'cancel'):
        with pytest.raises(AbortedError if outcome == 'abort' else asyncio.CancelledError):
            run()
        assert order == ['start', 'complete']
        assert events[-1]['status'] == 'aborted'
    else:
        assert run().message['content'] == 'ok'
        assert order == ['start', 'complete', 'retry'] * 2 + ['start', 'complete']
        assert [e['status'] for e in events[1::2]] == ['failed', 'failed', 'succeeded']
    assert all(slot.inflight == 0 for slot in slots)
    starts = events[::2]
    assert len({e['spanId'] for e in starts}) == len(starts)
    assert [e['attempt'] for e in starts] == list(range(1, len(starts) + 1))
    assert all(e['attemptScope'] == 'dispatcher' for e in events)
    assert 'sk-test' not in str(events)

    # Canonical events, not handcrafted request records, feed the pure fold.
    import lib.tasks_pkg.turn_trace as trace
    rows = [dict(event_id=i, type=e['type'], payload=e, ts_ms=1000 + i * 100)
            for i, e in enumerate(events)]
    monkeypatch.setattr(trace, '_read_trace_rows', lambda _: rows)
    folded = trace.fold_task_trace('mock-attempt', now_ms=2000)
    assert folded['eventsAvailable']
    assert folded['spans']


def test_start_fields_mask_slot_key_into_recognition_hint():
    observer = AttemptObserver(lambda **event: None)
    slot = SimpleNamespace(model='m', routing_provider_id='default',
                           provider_id='default',
                           api_key='2208abcdefghij5409')
    fields, _started = observer._start(slot)
    hint = fields['keyHint']
    assert hint == secret_hint('2208abcdefghij5409')
    assert hint and '2208' in hint and '5409' in hint
    assert 'abcdefghij' not in str(fields)
    # OAuth-style slots carry no key material; the fold drops the empty hint.
    oauth = SimpleNamespace(model='m', routing_provider_id='', provider_id='p',
                            api_key='')
    fields, _started = observer._start(oauth)
    assert fields['keyHint'] == ''


def test_elapsed_ignores_wall_clock(monkeypatch):
    from lib.llm_dispatch import _attempt_timing as timing
    ticks = iter([10.0, 12.5])
    monkeypatch.setattr(timing.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(timing.time, 'time', lambda: -100000)
    events = []
    observer = AttemptObserver(lambda **event: events.append(event))
    observer.call(lambda: ('ok', 'stop', {}), SimpleNamespace(
        model='m', routing_provider_id='', provider_id='p'))
    assert events[-1]['durationMs'] == 2500


@pytest.mark.parametrize('observer_failure', [None, 'flush', 'append'])
def test_manager_parents_attempts_under_ingress_guard(monkeypatch, observer_failure):
    import threading
    import lib.llm
    from lib.llm_dispatch import api
    from lib.tasks_pkg.manager import _stream as manager
    from lib.tasks_pkg.manager._provider_ingress_guard import active_provider_ingress_token

    dispatcher = _FakeDispatcher([_make_slot(key=f'k{i}') for i in range(3)])
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr(manager, 'checkpoint_task_partial', lambda task: None)
    calls = 0
    def transport(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError('retry')
        return {'role': 'assistant', 'content': 'ok', 'tool_calls': []}, 'stop', {}
    monkeypatch.setattr(lib.llm, 'stream_chat', transport)
    events = []
    failed = False
    original_flush = manager.TaskTextDeltaCoalescer.flush
    def flush(self):
        nonlocal failed
        if observer_failure == 'flush' and not failed:
            failed = True
            raise RuntimeError('synthetic pre-diagnostic flush failure')
        return original_flush(self)
    monkeypatch.setattr(manager.TaskTextDeltaCoalescer, 'flush', flush)
    def append(task, event):
        nonlocal failed
        if event.get('requestLevel') == 'provider_attempt':
            assert active_provider_ingress_token(task)
            if observer_failure == 'append' and not failed:
                failed = True
                raise RuntimeError('synthetic diagnostic append failure')
        events.append(event)
    monkeypatch.setattr(manager, 'append_event', append)
    task = dict(id='physical-manager', _userId=1, status='running', content='',
                thinking='', content_lock=threading.Lock(), events_lock=threading.Lock(),
                config={}, model='qwen-plus')
    manager.stream_llm_response(task, {'model': 'qwen-plus', 'messages': []}, tag='R1')
    logical = [e for e in events if e.get('requestLevel') == 'logical']
    children = [e for e in events if e.get('requestLevel') == 'provider_attempt']
    assert len(logical) == 2
    assert len(children) == (5 if observer_failure else 6)
    assert all(e['parentSpanId'] == logical[0]['spanId'] for e in children)
    assert [e['attempt'] for e in children
            if e['type'] == EventType.MODEL_REQUEST_COMPLETE] == [1, 2, 3]
    assert logical[-1]['observerIsolation']['diagnosticFailures'] == (
        1 if observer_failure else 0)
    assert logical[-1]['status'] == 'succeeded'
    assert active_provider_ingress_token(task) is None
