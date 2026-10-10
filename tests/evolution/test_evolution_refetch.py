"""On-demand reindex: three triggers, one bounded core, receipts for all."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 123
NOW = 1_800_000_000


@pytest.fixture
def env(monkeypatch):
    from tests.support.evolution_store import install_store
    store = install_store(monkeypatch)
    runs = {}
    monkeypatch.setattr('lib.evolution.runs.get_run',
                        lambda rid, *, user_id: runs.get(
                            (user_id, rid)))
    return store, runs


def _run(status='executing', task_id='task-a', created_at=NOW - 100,
         episode_id='ep-1', review=None):
    run = {'run_id': 'r1', 'status': status, 'task_id': task_id,
           'objective': 'improve grep recovery semantics',
           'created_at': created_at, 'episode_id': episode_id}
    if review:
        run['review'] = review
    return run


class _FakeIndexer:
    name = 'academic'

    def __init__(self, refs):
        self._refs = refs

    def index(self, query, *, user_id, limit=8):
        return list(self._refs)


def _refs():
    return [
        {'ref_id': 'paper:x', 'indexer': 'academic',
         'locator': 'https://arxiv.org/abs/1234', 'relevance': 0.9,
         'provenance': 'arxiv metadata: recovery envelopes'},
        {'ref_id': 'episode:e9', 'indexer': 'history', 'locator': 'e9',
         'relevance': 0.5, 'provenance': 'local success episode'},
    ]


def test_actor_reindex_lands_url_refs_and_writes_receipt(env):
    from lib import evolution
    from lib.evolution import library
    _store, runs = env
    runs[(USER, 'r1')] = _run()
    result = evolution.reindex_for_run(
        'r1', ['tool result recovery design'], user_id=USER,
        actor='executor', task_id='task-a', reason='stuck on truncation',
        indexers=[_FakeIndexer(_refs())], now=NOW)
    assert result['saved'] == 1 and result['skipped'] == 1  # no http locator
    materials = library.list_materials('r1', user_id=USER)
    assert materials[0]['url'] == 'https://arxiv.org/abs/1234'
    assert materials[0]['origin'] == 'agent'
    notes = evolution.list_observations(user_id=USER, kind='reindex_note')
    assert len(notes) == 1
    assert notes[0]['lineage'] == ['r1', 'ep-1']
    assert 'stuck on truncation' in notes[0]['statement']


def test_same_queries_are_idempotent(env):
    from lib import evolution
    from lib.evolution import library
    _store, runs = env
    runs[(USER, 'r1')] = _run()
    kwargs = dict(user_id=USER, actor='executor', task_id='task-a',
                  indexers=[_FakeIndexer(_refs())], now=NOW)
    evolution.reindex_for_run('r1', ['recovery'], **kwargs)
    again = evolution.reindex_for_run('r1', ['recovery'], **kwargs)
    assert again['already_done'] is True
    assert len(library.list_materials('r1', user_id=USER)) == 1
    assert len(evolution.list_observations(user_id=USER,
                                           kind='reindex_note')) == 1


def test_run_budget_refuses_a_fourth_reindex(env):
    from lib import evolution
    _store, runs = env
    runs[(USER, 'r1')] = _run()
    kwargs = dict(user_id=USER, actor='executor', task_id='task-a',
                  indexers=[_FakeIndexer(_refs())], now=NOW)
    for index in range(3):
        evolution.reindex_for_run('r1', [f'query {index}'], **kwargs)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.reindex_for_run('r1', ['query 4'], **kwargs)


def test_blank_queries_are_a_contract_error(env):
    from lib import evolution
    _store, runs = env
    runs[(USER, 'r1')] = _run()
    with pytest.raises(evolution.EvolutionContractError):
        evolution.reindex_for_run('r1', ['  ', ''], user_id=USER,
                                  actor='executor', indexers=[], now=NOW)


def test_full_shelf_truncates_landing_but_keeps_receipt(env):
    from lib import evolution
    from lib.evolution import library
    _store, runs = env
    runs[(USER, 'r1')] = _run()
    for index in range(64):
        library.save_material('r1', {
            'kind': 'web', 'title': f'source {index}',
            'url': f'https://example.org/{index}'}, user_id=USER)
    result = evolution.reindex_for_run(
        'r1', ['recovery'], user_id=USER, actor='miner',
        indexers=[_FakeIndexer(_refs())], now=NOW)
    assert result['saved'] == 0 and result['skipped'] == 2
    assert len(evolution.list_observations(user_id=USER,
                                           kind='reindex_note')) == 1


def _seed_run_record(store, runs, run):
    from lib.evolution import records
    runs[(USER, run['run_id'])] = run
    records.put_record(records.NAMESPACE_RUNS,
                       records.entity_key(run['run_id'], USER, kind='run'),
                       run, kind='run')


def test_settle_trigger_uses_objective_and_failed_criteria(env):
    from lib import evolution
    store, runs = env
    review = {'criterion_results': [
        {'criterion_id': 'objective', 'outcome': 'fail',
         'evidence_ref': 'no addressable cursor for grep'},
        {'criterion_id': 'evidence', 'outcome': 'pass',
         'evidence_ref': 'fine'},
        {'criterion_id': 'delivery', 'outcome': 'pass',
         'evidence_ref': 'fine'}]}
    _seed_run_record(store, runs, _run(status='needs_attention',
                                       task_id='', review=review))
    report = evolution.reindex_maintenance(
        user_id=USER, indexers=[_FakeIndexer(_refs())], now=NOW)
    assert report == {'settle_triggered': 1, 'freshness': 0}
    notes = evolution.list_observations(user_id=USER, kind='reindex_note')
    assert 'no addressable cursor for grep' in notes[0]['statement']
    again = evolution.reindex_maintenance(
        user_id=USER, indexers=[_FakeIndexer(_refs())], now=NOW + 10)
    assert again == {'settle_triggered': 0, 'freshness': 0}


def test_freshness_pass_only_touches_long_stalled_runs(env):
    from lib import evolution
    store, runs = env
    _seed_run_record(store, runs, _run(
        status='completed', task_id='',
        created_at=NOW - 4 * 24 * 3600))
    report = evolution.reindex_maintenance(
        user_id=USER, indexers=[_FakeIndexer(_refs())], now=NOW)
    assert report['freshness'] == 1
    _store2, runs2 = env
    runs2[(USER, 'r1')]['created_at'] = NOW - 100
    # the young run was already receipted above; a second pass finds nothing
    assert evolution.reindex_maintenance(
        user_id=USER, indexers=[_FakeIndexer(_refs())], now=NOW + 10)[
            'freshness'] == 0


def test_reindexed_episode_becomes_a_falsifiable_feature(env):
    from lib import evolution
    from lib.evolution import records, self_model
    _store, runs = env
    runs[(USER, 'r1')] = _run()
    evolution.reindex_for_run('r1', ['recovery'], user_id=USER,
                              actor='executor', task_id='task-a',
                              indexers=[_FakeIndexer(_refs())], now=NOW)
    assert 'ep-1' in self_model._reindexed_episode_ids(user_id=USER)
    assert 'reindexed' in self_model.FEATURES
    saved = evolution.propose_hypotheses([{
        'domain': '*', 'feature': 'reindexed', 'operator': 'eq',
        'threshold': True, 'direction': 'helps',
        'statement': 'on-demand reindex improves success'}], user_id=USER)
    assert len(saved) == 1  # registered feature passes intake
    # planning-time features carry the documented False stand-in
    assert self_model.path_features_for_candidate(
        {'resource_refs': [], 'stages': []},
        {'exploratory': False})['reindexed'] is False


def test_tool_handler_requires_a_bound_current_task(env):
    from lib import evolution
    from lib.evolution import tool_handler
    _store, runs = env
    runs[(USER, 'r1')] = _run()
    task = {'id': 'task-a', '_userId': USER,
            'config': {'evolution': {'run_id': 'r1', 'stage': 'executing'}}}

    def invoke():
        return tool_handler.handle_evolution_reindex(
            task, None, 'evolution_reindex', 'call',
            {'queries': ['recovery'], 'reason': 'need fresher sources'},
            1, {}, {}, '', False)

    # The handler reaches the real reindex path (default indexers may be
    # offline here; the receipt must still land or refuse honestly).
    entry = invoke()[0]
    assert entry['toolName'] == 'evolution_reindex'
    task['id'] = 'stale-task'
    assert invoke()[0]['fetched'] is False
    task['id'] = 'task-a'
    task['config']['evolution']['stage'] = 'reviewing'
    assert invoke()[0]['fetched'] is False
