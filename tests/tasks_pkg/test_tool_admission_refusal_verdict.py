"""Admission refusals halt like aborts but settle as failed, never as a user Stop.

A refused model request is never charged to a provider: no retry, key rotation
or fallback model is attempted.
"""

import lib.tasks_pkg.llm_fallback._pool as _fallback_pool
import lib.tasks_pkg.llm_fallback._retry as _fallback_retry
import lib.tasks_pkg.llm_fallback._transport as _fallback_transport
import lib.tasks_pkg.llm_fallback._usage as _fallback_usage
import lib.tasks_pkg.manager._events as _fallback_events
import threading

import pytest

from lib.llm.llm_errors import AbortedError, ModelAdmissionRefused, ToolAdmissionRefused
from lib.tasks_pkg.turn_retry import should_auto_retry_turn
from lib.turn_verdict import TurnStatus, derive_task_verdict

pytestmark = pytest.mark.unit


def fatal(monkeypatch, error):
    import lib.tasks_pkg.orchestrator._post_loop as post
    task = {'id': 'refused-worker', 'config': {'model': 'worker-model'}, 'content': '',
            'thinking': '', 'events': [], 'toolRounds': [],
            'content_lock': threading.Lock(), 'events_lock': threading.Lock()}
    events = []
    monkeypatch.setattr(post, '_settle_fatal_tool_rounds', lambda task: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.stamp_chat_task_terminal', lambda *a, **kw: None)
    monkeypatch.setattr('lib.tasks_pkg.compaction._settlement.settle_compaction_usage', lambda task: None)
    monkeypatch.setattr(post, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr(post, 'persist_task_result', lambda task: None)
    assert post.handle_task_fatal(task, error) is False
    return task, events


def test_refusal_settles_failed_with_tool_and_reason(monkeypatch):
    refusal = ToolAdmissionRefused('File execution requires its registered owned isolated worktree',
                                   tool_name='run_command')
    task, events = fatal(monkeypatch, refusal)
    assert task['status'] == 'error' and task['finishReason'] == 'error'
    assert not task.get('aborted')
    error = task['error']
    assert error['kind'] == 'tool_call_rejected' and error['retryable'] is False
    assert error['detail'] == ('`run_command` was refused before execution: '
                               'File execution requires its registered owned isolated worktree')
    assert events[-1]['finishReason'] == 'error' and events[-1]['error']['kind'] == 'tool_call_rejected'
    verdict = derive_task_verdict(task)
    assert verdict.status is TurnStatus.FAILED and verdict.cause != 'user_abort'
    assert should_auto_retry_turn(error, 0, {})[0] is False


def test_plain_abort_remains_an_interrupted_user_stop(monkeypatch):
    task, events = fatal(monkeypatch, AbortedError('Stopped by the user'))
    assert task['status'] == 'aborted' and task['aborted'] is True and task['error'] is None
    assert events[-1]['finishReason'] == 'aborted'
    verdict = derive_task_verdict(task)
    assert verdict.status is TurnStatus.INTERRUPTED and verdict.cause == 'user_abort'


_REFUSED = 'Coordinator request authority ended: Original request source or principal changed'


def test_model_refusal_settles_failed_without_blaming_a_provider(monkeypatch):
    task, events = fatal(monkeypatch, ModelAdmissionRefused(_REFUSED))
    assert task['status'] == 'error' and task['finishReason'] == 'error'
    assert not task.get('aborted')
    error = task['error']
    assert error['kind'] == 'permission' and error['retryable'] is False
    assert error['context'] == 'model-admission'
    assert error['detail'] == f'Refused before any provider was contacted: {_REFUSED}'
    # The kind's default title blames a credential; the refusal renders its own.
    assert 'titleKey' not in error and 'hintKey' not in error
    assert 'before it was sent' in error['message'] and 'NOT a key' in error['hint']
    assert events[-1]['finishReason'] == 'error' and events[-1]['error']['kind'] == 'permission'
    verdict = derive_task_verdict(task)
    assert verdict.status is TurnStatus.FAILED and verdict.cause != 'user_abort'
    assert should_auto_retry_turn(error, 0, {})[0] is False


@pytest.mark.parametrize('error, refused', [
    (ModelAdmissionRefused(_REFUSED), True),
    (ToolAdmissionRefused('Worker call root changed', tool_name='write_file'), True),
    (AbortedError('Stopped by the user'), False),
])
def test_only_a_plain_abort_in_a_model_round_becomes_a_user_stop(monkeypatch, error, refused):
    from types import SimpleNamespace
    from lib.tasks_pkg.orchestrator import _llm_round_call as round_call

    def call(*args, **kwargs):
        raise error

    monkeypatch.setattr(round_call, '_llm_call_with_fallback', call)
    task = {'id': 'round-task', 'abort_event': threading.Event()}
    state = SimpleNamespace(model='m', tool_call_happened=False, preset='default',
                            thinking_enabled=False, accumulated_usage={}, api_rounds=[])

    def run():
        return round_call.run_llm_call_with_fallback(
            task, state, {}, [], [], SimpleNamespace(on_tool_call_ready=None),
            round_num=0, tid='round', max_tokens=30)

    if refused:
        with pytest.raises(type(error)):
            run()
        assert not task.get('aborted') and not task['abort_event'].is_set()
    else:
        assert run() == 'break' and state.exit_reason == 'user_abort'
        assert task['aborted'] is True


@pytest.fixture
def fallback_streams(monkeypatch):
    import lib.tasks_pkg.llm_fallback._call as call_mod
    import lib.tasks_pkg.round_retry as round_retry_mod

    state = {'streams': [], 'events': [], 'rescues': 0}

    def refuse(task, body, tag='', on_tool_call_ready=None, **kwargs):
        state['streams'].append(body.get('model'))
        raise ModelAdmissionRefused(_REFUSED)

    def rescue(*args, **kwargs):
        state['rescues'] += 1

    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', refuse)
    monkeypatch.setattr(_fallback_retry, '_get_fallback_model', lambda task: 'kimi-k3')
    monkeypatch.setattr(_fallback_usage, '_emit_round_usage', lambda *a, **k: None)
    monkeypatch.setattr(_fallback_events, 'append_event', lambda task, event: state['events'].append(event))
    monkeypatch.setattr(_fallback_pool, '_attempt_pool_rescue', rescue)
    monkeypatch.setattr(round_retry_mod, 'append_event', lambda task, event: None)
    return call_mod, state


def test_model_refusal_never_tries_the_configured_fallback_or_the_pool(fallback_streams):
    """Regression for coordinator-1a390889: kimi-k3 was blamed for a refusal it never saw."""
    call_mod, state = fallback_streams
    task = {'id': 'refusal-task', 'convId': 'conv', '_userId': 1, 'config': {},
            'events': [], 'events_lock': threading.Lock(), 'content_lock': threading.Lock(),
            'content': '', 'thinking': '', '_round_base_content': '', '_round_base_thinking': ''}
    with pytest.raises(ModelAdmissionRefused):
        call_mod._llm_call_with_fallback(
            task, {'model': 'claude-opus-5.5'}, 'claude-opus-5.5', 0, 4096, False, None,
            [{'role': 'user', 'content': 'Continue.'}], 'medium', True, {}, [])
    assert state['streams'] == ['claude-opus-5.5'] and state['rescues'] == 0
    assert not [event for event in state['events']
                if 'fallback' in str(event.get('type', '')).lower()]
