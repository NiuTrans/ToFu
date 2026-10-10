"""Recent settlements and exact parents remain visible beyond owner-key caps."""
from copy import deepcopy
import pytest
from lib.evolution import episodes, records, cost_model, self_model, holdout
from lib.evolution.contracts import EvolutionStateError
from lib.storage.errors import StorageError
from tests.evolution.test_evolution_system import _settle, USER
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def test_late_settlements_and_parents_drive_cost_model_self_model_and_holdout(store, record_property):
    for index in range(200):
        _settle(store, f'a-goal-{index:03}', f'a-episode-{index:03}', 'success')
    new_ids = set()
    for index in range(5):
        eid=f'z-episode-{index}'
        _settle(store, f'z-goal-{index}', eid, 'failure', domain='fresh', tokens=900)
        store.rows[records.NAMESPACE_EPISODES, f'{USER}:{eid}']['settled_at']=100+index
        new_ids.add(eid)
    assert not new_ids.intersection(row['episode_id'] for row in episodes.list_episodes(user_id=USER,limit=200))
    assert not any(row['goal_id'].startswith('z-goal') for row in records.list_records(
        records.NAMESPACE_GOALS, USER, kind='goal', limit=200))
    reads=[];query=store.query
    def measured(operation,payload):
        reads.append((operation,deepcopy(payload)))
        return query(operation,payload)
    store.query=measured
    with episodes.episode_read_window(user_id=USER) as reader:
        selected=reader(user_id=USER,limit=200)
        assert len(selected)==200 and new_ids.issubset(row['episode_id'] for row in selected)
        priors=cost_model.recompute_priors(user_id=USER,episode_reader=reader,goal_reader=reader.read_goal)
        fresh=next(row for row in priors if row['domain']=='fresh')
        assert fresh['samples']==5 and fresh['tokens_median']==900 and fresh['failure_rate']==1
        rows=self_model._episode_rows(user_id=USER,episode_reader=reader,goal_reader=reader.read_goal)
        assert sum(row['domain']=='fresh' for row in rows)==5
        suite=holdout.build_holdout_suite(user_id=USER,episode_reader=reader,goal_reader=reader.read_goal)
        assert sum(item['domain']=='fresh' for item in suite)==4
        assert len(reader.goals)==200
        with pytest.raises(EvolutionStateError):reader.read_goal('a-goal-000',user_id=USER)
        with pytest.raises(EvolutionStateError):reader.read_goal('z-goal-0',user_id=USER+1)
    episode_pages=[payload for op,payload in reads if op=='record.list' and payload['namespace']==records.NAMESPACE_EPISODES]
    assert len(episode_pages)==2
    assert all(payload['limit']==200 and payload['prefix']==f'{USER}:' for payload in episode_pages)
    goal_points=[payload for op,payload in reads if op=='record.get' and payload['namespace']==records.NAMESPACE_GOALS]
    assert len(goal_points)==5  # Missing prefix references are fetched once across consumers.
    record_property('exact_parent_rpcs',len(goal_points))
    record_property('original_episode_count',205)
    record_property('episode_page_rpcs',len(episode_pages))


def test_pending_records_do_not_displace_settled_evidence(store):
    _settle(store,'goal','settled','success')
    prototype=deepcopy(store.rows[records.NAMESPACE_EPISODES,f'{USER}:settled'])
    for index in range(205):
        eid=f'a-running-{index:03}'
        store.rows[records.NAMESPACE_EPISODES,f'{USER}:{eid}']={**deepcopy(prototype),
            'episode_id':eid,'status':'running','settled_at':None,'verdict':None}
    assert [row['episode_id'] for row in episodes.recent_settled_episodes(user_id=USER)]==['settled']


@pytest.mark.parametrize('corruption',['key','settlement_time'])
def test_original_integrity_failure_refuses_recent_view(store,corruption):
    _settle(store,'goal','episode','success')
    if corruption=='key':store.rows[records.NAMESPACE_EPISODES,f'{USER}:episode']['episode_id']='foreign-identity'
    else:store.rows[records.NAMESPACE_EPISODES,f'{USER}:episode']['settled_at']=None
    with pytest.raises(StorageError):episodes.recent_settled_episodes(user_id=USER)


