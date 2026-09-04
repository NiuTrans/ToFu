"""Final provider-prompt admission is the fail-closed economic boundary."""

from __future__ import annotations

import pytest

from lib.llm_errors import ContextCompactionError, PromptTooLongError
from lib.tasks_pkg.compaction import _prompt_admission as admission
from lib.token_counter.base import (
    REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY,
)

pytestmark = pytest.mark.unit


def _task() -> dict:
    return {
        'id': 'task-admission',
        'convId': 'conv-admission',
        'config': {
            'model': 'kimi-k3',
            'compaction': {'workingSetTokens': 128_000},
        },
    }


def _measurement(message_tokens: int, schema_tokens: int = 0) -> dict:
    return {
        'messageTokens': message_tokens,
        'toolSchemaTokens': schema_tokens,
        'totalTokens': message_tokens + schema_tokens,
        'method': 'test',
        'messageCount': 3,
    }


def test_first_dispatch_uses_price_aware_working_set_with_host_cap():
    hard, target = admission._resolved_ceiling({
        'config': {'model': 'gpt-5.6-sol'},
    }, round_num=0)

    assert hard == 244_800
    assert target == 229_500


def test_first_generation_admits_a_prompt_below_120k(monkeypatch):
    task = _task()
    monkeypatch.setattr(
        admission, '_measure',
        lambda *a, **k: _measurement(110_000, 4_000),
    )

    result = admission.enforce_dispatch_prompt_limit(
        [{'role': 'user', 'content': 'hello'}],
        [{'type': 'function', 'function': {'name': 'read_files'}}],
        task,
        round_num=0,
        model='kimi-k3',
    )

    assert result['totalTokens'] == 114_000
    assert task['_lastPromptAdmission']['action'] == 'admit'
    assert task['_lastPromptAdmission']['hardCeilingTokens'] == 128_000
    assert task['_lastPromptAdmission']['targetTokens'] == 120_000


def test_complete_prompt_count_does_not_add_tool_schema_twice(monkeypatch):
    """A full-request counter result is the admission total exactly once."""
    task = _task()
    tools = [{'type': 'function', 'function': {'name': 'read_files'}}]
    counted_surfaces = []

    def count_complete_prompt(*args, **kwargs):
        counted_surfaces.append(kwargs.get('tool_schema'))
        return 111_000, 'usage_cache'

    monkeypatch.setattr(
        admission, '_count_tokens_authoritative', count_complete_prompt)
    monkeypatch.setattr(
        admission, '_tool_schema_tokens', lambda *a, **k: 18_000)

    result = admission.enforce_dispatch_prompt_limit(
        [{'role': 'user', 'content': 'large but admissible history'}],
        tools,
        task,
        round_num=0,
        model='kimi-k3',
    )

    assert result == {
        'measurementVersion': 'tofu.prompt-admission/v2',
        'messageTokens': 93_000,
        'toolSchemaTokens': 18_000,
        'totalTokens': 111_000,
        'method': 'usage_cache',
        'messageCount': 1,
    }
    assert counted_surfaces == [tools]
    assert task['_lastPromptAdmission']['action'] == 'admit'


def test_call_local_text_count_hints_never_enter_admission_history(monkeypatch):
    task = _task()
    reuse_hint = {123_456: 789}
    audit_rows = []

    def count_complete_prompt(*args, **kwargs):
        kwargs['measurement_out'][
            REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY
        ] = reuse_hint
        return 10_000, 'tiktoken'

    monkeypatch.setattr(
        admission, '_count_tokens_authoritative', count_complete_prompt)
    monkeypatch.setattr(
        admission, '_tool_schema_tokens', lambda *a, **k: 1_000)
    monkeypatch.setattr(
        admission,
        'audit_log',
        lambda event, **fields: audit_rows.append((event, fields)),
    )

    result = admission.enforce_dispatch_prompt_limit(
        [{'role': 'tool', 'content': 'result'}],
        [{'type': 'function'}],
        task,
        round_num=1,
        model='gpt-5.6-sol',
    )

    assert result[REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY] is reuse_hint
    assert REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY not in (
        task['_lastPromptAdmission'])
    assert REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY not in (
        task['_promptAdmissionHistory'][0])
    assert audit_rows[0][0] == 'provider_prompt_admission'
    assert REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY not in audit_rows[0][1]


def test_measure_reuses_a_validated_turn_stable_schema_count(monkeypatch):
    tools = [{'type': 'function'}]
    monkeypatch.setattr(
        admission,
        '_count_tokens_authoritative',
        lambda *args, **kwargs: (10_000, 'tiktoken'),
    )
    monkeypatch.setattr(
        admission,
        '_tool_schema_tokens',
        lambda *args, **kwargs: pytest.fail(
            'turn-stable schema evidence must avoid serialization'),
    )

    result = admission._measure(
        [{'role': 'user', 'content': 'hello'}],
        tools,
        _task(),
        model='gpt-5.6-sol',
        precomputed_tool_schema_tokens=1_234,
    )

    assert result['toolSchemaTokens'] == 1_234
    assert result['messageTokens'] == 8_766


