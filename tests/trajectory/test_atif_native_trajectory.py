"""ATIF must reflect executed native tool receipts, not display-card summaries."""
from copy import deepcopy

import pytest

from lib.trajectory import flatten

pytestmark = pytest.mark.unit


def _task():
    return {
        'id': 'native', 'status': 'done', 'finishReason': 'stop',
        'messages': [{'role': 'user', 'content': 'fix the file'}],
        'content': 'final answer', 'usage': {'prompt_tokens': 90, 'completion_tokens': 8},
        'toolRounds': [{
            'toolCallId': 'c1', 'toolName': 'read_files',
            'toolArgs': {'path': 'a.py'}, 'toolContent': 'actual model receipt',
            'status': 'done', 'taskId': 'native', 'llmRound': 0, 'roundNum': 1,
            'assistantContent': 'inspect first', 'thinking': 'reasoning',
            'results': [{'title': 'display summary; not model output'}],
        }],
    }


def test_native_rounds_export_call_and_actual_result():
    task = _task()
    before = deepcopy(task)
    doc = flatten(task, 'atif')['trajectory']
    assert [s['source'] for s in doc['steps']] == ['user', 'agent', 'agent']
    call = doc['steps'][1]
    assert call['tool_calls'] == [{
        'tool_call_id': 'c1', 'function_name': 'read_files', 'arguments': {'path': 'a.py'},
    }]
    assert call['message'] == 'inspect first'
    assert call['reasoning_content'] == 'reasoning'
    assert call['observation']['results'] == [
        {'source_call_id': 'c1', 'content': 'actual model receipt'},
    ]
    assert doc['final_metrics']['total_prompt_tokens'] == 90
    assert doc['final_metrics']['total_completion_tokens'] == 8
    assert task == before


def _call(call_id='c1'):
    return {'role': 'assistant', 'content': 'read', 'tool_calls': [{
        'id': call_id, 'function': {'name': 'read_files', 'arguments': '{"path":"a.py"}'},
    }]}


def test_full_working_transcript_is_not_duplicated_or_mapped_to_user():
    task = _task()
    task['messages'] += [
        _call(), {'role': 'tool', 'tool_call_id': 'c1', 'content': 'wire receipt'},
        {'role': 'user', '_isMeta': True, 'content': 'continue from receipts'},
        {'role': 'assistant', 'content': 'real final', 'reasoning_content': 'final reasoning'},
    ]
    task['content'] = 'read\nreal final'  # accumulated UI narration, not a new response
    steps = flatten(task, 'atif')['trajectory']['steps']
    assert [s['source'] for s in steps] == ['user', 'agent', 'user', 'agent']
    assert sum(len(s.get('tool_calls', [])) for s in steps) == 1
    assert steps[1]['observation']['results'][0]['content'] == 'wire receipt'
    assert steps[-1]['message'] == 'real final'
    assert steps[-1]['reasoning_content'] == 'final reasoning'


@pytest.mark.parametrize('status', ['done', 'error', 'rejected', 'aborted'])
def test_exact_receipt_survives_tool_verdict(status):
    task = _task()
    task['toolRounds'][0]['status'] = status
    task['toolRounds'][0]['toolContent'] = ''
    steps = flatten(task, 'atif')['trajectory']['steps']
    assert steps[1]['observation']['results'][0]['content'] == ''


def test_causal_gap_does_not_fabricate_later_successful_rounds():
    task = _task()
    missing = dict(task['toolRounds'][0], toolCallId='gap', toolContent=None)
    later = dict(task['toolRounds'][0], toolCallId='later')
    task['toolRounds'] += [missing, later]
    steps = flatten(task, 'atif')['trajectory']['steps']
    ids = [c['tool_call_id'] for s in steps for c in s.get('tool_calls', [])]
    assert ids == ['c1']


def test_equal_call_ids_in_separate_occurrences_pair_fifo():
    task = _task()
    task['messages'] += [
        _call(), {'role': 'tool', 'tool_call_id': 'c1', 'content': 'first'},
        _call(), {'role': 'tool', 'tool_call_id': 'c1', 'content': 'second'},
    ]
    steps = flatten(task, 'atif')['trajectory']['steps']
    agents = [s for s in steps if s.get('tool_calls')]
    assert len(agents) == 2
    assert [s['observation']['results'][0]['content'] for s in agents] == ['first', 'second']
    assert steps[-1]['message'] == 'final answer'


def test_orphan_receipt_stays_environment_evidence_not_user_message():
    task = _task()
    task['messages'].append({'role': 'tool', 'tool_call_id': 'missing', 'content': 'orphan'})
    steps = flatten(task, 'atif')['trajectory']['steps']
    orphan = steps[1]
    assert orphan['source'] == 'agent'
    assert orphan['extra'] == {'original_role': 'tool', 'orphan_call_id': 'missing'}
    assert orphan['observation']['results'] == [{'content': 'orphan'}]


@pytest.mark.parametrize('call_id', ['historical', 'c1'])
def test_input_history_tools_do_not_hide_current_native_receipt(call_id):
    task = _task()
    task['messages'] = [
        {'role': 'user', 'content': 'earlier task'},
        _call(call_id), {'role': 'tool', 'tool_call_id': call_id, 'content': 'old receipt'},
        {'role': 'assistant', 'content': 'earlier answer'},
        *task['messages'],
    ]
    steps = flatten(task, 'atif')['trajectory']['steps']
    observations = [r['content'] for s in steps for r in s.get('observation', {}).get('results', [])]
    assert observations == ['old receipt', 'actual model receipt']
    assert sum(len(s.get('tool_calls', [])) for s in steps) == 2


def test_explicit_working_transcript_provenance_does_not_replay_compacted_tools():
    task = _task()
    task['_messages_include_current_run'] = True
    task['messages'] += [{'role': 'assistant', 'content': 'real final after compaction'}]
    steps = flatten(task, 'atif')['trajectory']['steps']
    assert not any(s.get('tool_calls') for s in steps)
    assert steps[-1]['message'] == 'real final after compaction'


@pytest.mark.parametrize('flow', [False, True])
def test_new_run_marks_input_history_without_replacing_retry_snapshot(flow):
    from lib.tasks_pkg.orchestrator._task_open import snapshot_turn_input
    task = _task()
    task.update(_flow_managed=flow, _messages_include_current_run=True)
    pristine = [{'role': 'user', 'content': 'original'}]
    task['_turn_input_messages'] = pristine
    snapshot_turn_input(task)
    assert task['_messages_include_current_run'] is False
    assert task['_turn_input_messages'] is pristine


def test_parallel_native_calls_preserve_batch_and_outcomes():
    task = _task()
    task['toolRounds'].append(dict(task['toolRounds'][0], toolCallId='c2', roundNum=2,
                                   toolContent='other receipt'))
    steps = flatten(task, 'atif')['trajectory']['steps']
    assert len(steps[1]['tool_calls']) == 2
    assert [r['source_call_id'] for r in steps[1]['observation']['results']] == ['c1', 'c2']
