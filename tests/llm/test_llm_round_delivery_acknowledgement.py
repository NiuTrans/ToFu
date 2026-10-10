"""Only verified provider consumption may acknowledge drained inbox deliveries."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib import agent_inbox
from lib.cgroup_guard import MemoryPressureError
from lib.llm import AbortedError, BadRequestError, PermissionError_
from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
from lib.tasks_pkg.orchestrator import _llm_round_call as round_call
from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox

pytestmark = pytest.mark.unit


@pytest.fixture
def delivery(monkeypatch):
    """Drain a real completion twin; spy only on durable deletion/event ports."""
    agent_inbox.reset_for_test()
    task = {
        'id': 'ack-gate-task', 'convId': 'ack-gate-conversation', '_userId': 7,
        'config': {}, 'content': '', 'thinking': '', 'aborted': False,
        'toolRounds': [],
    }
    payload = '<background-command id="bg-ack" status="completed">ok</background-command>'
    agent_inbox.enqueue(
        task['convId'], payload, priority='next', mode='background-command',
        extra={'queueId': 'queue-bg', 'commandId': 'bg-ack', 'command': 'pytest'},
    )
    messages = [{'role': 'user', 'content': 'go'}]
    drain_and_inject_inbox(task=task, messages=messages, round_num=1, tid='ack-gate')
    assert payload in messages[-1]['content']
    # The shared flush must keep all lanes pending, not just the completion.
    task['_peer_inject_pending'] = [{'queueId': 'queue-peer', 'value': 'peer'}]
    task['_steer_inject_pending'] = [{'value': 'steer', 'blockId': 'steer-block'}]
    pending = {key: deepcopy(value) for key, value in task.items()
               if key.endswith('_inject_pending')}
    deleted, events = Mock(), []
    monkeypatch.setattr('lib.message_queue.dedup_inbox_durable_rows', deleted)
    monkeypatch.setattr(
        'lib.tasks_pkg.orchestrator._deferred_inbox_flush.append_event',
        lambda _task, event: events.append(event))
    rs = SimpleNamespace(
        model='primary-model', preset='low', thinking_enabled=False,
        tool_call_happened=True, accumulated_usage={}, api_rounds=[],
        last_usage={'prior': 1}, exit_reason=None,
    )
    callback = Mock()

    def call():
        return round_call.run_llm_call_with_fallback(
            task, rs, {'model': rs.model, 'messages': messages}, messages, [],
            SimpleNamespace(on_tool_call_ready=callback),
            round_num=1, tid='ack-gate', max_tokens=512)

    yield SimpleNamespace(task=task, rs=rs, pending=pending, deleted=deleted,
                          events=events, call=call)
    agent_inbox.reset_for_test()


def _assert_pending(delivery):
    for key, value in delivery.pending.items():
        assert delivery.task[key] == value
    delivery.deleted.assert_not_called()
    assert delivery.events == []
    assert not any(delivery.task.get(key) for key in (
        '_bgCommandInjects', '_peerInjects', '_userSteerInjects'))


def _provider_runtime(monkeypatch, stream, *, fallback_model='fallback-model'):
    """Keep the actual fallback policy and round seam; isolate provider I/O."""
    import lib.tasks_pkg.llm_fallback._call as fallback
    monkeypatch.setattr(fallback, 'stream_llm_response', stream)
    monkeypatch.setattr(fallback, '_get_fallback_model', lambda task: fallback_model)
    monkeypatch.setattr(fallback, '_attempt_pool_rescue', lambda *a, **kw: None)
    monkeypatch.setattr(fallback, '_emit_round_usage', lambda *a, **kw: None)
    monkeypatch.setattr(fallback, '_log_stream_attempt_outcome', lambda *a, **kw: None)
    monkeypatch.setattr(fallback, 'append_event', lambda *a, **kw: None)
    monkeypatch.setattr(fallback, 'audit_log', lambda *a, **kw: None)
    monkeypatch.setattr(fallback, 'model_supports_vision', lambda model: False)
    monkeypatch.setattr(fallback, 'build_body',
                        lambda model, messages, **kw: {'model': model, 'messages': messages})
    return fallback


@pytest.mark.parametrize('failure,exit_prefix', [
    ('memory', 'local_memory_pressure'),
    ('rejected', 'request_rejected'),
    ('no-fallback', 'opus_error_with_tool_calls'),
    ('both-failed', 'both_models_failed'),
    ('aborted', 'user_abort'),
])
def test_actual_fallback_failures_preserve_drained_completion(
        monkeypatch, delivery, failure, exit_prefix):
    error = PermissionError_('provider refused credentials')
    if failure == 'memory':
        error = MemoryPressureError('local request cannot fit')
    elif failure == 'rejected':
        error = BadRequestError('API HTTP 400: invalid request')
    elif failure == 'aborted':
        error = AbortedError('owner cancelled')
    stream = Mock(side_effect=error)
    fallback = _provider_runtime(
        monkeypatch, stream,
        fallback_model='' if failure == 'no-fallback' else 'fallback-model')
    if failure == 'memory':
        monkeypatch.setattr(fallback, '_attempt_request_payload_recovery',
                            lambda *a, **kw: (None, error, None))
    assert delivery.call() == 'break'
    assert delivery.rs.exit_reason.startswith(exit_prefix)
    _assert_pending(delivery)
    assert stream.call_count == (2 if failure == 'both-failed' else 1)


@pytest.mark.parametrize('state', [state for state in ProviderStreamState
                                  if state is not ProviderStreamState.PROVIDER_FINISHED])
def test_unusable_provider_return_never_acknowledges(monkeypatch, delivery, state):
    # A compatibility 'stop' and even partial prose cannot override typed failure.
    finish_seen = state in {ProviderStreamState.EMPTY_RESPONSE,
                           ProviderStreamState.TOOL_PAYLOAD_MISSING}
    result = ProviderStreamResult(
        message={'role': 'assistant', 'content': 'partial'},
        compatibility_finish_reason='stop', usage=None, state=state,
        provider_finish_reason='stop' if finish_seen else None,
        saw_finish_reason=finish_seen,
        malformed_frame_count=1 if state is ProviderStreamState.MALFORMED_STREAM else 0,
    )
    _provider_runtime(monkeypatch, Mock(return_value=result))
    assert delivery.call() == 'proceed'
    assert delivery.rs.last_stream_result.state is state
    _assert_pending(delivery)


@pytest.mark.parametrize('loop_action,include_stream', [(None, False), ('break', True)])
def test_synthetic_success_or_loop_break_is_not_delivery_proof(
        monkeypatch, delivery, loop_action, include_stream):
    result = {
        'assistant_msg': {'role': 'assistant', 'content': 'synthetic'},
        'finish_reason': 'stop', 'usage': None, 'model': 'primary-model',
        'preset': 'low', 'thinking_enabled': False,
        '_loop_action': loop_action, '_loop_exit_reason': 'failed',
    }
    if include_stream:
        result['stream_result'] = ProviderStreamResult.from_legacy(
            result['assistant_msg'], 'stop', None)
    monkeypatch.setattr(round_call, '_llm_call_with_fallback', lambda *a, **kw: result)
    assert delivery.call() == ('break' if loop_action else 'proceed')
    _assert_pending(delivery)


@pytest.mark.parametrize('use_fallback', [False, True])
def test_verified_primary_or_successful_fallback_acknowledges_once(
        monkeypatch, delivery, use_fallback):
    result = ProviderStreamResult(
        message={'role': 'assistant', 'content': 'consumed'},
        compatibility_finish_reason='stop', usage=None,
        state=ProviderStreamState.PROVIDER_FINISHED,
        provider_finish_reason='stop', saw_finish_reason=True,
    )
    attempts = [PermissionError_('primary unavailable'), result] if use_fallback else [result]
    stream = Mock(side_effect=attempts)
    _provider_runtime(monkeypatch, stream)
    assert delivery.call() == 'proceed'
    assert stream.call_count == (2 if use_fallback else 1)
    assert delivery.task['model'] == ('fallback-model' if use_fallback else 'primary-model')
    assert all(key not in delivery.task for key in delivery.pending)
    assert delivery.task['_bgCommandInjects'][0]['previews'][0]['commandId'] == 'bg-ack'
    # The background receipt uses Turn authority, never the separate peer ACK
    # port. This storage-free gate fixture exercises emission only; real atomic
    # deletion/rollback is covered in test_background_delivery_receipt.
    delivery.deleted.assert_called_once_with('ack-gate-conversation', ['queue-peer'], user_id=7)
    assert {event['type'] for event in delivery.events} == {
        'background_command_inject', 'peer_inbox_inject', 'user_steer_inject'}
