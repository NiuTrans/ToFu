"""Unrepresentable native history cannot hide the primary availability error."""
from copy import deepcopy

import pytest

from lib.llm import build_body
from lib.llm.anthropic_outbound import openai_body_to_anthropic_checked
from lib.llm.conversion import ProtocolConversionError
from lib.llm.llm_errors import (
    EndpointUnreachableError, LocalRequestPreparationError, PermissionError_,
    SubscriptionCredentialUnavailableError,
)
from lib.tasks_pkg.llm_fallback import _call
from tests.tasks_pkg.test_pool_rescue_fallback import (
    _base_task, _patch_common, _set_gate,
)

pytestmark = pytest.mark.unit


def _credential_error(transient=True):
    return SubscriptionCredentialUnavailableError(
        reason='codex_command_timeout' if transient else 'account_changed',
        credential_source='local', transient=transient)


@pytest.mark.parametrize('primary_error', [
    _credential_error(), _credential_error(False), PermissionError_('primary denied'),
])
@pytest.mark.parametrize('native_item', [
    {'type': 'compaction', 'encrypted_content': 'opaque-test'},
    {'type': 'shell_call', 'call_id': 'native-call', 'action': {}},
])
def test_native_conversion_preserves_primary_error_and_history(
        monkeypatch, primary_error, native_item):
    messages = [
        {'role': 'user', 'content': 'Continue the existing work'},
        {'role': 'assistant', 'content': 'Completed the first step',
         '_responses_items': [native_item],
         'tool_calls': [{'id': 'call-test', 'type': 'function', 'function': {
             'name': 'read_file', 'arguments': '{"path":"synthetic.txt"}'}}]},
        {'role': 'tool', 'tool_call_id': 'call-test', 'content': 'retained evidence'},
    ]
    before = deepcopy(messages)
    body = build_body('gpt-6-astra', messages, max_tokens=512, defer_reasoning=True)
    body_before = deepcopy(body)
    calls = []

    def stream(task, request, **kwargs):
        calls.append(request['model'])
        if len(calls) == 1:
            raise primary_error
        # Real fallback builder and adapter; no provider call can be made.
        openai_body_to_anthropic_checked(request).require_payload()
        pytest.fail('unrepresentable native state reached transport')

    _patch_common(monkeypatch, stream)
    gate = _set_gate(monkeypatch, has=True)
    task = _base_task()
    task['content'] = 'Prior visible work'
    task['toolRounds'] = [{'round': 28, 'content': 'Prior completed tool'}]
    rounds_before = deepcopy(task['toolRounds'])
    usage, api_rounds = {}, []
    result = _call._llm_call_with_fallback(
        task, body, 'gpt-6-astra', 28, 512, True, [], messages,
        'low', False, usage, api_rounds)

    assert calls == ['gpt-6-astra', 'kimi-k3']
    assert not gate.calls
    assert result['finish_reason'] == 'error'
    assert result['_loop_action'] == 'break'
    assert result['_loop_exit_reason'] == 'fallback_unrepresentable_round_28'
    assert result['model'] == 'gpt-6-astra'
    assert result['preset'] == 'low'
    assert result['thinking_enabled'] is False
    error = task['error']
    expected = _call.format_llm_error_for_user(primary_error, model='gpt-6-astra')
    for field in ('kind', 'retryable', 'message', 'hint', 'detail', 'model'):
        assert error[field] == expected[field]
    assert error['context'] == 'fallback-unrepresentable (gpt-6-astra→kimi-k3)'
    assert error['fallbackModel'] == 'kimi-k3'
    assert error['fallbackDetail'] == 'protocol_conversion:mandatory_continuity_unrepresentable'
    assert error['fallbackFailureStage'] == 'request_prepare'
    assert error['fallbackProviderReached'] is False
    assert error['autoRetryExhausted'] is True
    from lib.turn_lifecycle import automatic_recovery_blocker
    assert automatic_recovery_blocker(error)['retryPolicy']['automatic'] is False
    assert not any(key in task for key in (
        '_fallback_model', '_fallback_from', '_fallback_reason', '_fallback_kind'))
    assert messages == before and body == body_before
    assert task['content'] == 'Prior visible work'
    assert task['toolRounds'] == rounds_before
    assert usage == {} and api_rounds == []


