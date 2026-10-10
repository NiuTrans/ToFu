"""Version and rollback discovery are complete beyond bounded metadata pages."""
from copy import deepcopy
import pytest
from lib import evolution
from lib.evolution import gate,records
from lib.storage.errors import StorageError
from tests.evolution.test_evolution_gate import _settled_episode,USER

pytestmark=pytest.mark.unit

@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def historical(store):
    eid=_settled_episode(store)
    template={'schema_version':1,'mutation_id':'template','kind':'prompt_variant',
        'payload':{'text':'Scoped candidate'},'evidence_episode_ids':[eid],'status':'proposed',
        'version':1,'parent_version':0,'requires_confirmation':True,'created_at':1,'updated_at':1}
    for n in range(140):
        value={**deepcopy(template),'mutation_id':f'a-history-{n:03}','version':n+1}
        records.put_record(records.NAMESPACE_MUTATIONS,f"{USER}:{value['mutation_id']}",value,kind='mutation')
    active={**deepcopy(template),'mutation_id':'z-active','version':141,'status':'active'}
    latest={**deepcopy(template),'mutation_id':'z-latest','version':300}
    for value in (active,latest):records.put_record(records.NAMESPACE_MUTATIONS,f"{USER}:{value['mutation_id']}",value,kind='mutation')
    return eid,active,latest


def test_complete_history_sets_next_version_and_keeps_active_parent_visible(store):
    eid,active,_=historical(store)
    result=evolution.propose_mutation('prompt_variant',{'text':'Improved'},[eid],user_id=USER)
    assert result['version']==301 and result['parent_version']==141
    listed=evolution.list_mutations(user_id=USER,kind='prompt_variant')
    assert len(listed)==gate.MAX_MUTATIONS
    assert active['mutation_id'] in {row['mutation_id'] for row in listed}
    assert result['mutation_id'] in {row['mutation_id'] for row in listed}


def test_activation_and_rollback_reach_parent_after_first_history_page(store):
    eid,parent,_=historical(store)
    result=evolution.propose_mutation('prompt_variant',{'text':'Improved'},[eid],user_id=USER)
    evolution.confirm_mutation(result['mutation_id'],user_id=USER)
    evolution.build_holdout_suite(user_id=USER)
    evolution.run_regression(result,user_id=USER,probe=lambda mutation,item:'pass')
    evolution.activate_mutation(result['mutation_id'],user_id=USER)
    assert gate._load(parent['mutation_id'],user_id=USER)['status']=='rolled_back'
    evolution.rollback_mutation(result['mutation_id'],user_id=USER)
    assert gate._load(parent['mutation_id'],user_id=USER)['status']=='active'


def test_late_page_failure_refuses_proposal_without_publishing_partial_history(store):
    eid,_,_=historical(store);query=store.query;before=store.writes
    def failed(operation,payload):
        if operation=='record.list' and payload['namespace']==records.NAMESPACE_MUTATIONS and payload.get('after_key'):
            raise StorageError('database_integrity','unavailable second page')
        return query(operation,payload)
    store.query=failed
    with pytest.raises(StorageError):evolution.propose_mutation('prompt_variant',{'text':'Improved'},[eid],user_id=USER)
    assert store.writes==before


def test_ambiguous_active_versions_refuse_instead_of_choosing_a_parent(store):
    eid,active,_=historical(store)
    duplicate={**deepcopy(active),'mutation_id':'zz-other-active','version':400}
    records.put_record(records.NAMESPACE_MUTATIONS,f'{USER}:zz-other-active',duplicate,kind='mutation')
    before=store.writes
    with pytest.raises(evolution.EvolutionStateError,match='ambiguous'):
        evolution.propose_mutation('prompt_variant',{'text':'Improved'},[eid],user_id=USER)
    assert store.writes==before
