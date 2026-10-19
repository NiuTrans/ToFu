"""Local admission evidence survives the shared round-error and persistence path."""

import json

import pytest

from lib.error_envelope import from_exception, from_json, make_envelope, to_json
from lib.llm.llm_error_format import format_llm_error_for_user
from lib.llm.llm_errors import PromptTooLongError

pytestmark = pytest.mark.unit


def test_unknown_usage_keeps_its_identity_across_schema_and_persistence():
    from jsonschema import validate
    from lib.error_envelope import normalize_envelope, typed_error_envelope_schema

    envelope = normalize_envelope({'kind': 'execution_token_usage_unknown'},
        context='native-execution', source='native')
    assert envelope['kind'] == 'execution_token_usage_unknown'
    assert envelope['severity'] == 'warning'
    assert envelope['retryable'] is False
    assert envelope['titleKey'] == 'err.k.execution_token_usage_unknown.title'
    assert envelope['hintKey'] == 'err.k.execution_token_usage_unknown.hint'

    assert '检查并继续' in envelope['hint']
    assert 'Check and continue' in envelope['hint']
    assert not any(text in envelope['hint'] for text in (
        'maintainer', '维护者', '核对供应商', 'logs/', 'write code'))
    validate(envelope, typed_error_envelope_schema())
    assert from_json(to_json(envelope)) == envelope


def test_unknown_usage_is_not_inferred_from_untyped_occurrence_text():
    from lib.error_envelope import normalize_envelope

    envelope = normalize_envelope('Provider mentioned execution_token_usage_unknown in an error')
    assert envelope['kind'] == 'generic'
    assert make_envelope('budget_exceeded')['kind'] == 'budget_exceeded'


def test_provider_round_error_retains_existing_envelope():
    error = PromptTooLongError('HTTP 413: provider context overflow')
    envelope = format_llm_error_for_user(
        error, model='synthetic-model', context='task-fatal', source='orchestrator')
    assert envelope == make_envelope(
        'prompt_too_long', model='synthetic-model', context='task-fatal',
        source='orchestrator', detail=str(error), raw=str(error))
    assert envelope['retryable'] is False
    assert 'providerReached' not in envelope
    assert 'origin' not in envelope


def test_local_schema_floor_round_error_is_truthful_and_content_free():
    secret = 'synthetic-secret-prompt-and-tool-arguments'
    error = PromptTooLongError(
        secret, origin='local', reason='tool_schema_floor',
        tool_schema_tokens=13833, prompt_budget_tokens=1,
        total_tokens=29030, context_window_tokens=128000)
    error.__cause__ = RuntimeError(secret)
    envelope = format_llm_error_for_user(
        error, model='synthetic-model', context='task-fatal', source='orchestrator')
    assert envelope['kind'] == 'prompt_too_long'
    assert envelope['origin'] == 'local'
    assert envelope['providerReached'] is False
    assert envelope['retryable'] is False
    assert envelope['failureStage'] == 'prompt_admission'
    assert envelope['source'] == 'prompt-admission'
    assert envelope['reason'] == 'tool_schema_floor'
    assert envelope['toolSchemaTokens'] == 13833
    assert envelope['promptBudgetTokens'] == 1
    assert envelope['totalTokens'] == 29030
    assert envelope['contextWindowTokens'] == 128000
    assert '未发送模型请求' in envelope['message']
    assert 'no model request sent' in envelope['message']
    assert '工具的定义' in envelope['hint']
    assert 'tool schemas alone' in envelope['hint']
    assert '预算配置' in envelope['hint']
    assert 'prompt-budget configuration' in envelope['hint']
    assert 'titleKey' not in envelope
    assert 'hintKey' not in envelope
    text = json.dumps(envelope, ensure_ascii=False)
    assert secret not in text
    for incorrect in ('after auto', 'compaction attempted', 'automatically compacted',
                      'delete history', '删除历史', '已压缩', 'HTTP 400', 'HTTP 413'):
        assert incorrect not in text
    assert 'toolSchemaTokens=13833' in envelope['detail']
    assert from_json(to_json(envelope)) == envelope


@pytest.mark.parametrize('reason', ['summary_route_unavailable', 'prompt_budget_exceeded'])
def test_other_local_refusals_do_not_invent_compaction_attempts(reason):
    envelope = from_exception(PromptTooLongError('private body', origin='local', reason=reason))
    assert envelope['reason'] == reason
    assert envelope['providerReached'] is False
    assert envelope['retryable'] is False
    assert 'private body' not in json.dumps(envelope)
    assert 'titleKey' not in envelope
    assert 'hintKey' not in envelope
    assert 'after auto' not in envelope['message']


@pytest.mark.parametrize('bad_count', [-1, True, 1.5, 'synthetic-secret',
                                       {'prompt': 'synthetic-secret'}, 2**53])
def test_invalid_evidence_is_omitted_not_coerced_or_leaked(bad_count):
    envelope = from_exception(PromptTooLongError(
        'synthetic-secret', origin='local', reason='synthetic-secret',
        tool_schema_tokens=bad_count, prompt_budget_tokens=bad_count,
        total_tokens=bad_count, context_window_tokens=bad_count))
    assert envelope['reason'] == 'prompt_budget_exceeded'
    for field in ('toolSchemaTokens', 'promptBudgetTokens', 'totalTokens', 'contextWindowTokens'):
        assert field not in envelope
    assert 'synthetic-secret' not in json.dumps(envelope)


@pytest.mark.parametrize('count', [0, 2**53 - 1])
def test_count_boundaries_are_preserved(count):
    envelope = from_exception(PromptTooLongError(
        origin='local', total_tokens=count))
    assert envelope['totalTokens'] == count
    assert len(envelope['raw']) <= 300
    assert len(envelope['detail']) <= 300


def test_message_text_alone_never_marks_provider_error_as_local():
    envelope = from_exception(PromptTooLongError('Local admission refused before dispatch'))
    assert 'providerReached' not in envelope
    assert envelope['titleKey'] == 'err.k.prompt_too_long.title'


def test_positional_exception_args_remain_backwards_compatible():
    for args in ((), ('message',), ('message', 413)):
        assert PromptTooLongError(*args).args == Exception(*args).args
        assert str(PromptTooLongError(*args)) == str(Exception(*args))


def test_local_extensions_are_declared_in_wire_schema():
    from lib.error_envelope import typed_error_envelope_schema
    properties = typed_error_envelope_schema()['properties']
    assert properties['origin']['enum'] == ['local']
    assert properties['providerReached']['type'] == 'boolean'
    for field in ('toolSchemaTokens', 'promptBudgetTokens', 'totalTokens', 'contextWindowTokens'):
        assert properties[field] == {
            'type': 'integer', 'minimum': 0, 'maximum': 2**53 - 1}