def test_foreign_reasoning_allows_existing_fallback_without_replaying_tools(monkeypatch):
    messages = [
        {'role': 'user', 'content': 'Continue the existing work'},
        {'role': 'assistant', 'content': 'File already written',
         '_responses_items': [{'type': 'reasoning', 'id': 'rs-test',
                               'encrypted_content': 'opaque-test'}],
         'tool_calls': [{'id': 'call-test', 'type': 'function', 'function': {
             'name': 'write_file', 'arguments': '{"path":"done.txt","content":"saved"}'}}]},
        {'role': 'tool', 'tool_call_id': 'call-test', 'content': 'Already saved'},
    ]
    before = deepcopy(messages)
    body = build_body('gpt-6-astra', messages, max_tokens=512, defer_reasoning=True)
    body_before = deepcopy(body)
    calls = []

    def stream(task, request, **kwargs):
        calls.append(request['model'])
        if len(calls) == 1:
            raise EndpointUnreachableError('synthetic connection setup failure')
        wire = openai_body_to_anthropic_checked(request).require_payload()
        assert wire['messages'][1]['content'][-1] == {
            'type': 'tool_use', 'id': 'call-test', 'name': 'write_file',
            'input': {'path': 'done.txt', 'content': 'saved'}}
        assert wire['messages'][2]['content'] == [{
            'type': 'tool_result', 'tool_use_id': 'call-test', 'content': 'Already saved'}]
        assert 'opaque-test' not in repr(wire)
        return ({'role': 'assistant', 'content': 'Continued using the saved file'},
                'stop', {'prompt_tokens': 5, 'completion_tokens': 3})

    _patch_common(monkeypatch, stream)
    gate = _set_gate(monkeypatch, has=True)
    task = _base_task()
    task['toolRounds'] = [{'round': 28, 'content': 'File already written'}]
    rounds_before = deepcopy(task['toolRounds'])
    usage, api_rounds = {}, []
    result = _call._llm_call_with_fallback(
        task, body, 'gpt-6-astra', 28, 512, True, [], messages,
        'low', False, usage, api_rounds)

    assert calls == ['gpt-6-astra', 'kimi-k3']
    assert not gate.calls
    assert result['finish_reason'] == 'stop'
    assert result['_loop_action'] is None
    assert result['model'] == 'kimi-k3'
    assert 'error' not in task
    assert messages == before and body == body_before
    assert task['toolRounds'] == rounds_before
    assert len(api_rounds) == 1 and api_rounds[0]['model'] == 'kimi-k3'
    assert usage['prompt_tokens'] == 5


def test_wrapped_credential_failure_keeps_retry_and_auth_diagnostics():
    primary = LocalRequestPreparationError('credential preparation unavailable')
    primary.__cause__ = _credential_error()
    task = _base_task()
    result = _call._settle_failed_fallback(
        task, primary, original_model='gpt-6-astra', fallback_model='kimi-k3',
        preset='low', thinking_enabled=True, round_num=28,
        blocked_error=ProtocolConversionError('mandatory_continuity_unrepresentable'))
    assert result['model'] == 'gpt-6-astra'
    assert task['error']['authIssue'] == 'codex_command_timeout'
    assert task['error']['credentialSource'] == 'local'
    assert task['error']['failureStage'] == 'subscription_credentials'
    assert task['error']['providerReached'] is False
    assert task['error']['retryable'] is True


@pytest.mark.parametrize('error', [
    ProtocolConversionError('tool_arguments_object_required'),
    LocalRequestPreparationError('protocol_conversion:mandatory_continuity_unrepresentable'),
])
def test_other_local_defects_keep_fallback_attribution(monkeypatch, error):
    calls = []

    def stream(task, body, **kwargs):
        calls.append(body['model'])
        if len(calls) == 1:
            raise PermissionError_('primary denied')
        raise error

    _patch_common(monkeypatch, stream)
    gate = _set_gate(monkeypatch, has=True)
    task = _base_task()
    messages = [{'role': 'user', 'content': 'synthetic'}]
    result = _call._llm_call_with_fallback(
        task, {'model': 'gpt-6-astra'}, 'gpt-6-astra', 1, 512, True,
        [], messages, 'low', False, {}, [])
    assert result['model'] == 'kimi-k3'
    assert task['error']['detail'] == str(error)
    assert 'fallbackDetail' not in task['error']
    assert not gate.calls