def test_resource_refusal_retries_same_cursor_with_smaller_pages(store):
    for index in range(5):_settle(store,f'g{index}',f'e{index}','success')
    original=store.query;reads=[]
    def limited(operation,payload):
        if operation=='record.list' and payload['namespace']==records.NAMESPACE_EPISODES:
            reads.append(deepcopy(payload))
            if payload['limit']>2:
                raise StorageError('database_unavailable','versioned document page exceeds 8 MiB')
        return original(operation,payload)
    store.query=limited
    assert len(episodes.recent_settled_episodes(user_id=USER))==5
    assert all(row['after_key']=='' for row in reads if row['limit']>2)
    assert max(row['limit'] for row in reads)==200 and min(row['limit'] for row in reads)==1


def test_missing_or_foreign_parent_does_not_supply_learning_scope(store):
    _settle(store,'goal','episode','success')
    selected=episodes.recent_settled_episodes(user_id=USER)
    forged=deepcopy(store.rows.pop((records.NAMESPACE_GOALS,f'{USER}:goal')))
    forged['goal_id']='foreign'
    assert episodes.episode_goals(selected,user_id=USER)=={}
    with pytest.raises(StorageError):episodes.episode_goals(selected,user_id=USER,
        goal_reader=lambda *args,**kwargs:forged)


def test_parent_page_failure_is_retryable_without_losing_selected_episode_view(store):
    _settle(store,'goal','episode','success')
    original=store.query;attempts=[]
    def failed_first(operation,payload):
        if operation=='record.list' and payload['namespace']==records.NAMESPACE_GOALS:
            attempts.append(deepcopy(payload))
            if len(attempts)==1:raise StorageError('database_integrity','injected parent read failure')
        return original(operation,payload)
    store.query=failed_first
    with episodes.episode_read_window(user_id=USER) as reader:
        selected=reader(user_id=USER,limit=200)
        with pytest.raises(StorageError):episodes.episode_goals(selected,user_id=USER,goal_reader=reader.read_goal)
        assert reader.rows is not None and reader.goals=={}
        assert episodes.episode_goals(selected,user_id=USER,goal_reader=reader.read_goal)['goal']['goal_id']=='goal'
    assert len(attempts)==2


def test_oversized_parent_page_shrinks_and_missing_references_use_exact_reads(store):
    for index in range(5):_settle(store,f'g{index}',f'e{index}','success')
    original=store.query;limits=[]
    def limited(operation,payload):
        if operation=='record.list' and payload['namespace']==records.NAMESPACE_GOALS:
            limits.append(payload['limit'])
            if payload['limit']>2:raise StorageError('database_unavailable','versioned document page exceeds 8 MiB')
        return original(operation,payload)
    store.query=limited
    with episodes.episode_read_window(user_id=USER) as reader:
        selected=reader(user_id=USER,limit=200)
        assert len(episodes.episode_goals(selected,user_id=USER,goal_reader=reader.read_goal))==5
    assert limits[0]==200 and limits[-1]==1


def test_recent_window_reconciles_all_original_pages_after_selected_store_restart(storage, store, monkeypatch):
    _settle(store,'goal','prototype','success')
    prototype=deepcopy(store.rows[records.NAMESPACE_EPISODES,f'{USER}:prototype'])
    parent=deepcopy(store.rows[records.NAMESPACE_GOALS,f'{USER}:goal'])
    supervisor,_=storage
    monkeypatch.setattr(records,'_storage',lambda **kwargs:supervisor.client)
    records.put_record(records.NAMESPACE_GOALS,f'{USER}:goal',parent,kind='goal')
    for index in range(200):
        eid=f'a-episode-{index:03}'
        records.put_record(records.NAMESPACE_EPISODES,f'{USER}:{eid}',
            {**deepcopy(prototype),'episode_id':eid},kind='episode')
    records.put_record(records.NAMESPACE_EPISODES,f'{USER}:z-latest',
        {**deepcopy(prototype),'episode_id':'z-latest','settled_at':99},kind='episode')
    with episodes.episode_read_window(user_id=USER) as reader:
        selected=reader(user_id=USER,limit=200)
        assert len(selected)==200 and selected[0]['episode_id']=='z-latest'
        assert reader.read_goal('goal',user_id=USER)['goal_id']=='goal'
        supervisor.stop();supervisor.start()
        records.put_record(records.NAMESPACE_EPISODES,f'{USER}:zz-newest',
            {**deepcopy(prototype),'episode_id':'zz-newest','settled_at':100},kind='episode')
        assert reader(user_id=USER,limit=200)[0]['episode_id']=='z-latest'
    with episodes.episode_read_window(user_id=USER) as fresh:
        assert [row['episode_id'] for row in fresh(user_id=USER,limit=200)[:2]]==['zz-newest','z-latest']
