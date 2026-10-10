from copy import deepcopy
import pytest
from lib.research import recipe

pytestmark = pytest.mark.unit


def test_targeted_repair_publish_and_resume(monkeypatch, tmp_path):
    calls = []
    ctx = dict(direction='memory', user_id=1, folder_id='f', workdir=str(tmp_path),
               artifacts={'harvest': {'folder_id': 'f', 'arxiv_ids': ['2401.00001']},
                          'survey': {'survey_md': 'bad neighbor', 'open_gaps': {'open_gaps': [{'id': 'g'}]}},
                          'ideate': {'accepted': [{'idea_id': 'i', 'linked_gap_id': 'g', 'title': 'idea'}], 'rejected': []}})
    def search(*a, **k):
        calls.append('search')
        return [{'arxiv_id': '2401.00002'}]
    def harvest(*a, **k):
        calls.append('harvest')
        return {'parsed': 1, 'results': [{'arxivId': '2401.00002', 'status': 'parsed'}]}
    def survey(*a, **k):
        calls.append('survey')
        assert '2401.00002' in a[1]
        return {'ok': True, 'survey_md': 'verified neighbor', 'open_gaps': {'open_gaps': [{'id': 'g'}]}, 'usage': {'calls': 1}}
    def ideas(*a, **k):
        calls.append('ideas')
        assert k['revision_context']['revalidate_all_dependencies']
        out = deepcopy(k['revision_context']['previous_ideas'])
        out['accepted'][0]['closest_neighbor'] = '2401.00002'
        return {**out, 'ok': True, 'usage': {'calls': 1}}
    def review(direction, frozen, **k):
        calls.append('review')
        issues = frozen['revision_issues']
        if issues:
            assert frozen['survey_md'] == 'verified neighbor'
            return {'ok': True, 'issue_resolutions': [{'issue_id': issues[0]['issue_id'], 'resolved': True, 'evidence': 'verified neighbor and dependent claim updated'}], 'usage': {'calls': 2}}
        return {'ok': True, 'recommended_changes': [{'target': 'retrieval', 'priority': 'high', 'change': 'verify closest neighbor', 'evidence': 'bad neighbor'}], 'usage': {'calls': 2}}
    for name, fn in [('_search_arxiv', search), ('_harvest_batch', harvest), ('_build_survey', survey), ('_generate_ideas', ideas), ('_evaluate_result', review)]:
        monkeypatch.setattr(recipe, name, fn)
    evaluation = recipe._run_evaluate(ctx)
    assert evaluation['research_cycle']['status'] == 'complete'
    assert calls == ['review', 'search', 'harvest', 'survey', 'ideas', 'review']
    assert recipe._run_evaluate(ctx) == evaluation
    assert len(calls) == 6
    from lib.research import persistence
    rows = []
    monkeypatch.setattr(persistence, '_upsert_row', lambda phash, lang, report, meta, model, **kw: rows.append({'report': report, 'meta': meta}))
    class Storage:
        def query(self, *a, **k):
            return rows
    monkeypatch.setattr(persistence, '_storage', lambda: Storage())
    recipe._run_publish({**ctx, 'artifacts': {**ctx['artifacts'], 'evaluate': evaluation}})
    loaded = persistence.load_research_artifacts('memory', user_id=1)
    assert loaded['survey_md'] == 'verified neighbor'
    assert loaded['accepted'][0]['closest_neighbor'] == '2401.00002'
    assert loaded['research_cycle']['issues'][0]['status'] == 'resolved'
    assert loaded['usage']['total']['calls'] == 6


def test_closed_gap_retires_without_rewrite():
    from lib.research.revision import run_revision_cycle
    def review(ideas, issues, *rest):
        return {'ok': True, 'recommended_changes': [] if issues else [{'target': 'survey', 'priority': 'high', 'change': 'check closure', 'evidence': 'solved'}]}
    state = run_revision_cycle({'accepted': [{'idea_id': 'i', 'linked_gap_id': 'g'}]},
        survey={'open_gaps': {'open_gaps': [{'id': 'g'}]}}, harvest={}, review=review,
        revise=lambda _: pytest.fail('closed gap must not be rewritten'), gate=lambda _: [],
        rebuild=lambda *_: {'ok': True, 'survey': {'open_gaps': {'open_gaps': [], 'closed_gaps': [{'id': 'g'}]}}})
    assert not state['current']['accepted']
    assert state['current']['retired'][0]['idea_id'] == 'i'
    assert state['research_cycle']['issues'][0]['status'] == 'unresolved'
    assert state['research_cycle']['revision_count'] == 2
