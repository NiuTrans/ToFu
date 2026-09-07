"""Lossless-continue executor resume authorities.

2026-08-31 root-cause fix: a turn interrupted after completed tool rounds
resumed with prose prefill only, so the model went blind to every tool fact
it had produced (the replayed history the user still saw on screen).  The
``continue`` branch must ship the retained checkpoint rounds alongside the
prefill; retention keeps pre-gap display carriers, replay stays the causal
prefix.
"""

from __future__ import annotations

import pytest

from lib.conversation_sync.command_service import (
    _journal_resume_file_fields,
    _resume_executor_config,
)
from lib.tool_round_replay import scan_replayable_tool_round_prefix

pytestmark = pytest.mark.unit
_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/foo.py'}


def _completed(call_id: str) -> dict:
    return {
        'toolCallId': call_id,
        'toolName': 'run_command',
        'toolArgs': {'command': 'pwd'},
        'toolContent': '/repo',
        'status': 'done',
    }


_DISPLAY_ROW = {
    # Program-shell/progress carrier: no toolCallId, never dispatched.
    'toolName': 'execute_tools',
    'toolArgs': {'calls': []},
    'status': 'done',
}

_IN_FLIGHT = {
    # Interrupted mid-dispatch: identity assigned, result never arrived.
    'toolCallId': 'call-tail',
    'toolName': 'run_command',
    'toolArgs': {'command': 'sleep 60'},
    'toolContent': None,
    'status': 'running',
}


def test_continue_replays_completed_rounds_and_display_carriers() -> None:
    config = _resume_executor_config('continue', {
        'content': 'partial answer',
        'images': [],
        'toolRounds': [dict(_DISPLAY_ROW), _completed('call-1'),
                       dict(_IN_FLIGHT)],
    })

    assert config['resumePrefill'] == 'partial answer'
    assert config['contentPrefix'] == 'partial answer'
    retained = config['checkpointToolRounds']
    # The result-less in-flight tail is amputated; the display carrier and
    # the completed execution receipt survive.
    assert [item.get('toolCallId') for item in retained] == [None, 'call-1']


def test_continue_without_tool_rounds_omits_checkpoint() -> None:
    config = _resume_executor_config('continue', {'content': 'tail'})

    assert 'checkpointToolRounds' not in config
    assert config['resumePrefill'] == 'tail'
    assert config['checkpointImages'] == []


def test_continue_seeds_thinking_prefix_only_when_prose_continues() -> None:
    config = _resume_executor_config('continue', {
        'content': 'partial answer',
        'thinking': 'interrupted reasoning',
    })
    assert config['thinkingPrefix'] == 'interrupted reasoning'

    # Empty prose lane: the write boundary already moved the thinking tail
    # into rolledBack — a seed would resurrect it inside the live lane.
    replay_only = _resume_executor_config('continue', {
        'content': '',
        'thinking': 'interrupted reasoning',
    })
    assert 'thinkingPrefix' not in replay_only


def test_prepare_and_apply_resume_state_hydrates_thinking_prefix() -> None:
    import threading

    from lib.tasks_pkg.orchestrator._resume_state import (
        apply_resume_state,
        prepare_resume_state,
    )

    task = {
        'id': 'task-1', 'convId': 'conv-1',
        'content': '', 'thinking': '',
        'content_lock': threading.Lock(),
    }
    prepared = prepare_resume_state({
        'contentPrefix': 'prose',
        'thinkingPrefix': 'reasoning',
    })
    apply_resume_state(
        task=task, cfg={}, messages=[], model='gpt-4o', tid='task-1',
        prepared_state=prepared)
    assert task['content'] == 'prose'
    assert task['thinking'] == 'reasoning'

    blank = prepare_resume_state({'contentPrefix': 'prose'})
    task['thinking'] = ''
    apply_resume_state(
        task=task, cfg={}, messages=[], model='gpt-4o', tid='task-1',
        prepared_state=blank)
    assert task['thinking'] == ''


def test_continue_with_only_an_in_flight_round_omits_checkpoint() -> None:
    config = _resume_executor_config('continue', {
        'content': '',
        'toolRounds': [dict(_IN_FLIGHT)],
    })

    assert 'checkpointToolRounds' not in config


