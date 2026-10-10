"""A Continue is one logical Turn; checkpoint replay is not tool execution."""
from copy import deepcopy
import threading

import pytest

from lib.agent_loop import AbortSignal, run_agent_loop
from lib.conversation_sync.command_service import _resume_executor_config
from lib.tasks_pkg.continuation_rounds import continuation_round_counters
from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state, prepare_resume_state
from lib.tasks_pkg.orchestrator._round_state import RoundState

pytestmark = pytest.mark.unit


def receipt(n, lr, attempt='old', **extra):
    return {'roundNum': n, 'llmRound': lr, 'attemptId': attempt,
            'taskId': 'task-' + attempt, 'toolCallId': f'{attempt}-{n}',
            'toolName': 'run_command', 'toolArgs': {'command': 'pwd'},
            'toolContent': '/repo', 'status': 'done', **extra}


@pytest.mark.parametrize('rounds,expected', [
    ([], (0, 0)),
    ([receipt(1, 0), receipt(2, 0), receipt(3, 1)], (2, 3)),
    ([receipt(1, 0), receipt(2, 4), receipt(3, 5)], (6, 3)),
    ([receipt(1, 0), receipt(2, 1), receipt(1, 0, 'new'),
      receipt(2, 1, 'new')], (4, 4)),
    ([receipt(1, 0), receipt(2, 1), receipt(3, 2, 'new')], (3, 3)),
    ([receipt(1, 0), receipt(8_700_000, 0, source='execute_program'),
      receipt(2, 1)], (2, 2)),
    ([{**receipt(1, 0), 'attemptId': '', 'taskId': ''},
      {**receipt(2, 1), 'attemptId': '', 'taskId': ''},
      {**receipt(1, 0, 'new'), 'attemptId': '', 'taskId': ''}], (3, 3)),
])
def test_frontier_folds_chronology_without_rewriting_receipts(rounds, expected):
    before = deepcopy(rounds)
    assert continuation_round_counters(rounds) == expected
    assert rounds == before


def test_thirty_retained_receipts_seed_next_root_dispatch_without_reexecution():
    rounds = [receipt(i + 1, i // 2) for i in range(30)]
    original = deepcopy(rounds)
    cfg = _resume_executor_config('continue', {'toolRounds': rounds})
    task = {'id': 'new-task', 'content_lock': threading.Lock()}
    messages = [{'role': 'system', 'content': 'system'}]
    state = RoundState(model='gpt-6.1-sol', preset='default', thinking_enabled=False)
    apply_resume_state(task=task, cfg=cfg, messages=messages,
                       model=state.model, tid='new-task', round_state=state)
    assert state.model_round_start == 15
    assert state.tool_round_num == 30
    assert state.tool_call_happened
    assert len([m for m in messages if m['role'] == 'tool']) == 30
    assert [m['tool_call_id'] for m in messages if m['role'] == 'tool'] == [
        r['toolCallId'] for r in rounds]
    dispatched = []
    out = run_agent_loop(abort=AbortSignal.never(), round_tools=[],
        initial_round=state.model_round_start,
        dispatch=lambda n, _: dispatched.append(n) or ({'content': 'done'}, 'stop', {}),
        execute_tool=lambda *args: pytest.fail('replayed receipt executed'))
    assert dispatched == [15] and out.rounds == 1 and out.completed
    assert task['_checkpointToolRounds'] == original and rounds == original


@pytest.mark.parametrize('operation', ['continue', 'checkpoint_resume'])
def test_progress_and_paid_requests_are_validated_detached_and_preserved(operation):
    projection = {
        'toolRounds': [receipt(1, 0)],
        'todoState': {'version': 3, 'todos': [
            {'content': 'retain results', 'status': 'completed'},
            {'content': 'finish', 'status': 'in_progress'}],
            'explanation': 'continuing', 'update_count': 2},
        'usage': {'prompt_tokens': 20, 'completion_tokens': 4},
        'apiRounds': [{'round': 1, 'usage': {'prompt_tokens': 20}},
                      {'round': 3, 'usage': {'completion_tokens': 4}}],
    }
    cfg = _resume_executor_config(operation, projection)
    prepared = prepare_resume_state(cfg)
    projection['usage']['prompt_tokens'] = 999
    projection['todoState']['todos'][0]['content'] = 'mutated'
    task = {'id': 'new', 'content_lock': threading.Lock()}
    state = RoundState(model='gpt-6.1-sol', preset='default', thinking_enabled=False)
    apply_resume_state(task=task, cfg=cfg, messages=[], model=state.model,
                       tid='new', prepared_state=prepared, round_state=state)
    assert state.model_round_start == 3
    assert task['_checkpointUsage']['prompt_tokens'] == 20
    assert [r['round'] for r in task['_checkpointApiRounds']] == [1, 3]
    assert task['_todos'][0]['content'] == 'retain results'
    assert task['_todos'][1]['status'] == 'in_progress'


@pytest.mark.parametrize('initial', [-1, True, 1.5])
def test_invalid_frontier_refused_before_dispatch(initial):
    with pytest.raises(ValueError, match='initial_round'):
        run_agent_loop(abort=AbortSignal.never(), round_tools=[], initial_round=initial,
            dispatch=lambda *args: pytest.fail('invalid checkpoint dispatched'))


def test_frontier_does_not_create_another_loop_checkpoint_authority():
    with pytest.raises(ValueError, match='already owns'):
        run_agent_loop(abort=AbortSignal.never(), round_tools=[], initial_round=4,
            resume_checkpoint={}, dispatch=lambda *args: pytest.fail('dispatch'))
