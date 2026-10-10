"""Result-driven decisions do not equate scientific support with job success."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from lib.research.decisions import AUDIT_CHECKS
from tests.support.research_observations import observation_payload
from lib.research.program import (dependency_digests, normalize_program_fields,
    claim_evidence_digest, scientific_dependency_digest, readiness)
from lib.research.workspace import empty_workspace

pytestmark = pytest.mark.unit


def result_workspace(outcome='refuted'):
    w = empty_workspace('test')
    w['hypothesis'] = 'Intervention improves the metric'
    w['protocol'].update(dataset='held-out',primary_metric='accuracy',baseline='reference',random_seeds=[1,2,3])
    w['runs'] = [{'id': 'r1', 'execution_status': 'completed',
        'scientific_outcome': outcome, **dependency_digests(w),
        'artifact_ref': 'artifact:1', 'tool_receipts': [{'status': 'done',
        'result_digest': 'abc', 'result_excerpt': json.dumps(observation_payload(w['protocol'],'artifact:1',outcome))}]}]
    w.update(normalize_program_fields(w))
    return w


def test_negative_result_claim_and_draft_invalidation():
    w = result_workspace()
    assert w['decision']['status'] == 'refute'
    assert w['decision']['next_actions'][0]['requires_confirmation'] is True
    c = {'id': 'c1', 'text': 'No improvement in this tested setting',
         'status': 'supported', 'outcome_scope': 'refuted',
         'evidence_refs': ['r1'], **dependency_digests(w)}
    c['evidence_digest'] = claim_evidence_digest(c, w['runs'])
    w['claims'] = [c]
    w.update(normalize_program_fields(w))
    assert w['claims'][0]['validity'] == 'current'
    assert w['manuscript_audit']['passed']
    w['manuscript']['scientific_dependency_digest'] = scientific_dependency_digest(w)
    old = scientific_dependency_digest(w)
    w['runs'][0]['scientific_outcome'] = 'supported'
    w.update(normalize_program_fields(w))
    assert w['claims'][0]['status'] == 'contested'
    assert scientific_dependency_digest(w) != old
    assert not readiness(w)['ready']
    c = dict(w['claims'][0], text='Improvement in this tested setting',
             outcome_scope='supported', status='supported')
    c['evidence_digest'] = claim_evidence_digest(c, w['runs'])
    w['claims'] = [c]
    w.update(normalize_program_fields(w))
    assert not w['manuscript_audit']['passed']
    assert w['decision']['status'] == 'inconclusive'
    w['manuscript']['scientific_dependency_digest'] = scientific_dependency_digest(w)
    assert w['manuscript']['scientific_dependency_digest'] != old
    schema = json.loads(Path('contracts/research_program_v1.schema.json').read_text())
    Draft202012Validator(schema).validate(w)


def test_legacy_strings_do_not_prove_audit_or_decision():
    w = result_workspace('supported')
    w['runs'][0]['tool_receipts'][0]['result_excerpt'] = 'baseline fair seeds uncertainty held-out validation verified'
    w.update(normalize_program_fields(w))
    assert not w['manuscript_audit']['passed']
    w['runs'][0]['protocol_digest'] = ''
    w.update(normalize_program_fields(w))
    assert w['decision']['status'] == 'inconclusive'


def test_portfolio_normalization_is_bounded_and_legacy_hash_stable():
    w = empty_workspace('test')
    before = dependency_digests(w)
    w['protocol']['competing_hypotheses'] = 'malformed legacy value'
    assert dependency_digests(w) == before
    w['protocol']['competing_hypotheses'] = [{'id': 'h1', 'statement': 'a'}]
    w['protocol']['discriminating_experiments'] = [{'id': 'e1', 'hypothesis_ids': ['h1'],
        'expected_information': 'Separate mechanisms', 'expected_value': 'Avoid full-scale trial',
        'cost': 'Unknown pending estimate'}]
    w.update(normalize_program_fields(w))
    assert w['decision']['next_actions'][0]['id'] == 'e1'
    assert dependency_digests(w) != before


def test_production_result_to_decision_to_revised_manuscript():
    from lib.research.action import apply_action_result

    def apply(w, action, payload, receipts=None):
        return apply_action_result(action=action, workspace=w, payload=payload,
                                   receipts=receipts or [], task_id='task-1', bindings=[])

    w = empty_workspace('production')
    def experiment(w, outcome):
        return apply(w, 'experiment', {'run': {'id': 'r1'}, 'claims': [{
            'id': 'c1', 'text': 'Scoped result', 'status': 'supported',
            'outcome_scope': outcome, 'evidence_refs': ['r1']}]}, [{
            'tool': 'run_command', 'status': 'done', 'artifact_ref': 'artifact:1',
            'result_digest': outcome, 'result_excerpt': json.dumps({
                'execution_status': 'completed', 'scientific_outcome': outcome})}])
    w = experiment(w, 'supported')
    assert w['decision']['status'] == 'retain'
    w = apply(w, 'manuscript', {'manuscript': {'title': 'Initial result'}})
    old = w['manuscript']['scientific_dependency_digest']
    # An opposing observation retains the previous result and requires refinement.
    w = apply(w, 'experiment', {'run': {'id': 'r1'}}, [{
        'tool': 'run_command', 'status': 'done', 'artifact_ref': 'artifact:1',
        'result_digest': 'new', 'result_excerpt': json.dumps({'scientific_outcome': 'refuted'})}])
    assert w['decision']['status'] == 'refine'
    assert len(w['runs']) == 2
    assert {run['scientific_outcome'] for run in w['runs']} == {'supported', 'refuted'}
    assert w['claims'][0]['status'] == 'contested'
    assert scientific_dependency_digest(w) != old
    # The owner freezes a narrower population before a new experiment.
    w['protocol']['dataset'] = 'Independent narrowed evaluation population'
    w = experiment(w, 'refuted')
    assert w['decision']['status'] == 'refute'
    assert len(w['runs']) == 3
    assert w['runs'][0]['validity'] == 'stale'
    assert w['claims'][0]['status'] == 'supported'
    w = apply(w, 'manuscript', {'manuscript': {'title': 'Scoped negative result'}})
    assert w['manuscript']['scientific_dependency_digest'] == scientific_dependency_digest(w)
    assert w['manuscript']['scientific_dependency_digest'] != old
