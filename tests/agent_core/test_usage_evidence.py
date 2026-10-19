"""Public runtime evidence preserves the canonical account's unknown/pending state."""
import pytest

from lib.agent_core.execution_budget import model_budget_admission
from lib.agent_core.run_contract import project_agent_evidence

pytestmark = pytest.mark.unit


def _task():
    return {'id': 'usage-owner', '_userId': 7, 'status': 'running', 'usage': {},
            'config': {'executionLimits': {'max_tokens': 100,
                                          'max_tool_calls': None, 'deadline': None}}}


def _evidence(task):
    return project_agent_evidence(task, model='fixture-model')['usageAccounting']


def test_evidence_observation_does_not_create_an_account_or_infer_completeness():
    task = _task()
    task['usage'] = {'prompt_tokens': 10, 'completion_tokens': 5}
    assert _evidence(task)['complete'] is None
    assert '_executionBudget' not in task
    task['_executionActualsComplete'] = False
    assert _evidence(task)['complete'] is False
    assert '_executionBudget' not in task


@pytest.mark.parametrize('settlement,expected_tokens,complete', [
    ({'prompt_tokens': 20, 'completion_tokens': 5}, 25, True),
    ({'prompt_tokens': 20}, 20, False),
    ({}, 0, False),
])
def test_public_account_evidence_waits_for_physical_settlement(settlement, expected_tokens, complete):
    task = _task()
    lease = model_budget_admission(task)(input_tokens=20, max_output_tokens=30)
    lease.mark_dispatched()
    assert _evidence(task)['complete'] is False
    assert _evidence(task)['observedTokens'] == 0
    lease.settle(settlement)
    evidence = _evidence(task)
    assert evidence == {'complete': complete, 'observedTokens': expected_tokens,
                        'scope': 'execution_tree', 'rootTaskId': task['id']}


def test_known_unsent_request_is_distinct_from_unknown_sent_usage():
    task = _task()
    lease = model_budget_admission(task)(input_tokens=20, max_output_tokens=30)
    assert _evidence(task)['complete'] is False
    lease.settle(unsent=True)
    assert _evidence(task)['complete'] is True
    assert _evidence(task)['observedTokens'] == 0
