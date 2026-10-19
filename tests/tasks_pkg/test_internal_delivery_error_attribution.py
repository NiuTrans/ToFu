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


@pytest.mark.parametrize('aborted', [False, True])
def test_fatal_delivery_keeps_original_envelope_over_secondary_failure(monkeypatch, aborted):
    import lib.tasks_pkg.orchestrator._post_loop as post

    task = _task()
    task['aborted'] = aborted
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


@pytest.mark.parametrize('marker', ['EventDeliveryCancelled', 'EventDeliveryRevoked'])
def test_later_admission_preserves_cancellation_identity(marker):
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager import _events
    from lib.error_envelope import from_exception
    task = _task()
    task['_eventDeliveryLane'] = DeliveryLane(task)
    task['_eventDeliveryAdmissionError'] = marker
    with pytest.raises(getattr(event_delivery, marker)) as raised:
        _events.append_event(task, {'type': 'round_end', 'roundNum': 8})
    assert from_exception(raised.value)['kind'] == 'aborted'


@pytest.mark.parametrize('reason', ['', 'server_shutdown'])
def test_cancelled_delivery_settles_interrupted_without_capacity_error(monkeypatch, reason):
    import lib.tasks_pkg.orchestrator._post_loop as post
    from lib.tasks_pkg.event_delivery import EventDeliveryCancelled
    from lib.turn_verdict import derive_task_verdict
    task = _task()
    task.update(status='running', aborted=True, _abort_reason=reason,
                content='retained partial output', toolRounds=[], usage={'total_tokens': 23})
    emitted, persisted = [], []
    monkeypatch.setattr(post, '_settle_fatal_tool_rounds', lambda task: None)
    monkeypatch.setattr('lib.tasks_pkg.compaction._settlement.settle_compaction_usage', lambda task: None)
    monkeypatch.setattr(post, '_maybe_auto_retry_turn', lambda *a: pytest.fail('cancel replay'))
    monkeypatch.setattr(post, 'append_event', lambda task, event: emitted.append(event))
    monkeypatch.setattr(post, 'persist_task_result', lambda task: persisted.append(dict(task)))
    assert post.handle_task_fatal(task, EventDeliveryCancelled('cancelled by owner')) is False
    assert task['status'] == task['finishReason'] == 'aborted'
    assert task['error'] is None
    assert emitted[0]['finishReason'] == 'aborted' and not emitted[0].get('error')
    verdict = derive_task_verdict(task, emitted[0])
    assert verdict.status.value == 'interrupted'
    assert verdict.cause == ('server_restart' if reason else 'user_abort')
    assert persisted[0]['content'] == 'retained partial output'
    assert persisted[0]['usage'] == {'total_tokens': 23}
    assert task['_chat_terminal_stamped'] and task['finished_at'] > 0


@pytest.mark.parametrize('guard', ['stalled', 'deadline', 'internal_fence'])
def test_delivery_cancellation_does_not_hide_internal_stop(monkeypatch, guard):
    import lib.tasks_pkg.orchestrator._post_loop as post
    from lib.tasks_pkg.event_delivery import EventDeliveryCancelled
    from lib.turn_verdict import derive_task_verdict
    from lib.error_envelope import make_envelope
    task = _task()
    task.update(status='running', aborted=True)
    diagnostic = make_envelope('internal', context='invocation-guard', source='task-engine')
    if guard == 'internal_fence':
        task['_abort_reason'] = 'storage_authority_integrity'
    else:
        task['_invocationStopReason'] = guard
        diagnostic['invocationStopReason'] = guard
    task['error'] = diagnostic
    monkeypatch.setattr(post, '_settle_fatal_tool_rounds', lambda task: None)
    monkeypatch.setattr('lib.tasks_pkg.compaction._settlement.settle_compaction_usage', lambda task: None)
    monkeypatch.setattr(post, 'append_event', lambda *a: None)
    monkeypatch.setattr(post, 'persist_task_result', lambda *a: None)
    post.handle_task_fatal(task, EventDeliveryCancelled('physical cancellation'))
    assert task['status'] == task['finishReason'] == 'error'
    assert task['error']['kind'] == 'internal'
    assert derive_task_verdict(task).status.value == 'failed'


