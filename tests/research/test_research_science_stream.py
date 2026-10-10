"""Complete scientific streams retain contradictions, invalid numerics and scope."""
from copy import deepcopy
import json
import pytest

from lib.research.science_reduction import reduce_scientific_receipts, ScientificReceiptReducer
from lib.research.receipt_pages import empty_reference
from lib.research.workspace import save_workspace, read_run_scientific_state
from lib.research.program import dependency_digests, normalize_run_evidence
from tests.research.test_research_receipt_pages import repository, fact
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_workspace_evidence_guard import storage as storage
from tests.research.test_research_decisions import result_workspace
from tests.research.test_research_receipt_publication import prepare, DIRECTION
from tests.support.research_observations import observation_payload

pytestmark = pytest.mark.unit
RULE = {'direction': 'higher', 'minimum_effect': 0, 'uncertainty_multiplier': 2}


def protocol():
    return dict(result_workspace()['protocol'], decision_rule=deepcopy(RULE))


def measured(outcome='supported', **overrides):
    payload = observation_payload(protocol(), 'artifact:1', outcome)
    payload.update(overrides)
    return dict(fact(1), result_excerpt=json.dumps(payload))


def audit(rows, expected='supported'):
    return reduce_scientific_receipts(rows, expected_outcome=expected,
        protocol=protocol(), evidence_refs=['artifact:1'])


def test_early_refutation_survives_many_successful_later_pages():
    repo = repository(MemoryRecords())
    reference = repo.prepare(empty_reference(), [measured('refuted')] + [fact(n) for n in range(1000)])
    with repo.open_receipts(reference, [measured('supported')]) as receipts:
        summary = audit(receipts)
    assert summary['scientific_outcome'] == 'inconclusive'
    assert not summary['outcome_consistent'] and not any(summary['checks'].values())
    assert summary['observed_receipts'] == 1002


def test_invalid_early_numeric_receipt_cannot_be_replaced_by_later_valid_audit():
    wrong = measured()
    payload = json.loads(wrong['result_excerpt'])
    payload['scientific_observations']['samples']['mean_difference'] = 999
    wrong['result_excerpt'] = json.dumps(payload)
    summary = audit(iter([wrong, measured()]))
    assert not summary['outcome_consistent'] and not any(summary['checks'].values())


def test_one_complete_linked_proof_is_required_without_stitching_partial_receipts():
    rows = []
    for key in ('baseline_fairness', 'uncertainty', 'seeds', 'validation_split'):
        payload = observation_payload(protocol(), 'artifact:1')
        payload['scientific_audit'] = {key: payload['scientific_audit'][key]}
        rows.append(dict(fact(1), result_excerpt=json.dumps(payload)))
    summary = audit(iter(rows))
    assert summary['outcome_consistent'] and not any(summary['checks'].values())
    assert all(audit(iter(rows + [measured()]))['checks'].values())


def test_unlinked_or_foreign_evidence_refs_cannot_prove_audit():
    summary = reduce_scientific_receipts(iter([measured()]), expected_outcome='supported',
        protocol=protocol(), evidence_refs=['another-artifact'])
    assert summary['outcome_consistent'] and not any(summary['checks'].values())


def test_complete_stream_is_consumed_after_invalid_explicit_outcome():
    consumed = []
    def rows():
        for n in range(100):
            consumed.append(n)
            yield measured(scientific_outcome='unknown') if n == 0 else fact(n)
    assert not audit(rows())['outcome_consistent']
    assert len(consumed) == 100


def test_frozen_numeric_rule_cannot_change_while_streaming():
    frozen = protocol(); reducer = ScientificReceiptReducer(frozen, ['artifact:1'])
    frozen['decision_rule']['direction'] = 'lower'
    reducer.observe(measured())
    assert all(reducer.summary('supported')['checks'].values())


def test_error_or_pending_receipts_do_not_override_completed_reported_measurements():
    rows = [measured(), dict(measured('refuted'), status='error'), dict(measured('refuted'), status='pending')]
    assert all(audit(iter(rows))['checks'].values())
    assert audit(iter(rows))['observed_receipts'] == 1


def test_workspace_scientific_stream_uses_published_prefix_after_restart(storage):
    supervisor, _ = storage
    workspace = result_workspace('supported'); workspace['direction'] = DIRECTION
    workspace['protocol']['decision_rule'] = deepcopy(RULE)
    run = workspace['runs'][0]
    run.update(dependency_digests(workspace), execution_status='running', id='long-experiment')
    run['tool_receipts'] = [measured('refuted')] + [fact(n) for n in range(39)]
    workspace['runs'] = normalize_run_evidence([run])
    saved = save_workspace(DIRECTION, 'en', workspace, expected_revision=0, user_id=1)
    saved = save_workspace(DIRECTION, 'en', saved, expected_revision=1, user_id=1,
                           receipt_publication=prepare(supervisor.client, saved))
    supervisor.stop(); supervisor.start()
    summary = read_run_scientific_state(DIRECTION, user_id=1, run_id='long-experiment')
    assert summary['basis_current'] and summary['revision'] == 2 and summary['count'] == 40
    assert summary['scientific_outcome'] == 'refuted' and not summary['outcome_consistent']
    assert not any(summary['checks'].values())
    changed = deepcopy(saved); changed['hypothesis'] = 'Changed hypothesis'
    save_workspace(DIRECTION, 'en', changed, expected_revision=2, user_id=1)
    summary = read_run_scientific_state(DIRECTION, user_id=1, run_id='long-experiment')
    assert not summary['basis_current'] and not any(summary['checks'].values())
