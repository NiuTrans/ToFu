"""Terminal synthesis preserves admitted context and the loop's failure cause."""

from copy import deepcopy
import time
from uuid import uuid4

import pytest

from lib.llm.llm_errors import AbortedError, ContextCompactionError
from lib.llm.stream_result import ProviderStreamResult
from lib.tasks_pkg.compaction import _prompt_admission as admission
from lib.tasks_pkg.orchestrator import _finalize

pytestmark = pytest.mark.unit


@pytest.fixture
def finalize_turn(monkeypatch):
    events, bodies = [], []
    monkeypatch.setattr(
        _finalize, 'append_event',
        lambda _task, event: events.append(deepcopy(event)))

    def stream(task, body, **kwargs):
        bodies.append(deepcopy(body))
        task['content'] = 'Answer from the current context and fetched evidence.'
        return {'role': 'assistant', 'content': task['content']}, 'stop', None

    monkeypatch.setattr(_finalize, 'stream_llm_response', stream)

    def run(*, finish='stop', error=None, aborted=False, messages=None,
            original_messages=None, fields=None, model='gpt-5.6-sol', stream_result=None):
        config = {'model': 'gpt-5.6-sol', 'maxTokens': 512}
        task = {
            'id': 'synthesis-' + uuid4().hex, '_userId': 1,
            'convId': '', 'status': 'running', 'aborted': aborted,
            'content': '', 'thinking': '', 'error': error,
            'toolRounds': [], 'toolCallCount': 1, 'config': config,
            'model': model, 'created_at': time.time(),
            '_transientRuntime': True, '_invocationEventSink': lambda _event: None,
        }
        task.update(fields or {})
        current = messages if messages is not None else [
            {'role': 'user', 'content': 'Current request'}]
        original = original_messages if original_messages is not None else deepcopy(current)
        task['messages'] = current
        _finalize._finalize_and_emit_done(
            task, model=model, preset='medium', thinking_depth=None,
            cfg=config, last_finish_reason=finish, last_usage=None,
            last_stream_result=stream_result, accumulated_usage={}, api_rounds=[],
            tool_call_happened=True, messages=current, original_messages=original,
            all_search_results_text=['FETCHED EVIDENCE'], max_tokens=512,
            thinking_enabled=False, temperature=1.0, _loop_exit_reason=finish,
            _abort_detected_phase='', project_path='', project_enabled=False,
            round_num=120, assistant_msg={'role': 'assistant', 'content': ''})
        done, = [event for event in events if event['type'] == 'done']
        assert task['status'] == 'done'
        return task, done, bodies, events

    return run


def test_synthesis_uses_compacted_history_and_current_steering(finalize_turn):
    original = [{'role': 'user', 'content': 'OBSOLETE UNCOMPACTED HISTORY'}]
    current = [
        {'role': 'system', 'content': 'Required policy'},
        {'role': 'user', 'content': 'Accepted continuation summary'},
        {'role': 'user', 'content': 'Current owner correction'},
    ]
    before = deepcopy(current)
    task, done, bodies, events = finalize_turn(
        messages=current, original_messages=original)

    body, = bodies
    assert body['messages'][0] == before[0]
    for message in before[1:]:
        assert message['content'] in str(body['messages'])
    assert 'OBSOLETE UNCOMPACTED HISTORY' not in str(body)
    assert 'FETCHED EVIDENCE' in str(body)
    assert current == before
    assert original == [{'role': 'user', 'content': 'OBSOLETE UNCOMPACTED HISTORY'}]
    assert task['_lastPromptAdmission']['action'] == 'admit'
    assert done.get('error') is None
    snapshot, = [event for event in events if event['type'] == 'messages_snapshot']
    assert snapshot['messages'] == body['messages']


@pytest.mark.parametrize('finish,error,aborted', [
    ('error', {'kind': 'endpoint_unreachable', 'detail': 'DNS connection failed'}, False),
    ('stop', {'kind': 'execution_budget_exceeded', 'detail': 'Budget exhausted'}, False),
    ('error', None, False),
    ('content_filter', None, False),
    ('length', None, False),
    ('stop', None, True),
])
def test_terminal_failure_or_stop_never_starts_synthesis(
        finalize_turn, finish, error, aborted):
    task, done, bodies, events = finalize_turn(finish=finish, error=error, aborted=aborted)

    assert bodies == []
    assert task['error'] == error
    assert done.get('error') == error
    assert not any(event['type'] == 'messages_snapshot' for event in events)


@pytest.mark.parametrize('usage', [None, {'_empty_stop': True}])
def test_verified_empty_stop_recovers_without_retaining_the_empty_response_error(
        finalize_turn, usage):
    empty = ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': ''}, 'stop', usage)
    error = {'kind': 'abnormal_stop', 'detail': 'Empty response recovery exhausted'}
    task, done, bodies, _ = finalize_turn(
        finish='abnormal_stop', error=error, stream_result=empty)

    assert len(bodies) == 1
    assert task['error'] is None
    assert done.get('error') is None
    assert done['finishReason'] == 'stop'