def test_shutdown_cancels_new_admission_but_drains_accepted_evidence():
    from lib.tasks_pkg.event_delivery import EventDeliveryCancelled
    owner = EventDeliveryCoordinator(workers=1)
    lane = DeliveryLane(_task())
    entered, release, committed = threading.Event(), threading.Event(), []
    def persist():
        entered.set()
        assert release.wait(5)
        committed.append(1)
    try:
        owner.submit(lane, 1, 32, persist, lambda: None)
        assert entered.wait(5)
        assert not owner.close(0)
        with pytest.raises(EventDeliveryCancelled):
            owner.submit(lane, 2, 32, lambda: committed.append(2), lambda: None)
        release.set()
        owner.flush(lane)
        assert committed == [1]
    finally:
        release.set()
        assert owner.close(5)


@pytest.mark.parametrize('status,finish', [('done', 'stop'), ('error', 'error'), ('aborted', 'aborted')])
def test_late_delivery_cancellation_preserves_stamped_terminal(monkeypatch, status, finish):
    import lib.tasks_pkg.orchestrator._post_loop as post
    from lib.tasks_pkg.event_delivery import EventDeliveryCancelled
    from lib.tasks_pkg.manager import stamp_chat_task_terminal
    task = _task()
    stamp_chat_task_terminal(task, status=status, finish_reason=finish)
    before = dict(task)
    monkeypatch.setattr(post, 'append_event', lambda *a: pytest.fail('terminal rewrite'))
    monkeypatch.setattr(post, 'persist_task_result', lambda *a: pytest.fail('candidate rewrite'))
    assert post.handle_task_fatal(task, EventDeliveryCancelled('late callback')) is True
    assert task == before


@pytest.mark.parametrize('error_type', ['delivery', 'persistence'])
@pytest.mark.parametrize('guard', ['', 'stalled', 'deadline', 'storage_authority_integrity'])
def test_typed_cancellation_preserves_history_and_guarded_failures(monkeypatch, error_type, guard):
    import lib.tasks_pkg.orchestrator._post_loop as post
    from lib.tasks_pkg.event_delivery import EventDeliveryCancelled
    from lib.llm.llm_errors import AbortedError
    task = _task()
    task['aborted'] = True
    task['content'] = 'partial work'
    task['messages'] = [{'role': 'tool', 'content': 'committed evidence'}]
    if guard in {'stalled', 'deadline'}:
        task['_invocationStopReason'] = guard
    elif guard:
        task['_abort_reason'] = guard
    emitted, persisted, stamps = [], [], []
    monkeypatch.setattr(post, '_settle_fatal_tool_rounds', lambda task: None)
    monkeypatch.setattr('lib.tasks_pkg.compaction._settlement.settle_compaction_usage', lambda task: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.stamp_chat_task_terminal',
                        lambda task, **kw: stamps.append(kw))
    monkeypatch.setattr(post, 'append_event', lambda task, event: emitted.append(event))
    monkeypatch.setattr(post, 'persist_task_result', lambda task: persisted.append(dict(task)))
    monkeypatch.setattr(post, '_maybe_auto_retry_turn', lambda *a: pytest.fail('Stop must not retry'))
    error = (EventDeliveryCancelled('owner Stop') if error_type == 'delivery'
             else AbortedError('stopped at persistence checkpoint'))
    assert post.handle_task_fatal(task, error) is False
    assert task['content'] == 'partial work'
    assert persisted[0]['messages'] == [{'role': 'tool', 'content': 'committed evidence'}]
    if guard:
        assert task['status'] == task['finishReason'] == 'error'
        assert task['error']['failureAttribution']['phase'] == 'task-fatal'
    else:
        assert task['status'] == task['finishReason'] == 'aborted'
        assert not task.get('error') and not emitted[0].get('error')
        assert stamps == [{'status': 'aborted', 'finish_reason': 'aborted'}]
        from lib.tasks_pkg.manager._stream import _abort_request_attribution
        assert _abort_request_attribution(task, error) == ('aborted', None, None)
