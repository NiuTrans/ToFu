"""Claim fan-out shares numerical checks without retaining evidence across calls."""
from copy import deepcopy
import json

import pytest

from lib.research import program, science_reduction
from tests.support.research_observations import observation_payload

pytestmark = pytest.mark.unit


def measured_program(*, runs=4, claims=1, outcome='supported'):
    """Synthetic measured receipts; no execution or independent-reproduction claim."""
    raw = {'hypothesis': 'A scoped comparison', 'protocol': {
        'dataset': 'synthetic', 'baseline': 'control', 'primary_metric': 'accuracy',
        'random_seeds': [1, 2, 3],
        'decision_rule': {'direction': 'higher', 'minimum_effect': 0,
                          'uncertainty_multiplier': 2}}, 'runs': []}
    dependencies = program.dependency_digests(raw)
    for index in range(runs):
        reference = f'evidence:{index}'
        raw['runs'].append({
            'id': f'run-{index}', 'task_id': f'task-{index}',
            'execution_status': 'completed', 'scientific_outcome': outcome,
            'artifact_ref': reference, **dependencies,
            'tool_receipts': [{'tool': 'run_command', 'status': 'done',
                'result_digest': f'measured-{index}', 'artifact_ref': reference,
                'result_excerpt': json.dumps(observation_payload(
                    raw['protocol'], reference, outcome))}]})
    raw.update(program.normalize_program_fields(raw))
    raw['claims'] = []
    for index in range(claims):
        claim = {'id': f'claim-{index}', 'text': f'Scoped claim {index}',
                 'status': 'supported', 'outcome_scope': outcome,
                 'evidence_refs': [f'run-{index % runs}'], **dependencies}
        claim['evidence_digest'] = program.claim_evidence_digest(claim, raw['runs'])
        raw['claims'].append(claim)
    return raw


@pytest.mark.parametrize('outcome', ['supported', 'refuted'])
def test_more_claims_do_not_repeat_numeric_verification(monkeypatch, outcome):
    small = measured_program(claims=1, outcome=outcome)
    large = measured_program(claims=program.MAX_CLAIMS, outcome=outcome)
    verify = science_reduction.verify_observations
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return verify(*args, **kwargs)

    monkeypatch.setattr(science_reduction, 'verify_observations', counted)
    program.normalize_program_fields(small)
    small_count = len(calls)
    calls.clear()
    normalized = program.normalize_program_fields(large)
    assert all(claim['validity'] == 'current' for claim in normalized['claims'])
    assert all(claim['outcome_scope'] == outcome for claim in normalized['claims'])
    assert 0 < len(calls) <= small_count


@pytest.mark.parametrize('change', ['measurement', 'rule', 'opposition'])
def test_reuse_never_certifies_changed_science_or_mutates_input(change):
    raw = measured_program(claims=program.MAX_CLAIMS)
    before = deepcopy(raw)
    assert all(claim['validity'] == 'current'
               for claim in program.normalize_program_fields(raw)['claims'])
    assert raw == before
    if change == 'measurement':
        receipt = raw['runs'][0]['tool_receipts'][0]
        payload = json.loads(receipt['result_excerpt'])
        payload['scientific_observations']['samples']['method_values'][0] = 0
        receipt['result_excerpt'] = json.dumps(payload)
    elif change == 'rule':
        raw['protocol']['decision_rule']['minimum_effect'] = 1
    else:
        opposing = measured_program(runs=1, outcome='refuted')['runs'][0]
        opposing.update(id='opposing', task_id='opposing-task')
        raw['runs'].append(opposing)
    assert all(claim['status'] == 'contested'
               for claim in program.normalize_program_fields(raw)['claims'])
    assert all(claim['validity'] == 'current'
               for claim in program.normalize_program_fields(before)['claims'])
