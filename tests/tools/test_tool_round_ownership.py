"""Execution identity belongs to the producer, not the displaying task/Turn."""
from copy import deepcopy

import pytest

from lib.tool_results.tool_round_identity import tool_round_with_execution_identity

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('overwrite', [False, True])
def test_child_identity_never_inherits_the_parent_attempt(overwrite):
    row = {'taskId': 'child', 'toolCallId': 'one', 'llmRound': 0}
    stamped = tool_round_with_execution_identity(
        row, task_id='parent', attempt_id='visible-phase', overwrite=overwrite)
    assert stamped == row
    assert stamped is not row


def test_root_identity_fills_missing_scope_without_changing_input():
    row = {'taskId': 'root', 'toolCallId': 'one'}
    assert tool_round_with_execution_identity(
        row, task_id='root', attempt_id='attempt', overwrite=True
    ) == {**row, 'attemptId': 'attempt'}
    assert 'attemptId' not in row


def test_historical_attempt_without_task_never_borrows_a_successor_task():
    row = {'attemptId': 'previous', 'toolCallId': 'old'}
    assert tool_round_with_execution_identity(
        row, task_id='successor', attempt_id='next') == row


def test_root_and_child_tool_announcements_stamp_the_producer():
    from lib.tasks_pkg.tool_display._dispatch import _build_tool_round_entry
    for owner in ('root', 'child'):
        _, row, event = _build_tool_round_entry(
            'read_files', {'path': 'synthetic.txt'}, 'one', '{}', 0, False,
            task={'id': owner, '_userId': 1})
        assert row['taskId'] == event['taskId'] == owner


def test_resume_backfill_keeps_child_segments_and_rounds_in_one_scope():
    from lib.tool_results.tool_round_identity import projection_history_with_execution_identity
    projection = {'toolRounds': [{'taskId': 'child', 'toolCallId': 'one'}],
                  'segments': [
                      {'type': 'text', 'taskId': 'child', 'llmRound': 0, 'text': 'child narration'},
                      {'type': 'tool_use', 'taskId': 'child', 'id': 'one',
                       '_round': {'taskId': 'child', 'toolCallId': 'one'}},
                      {'type': 'text', 'text': 'answer', 'terminal': True},
                  ]}
    original = deepcopy(projection)
    assert projection_history_with_execution_identity(
        projection, task_id='parent', attempt_id='display-attempt') == original
    assert projection == original


def test_checkpoint_and_live_projection_keep_delegated_occurrences_distinct():
    from lib.tasks_pkg.manager._persist import _merge_tool_rounds
    from lib.turn_lifecycle import _task_projection
    rows = [
        {'taskId': 'child-a', 'toolCallId': 'recycled', 'llmRound': 0, 'roundNum': 1,
         'toolName': 'read_files', 'toolContent': 'first', 'status': 'done'},
        {'taskId': 'child-b', 'toolCallId': 'recycled', 'llmRound': 0, 'roundNum': 1,
         'toolName': 'read_files', 'toolContent': 'second', 'status': 'done'},
    ]
    task = {'id': 'parent', '_attemptId': 'visible-phase', 'toolRounds': rows,
            'content': 'final', 'thinking': '', 'config': {}}
    original = deepcopy(task)
    for actual in (_merge_tool_rounds(task), _task_projection(task, {})['toolRounds']):
        assert [row['taskId'] for row in actual] == ['child-a', 'child-b']
        assert all('attemptId' not in row for row in actual)
        assert [row['toolContent'] for row in actual] == ['first', 'second']
    assert task == original
