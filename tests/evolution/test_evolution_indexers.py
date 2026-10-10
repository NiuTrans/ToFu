"""History indexer: settled episodes are the cheapest planning resource."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _history(goal_id, statement, domain, episode_id, outcome, *,
             user_id=USER, exploratory=False):
    from lib import evolution
    from lib.evolution import records
    criteria = [] if exploratory else [
        {'criterion_id': 'c1', 'kind': 'test_command', 'params': {'command': 'x'}}]
    evolution.admit_goal({
        'schema_version': 1, 'goal_id': goal_id, 'statement': statement,
        'domain': domain, 'exploratory': exploratory, 'criteria': criteria,
        'budget_envelope': {'max_tokens': 1000, 'max_wall_clock_seconds': 100,
                            'max_tool_calls': 10},
        'status': 'draft', 'created_at': 1}, user_id=user_id)
    records.put_record(records.NAMESPACE_EPISODES, f'{user_id}:{episode_id}', {
        'schema_version': 1, 'episode_id': episode_id, 'goal_id': goal_id,
        'plan_id': 'p', 'selected_path_id': 'path-a', 'exploratory': False,
        'task_id': f't-{episode_id}', 'status': outcome,
        'created_at': 1, 'updated_at': 2, 'settled_at': 2,
        'actuals': {'tokens': 10, 'wall_clock_seconds': 5, 'tool_calls': 1},
        'verdict': None,
        'learning': {'resource_weight_deltas': [],
                     'cost_actual_vs_estimate': None,
                     'strategy_outcome': None, 'capability_gap': None},
    }, kind='episode')


def test_history_indexer_ranks_similar_settled_episodes(store):
    from lib.evolution.indexers import HistoryIndexer
    _history('g1', 'Reproduce transformer baseline within error bounds',
             'research', 'e1', 'success')
    _history('g2', 'Unrelated grocery list organizer', 'web', 'e2', 'success')
    refs = HistoryIndexer().index('reproduce transformer baseline', user_id=USER)
    assert [r['ref_id'] for r in refs] == ['episode:e1']
    assert refs[0]['indexer'] == 'history'
    assert 0 < refs[0]['relevance'] <= 1
    assert 'success' in refs[0]['provenance']


def test_history_indexer_is_owner_scoped(store):
    from lib.evolution.indexers import HistoryIndexer
    _history('g1', 'Reproduce transformer baseline', 'research', 'e1',
             'success', user_id=99)
    assert HistoryIndexer().index('reproduce transformer baseline',
                                  user_id=USER) == []


def test_history_indexer_matches_cjk_statements(store):
    from lib.evolution.indexers import HistoryIndexer
    _history('g1', '复现论文里的基线实验结果', 'research', 'e1', 'failure')
    refs = HistoryIndexer().index('复现基线实验', user_id=USER)
    assert [r['ref_id'] for r in refs] == ['episode:e1']
    assert 'failure' in refs[0]['provenance']


def test_history_indexer_skips_running_and_bounds_results(store):
    from lib import evolution
    from lib.evolution import records
    from lib.evolution.indexers import HistoryIndexer, MAX_INDEX_RESULTS
    _history('g1', 'Reproduce transformer baseline', 'research', 'e1', 'success')
    records.put_record(records.NAMESPACE_EPISODES, f'{USER}:e-running', {
        'schema_version': 1, 'episode_id': 'e-running', 'goal_id': 'g1',
        'plan_id': 'p', 'selected_path_id': 'path-a', 'exploratory': False,
        'task_id': 't-running', 'status': 'running',
        'created_at': 1, 'updated_at': 2, 'settled_at': None,
        'actuals': {'tokens': 0, 'wall_clock_seconds': 0, 'tool_calls': 0},
        'verdict': None,
        'learning': {'resource_weight_deltas': [],
                     'cost_actual_vs_estimate': None,
                     'strategy_outcome': None, 'capability_gap': None},
    }, kind='episode')
    refs = HistoryIndexer().index('reproduce transformer baseline',
                                  user_id=USER, limit=10)
    assert [r['ref_id'] for r in refs] == ['episode:e1']
    assert HistoryIndexer().index('reproduce transformer', user_id=USER,
                                  limit=MAX_INDEX_RESULTS + 50)


def test_index_all_merges_indexers_by_relevance(store):
    from lib.evolution.indexers import HistoryIndexer, index_all
    _history('g1', 'Reproduce transformer baseline', 'research', 'e1', 'success')

    class FakeProjectIndexer:
        name = 'project'

        def index(self, query, *, user_id, limit=8):
            return [{'ref_id': 'gh:owner/repo', 'indexer': 'project',
                     'locator': 'https://example.invalid/repo',
                     'relevance': 0.99, 'provenance': 'fake'}]

    refs = index_all('reproduce transformer baseline',
                     [HistoryIndexer(), FakeProjectIndexer()], user_id=USER)
    # Exact local match (1.0) legitimately outranks the project hit (0.99);
    # what matters is both indexers merged under one relevance order.
    assert [r['ref_id'] for r in refs] == ['episode:e1', 'gh:owner/repo']
    assert index_all('reproduce transformer', [], user_id=USER) == []


def test_project_indexer_shapes_and_ranks_github_hits(store):
    from lib.evolution.indexers import ProjectIndexer

    def fake_fetch(url, params):
        assert url.endswith('/search/repositories')
        assert params['per_page'] <= 8
        return {'items': [
            {'full_name': 'owner/popular', 'html_url': 'https://x/1',
             'score': 40.0, 'stargazers_count': 9000},
            {'full_name': 'owner/niche', 'html_url': 'https://x/2',
             'score': 20.0, 'stargazers_count': 3},
            {'no_full_name': True},
        ]}

    refs = ProjectIndexer(fetch=fake_fetch).index('transformer', user_id=USER)
    assert [r['ref_id'] for r in refs] == ['gh:owner/popular', 'gh:owner/niche']
    assert refs[0]['relevance'] == 1.0
    assert refs[1]['relevance'] == 0.5
    assert refs[0]['indexer'] == 'project'
    assert '9000 stars' in refs[0]['provenance']


def test_project_indexer_fails_soft_on_transport_errors(store):
    from lib.evolution.indexers import ProjectIndexer

    def broken(url, params):
        raise RuntimeError('network down')

    assert ProjectIndexer(fetch=broken).index('anything', user_id=USER) == []


def test_academic_indexer_wraps_existing_discovery(store):
    from lib.evolution.indexers import AcademicIndexer

    def fake_search(query, *, max_results):
        assert max_results <= 8
        return [
            {'arxiv_id': '2401.0001', 'title': 'Paper A', 'source': 'arxiv'},
            {'doi': '10.1/xyz', 'title': 'Paper B', 'source': 'crossref'},
            {'title': 'no identity, skipped'},
        ]

    refs = AcademicIndexer(search=fake_search).index('attention', user_id=USER)
    assert [r['ref_id'] for r in refs] == ['paper:2401.0001', 'paper:10.1/xyz']
    assert refs[0]['locator'] == 'https://arxiv.org/abs/2401.0001'
    assert refs[1]['locator'] == 'https://doi.org/10.1/xyz'
    assert refs[0]['relevance'] > refs[1]['relevance']
    broken = AcademicIndexer(search=lambda q, *, max_results: 1 / 0)
    assert broken.index('attention', user_id=USER) == []
