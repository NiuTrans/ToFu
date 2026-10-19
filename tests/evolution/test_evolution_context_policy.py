"""Schema cost policy preserves execution authority and survives coordination."""
from copy import deepcopy

import pytest

from tests.evolution.test_evolution_runs import OWNER, finish, runtime as runtime

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('budget', [0, 4000, 128000])
def test_schema_policy_is_durable_executor_only_and_idempotent(runtime, budget):
    runs, _, tasks = runtime
    payload = {'objective': 'Repair the project.', 'executor_schema_budget_tokens': budget}
    run = runs.submit_run(payload, user_id=OWNER, idempotency_key='schema-policy')
    assert run['executor_schema_budget_tokens'] == budget
    assert tasks[0]['config']['tools']['schemaBudgetTokens'] == budget
    repeated = runs.submit_run(payload, user_id=OWNER, idempotency_key='schema-policy')
    assert repeated['run_id'] == run['run_id'] and len(tasks) == 1
    with pytest.raises(runs.EvolutionStateError):
        runs.submit_run({**payload, 'executor_schema_budget_tokens': 1},
                        user_id=OWNER, idempotency_key='schema-policy')
    with pytest.raises(runs.EvolutionStateError):
        runs.submit_run({'objective': payload['objective']},
                        user_id=OWNER, idempotency_key='schema-policy')
    reviewed = finish(runtime, tasks[0], 'Implementation and tests delivered.')
    assert reviewed['executor_schema_budget_tokens'] == budget
    assert 'schemaBudgetTokens' not in tasks[1]['config'].get('tools', {})


def test_omitted_schema_policy_keeps_deployment_defaults(runtime):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Keep ordinary defaults.'}, user_id=OWNER)
    assert 'executor_schema_budget_tokens' not in run
    assert 'schemaBudgetTokens' not in tasks[0]['config'].get('tools', {})


@pytest.mark.parametrize('value', [-1, 128001, True, 3.5, '4000', None])
def test_invalid_schema_policy_is_rejected_before_effects(runtime, value):
    runs, store, tasks = runtime
    before = store.writes
    with pytest.raises(runs.EvolutionContractError):
        runs.submit_run({'objective': 'Code.', 'executor_schema_budget_tokens': value}, user_id=OWNER)
    assert store.writes == before and tasks == []


def test_unsupported_research_policy_is_rejected_before_effects(runtime):
    runs, store, tasks = runtime
    before = store.writes
    with pytest.raises(runs.EvolutionContractError, match='executor_schema_budget_tokens'):
        runs.submit_run({'objective': 'Research.', 'domain': 'research',
                         'executor_schema_budget_tokens': 4000}, user_id=OWNER)
    assert store.writes == before and tasks == []


def test_workers_inherit_only_schema_cost_policy_and_can_explicitly_disable_it():
    from lib.tasks_pkg.child_invocation import inherit_child_config
    parent = {'tools': {'schemaBudgetTokens': 4000, 'executionScope': 'forged'},
              'toolSpecAllowlist': ['privileged'], '_taskId': 'parent'}
    before = deepcopy(parent)
    inherited = inherit_child_config(parent)
    assert inherited == {'tools': {'schemaBudgetTokens': 4000}}
    inherited['tools']['schemaBudgetTokens'] = 1
    assert parent == before
    assert inherit_child_config(parent, {'tools.schemaBudgetTokens': 0}) == {
        'tools': {'schemaBudgetTokens': 0}}
    assert inherit_child_config({'tools.schemaBudgetTokens': 4000},
        {'tools': {'schemaBudgetTokens': 8000}, 'tools.schemaBudgetTokens': 1}) == {
        'tools': {'schemaBudgetTokens': 8000}}