def test_checkpoint_resume_passes_retained_projection_verbatim() -> None:
    rounds = [dict(_DISPLAY_ROW), _completed('call-1')]
    config = _resume_executor_config('checkpoint_resume', {
        'content': 'partial',
        'toolRounds': rounds,
    })

    assert config['checkpointToolRounds'] == rounds
    assert config['contentPrefix'] == 'partial'
    assert 'resumePrefill' not in config


def _awaiting_human(call_id: str, guidance_id: str) -> dict:
    return {
        'toolCallId': call_id,
        'toolName': 'ask_human',
        'toolArgs': {'question': 'Which scope?'},
        'toolContent': None,
        'status': 'awaiting_human',
        'guidanceId': guidance_id,
        'guidanceQuestion': 'Which scope?',
        'guidanceType': 'free_text',
    }


def test_answer_guidance_seeds_thinking_prefix_with_surviving_prose() -> None:
    config = _resume_executor_config('answer_guidance', {
        'content': 'partial answer',
        'thinking': 'interrupted reasoning',
        'images': [],
        'toolRounds': [_awaiting_human('call-ask', 'hg-1')],
    }, {
        'guidanceId': 'hg-1',
        'toolCallId': 'call-ask',
        'response': '仅服务商侧改造',
    })
    assert config['thinkingPrefix'] == 'interrupted reasoning'

    blank = _resume_executor_config('answer_guidance', {
        'content': '',
        'thinking': 'interrupted reasoning',
        'toolRounds': [_awaiting_human('call-ask', 'hg-1')],
    }, {
        'guidanceId': 'hg-1',
        'toolCallId': 'call-ask',
        'response': '继续',
    })
    assert 'thinkingPrefix' not in blank


def test_answer_guidance_completes_the_interrupted_question_round() -> None:
    config = _resume_executor_config('answer_guidance', {
        'content': 'partial answer',
        'images': [],
        'toolRounds': [dict(_DISPLAY_ROW), _completed('call-1'),
                       _awaiting_human('call-ask', 'hg-1')],
    }, {
        'guidanceId': 'hg-1',
        'toolCallId': 'call-ask',
        'response': '仅服务商侧改造',
    })

    assert config['resumePrefill'] == 'partial answer'
    assert config['contentPrefix'] == 'partial answer'
    rounds = config['checkpointToolRounds']
    assert [item.get('toolCallId') for item in rounds] == [
        None, 'call-1', 'call-ask',
    ]
    answered = rounds[-1]
    assert answered['status'] == 'done'
    assert answered['toolContent'] == 'Human response: 仅服务商侧改造'
    meta = answered['results'][0]
    assert meta['source'] == 'HumanGuidance'
    assert meta['badge'] == 'answered'
    assert meta['userResponse'] == '仅服务商侧改造'
    assert meta['guidanceId'] == 'hg-1'
    assert meta['responseType'] == 'free_text'
    # prepare_resume_state rejects any causal gap in checkpointToolRounds:
    # the synthesized list must scan clean end to end.
    prefix = scan_replayable_tool_round_prefix(rounds)
    assert prefix.blocked_position is None
    assert len(prefix.rounds) == 2


def test_answer_guidance_rejects_mismatched_or_missing_answer() -> None:
    projection = {'toolRounds': [_awaiting_human('call-ask', 'hg-1')]}
    with pytest.raises(ValueError):
        _resume_executor_config('answer_guidance', projection, None)
    with pytest.raises(ValueError):
        _resume_executor_config('answer_guidance', projection, {
            'guidanceId': 'hg-other', 'response': 'x',
        })
    with pytest.raises(ValueError):
        _resume_executor_config('answer_guidance', projection, {
            'guidanceId': 'hg-1', 'response': '',
        })
    # No interrupted question: a completed projection cannot be answered.
    with pytest.raises(ValueError):
        _resume_executor_config('answer_guidance', {
            'toolRounds': [_completed('call-1')],
        }, {'guidanceId': 'hg-1', 'response': 'x'})


