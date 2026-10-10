"""Offline specifications for bounded, honest research revisions."""
from copy import deepcopy
import pytest
from lib.research.revision import run_revision_cycle

pytestmark = pytest.mark.unit


def original():
    return {'accepted': [{'idea_id': 'i1', 'title': 'Original'}], 'rejected': []}


def review_result(changes=True):
    return {'ok': True, 'usage': {'calls': 2}, 'recommended_changes': ([{
        'target': 'ideate', 'priority': 'high', 'change': 'Add prediction',
        'evidence': 'No measured prediction', 'idea_id': 'i1'}] if changes else [])}


def test_bounded_revision_and_silent_review_does_not_resolve():
    source = original()
    def revise(context):
        out = deepcopy(context['previous_ideas'])
        out['accepted'][0]['title'] += ' revised'
        return {**out, 'ok': True, 'usage': {'calls': 1}}
    state = run_revision_cycle(source, review=lambda *_: review_result(),
                               revise=revise, gate=lambda _: [])
    cycle = state['research_cycle']
    assert cycle['status'] == 'budget_exhausted'
    assert cycle['revision_count'] == 2 and len(cycle['history']) == 3
    assert cycle['history'][0]['ideas']['accepted'][0]['title'] == 'Original'
    assert source == original()
    assert state['current']['accepted'][0]['idea_version'] == 3
    assert state['review_usage']['calls'] == 6
    assert state['revision_usage']['calls'] == 2


def test_no_change_and_nonactionable_stop():
    state = run_revision_cycle(original(), review=lambda *_: review_result(),
        revise=lambda c: {**c['previous_ideas'], 'ok': True}, gate=lambda _: [])
    assert state['research_cycle']['status'] == 'no_change'
    result = review_result()
    result['recommended_changes'][0]['target'] = 'survey'
    state = run_revision_cycle(original(), review=lambda *_: result,
        revise=lambda _: pytest.fail('must not revise survey'), gate=lambda _: [])
    assert state['research_cycle']['status'] == 'unresolved'


def test_resume_and_explicit_resolution(tmp_path):
    calls = []
    def review(ideas, issues):
        calls.append('review')
        result = review_result(not issues)
        if issues:
            result['issue_resolutions'] = [{'issue_id': issues[0]['issue_id'],
                'resolved': True, 'evidence': 'Prediction now specifies recall at fixed memory'}]
        return result
    def revise(c):
        calls.append('revise')
        out = deepcopy(c['previous_ideas'])
        out['accepted'][0]['falsifiable_prediction'] = 'Recall at fixed memory'
        return {**out, 'ok': True}
    kwargs = dict(review=review, revise=revise, gate=lambda _: [], workdir=str(tmp_path))
    state = run_revision_cycle(original(), **kwargs)
    assert state['research_cycle']['status'] == 'complete'
    assert calls == ['review', 'revise', 'review']
    assert run_revision_cycle(original(), **kwargs) == state
    assert len(calls) == 3


def test_errors_and_cancellation_preserve_honesty():
    state = run_revision_cycle(original(), review=lambda *_: review_result(),
        revise=lambda _: {'ok': False, 'error': 'provider unavailable'}, gate=lambda _: [])
    assert state['research_cycle']['status'] == 'error'
    assert state['research_cycle']['issues'][0]['status'] == 'unresolved'
    from lib.production.stages import StageAborted
    with pytest.raises(StageAborted):
        run_revision_cycle(original(), review=lambda *_: pytest.fail(),
                           revise=lambda _: pytest.fail(), gate=lambda _: [], abort=lambda: True)


def test_review_silence_is_not_resolution():
    count = 0
    def review(*_):
        nonlocal count
        count += 1
        return review_result(count == 1)
    def revise(c):
        out = deepcopy(c['previous_ideas'])
        out['accepted'][0]['title'] += '!'
        return {**out, 'ok': True}
    state = run_revision_cycle(original(), review=review, revise=revise, gate=lambda _: [])
    assert state['research_cycle']['status'] == 'budget_exhausted'
    assert state['research_cycle']['issues'][0]['status'] == 'unresolved'


