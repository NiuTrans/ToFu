"""Release quality is independent of spend; evidence and safety still gate it."""
from copy import deepcopy

import pytest

from lib.benchmark_contract import RELEASE_TASK_MATRIX_V2, acceptance_decision_v2

pytestmark = pytest.mark.unit


@pytest.fixture
def release_inputs():
    tasks, outcomes = [], {}
    for (family, dataset), count in RELEASE_TASK_MATRIX_V2.items():
        tasks.extend({'taskId': f'{dataset}:{index}', 'family': family,
                      'dataset': dataset} for index in range(count))
        outcomes.setdefault(family, []).extend([True] * count)
    return {
        'task_table': tasks,
        'candidate_by_family': outcomes,
        'baseline_by_family': deepcopy(outcomes),
        'candidate_agent_cost_usd': 1_000_000,
        'baseline_agent_cost_usd': 100,
        'candidate_p90_oracle_ready_ms': 800,
        'baseline_p90_oracle_ready_ms': 1_000,
        'candidate_critical_incidents': 0,
        'judge_passes': {'claude-opus-5': True, 'glm-5.3': True},
        'infrastructure_failure_rate': 0.01,
        'maximum_infrastructure_failure_rate': 0.02,
        'candidate_orchestration_adoption': {
            'contractVersion': 'tofu.orchestration-adoption-summary/v1',
            'taskRecords': len(tasks), 'tasksWithV2Decisions': len(tasks),
            'v2Decisions': len(tasks), 'programTrajectories': 1,
            'agentTrajectories': 1, 'falseAdoptionClaims': 0,
        },
    }


@pytest.mark.parametrize('candidate_cost,baseline_cost', [(80, 100), (1_000_000, 100), (1_000_000, 0)])
def test_higher_cost_cannot_disqualify_a_verified_candidate(release_inputs, candidate_cost, baseline_cost):
    release_inputs.update(candidate_agent_cost_usd=candidate_cost,
                          baseline_agent_cost_usd=baseline_cost)
    decision = acceptance_decision_v2(**release_inputs)
    assert decision['releaseEligible'] is True
    assert decision['acceptancePolicy'] == 'tofu-long-agent-quality-first/v1'
    assert 'costPerSuccessAtMostEightyFivePercent' not in decision['gates']
    assert decision['candidateAgentCostPerSuccessUsd'] == candidate_cost / len(release_inputs['task_table'])
    if baseline_cost:
        assert decision['costPerSuccessRatio'] == pytest.approx(candidate_cost / baseline_cost)
    else:
        assert decision['costPerSuccessRatio'] is None


@pytest.mark.parametrize('field,value,gate', [
    ('candidate_critical_incidents', 1, 'zeroCriticalIncidents'),
    ('judge_passes', {'claude-opus-5': True, 'glm-5.3': False}, 'bothBlindJudgesPass'),
    ('infrastructure_failure_rate', 0.03, 'infrastructureFailureRateWithinPreregisteredLimit'),
    ('candidate_p90_oracle_ready_ms', 1_100, 'p90OracleReadyAtMostEightyFivePercent'),
])
def test_ignoring_cost_never_overrides_other_acceptance_gates(release_inputs, field, value, gate):
    release_inputs[field] = value
    decision = acceptance_decision_v2(**release_inputs)
    assert decision['releaseEligible'] is False
    assert decision['gates'][gate] is False


def test_higher_spend_does_not_compensate_for_lower_quality(release_inputs):
    family = next(iter(release_inputs['candidate_by_family']))
    release_inputs['candidate_by_family'][family] = [False] * len(release_inputs['candidate_by_family'][family])
    decision = acceptance_decision_v2(**release_inputs)
    assert decision['releaseEligible'] is False
    assert decision['gates']['qualityPointNotLower'] is False
    assert decision['gates']['allFamiliesWithinFivePoints'] is False