def test_resume_ops_carry_the_settled_modified_file_ledger() -> None:
    files = [
        {'path': 'JOURNAL.md', 'action': 'patched'},
        {'path': 'lib/tool_round_replay.py', 'action': 'written', 'root': 'chatui'},
    ]
    projection = {
        'content': 'partial',
        'toolRounds': [_completed('call-1')],
        'modifiedFiles': 2,
        'modifiedFileList': files,
    }
    for operation in ('continue', 'checkpoint_resume'):
        config = _resume_executor_config(operation, projection)
        assert config['checkpointModifiedFileList'] == files
        assert config['checkpointModifiedFiles'] == 2

    answered = _resume_executor_config('answer_guidance', {
        'content': 'partial',
        'toolRounds': [_awaiting_human('call-ask', 'hg-1')],
        'modifiedFiles': 2,
        'modifiedFileList': files,
    }, {'guidanceId': 'hg-1', 'response': '继续'})
    assert answered['checkpointModifiedFileList'] == files
    assert answered['checkpointModifiedFiles'] == 2


def test_resume_file_ledger_count_defaults_to_list_length() -> None:
    config = _resume_executor_config('continue', {
        'content': 'x',
        'modifiedFileList': [{'path': 'a.py', 'action': 'patched'}],
    })

    assert config['checkpointModifiedFiles'] == 1


def test_resume_without_file_ledger_omits_checkpoint_keys() -> None:
    for projection in ({'content': 'x'},
                       {'content': 'x', 'modifiedFileList': []},
                       {'content': 'x',
                        'modifiedFileList': [{}, '', None]}):
        config = _resume_executor_config('continue', projection)
        assert 'checkpointModifiedFileList' not in config
        assert 'checkpointModifiedFiles' not in config


def test_carried_file_ledger_passes_resume_state_validation() -> None:
    from lib.tasks_pkg.orchestrator._resume_state import (
        prepare_resume_state)

    files = [{'path': 'JOURNAL.md', 'action': 'patched'}]
    config = _resume_executor_config('continue', {
        'content': 'partial',
        'modifiedFiles': 1,
        'modifiedFileList': files,
    })

    prepared = prepare_resume_state(config)
    assert list(prepared.checkpoint_modified_file_list) == files
    assert prepared.checkpoint_modified_files == 1


def test_journal_fallback_rebuilds_the_ledger_after_restart(
        monkeypatch) -> None:
    import lib.tasks_pkg.commit_round._derive as derive_mod
    captured = {}

    def fake_derive(task, project_path, project_paths):
        captured['task'] = task
        captured['project_path'] = project_path
        captured['project_paths'] = project_paths
        return ([{'path': 'lib/foo.py', 'action': 'patched'}], 1, True)

    monkeypatch.setattr(
        derive_mod, 'derive_round_modified_files', fake_derive)
    turn = {'createdAt': 1788137746471, 'conversationId': 'conv-1'}
    config = {'projectPath': '/repo', 'projectPaths': ['/repo', '/extra']}

    out = _journal_resume_file_fields(turn, config)

    assert out == {
        'checkpointModifiedFileList': [
            {'path': 'lib/foo.py', 'action': 'patched'}],
        'checkpointModifiedFiles': 1,
    }
    # Empty task id: no taskId-stamped row matches, so the conv-scoped
    # timestamp scan covers every attempt of the turn in one pass.
    assert captured['task']['id'] == ''
    assert captured['task']['convId'] == 'conv-1'
    # Turn timestamps are epoch ms; journal rows are time.time() seconds.
    assert captured['task']['created_at'] == pytest.approx(1788137746.471)
    assert captured['project_path'] == '/repo'
    assert captured['project_paths'] == ['/repo', '/extra']


def test_journal_fallback_is_silent_without_floor_rows_or_project(
        monkeypatch) -> None:
    import lib.tasks_pkg.commit_round._derive as derive_mod
    monkeypatch.setattr(
        derive_mod, 'derive_round_modified_files',
        lambda task, project_path, project_paths: ([], 0, False))
    turn = {'createdAt': 1788137746471, 'conversationId': 'conv-1'}

    assert _journal_resume_file_fields(turn, {'projectPath': '/repo'}) == {}
    assert _journal_resume_file_fields(
        {'conversationId': 'conv-1'}, {'projectPath': '/repo'}) == {}
    assert _journal_resume_file_fields(turn, {}) == {}

def test_other_operations_get_no_resume_authorities() -> None:
    assert _resume_executor_config('regenerate', {
        'content': 'x',
        'toolRounds': [_completed('call-1')],
    }) == {}
