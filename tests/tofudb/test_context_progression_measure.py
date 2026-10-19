"""Reject incomparable context progressions before their clocks are reported."""
import copy

import pytest

from benchmarks.tofudb_context_progression import verify_suffix
from benchmarks.tofudb_context_read import history_proof

pytestmark = pytest.mark.unit


def valid_fixture():
    rows = [
        {'turnId': f'turn-{index}', 'parentTurnId': None,
         'status': 'completed', 'currentAttemptId': None,
         'modelContextRevision': 1,
         'projection': {'content': f'exact content {index}'}}
        for index in range(4)
    ]
    checkpoint = {'messages': [{'role': 'assistant', 'content': 'retained'}]}
    value = {'supported': True, 'continuationStatus': 'available', 'covered': 2,
             'nextTurnId': None, 'proofEligible': True,
             'continuation': checkpoint, 'historyProof': history_proof(rows),
             'turns': copy.deepcopy(rows[2:])}
    return value, rows, checkpoint


def test_exact_public_suffix_is_accepted():
    value, rows, checkpoint = valid_fixture()
    verify_suffix(value, rows, checkpoint, 2)


@pytest.mark.parametrize('corruption', [
    'prefix_proof', 'old_header', 'missing_turn', 'swapped_turns',
    'changed_evidence', 'checkpoint', 'covered', 'pagination', 'eligibility',
])
def test_changed_or_incomplete_work_is_not_measurable(corruption):
    value, rows, checkpoint = valid_fixture()
    if corruption == 'prefix_proof':
        value['historyProof']['digest'] = '0' * 64
    elif corruption == 'old_header':
        rows[0]['modelContextRevision'] += 1
    elif corruption == 'missing_turn':
        value['turns'].pop()
    elif corruption == 'swapped_turns':
        value['turns'].reverse()
    elif corruption == 'changed_evidence':
        value['turns'][0]['projection']['content'] = 'different evidence'
    elif corruption == 'checkpoint':
        value['continuation'] = None
    elif corruption == 'covered':
        value['covered'] = 3
    elif corruption == 'pagination':
        value['nextTurnId'] = 'turn-2'
    else:
        value['proofEligible'] = False
    with pytest.raises(ValueError, match='exact public history'):
        verify_suffix(value, rows, checkpoint, 2)
