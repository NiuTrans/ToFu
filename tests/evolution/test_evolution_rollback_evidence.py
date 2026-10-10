"""History shifts cannot authorize automatic rollback without matched evidence."""
from copy import deepcopy
import pytest
from lib import evolution
from lib.evolution import holdout,records,gate
from tests.evolution.test_evolution_holdout import _settle,_propose,USER
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark=pytest.mark.unit

@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def monitored(store,after='failure'):
    _settle(store,'training','training','success',settled_at=40)
    for index in range(3):_settle(store,f'before-{index}',f'before-{index}','success',settled_at=50+index)
    evolution.build_holdout_suite(user_id=USER,now=60)
    value=_propose(store,evidence='training',now=100)
    evolution.confirm_mutation(value['mutation_id'],user_id=USER,now=101)
    evolution.run_regression(value,user_id=USER,probe=lambda mutation,item:'pass',now=102)
    evolution.activate_mutation(value['mutation_id'],user_id=USER,now=103)
    for index in range(3):_settle(store,f'after-{index}',f'after-{index}',after,settled_at=130+index)
    return value


def paired_receipt(value,*,now=150,skip=False):
    def paired(mutation,item):
        return {arm:{'outcome':outcome,'tokens':None,'wall_clock_seconds':1}
            for arm,outcome in [('baseline','skip' if skip else 'pass'),('candidate','fail')]}
    return evolution.run_regression(value,user_id=USER,paired_probe=paired,now=now)


def test_observed_decline_without_matched_measurement_does_not_revert(store):
    value=monitored(store)
    result=evolution.monitor_active_mutations(user_id=USER,now=200)[0]
    assert result['observed_decline'] is True and result['before_rate']==1 and result['after_rate']==0
    assert result['breach'] is False and result['rollback_evidence']=='unbound_or_stale_regression'
    assert gate._load(value['mutation_id'],user_id=USER)['status']=='active'


def test_fresh_complete_paired_loss_can_revert_without_temporal_decline(store):
    value=monitored(store,after='success');paired_receipt(value)
    result=evolution.monitor_active_mutations(user_id=USER,now=200)[0]
    assert result['observed_decline'] is False and result['breach'] is True
    assert result['rollback_evidence']=='current_paired_regression'
    assert gate._load(value['mutation_id'],user_id=USER)['status']=='rolled_back'


@pytest.mark.parametrize('invalid',['old','future','foreign','changed_mutation','changed_suite','incomplete','forged_comparison','forged_score','duplicate_results','unbound_parent','legacy_context'])
def test_unverified_paired_receipts_never_authorize_rollback(store,invalid):
    value=monitored(store)
    paired_receipt(value,now=100 if invalid=='old' else 250 if invalid=='future' else 150,
                   skip=invalid=='incomplete')
    key=records.entity_key(value['mutation_id'],USER,kind='regression receipt')
    receipt=store.rows[records.NAMESPACE_REGRESSIONS,key]
    if invalid=='foreign':receipt['mutation_id']='another-original'
    elif invalid=='changed_mutation':receipt['mutation_fingerprint']='0'*64
    elif invalid=='changed_suite':receipt['suite_fingerprint']='0'*64
    elif invalid=='forged_comparison':receipt['comparison']['losses']=0
    elif invalid=='forged_score':receipt['score']=1
    elif invalid=='duplicate_results':receipt['item_results'][1]=deepcopy(receipt['item_results'][0])
    elif invalid=='unbound_parent':receipt['paired_context_fingerprint']='0'*64
    elif invalid=='legacy_context':receipt.pop('paired_context_fingerprint')
    before=deepcopy(store.rows)
    result=evolution.monitor_active_mutations(user_id=USER,now=200)[0]
    assert result['observed_decline'] is True and result['breach'] is False
    assert gate._load(value['mutation_id'],user_id=USER)['status']=='active'
    assert store.rows==before


def test_missing_history_baseline_stays_unknown_instead_of_half_success(store):
    value=monitored(store)
    for (namespace,key),row in list(store.rows.items()):
        if namespace==records.NAMESPACE_EPISODES and row['settled_at']<103:store.rows.pop((namespace,key))
    result=evolution.monitor_active_mutations(user_id=USER,now=200)[0]
    assert result['before_samples']==0 and result['before_rate'] is None
    assert result['observed_decline'] is False and result['breach'] is False


def test_post_activation_paired_evidence_survives_selected_store_restart(storage,store,monkeypatch):
    value=monitored(store);paired_receipt(value)
    originals=deepcopy(store.rows);supervisor,_=storage
    monkeypatch.setattr(records,'_storage',lambda **kwargs:supervisor.client)
    for (namespace,key),row in originals.items():records.put_record(namespace,key,row,kind='fixture')
    supervisor.stop();supervisor.start()
    result=evolution.monitor_active_mutations(user_id=USER,now=200)[0]
    assert result['breach'] is True and result['rollback_evidence']=='current_paired_regression'
    assert gate._load(value['mutation_id'],user_id=USER)['status']=='rolled_back'
