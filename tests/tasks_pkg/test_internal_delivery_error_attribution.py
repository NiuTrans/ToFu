"""Local delivery identity survives model recovery and fatal settlement."""
import threading

import pytest

from lib.tasks_pkg.event_delivery import (
    DeliveryLane, EventDeliveryCoordinator, EventDeliveryError,
)

pytestmark = pytest.mark.unit


def _task():
    return {'id': 'delivery-attribution', 'config': {'model': 'primary'},
            'content': '', 'thinking': '', 'events': [],
            'content_lock': threading.Lock(), 'events_lock': threading.Lock()}


@pytest.mark.parametrize('failure_attempt', [1, 2, 3])
def test_delivery_error_bypasses_all_model_recovery(monkeypatch, failure_attempt):
    import lib.tasks_pkg.llm_fallback._call as call
    from lib.llm import PermissionError_

    task = _task()
    error = EventDeliveryError('local delivery timeout HTTP 401 429')
    calls = []
    monkeypatch.setattr(call, '_get_fallback_model', lambda task: 'fallback')
    monkeypatch.setattr(call, '_get_pool_rescue_model', lambda failed: 'rescue')
    monkeypatch.setattr(call, 'append_event', lambda *a: None)
    class Gate:
        def has_capable_slots(self, *a, **kw):
            return True
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: Gate())

    def stream(*a, **kw):
        calls.append(kw)
        if len(calls) == failure_attempt:
            raise error
        raise PermissionError_('HTTP 401 provider failure')

    monkeypatch.setattr(call, 'stream_llm_response', stream)
    with pytest.raises(EventDeliveryError) as raised:
        call._llm_call_with_fallback(
            task, {'model': 'primary'}, 'primary', 0, 512, False, None,
            [{'role': 'user', 'content': 'hi'}], 'low', False, {}, [])
    assert raised.value is error
    assert len(calls) == failure_attempt
    assert 'error' not in task


def test_fatal_delivery_keeps_original_envelope_over_secondary_failure(monkeypatch):
    import lib.tasks_pkg.orchestrator._post_loop as post

    task = _task()
    first = EventDeliveryError('first durable write failed')
    first.__cause__ = RuntimeError('original disk fault, not HTTP 401 429 timeout')
    first._user_message = {'kind': 'permission', 'message': 'wrong provider attribution'}
    persisted = []
    monkeypatch.setattr(post, '_settle_fatal_tool_rounds', lambda task: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.stamp_chat_task_terminal', lambda *a, **kw: None)
    monkeypatch.setattr('lib.tasks_pkg.compaction._settlement.settle_compaction_usage', lambda task: None)
    def forbid_retry(*a):
        pytest.fail('local delivery failure must not replay the turn')
    monkeypatch.setattr(post, '_maybe_auto_retry_turn', forbid_retry)
    def terminal_failure(*a):
        raise EventDeliveryError('secondary phase publication previously failed')
    monkeypatch.setattr(post, 'append_event', terminal_failure)
    def persist(task):
        persisted.append(dict(task['error']))
        raise RuntimeError('secondary persistence failure')
    monkeypatch.setattr(post, 'persist_task_result', persist)

    assert post.handle_task_fatal(task, first) is False
    envelope = task['error']
    assert persisted == [envelope]
    assert envelope['kind'] == 'server_busy'
    assert envelope['source'] == 'task-event-delivery'
    assert envelope['model'] == ''
    assert 'original disk fault' in envelope['raw']
    assert 'secondary' not in envelope['raw']
    assert 'LLM call failed' not in envelope['message']
    assert envelope['hintKey'] == 'err.k.server_busy.hint'
    assert task['status'] == task['finishReason'] == 'error'


def test_first_delivery_cause_survives_later_submit_and_flush():
    """Exercise coordinator interfaces, not lane.error mutation."""
    owner = EventDeliveryCoordinator(workers=1, retry_attempts=1, retry_delay=0)
    lane = DeliveryLane(_task())
    original = RuntimeError('first disk write failed')
    recovered = threading.Event()
    def persist():
        if not recovered.is_set():
            raise original
    try:
        owner.submit(lane, 1, 32, persist, lambda: None)
        with pytest.raises(EventDeliveryError) as first:
            owner.flush(lane, recover=False)
        assert first.value.__cause__ is original
        with pytest.raises(EventDeliveryError) as later:
            owner.submit(lane, 2, 32, lambda: None, lambda: None)
        assert later.value.__cause__ is original
        with pytest.raises(EventDeliveryError) as flushed:
            owner.flush(lane, recover=False)
        assert flushed.value.__cause__ is original

        from lib.tasks_pkg.manager import _events
        lane.task['_eventDeliveryLane'] = lane
        lane.task['_eventDeliveryAdmissionError'] = 'EventDeliveryError'
        with pytest.raises(EventDeliveryError) as round_end:
            _events.append_event(lane.task, {'type': 'round_end', 'roundNum': 1})
        assert round_end.value.__cause__ is original
    finally:
        recovered.set()
        owner.flush(lane)
        assert owner.close(2)


def test_reactive_retry_propagates_delivery_error(monkeypatch):
    import lib.tasks_pkg.llm_fallback._call as call

    task = _task()
    error = EventDeliveryError('reactive stream durable write failed')
    monkeypatch.setattr(call, '_learn_context_limit_from_overflow', lambda *a: None)
    monkeypatch.setattr(call, '_prepare_reactive_retry_body', lambda *a, **kw: {'model': 'primary'})
    def stream(*a, **kw):
        raise error
    monkeypatch.setattr(call, 'stream_llm_response', stream)
    call._reactive_compact_attempts.pop(task['id'], None)
    try:
        with pytest.raises(EventDeliveryError) as raised:
            call._attempt_request_payload_recovery(
                task, {'model': 'primary'}, 'primary', 0, None, [],
                'low', False, {}, [], None, RuntimeError('context limit'))
        assert raised.value is error
    finally:
        call._reactive_compact_attempts.pop(task['id'], None)
