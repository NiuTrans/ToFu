"""Retained conclusions cannot contradict immutable completed tool outcomes."""
from copy import deepcopy
import json
import pytest

from lib.research.observation_audit import AUDIT_VERSION
from lib.research.decisions import receipt_outcome_consistent
from lib.research.program import normalize_program_fields, scientific_dependency_digest
from lib.research.workspace import load_workspace, save_workspace
from lib.research.persistence import research_direction_hash
from tests.research.test_research_decisions import result_workspace
from tests.research.test_research_history import DIRECTION, draft_with_runs, save
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('original,replacement', [('refuted','supported'), ('supported','refuted'),
    ('inconclusive','supported'), ('refuted','inconclusive')])
def test_changed_conclusion_cannot_pass_decision_or_manuscript(original,replacement):
    workspace = result_workspace(original)
    workspace['runs'][0]['scientific_outcome'] = replacement
    workspace.update(normalize_program_fields(workspace))
    assert workspace['decision']['status'] == 'inconclusive'
    scientific = [c for c in workspace['manuscript_audit']['checks'] if c['id'] != 'claim_traceability']
    assert not any(c['ok'] for c in scientific)


@pytest.mark.parametrize('invalid', ['unknown', None, [], True])
def test_unknown_explicit_receipt_outcome_fails_closed(invalid):
    run = result_workspace()['runs'][0]
    payload = json.loads(run['tool_receipts'][0]['result_excerpt'])
    payload['scientific_outcome'] = invalid
    run['tool_receipts'][0]['result_excerpt'] = json.dumps(payload)
    assert not receipt_outcome_consistent(run)


@pytest.mark.parametrize('status,digest,expected', [('done','present',False),
    ('running','present',True), ('error','present',True), ('done','',True)])
def test_every_completed_receipt_checked_without_cherry_picking(status,digest,expected):
    run = result_workspace('supported')['runs'][0]
    run['tool_receipts'].append({'status':status,'result_digest':digest,
        'result_excerpt':json.dumps({'scientific_outcome':'refuted'})})
    assert receipt_outcome_consistent(run) is expected


def test_outcome_free_legacy_receipt_does_not_create_a_new_proof():
    run = result_workspace('supported')['runs'][0]
    payload = json.loads(run['tool_receipts'][0]['result_excerpt'])
    payload.pop('scientific_outcome')
    run['tool_receipts'][0]['result_excerpt'] = json.dumps(payload)
    assert receipt_outcome_consistent(run)


def test_missing_run_conclusion_cannot_bind_explicit_measurement_outcome():
    run = result_workspace('supported')['runs'][0]
    run.pop('scientific_outcome')
    assert not receipt_outcome_consistent(run)


def test_compact_observation_is_authority_even_when_presentation_differs():
    run = result_workspace('refuted')['runs'][0]
    receipt = run['tool_receipts'][0]
    receipt['observation_excerpt'] = receipt['result_excerpt']
    receipt['result_excerpt'] = json.dumps({'scientific_outcome':'supported'})
    assert receipt_outcome_consistent(run)
    run['scientific_outcome'] = 'supported'
    assert not receipt_outcome_consistent(run)


def test_mixed_completed_outcomes_require_inconclusive_conclusion():
    run = result_workspace('supported')['runs'][0]
    run['tool_receipts'].append({'status':'done','result_digest':'opposing',
        'result_excerpt':json.dumps({'scientific_outcome':'refuted'})})
    run['scientific_outcome'] = 'inconclusive'
    assert receipt_outcome_consistent(run)


def test_archived_mismatch_is_preserved_but_cannot_support_claim(storage):
    draft = draft_with_runs(24)
    draft['runs'][0]['scientific_outcome'] = 'refuted'
    from lib.research.program import normalize_run_evidence
    original = normalize_run_evidence(draft['runs'])[0]['tool_receipts']
    reviewed = scientific_dependency_digest(draft)
    saved = save(draft)
    assert scientific_dependency_digest(saved) == reviewed
    assert saved['history']['outcomes'] == {'supported':7,'refuted':0,'inconclusive':1}
    assert all(value == 1 for value in saved['history']['audit_failures'].values())
    assert saved['decision']['status'] == 'inconclusive'
    from lib.research.workspace import load_history_page
    archived = load_history_page(DIRECTION,'en',user_id=1)
    assert archived['runs'][0]['tool_receipts'] == original


def test_version_two_success_cache_cannot_hide_contradictory_archived_receipt(storage):
    draft = draft_with_runs(24)
    draft['runs'][0]['scientific_outcome'] = 'refuted'
    saved = save(draft)
    legacy = deepcopy(saved)
    legacy['history']['audit_version'] = 2
    legacy['history']['audit_failures'] = dict.fromkeys(legacy['history']['audit_failures'],0)
    legacy['history']['outcomes'] = {'supported':8,'refuted':0,'inconclusive':0}
    storage[0].client.command('research.workspace.put', {
        'user_id':1,'paper_hash':research_direction_hash(DIRECTION),'lang':'en',
        'expected_revision':saved['revision'],'workspace':legacy,'updated_at':0},'legacy-outcome-cache')
    before = load_workspace(DIRECTION,'en',user_id=1)
    assert before['decision']['status'] == 'inconclusive'
    assert not before['manuscript_audit']['passed']
    repaired = save_workspace(DIRECTION,'en',before,user_id=1,expected_revision=before['revision'])
    assert repaired['history']['audit_version'] == AUDIT_VERSION
    assert repaired['history']['head'] == saved['history']['head']
    assert repaired['history']['outcomes']['inconclusive'] == 1
    assert all(value == 1 for value in repaired['history']['audit_failures'].values())