@pytest.mark.parametrize('diagnostic', ['reject_reason', 'reject_stage', 'unexpected_diagnostic'])
def test_diagnostics_reordering_and_gate_placement_are_not_science(diagnostic):
    source = original()
    source['accepted'].append({'idea_id': 'i2', 'title': 'Other'})
    def revise(context):
        out = deepcopy(context['previous_ideas'])
        out['rejected'] = list(reversed(out.pop('accepted')))
        for row in out['rejected']:
            row[diagnostic] = 'new diagnostic'
        return {**out, 'ok': True}
    state = run_revision_cycle(source, review=lambda *_: review_result(),
                               revise=revise, gate=lambda _: [])
    assert state['research_cycle']['status'] == 'no_change'
    assert len(state['research_cycle']['history']) == 1
    assert state['current']['accepted'][0]['idea_version'] == 1


@pytest.mark.parametrize('budget', [{'usage': {'forced_final': True}}, {'degraded': True}])
def test_initial_budget_still_reviews_but_never_revises(budget):
    calls = []
    def review(*_):
        calls.append('review')
        return review_result()
    state = run_revision_cycle({**original(), **budget}, review=review,
        revise=lambda _: pytest.fail('budget exhausted'), gate=lambda _: [])
    assert calls == ['review']
    assert state['generation_budget_exhausted'] is True
    assert state['research_cycle']['status'] == 'budget_exhausted'
    assert state['research_cycle']['revision_count'] == 0


def test_revision_context_is_bounded_without_nested_history_or_lost_lineage():
    from lib.research.revision import identify, _revision_context, critique_issues
    source = original()
    row = source['accepted'][0]
    row.update(core_mechanism='x' * 20000, original_candidate={'secret': 'huge'},
               repair_records=[{'original': 'huge'}])
    source['research_cycle'] = {'history': ['huge']}
    source['rejected'] = [{'idea_id': f'i{n}', 'title': 'Other'} for n in range(2, 13)]
    issues = critique_issues(review_result()) * 30
    context = _revision_context(identify(source), issues, 1)
    previous = context['previous_ideas']
    assert set(previous) == {'accepted', 'rejected'}
    assert len(previous['accepted']) + len(previous['rejected']) == 12
    assert len(context['issues']) == 24
    assert previous['accepted'][0] == {
        'idea_id': 'i1', 'idea_version': 1, 'title': 'Original',
        'core_mechanism': 'x' * 1200}
    assert [r['idea_id'] for r in previous['rejected']] == [f'i{n}' for n in range(2, 13)]
    source['rejected'].append({'idea_id': 'overflow'})
    with pytest.raises(ValueError, match='12 idea lineages'):
        _revision_context(identify(source), issues, 1)


def test_aggregate_critique_is_per_lineage_and_keeps_strongest_priority():
    from lib.research.evaluation import _aggregate, _clean_judgement, EVALUATION_AXES
    def judge(idea_id, priority, evidence):
        return _clean_judgement({
            'scores': dict.fromkeys(EVALUATION_AXES, 3), 'worth_following_up': True,
            'recommended_changes': [{'target': 'ideate', 'idea_id': idea_id,
                'priority': priority, 'change': 'Add prediction', 'evidence': evidence}]})
    judges = [judge('i1', 'low', 'Missing metric'), judge('i2', 'high', 'Missing baseline'),
              judge('i1', 'high', 'No test ' + 'x' * 800)]
    result = _aggregate(judges, 3, [], {})
    rows = {r['idea_id']: r for r in result['recommended_changes']}
    assert set(rows) == {'i1', 'i2'}
    assert rows['i1']['priority'] == 'high'
    assert rows['i1']['evidence'].startswith('Missing metric\nNo test')
    assert len(rows['i1']['evidence']) <= 600
    assert judges[0]['recommended_changes'][0]['priority'] == 'low'


def test_final_artifact_keeps_outer_failure_separate_from_successful_raw_review():
    from lib.research.recipe import _final_idea_artifact
    raw = {'ok': True, 'degraded': False}
    evaluation = {'raw_evaluation': raw, 'final_ideas': original(),
                  'degraded': True, 'degraded_reason': 'revision unavailable',
                  'research_cycle': {'status': 'error'}}
    artifact = _final_idea_artifact({**original(), 'degraded': False}, evaluation)
    assert artifact['degraded'] is True
    assert artifact['degraded_reason'] == 'revision unavailable'
    assert artifact['evaluation'] == raw
    assert artifact['evaluation']['degraded'] is False