def test_abnormal_transport_close_is_not_an_empty_stop_recovery(finalize_turn):
    interrupted = ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': ''}, 'premature_close', None)
    error = {'kind': 'abnormal_stop', 'detail': 'Provider closed without finish marker'}
    task, done, bodies, _ = finalize_turn(
        finish='abnormal_stop', error=error, stream_result=interrupted)

    assert bodies == []
    assert task['error'] == done['error'] == error


def test_empty_synthesis_cannot_clear_the_existing_error(monkeypatch, finalize_turn):
    empty = ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': ''}, 'stop', {'_empty_stop': True})
    monkeypatch.setattr(_finalize, 'stream_llm_response', lambda *a, **k: empty)
    error = {'kind': 'abnormal_stop', 'detail': 'Empty response recovery exhausted'}
    task, done, _, _ = finalize_turn(
        finish='abnormal_stop', error=error, stream_result=empty)

    assert task['error'] == done['error'] == error


def test_admission_failure_settles_without_dispatch_or_source_mutation(
        monkeypatch, finalize_turn):
    current = [{'role': 'user', 'content': [{'type': 'text', 'text': 'Keep source'}]}]
    before = deepcopy(current)

    def refuse(messages, tools, task, **kwargs):
        assert 'FETCHED EVIDENCE' in str(messages)
        assert tools == []
        messages[0]['content'][0]['text'] = 'Detached candidate'
        raise ContextCompactionError('Required context does not fit')

    monkeypatch.setattr(admission, 'enforce_dispatch_prompt_limit', refuse)
    monkeypatch.setattr(
        _finalize.orchestrator_ports, 'build_request_body',
        lambda *a, **k: pytest.fail('refused prompts cannot build a request'))
    task, done, bodies, _ = finalize_turn(messages=current)

    assert bodies == []
    assert current == before
    assert task['error']['context'] == 'post-loop-fallback'
    assert done['error']['kind'] == task['error']['kind'] == 'internal'
    assert task['finishReason'] == 'error'


def test_admission_compacts_before_request_and_snapshot(monkeypatch, finalize_turn):
    current = [{'role': 'user', 'content': 'Oversized current history'}]
    before = deepcopy(current)

    def compact(messages, tools, task, **kwargs):
        assert messages[0] == current[0]
        assert 'FETCHED EVIDENCE' in str(messages)
        messages[0]['content'] = 'Fitting continuation summary'
        return {'totalTokens': 123}

    monkeypatch.setattr(admission, 'enforce_dispatch_prompt_limit', compact)
    task, done, bodies, events = finalize_turn(messages=current)

    body, = bodies
    assert body['messages'][0]['content'] == 'Fitting continuation summary'
    snapshot, = [event for event in events if event['type'] == 'messages_snapshot']
    assert snapshot['messages'] == body['messages']
    assert current == before
    assert task['error'] is None
    assert done['finishReason'] == 'stop'


@pytest.mark.parametrize('raises_abort', [False, True])
def test_owner_stop_during_admission_never_dispatches(
        monkeypatch, finalize_turn, raises_abort):
    def stop(messages, tools, task, **kwargs):
        task['aborted'] = True
        if raises_abort:
            raise AbortedError('Owner stopped synthesis admission')
        return {'totalTokens': 123}

    monkeypatch.setattr(admission, 'enforce_dispatch_prompt_limit', stop)
    task, done, bodies, _ = finalize_turn()

    assert bodies == []
    assert task['error'] is None
    assert done['finishReason'] == 'aborted'


@pytest.mark.parametrize('refused', [False, True])
def test_admission_uses_effective_model_without_changing_configured_selection(
        monkeypatch, finalize_turn, refused):
    admitted = []
    builds = []

    def admit(messages, tools, task, **kwargs):
        admitted.append((task, dict(task['config'])))
        assert kwargs['model'] == 'synthesis-model'
        if refused:
            raise ContextCompactionError('Required context does not fit')
        task['_contextBudgetDecision'] = {'contextWindowTokens': 20_000}
        return {'totalTokens': 123}

    def build(model, messages, **kwargs):
        builds.append(kwargs)
        return {'model': model, 'messages': messages}

    monkeypatch.setattr(admission, 'enforce_dispatch_prompt_limit', admit)
    monkeypatch.setattr(_finalize.orchestrator_ports, 'build_request_body', build)
    task, _, bodies, _ = finalize_turn(model='synthesis-model')

    admitted_task, selection = admitted[0]
    assert admitted_task is task
    assert selection == {'model': 'synthesis-model', 'maxTokens': 512}
    assert task['config'] == {'model': 'gpt-5.6-sol', 'maxTokens': 512}
    if refused:
        assert bodies == builds == []
    else:
        assert bodies[0]['model'] == 'synthesis-model'
        assert builds[0]['context_window'] == 20_000
        assert builds[0]['precomputed_input_tokens'] == 123


def test_request_build_failure_settles_as_synthesis_error(monkeypatch, finalize_turn):
    def fail(*args, **kwargs):
        raise ValueError('Invalid request shape')

    monkeypatch.setattr(_finalize.orchestrator_ports, 'build_request_body', fail)
    task, done, bodies, _ = finalize_turn()

    assert bodies == []
    assert task['finishReason'] == 'error'
    assert done['error']['context'] == 'post-loop-fallback'
