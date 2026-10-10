"""Delegated execution receipts must not become invented provider actions."""

from copy import deepcopy
import json

import pytest

from lib.tool_round_replay import scan_replayable_tool_round_prefix, checkpoint_retention_positions
from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages

pytestmark = pytest.mark.unit


def _rounds(source='execute_calls'):
    scope = {'attemptId': 'attempt-one', 'taskId': 'task-one', 'llmRound': 1}
    parent = {**scope, 'toolName': 'execute_tools', 'toolCallId': 'gateway-one',
              '_hiddenToolAdapter': True, 'roundNum': 2, 'status': 'done',
              'toolArgs': json.dumps({'calls': [{'name': 'run_command',
                          'call_id': '1', 'arguments': {'command': 'synthetic'}}]}),
              'toolContent': json.dumps({'results': [{'call_id': '1',
                             'resultId': 'result-one', 'output': 'actual result'}]})}
    child = {**scope, 'source': source, 'toolName': 'code_exec',
             'toolCallId': '1', 'roundNum': 8700001, 'status': 'done',
             'toolArgs': '{"command":"synthetic"}', 'toolContent': 'actual result',
             'toolResultEvidence': {'resultId': 'result-one'}}
    return [parent, child]


@pytest.mark.parametrize('source', ['execute_calls', 'execute_program'])
def test_covered_child_receipt_is_not_replayed_as_another_assistant_call(source):
    rounds = _rounds(source)
    original = deepcopy(rounds)
    prefix = scan_replayable_tool_round_prefix(rounds)
    assert prefix.blocked_position is None
    assert prefix.positions == (0,)
    assert prefix.ignored_count == 1
    assert prefix.delegated_receipt_count == 1
    assert checkpoint_retention_positions(rounds, prefix)[1] == [0, 1]
    wire = _reconstruct_tool_call_messages(rounds)
    assert [call['function']['name'] for message in wire
            for call in message.get('tool_calls', [])] == ['execute_tools']
    assert len(wire) == 2 and 'actual result' in wire[1]['content']
    assert rounds == original


@pytest.mark.parametrize('change', ['attempt', 'task', 'llm_round', 'result_id',
                                   'native_call', 'missing_scope', 'no_parent',
                                   'parent_not_container', 'compacted_parent',
                                   'malformed_source'])
def test_independent_or_unproven_receipts_are_never_deduplicated(change):
    rounds = _rounds()
    child = rounds[1]
    if change == 'attempt': child['attemptId'] = 'another-attempt'
    if change == 'task': child['taskId'] = 'another-task'
    if change == 'llm_round': child['llmRound'] = 2
    if change == 'result_id': child['toolResultEvidence']['resultId'] = 'another-result'
    if change == 'native_call': child['source'] = 'native_direct'
    if change == 'missing_scope':
        for row in rounds:
            row.pop('taskId'); row.pop('attemptId')
    if change == 'no_parent': rounds = [child]
    if change == 'parent_not_container': rounds[0].pop('_hiddenToolAdapter')
    if change == 'compacted_parent': rounds[0]['toolContent'] = '{"artifactRef":"retained"}'
    if change == 'malformed_source': child['source'] = {'unknown': True}
    assert len(scan_replayable_tool_round_prefix(rounds).rounds) == len(rounds)


def test_parent_receipt_covers_incomplete_child_display_without_a_causal_gap():
    rounds = _rounds()
    rounds[1].pop('toolContent')
    prefix = scan_replayable_tool_round_prefix(rounds)
    assert prefix.positions == (0,) and prefix.blocked_position is None
    rounds[0].pop('toolContent')
    assert scan_replayable_tool_round_prefix(rounds).blocked_position == 0


def test_continue_retains_child_audit_but_sends_only_the_provider_container():
    from lib.conversation_sync.command_service import _resume_executor_config
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state
    config = _resume_executor_config('continue', {
        'content': 'partial response', 'toolRounds': _rounds()})
    assert len(config['checkpointToolRounds']) == 2
    prepared = prepare_resume_state(config)
    assert len(prepared.checkpoint_tool_rounds) == 2
    assert len(prepared.checkpoint_messages) == 2
    calls = prepared.checkpoint_messages[0]['tool_calls']
    assert len(calls) == 1 and calls[0]['function']['name'] == 'execute_tools'
