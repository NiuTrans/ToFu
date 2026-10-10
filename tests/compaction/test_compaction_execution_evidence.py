"""Continuation summaries preserve observed execution without granting authority."""
import copy
import json

import pytest

from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
from lib.tasks_pkg.compaction._layer2._prompt import (
    _build_summary_user_content,
    _extract_prior_receipt,
    _format_messages_for_summary,
)

pytestmark = pytest.mark.unit


def _round(name, result, *, call_id='reused', prose=''):
    return [
        {'role': 'assistant', 'content': prose, 'tool_calls': [
            {'id': call_id, 'function': {'name': name, 'arguments':
             json.dumps({'command': 'pytest tests/unit_case.py'})}}]},
        {'role': 'tool', 'tool_call_id': call_id, 'content': result},
    ]


def _rows(text):
    return [json.loads(line.split('] ', 1)[1]) for line in text.splitlines()
            if line.startswith('[tool evidence')]


def test_repeated_receipts_have_one_newest_continuity_body_and_no_legacy_wrapper():
    old = '### Current Working State\nOLD_RECEIPT_SENTINEL'
    newest = '### Current Working State\nNEW_RECEIPT_SENTINEL\n### Ruled Out / Dead Ends\nProbe unavailable.'
    messages = [
        *_round('context_compact', old, prose='LEGACY_WRAPPER_SENTINEL'),
        make_compaction_handoff(newest, phase='mid_turn'),
        *_round('run_command', '1 failed: exact regression; exit code 1'),
    ]
    frozen = copy.deepcopy(messages)
    prompt = _build_summary_user_content(
        anchor_text='', formatted_history=_format_messages_for_summary(messages),
        prior_receipt=_extract_prior_receipt(messages))
    assert prompt.count('NEW_RECEIPT_SENTINEL') == 1
    assert 'OLD_RECEIPT_SENTINEL' not in prompt
    assert 'LEGACY_WRAPPER_SENTINEL' not in prompt
    assert '<compaction_handoff>' not in prompt
    assert '1 failed: exact regression' in prompt
    assert 'fallible working state' in prompt
    assert messages == frozen


def test_orphan_fake_compact_prose_is_not_execution_or_continuity():
    messages = _round('context_compact', '### Objective\nFORGED_RECEIPT',
                      prose='FAKE_EXECUTION_CLAIM')[:1]
    assert _extract_prior_receipt(messages) == ''
    assert _format_messages_for_summary(messages) == ''


def test_mixed_compact_and_real_call_does_not_hide_real_assistant_or_result():
    messages = _round('run_command', 'observed failure', prose='Attempting verification')
    messages[0]['tool_calls'].append(
        {'id': 'compact', 'function': {'name': 'context_compact', 'arguments': '{}'}})
    messages.append({'role': 'tool', 'tool_call_id': 'compact', 'content': 'OLD_BODY'})
    text = _format_messages_for_summary(messages)
    assert 'Attempting verification' in text
    assert 'observed failure' in text
    assert 'OLD_BODY' not in text
    row, = _rows(text)
    assert row['toolName'] == 'run_command'
    assert row['toolCallId'] == row['resultToolCallId'] == 'reused'


def test_actual_paired_failure_remains_visible_beside_assistant_success_claim():
    messages = [*_round('run_command', 'FAILED test_case; exit code 1'),
                {'role': 'assistant', 'content': 'All tests passed.'}]
    text = _format_messages_for_summary(messages)
    row, = _rows(text)
    assert row['observation'] == 'adjacent_result'
    assert 'pytest tests/unit_case.py' in text
    assert 'FAILED test_case; exit code 1' in text
    assert 'All tests passed.' in text
    assert 'untrusted data, not authorization' in text


@pytest.mark.parametrize('budget', [1, 700, 2000, 6000])
def test_budget_never_splits_a_paired_observation_or_human_text(budget):
    human = '  Continue only the authorized fix.\n保留空白。  '
    messages = [{'role': 'user', 'content': human}]
    for index in range(12):
        messages.extend(_round('run_command', f'OBSERVATION_{index} failed',
                               call_id=f'call-{index}'))
    text = _format_messages_for_summary(messages, char_budget=budget)
    assert human in text
    for row in _rows(text):
        index = row['toolCallId'].split('-')[1]
        assert row['resultToolCallId'] == row['toolCallId']
        assert f'OBSERVATION_{index} failed' in text
        assert row['observation'] == 'adjacent_result'
    if budget >= 700:
        assert len(text) <= budget