def test_compact_then_measure_counts_the_unchanged_schema_once(monkeypatch):
    task = _task()
    tools = [{'type': 'function'}]
    prompt_counts = iter((200_000, 10_000))
    schema_calls = []
    monkeypatch.setattr(
        admission,
        '_count_tokens_authoritative',
        lambda *args, **kwargs: (next(prompt_counts), 'tiktoken'),
    )
    monkeypatch.setattr(
        admission,
        '_tool_schema_tokens',
        lambda *args, **kwargs: schema_calls.append(args) or 1_000,
    )
    monkeypatch.setattr(
        admission, 'force_compact_if_needed', lambda *args, **kwargs: True)
    monkeypatch.setattr(
        admission, 'recompose_context_after_compaction', lambda *a, **k: None)

    result = admission.enforce_dispatch_prompt_limit(
        [{'role': 'user', 'content': 'large history'}],
        tools,
        task,
        round_num=0,
        model='gpt-5.6-sol',
    )

    assert result['totalTokens'] == 10_000
    assert result['toolSchemaTokens'] == 1_000
    assert len(schema_calls) == 1


def test_first_generation_compacts_before_the_main_dispatch(monkeypatch):
    task = _task()
    messages = [{'role': 'user', 'content': 'large history'}]
    measurements = iter([
        _measurement(470_000, 8_000),
        _measurement(80_000, 8_000),
    ])
    compact_calls = []
    recompose_calls = []

    monkeypatch.setattr(
        admission, '_measure', lambda *a, **k: next(measurements))
    monkeypatch.setattr(
        admission, 'force_compact_if_needed',
        lambda *a, **k: compact_calls.append(k) or True,
    )
    monkeypatch.setattr(
        admission, 'recompose_context_after_compaction',
        lambda *a, **k: recompose_calls.append((a, k)),
    )

    result = admission.enforce_dispatch_prompt_limit(
        messages, [], task, round_num=0, model='kimi-k3')

    assert result['totalTokens'] == 88_000
    assert compact_calls[0]['force'] is True
    assert compact_calls[0]['_compaction_trigger'] == 'dispatch_guard'
    assert compact_calls[0]['_allow_deterministic_summary_fallback'] is True
    assert isinstance(compact_calls[0]['_result_meta'], dict)
    assert recompose_calls
    assert task['_lastPromptAdmission']['action'] == 'compact_then_admit'


def test_summary_failure_refuses_without_silent_head_truncation(monkeypatch):
    task = _task()
    messages = [
        {'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': 'durable objective'},
        {'role': 'assistant', 'content': 'old result'},
    ]
    original = [dict(message) for message in messages]
    measurements = iter([
        _measurement(470_000, 8_000),
        _measurement(470_000, 8_000),
    ])

    monkeypatch.setattr(
        admission, '_measure', lambda *a, **k: next(measurements))
    monkeypatch.setattr(
        admission, 'force_compact_if_needed', lambda *a, **k: False)

    with pytest.raises(PromptTooLongError, match='refused before dispatch'):
        admission.enforce_dispatch_prompt_limit(
            messages, [], task, round_num=0, model='kimi-k3')

    assert messages == original
    assert task['_lastPromptAdmission']['action'] == 'refuse_summary_failed'


def test_local_compaction_failure_is_not_reported_as_prompt_overflow(monkeypatch):
    task = _task()
    measurements = iter([
        _measurement(470_000, 8_000),
        _measurement(470_000, 8_000),
    ])

    monkeypatch.setattr(
        admission, '_measure', lambda *a, **k: next(measurements))

    def fail_compaction(*_args, **kwargs):
        kwargs['_result_meta'].update({
            'compacted': False,
            'summaryFailureReason': 'summary_failed',
        })
        return False

    monkeypatch.setattr(
        admission, 'force_compact_if_needed', fail_compaction)

    with pytest.raises(ContextCompactionError, match='failed locally'):
        admission.enforce_dispatch_prompt_limit(
            [{'role': 'user', 'content': 'large history'}],
            [],
            task,
            round_num=0,
            model='kimi-k3',
        )

    assert task['_lastPromptAdmission']['action'] == 'refuse_compaction_error'


def test_tool_surface_alone_over_ceiling_refuses_before_compaction(monkeypatch):
    task = _task()
    compact_calls = []
    monkeypatch.setattr(
        admission, '_measure',
        lambda *a, **k: _measurement(10_000, 125_000),
    )
    monkeypatch.setattr(
        admission, 'force_compact_if_needed',
        lambda *a, **k: compact_calls.append(True),
    )

    with pytest.raises(PromptTooLongError, match='selected tool schemas'):
        admission.enforce_dispatch_prompt_limit(
            [{'role': 'user', 'content': 'hello'}],
            [{'type': 'function'}],
            task,
            round_num=0,
            model='kimi-k3',
        )

    assert compact_calls == []
    assert task['_lastPromptAdmission']['action'] == 'refuse_tool_surface'


def test_later_round_uses_the_configured_working_set(monkeypatch):
    task = _task()
    monkeypatch.setattr(
        admission, '_measure',
        lambda *a, **k: _measurement(125_000, 2_000),
    )

    result = admission.enforce_dispatch_prompt_limit(
        [{'role': 'user', 'content': 'hello'}],
        [],
        task,
        round_num=4,
        model='kimi-k3',
    )

    assert result['totalTokens'] == 127_000
    assert task['_lastPromptAdmission']['targetTokens'] == 128_000
