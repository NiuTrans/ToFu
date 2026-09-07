"""Bounded terminal decisions preserve prior operations, independent of rounds."""

from copy import deepcopy

import pytest

from lib.tasks_pkg.llm_fallback._retry import _flag_empty_stop_for_retry
from lib.tasks_pkg.stream_handler import _terminal_recovery as recovery

pytestmark = pytest.mark.unit


@pytest.fixture
def phases(monkeypatch):
    events = []
    monkeypatch.setattr(recovery, 'emit_phase', lambda *args, **kw: events.append(kw))
    return events


def decide(task, *, content='', finish='stop', calls=None, usage=None, round_num=4):
    return recovery.recover_abnormal_terminal(
        {'content': content, 'tool_calls': calls}, finish, task,
        round_num=round_num, model='test-model', usage=usage,
        result={'action': 'proceed', 'last_finish_reason': finish,
                'premature_retry_count': 0, 'abort_detected_phase': None},
    )


def test_two_recoveries_per_task_across_rounds_preserve_operations(phases):
    task = {'content': 'Earlier narration', 'thinking': 'Earlier reasoning',
            'tool_rounds': [{'tool': 'write_file', 'toolContent': 'written'}]}
    preserved = deepcopy(task)
    assert decide(task, round_num=0)['action'] == 'continue'
    # A valid tool round must neither consume nor reset the task-wide budget.
    assert decide(task, calls=[{'id': 'call-2'}], finish='tool_calls') is None
    assert decide(task, content='Interim answer') is None
    task['_premature_retry_count_phase'] = 0
    assert decide(task, finish='tool_calls', round_num=8)['action'] == 'continue'
    result = decide(task, round_num=12)
    assert result['action'] == 'break'
    assert result['last_finish_reason'] == 'abnormal_stop'
    assert task['error']['kind'] == 'abnormal_stop'
    assert 'exhausted' in task['error']['detail']
    assert task['_abnormal_terminal_recovery_count'] == 2
    assert task['_suppress_whole_turn_retry_to_preserve_partial'] is True
    for key, value in preserved.items():
        assert task[key] == value
    assert [event['attempt'] for event in phases] == [1, 2]
    assert all(event['max'] == 2 for event in phases)


def test_analyser_enforces_task_wide_cap_after_tools(phases):
    from lib.tasks_pkg.stream_handler._analyse import analyse_stream_result

    task = {'id': 'terminal-test', 'aborted': False, 'content': 'Prior work',
            'toolRounds': [{'tool': 'write_file', 'toolContent': 'written'}]}
    messages = [{'role': 'tool', 'tool_call_id': 'prior-write', 'content': 'written'}]
    before = deepcopy(messages)
    for round_num, expected in [(3, 'continue'), (7, 'continue'), (9, 'break')]:
        result = analyse_stream_result(
            {'content': '\n'}, 'stop', task, 'terminal-test', 'test-model',
            round_num, 0, messages, usage={},
        )
        assert result['action'] == expected
        assert messages == before
        assert task['toolRounds'][0]['toolContent'] == 'written'
    assert result['last_finish_reason'] == 'abnormal_stop'
    assert task['error']['kind'] == 'abnormal_stop'


@pytest.mark.parametrize('finish', ['error', 'aborted', 'cancelled', 'length', 'content_filter'])
def test_other_terminal_causes_have_precedence(finish, phases):
    task = {}
    assert decide(task, finish=finish) is None
    assert task == {}
    assert not phases


@pytest.mark.parametrize('task', [{'aborted': True}, {'error': {'kind': 'provider_error'}}])
def test_cancellation_and_existing_failure_do_not_recover(task, phases):
    before = deepcopy(task)
    assert decide(task) is None
    assert task == before
    assert not phases


@pytest.mark.parametrize('task', [
    {'pendingGuidanceId': 'guidance-1'},
    {'_pendingHumanGuidance': True},
    {'toolRounds': [{'status': 'pending_approval', 'tool': 'write_file'}]},
])
def test_pending_handoff_does_not_consume_recovery(task, phases):
    before = deepcopy(task)
    assert decide(task) is None
    assert decide(task, content='<packet>',
                  usage={'_unresolved_tool_marker': True}) is None
    assert task == before
    assert not phases


def test_explicit_missing_path_scope_cannot_borrow_global_roots(tmp_path, monkeypatch):
    from lib.project_mod import abs_path_guard as guard
    from lib.project_mod import config
    monkeypatch.setattr(config, '_roots', {'other': {'path': str(tmp_path)}})
    token = guard.set_restricted(True)
    try:
        for identity in (None, ''):
            with guard.registered_root_scope(identity):
                with pytest.raises(guard.AbsPathDenied):
                    guard.enforce_abs_read(str(tmp_path / 'secret.txt'))
                with pytest.raises(guard.AbsPathDenied):
                    guard.enforce_abs_write(str(tmp_path / 'secret.txt'))
    finally:
        guard.reset_restricted(token)


def test_unresolved_marker_requires_explicit_normalizer_evidence(phases):
    assert decide({}, content='Text mentioning a tool marker') is None
    assert decide({}, content='<unresolved-control>',
                  usage={'_unresolved_tool_marker': True})['action'] == 'continue'
    assert phases[-1]['bucket'] == 'unresolved_tool_marker'


@pytest.mark.parametrize('round_num', [0, 1, 12])
def test_flag_blank_current_round_despite_prior_prose(round_num):
    usage = {}
    task = {'content': 'Prior progress', 'thinking': 'Prior reasoning'}
    assert _flag_empty_stop_for_retry({'content': ' \n'}, 'stop', task, round_num, usage)
    assert usage['_empty_stop'] and usage['_stream_anomaly']


@pytest.mark.parametrize('message,task', [
    ({'content': '', 'tool_calls': [{'id': 'executed-once'}]}, {}),
    ({'content': ''}, {'aborted': True}),
    ({'content': 'Answer'}, {}),
])
def test_flag_excludes_actual_tool_rounds_cancellation_and_answers(message, task):
    usage = {}
    assert not _flag_empty_stop_for_retry(message, 'stop', task, 3, usage)
    assert usage == {}


def test_protocol_reissue_preserves_receipts_and_records_note(phases):
    task = {'config': {'harnessProfile': 'longcat_flash'}}
    messages = [{'role': 'tool', 'tool_call_id': 'prior-write', 'content': 'written'}]
    receipt = deepcopy(messages[0])
    result = recovery.recover_abnormal_terminal(
        {'content': '<longcat_tool_call>run_command</longcat_tool_call>'},
        'stop', task, round_num=4, model='ckpt_00000588',
        result={'action': 'proceed', 'premature_retry_count': 0},
        messages=messages)
    assert result['action'] == 'continue'
    assert messages[0] == receipt
    assert len(messages) == 2
    assert 'native tool schema' in messages[-1]['content']
    assert task['_injected_notes'][-1]['text'] == messages[-1]['content']
    assert task['_abnormal_terminal_recovery_count'] == 1
