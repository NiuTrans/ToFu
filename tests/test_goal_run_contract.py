"""Pure GoalRun policy, objective and terminal-classification contract."""

import pytest

from lib.goal_runs.contract import (
    DEFAULT_GOAL_MAX_ITERATIONS,
    GOAL_POLICY_DIRECTIVE,
    GOAL_RUN_FORMAT,
    MAX_GOAL_MAX_ITERATIONS,
    goal_iteration_budget,
    goal_run_contract,
    is_valid_goal_transition,
    terminal_goal_transition,
)
from lib.goal_runs.objective import objective_from_task


pytestmark = pytest.mark.unit


def test_goal_run_contract_declares_one_long_horizon_flow_owner():
    contract = goal_run_contract()

    assert contract['format'] == GOAL_RUN_FORMAT
    assert contract['executionOwner'] == 'flow_executor'
    assert contract['objectiveSource'] == 'accepted_human_turn'
    assert contract['initialStatus'] == 'active'
    assert contract['terminalStatuses'] == [
        'completed', 'blocked', 'failed', 'cancelled']
    assert contract['policy'] == {
        'solutionHorizon': 'long_term',
        'rootCauseRequired': True,
        'verificationEvidenceRequired': True,
        'temporaryPatchPolicy': 'reject_when_robust_solution_is_in_scope',
        'iterationBudget': {
            'default': DEFAULT_GOAL_MAX_ITERATIONS,
            'hardCeiling': MAX_GOAL_MAX_ITERATIONS,
        },
        'directive': GOAL_POLICY_DIRECTIVE,
    }

    assert goal_iteration_budget() == 40
    assert goal_iteration_budget(10_000) == 64
    assert is_valid_goal_transition('completed', 'objective_verified') is True
    assert is_valid_goal_transition('completed', 'runtime_failure') is False


def test_objective_is_latest_accepted_human_not_first_or_virtual_user():
    task = {'messages': [
        {'role': 'user', 'content': 'obsolete first request'},
        {'role': 'assistant', 'content': 'old response'},
        {
            'role': 'user', 'content': 'synthetic follow-up',
            '_isVirtualUser': True,
        },
        {'role': 'user', 'content': 'current accepted objective'},
        {
            'role': 'user', 'content': 'flow review',
            '_isFlowReview': True,
        },
    ]}

    assert objective_from_task(task) == 'current accepted objective'


def test_server_stamped_objective_wins_over_rendered_history():
    assert objective_from_task({
        'config': {'_goalObjective': 'exact accepted input'},
        'messages': [{'role': 'user', 'content': 'rewritten model context'}],
    }) == 'exact accepted input'


@pytest.mark.parametrize(
    ('category', 'stop', 'abort', 'expected'),
    [
        ('success', 'completed', '', ('completed', 'objective_verified')),
        ('incomplete', 'max_iterations', '',
         ('blocked', 'iteration_budget_exhausted')),
        ('incomplete', 'task_budget_exceeded', '',
         ('blocked', 'execution_budget_exhausted')),
        ('incomplete', 'stuck', '', ('blocked', 'no_verified_progress')),
        ('aborted', 'aborted', 'api_chat_abort',
         ('cancelled', 'human_stop')),
        ('aborted', 'aborted', 'superseded_by_new_task',
         ('cancelled', 'superseded_by_human')),
        ('aborted', 'aborted', 'worker_lost', ('failed', 'worker_lost')),
        ('aborted', 'aborted', 'runtime_shutdown',
         ('cancelled', 'runtime_shutdown')),
        ('failure', 'worker_lost', '', ('failed', 'worker_lost')),
        ('failure', 'node_failed', '', ('failed', 'runtime_failure')),
    ],
)
def test_terminal_mapping_is_typed(category, stop, abort, expected):
    assert terminal_goal_transition(
        category, stop_reason=stop, abort_reason=abort) == expected


def test_canonical_autopilot_graph_executes_the_published_policy():
    from lib.orchestration._builtin_definitions import build_autopilot_definition

    definition = build_autopilot_definition()
    loop = next(node for node in definition['nodes']
                if node.get('kind') == 'loop')
    assert loop['params']['max_iterations'] == DEFAULT_GOAL_MAX_ITERATIONS
    objectives = {
        node['role']: node['params']['objective']
        for node in definition['nodes'] if node.get('type') == 'role'
    }
    assert GOAL_POLICY_DIRECTIVE in objectives['worker']
    assert GOAL_POLICY_DIRECTIVE in objectives['virtual_user']
