"""Pre-ingress credential failures retry the same round without replaying tools."""
import pytest

from lib.llm_errors import (AbortedError, LocalRequestPreparationError,
                            SubscriptionCredentialUnavailableError)
from lib.tasks_pkg.llm_fallback import _call, _credential_recovery as recovery

pytestmark = pytest.mark.unit


def _failure(*, transient=True, provider_reached=False):
    cause = SubscriptionCredentialUnavailableError(
        reason='codex_command_timeout' if transient else 'account_changed',
        credential_source='local', transient=transient)
    error = LocalRequestPreparationError('synthetic credential failure')
    error.__cause__ = cause
    error.provider_reached = provider_reached
    return error


@pytest.fixture
def clock(monkeypatch):
    from types import SimpleNamespace
    now = [0.0]
    def sleep(seconds):
        now[0] += seconds
    monkeypatch.setattr(recovery, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=sleep))
    monkeypatch.setattr(recovery, 'append_event', lambda task, event: task['events'].append(event))
    return now


def test_late_round_recovery_preserves_body_model_and_completed_tool_history(monkeypatch, clock):
    from tests.tasks_pkg.test_pool_rescue_fallback import _base_task, _patch_common, _set_gate
    calls = []
    body = {'model': 'gpt-6.1-sol', 'messages': [
        {'role': 'user', 'content': 'continue'},
        {'role': 'tool', 'tool_call_id': 'completed-tool', 'content': 'already committed'}]}
    def stream(task, request, **kwargs):
        calls.append((request, kwargs['tag']))
        if len(calls) < 3:
            raise _failure()
        return ({'role': 'assistant', 'content': 'continued'}, 'stop', {})
    _patch_common(monkeypatch, stream)
    gate = _set_gate(monkeypatch, has=True)
    task = _base_task()
    result = _call._llm_call_with_fallback(
        task, body, 'gpt-6.1-sol', 128, 512, True, [], body['messages'], 'low', False, {}, [])
    assert result['finish_reason'] == 'stop'
    assert result['assistant_msg']['content'] == 'continued'
    assert len(calls) == 3
    assert all(request is body and tag == 'R129' for request, tag in calls)
    assert body['messages'][-1]['content'] == 'already committed'
    assert not task.get('error')
    assert not gate.calls


@pytest.mark.parametrize('error', [
    _failure(transient=False), _failure(provider_reached=True),
    LocalRequestPreparationError('invalid body'), RuntimeError('stream already sent')])
def test_only_proven_transient_pre_ingress_failures_retry(monkeypatch, clock, error):
    calls = []
    def stream(*args, **kwargs):
        calls.append(1)
        raise error
    monkeypatch.setattr(_call, 'stream_llm_response', stream)
    with pytest.raises(type(error)) as raised:
        _call._stream_with_credential_recovery(_call.stream_llm_response, {'id': 'task', 'events': []}, {'model': 'same'})
    assert raised.value is error
    assert calls == [1]


def test_delivered_tool_callback_prevents_replay(monkeypatch, clock):
    tools = []
    def stream(task, body, **kwargs):
        kwargs['on_tool_call_ready']('side-effect')
        raise _failure()
    monkeypatch.setattr(_call, 'stream_llm_response', stream)
    with pytest.raises(LocalRequestPreparationError):
        _call._stream_with_credential_recovery(_call.stream_llm_response,
            {'id': 'task', 'events': []}, {}, on_tool_call_ready=tools.append)
    assert tools == ['side-effect']


def test_persistent_failure_is_bounded_and_keeps_final_typed_error(monkeypatch, clock):
    errors = []
    def stream(*args, **kwargs):
        error = _failure()
        errors.append(error)
        raise error
    monkeypatch.setattr(_call, 'stream_llm_response', stream)
    with pytest.raises(LocalRequestPreparationError) as raised:
        _call._stream_with_credential_recovery(_call.stream_llm_response, {'id': 'task', 'events': []}, {})
    assert len(errors) == 3
    assert raised.value is errors[-1]


def test_cancellation_interrupts_backoff_without_another_attempt(monkeypatch, clock):
    task = {'id': 'task', 'events': []}
    calls = []
    def stream(*args, **kwargs):
        calls.append(1)
        raise _failure()
    def sleep(seconds):
        clock[0] += seconds
        task['aborted'] = True
    monkeypatch.setattr(recovery.time, 'sleep', sleep)
    monkeypatch.setattr(_call, 'stream_llm_response', stream)
    with pytest.raises(AbortedError):
        _call._stream_with_credential_recovery(_call.stream_llm_response, task, {})
    assert calls == [1]
    assert clock[0] <= 0.1


def test_recovery_deadline_prevents_another_attempt(monkeypatch, clock):
    errors = []
    def stream(*args, **kwargs):
        clock[0] += 29.5
        error = _failure()
        errors.append(error)
        raise error
    monkeypatch.setattr(_call, 'stream_llm_response', stream)
    with pytest.raises(LocalRequestPreparationError) as raised:
        _call._stream_with_credential_recovery(_call.stream_llm_response, {'id': 'task', 'events': []}, {})
    assert raised.value is errors[0]
    assert len(errors) == 1
    assert clock[0] == 30


def test_real_task_stream_recovers_pre_ingress_failure_without_terminal_task_error(monkeypatch, clock):
    import threading
    from lib.tasks_pkg.manager import _stream as manager
    from tests.support.chat_tasks import chat_task_fixture_guard, chat_task_registry
    task = {'id': 'shared-recovery-real-stream', 'convId': 'synthetic', '_userId': 1,
            'status': 'running', '_transientRuntime': True, 'config': {},
            'content': '', 'thinking': '', 'events': [], 'toolRounds': [],
            'content_lock': threading.Lock(), 'events_lock': threading.Lock()}
    calls = []
    def dispatch(body, **kwargs):
        calls.append(body['model'])
        if len(calls) == 1:
            raise _failure()
        return ({'role': 'assistant', 'content': 'resumed', 'reasoning_content': ''}, 'stop', {})
    monkeypatch.setattr(manager, 'dispatch_stream', dispatch)
    with chat_task_fixture_guard:
        chat_task_registry[task['id']] = task
    try:
        result = recovery.stream_with_credential_recovery(
            manager.stream_llm_response, task, {'model': 'gpt-6.1-sol'})
    finally:
        with chat_task_fixture_guard:
            chat_task_registry.pop(task['id'], None)
    assert result.message['content'] == 'resumed'
    assert calls == ['gpt-6.1-sol', 'gpt-6.1-sol']
    assert task['status'] == 'running'
    assert not task.get('error')
