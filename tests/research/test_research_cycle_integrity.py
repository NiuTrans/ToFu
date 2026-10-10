"""Fail-closed research repair and evidence-boundary regressions (no model calls)."""
from copy import deepcopy
import hashlib
import json

import pytest

from lib.research.entailment import assess_gaps
from lib.research.revision import critique_issues, run_revision_cycle
from lib.research.decisions import AUDIT_CHECKS, manuscript_audit

pytestmark = pytest.mark.unit


def cycle(**kwargs):
    defaults = dict(review=lambda *_: {'ok': True}, revise=lambda _: pytest.fail('unexpected rewrite'),
                    gate=lambda _: [])
    defaults.update(kwargs)
    return run_revision_cycle({'accepted': [{'idea_id': 'i', 'title': 'idea'}]}, **defaults)


def test_checkpoint_includes_upstream_snapshots(tmp_path):
    calls = []
    def review(*args):
        calls.append(args[2]['survey_md'])
        return {'ok': True}
    for text in ('old', 'new', 'new'):
        cycle(workdir=str(tmp_path), survey={'survey_md': text}, harvest={}, review=review)
    assert calls == ['old', 'new']


def test_gap_specific_issues_have_distinct_stable_ids():
    rows = [dict(target='survey', priority='high', change='verify support', evidence='quote', gap_id=g)
            for g in ('g1', 'g2')]
    issues = critique_issues({'recommended_changes': rows})
    assert len({i['issue_id'] for i in issues}) == 2
    assert issues == critique_issues({'recommended_changes': deepcopy(rows)})


def test_missing_gap_identity_cannot_retire_unlinked_idea():
    def review(*_):
        return {'ok': True, 'recommended_changes': [dict(target='survey', priority='high',
                change='verify', evidence='quote')]}
    def revise(context):
        return dict(context['previous_ideas'], ok=True)
    state = cycle(survey={'open_gaps': {}}, review=review, revise=revise, max_revisions=1,
                  rebuild=lambda *_: {'ok': True, 'survey': {'open_gaps': {'closed_gaps': [{}]}}})
    assert state['current']['accepted'][0]['idea_id'] == 'i'
    assert not state['current'].get('retired')


def test_review_budget_receipt_cannot_report_complete():
    state = cycle(review=lambda *_: {'ok': True, 'usage': {'forced_final': True}})
    assert state['research_cycle']['status'] == 'budget_exhausted'
    assert state['research_cycle']['history']


def test_issue_overflow_is_explicit_not_silent_success():
    def review(*_):
        return {'ok': True, 'recommended_changes': [dict(target='survey', priority='low',
            change=f'check {i}', evidence='quote') for i in range(25)]}
    state = cycle(review=review)
    assert state['research_cycle']['status'] == 'error'
    assert 'issue' in state['research_cycle']['error']
    assert len(state['research_cycle']['history'][0]['evaluation']['recommended_changes']) == 25


def evidence():
    text = 'verified quote'
    source = {'source': 'parsed_text', 'content': text}
    cards = [dict(card_id=f'c{i}', arxiv_id='p', source='parsed_text', start=0,
                  end=len(text), quote=text, snapshot_sha256=hashlib.sha256(text.encode()).hexdigest())
             for i in range(2)]
    return cards, {'p': source}


@pytest.mark.parametrize('invalid_card', [False, True])
def test_incomplete_evidence_never_reports_open_gap(invalid_card):
    cards, sources = evidence()
    if invalid_card:
        cards[1]['quote'] = 'fabricated'
    def call(*_):
        return {'assessments': [{'card_id': 'c0', 'status': 'supported'},
                                {'card_id': 'c1', 'status': 'garbage'}]}
    gap = assess_gaps([{'id': 'g'}], cards, sources, call)[0]
    assert gap['status'] == 'uncertain'
    assert gap['assessment_incomplete'] is True
    assert gap['low_confidence'] is True


def test_invalid_batch_bound_is_rejected():
    with pytest.raises(ValueError):
        assess_gaps([{'id': 'g'}], [], {}, lambda *_: {}, batch_size=-1)


def test_structured_audit_outputs_real_booleans():
    from tests.support.research_observations import observation_payload
    protocol={'dataset':'held-out','primary_metric':'accuracy','baseline':'reference','random_seeds':[1,2,3]}
    protocol['decision_rule']={'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2}
    run = {'id': 'r', 'validity': 'current', 'execution_status': 'completed',
           'scientific_outcome':'supported',
           'tool_receipts': [{'status': 'done', 'result_digest': 'digest',
               'result_excerpt': json.dumps(observation_payload(protocol,'r'))}]}
    audit = manuscript_audit([run], [{'status': 'supported', 'validity': 'current'}],protocol=protocol)
    assert audit['passed'] is True
    assert all(row['ok'] is True for row in audit['checks'])
    run['tool_receipts'][0]['status'] = 'failed'
    assert manuscript_audit([run], [])['passed'] is False
