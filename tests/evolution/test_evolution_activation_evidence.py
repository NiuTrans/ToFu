"""Activation recomputes paired receipts through the same rollback boundary."""
from copy import deepcopy
import pytest
from lib import evolution
from lib.evolution import records,gate,holdout
from tests.evolution.test_evolution_holdout import _settle,_propose,USER
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark=pytest.mark.unit

@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def measured(store,*,loss=False,skip=False):
    _settle(store,'training','training','success')
    for index in range(3):_settle(store,f'g{index}',f'e{index}','success',settled_at=101+index)
    evolution.build_holdout_suite(user_id=USER)
    value=_propose(store,evidence='training')
    evolution.confirm_mutation(value['mutation_id'],user_id=USER)
    def paired(mutation,item):
        return {arm:{'outcome':outcome,'tokens':None,'wall_clock_seconds':1}
            for arm,outcome in [('baseline','pass' if loss else 'skip' if skip else 'fail'),('candidate','fail' if loss else 'pass')]}
    receipt=evolution.run_regression(value,user_id=USER,paired_probe=paired)
    return value,receipt


@pytest.mark.parametrize('invalid',['verdict','losses','scores','missing_case','duplicate_result','parent_binding','legacy_context','wrong_mutation','suite'])
def test_corrupt_paired_receipt_cannot_activate_even_with_waiver(store,invalid):
    value,receipt=measured(store,loss=invalid=='verdict')
    key=records.entity_key(value['mutation_id'],USER,kind='regression receipt')
    corrupted=store.rows[records.NAMESPACE_REGRESSIONS,key]
    if invalid=='verdict':corrupted['verdict']='pass'
    elif invalid=='losses':corrupted['comparison']['losses']=4
    elif invalid=='scores':corrupted['score']=0
    elif invalid=='missing_case':corrupted['comparison']['pairs'].pop()
    elif invalid=='duplicate_result':corrupted['item_results'][1]=deepcopy(corrupted['item_results'][0])
    elif invalid=='parent_binding':corrupted['paired_context_fingerprint']='0'*64
    elif invalid=='legacy_context':corrupted.pop('paired_context_fingerprint')
    elif invalid=='wrong_mutation':corrupted['mutation_id']='other-original'
    else:corrupted['suite_fingerprint']='0'*64
    before=deepcopy(store.rows)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.activate_mutation(value['mutation_id'],user_id=USER,allow_unmeasurable=True)
    assert store.rows==before and gate._load(value['mutation_id'],user_id=USER)['status']=='confirmed'


def test_valid_gain_activates_but_real_loss_cannot_be_waived(store):
    value,_=measured(store)
    assert evolution.activate_mutation(value['mutation_id'],user_id=USER)['status']=='active'
    second,_=measured(store,loss=True)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.activate_mutation(second['mutation_id'],user_id=USER,allow_unmeasurable=True)


def test_consistent_unknown_pairing_still_requires_explicit_operator_waiver(store):
    value,receipt=measured(store,skip=True)
    assert receipt['verdict']=='unmeasurable'
    with pytest.raises(evolution.EvolutionStateError):evolution.activate_mutation(value['mutation_id'],user_id=USER)
    assert evolution.activate_mutation(value['mutation_id'],user_id=USER,allow_unmeasurable=True)['status']=='active'


def test_raw_malformed_monitor_receipt_stays_invalid_without_disrupting_history_report(store):
    from tests.evolution.test_evolution_rollback_evidence import monitored
    value=monitored(store)
    key=records.entity_key(value['mutation_id'],USER,kind='regression receipt')
    store.rows[records.NAMESPACE_REGRESSIONS,key]['created_at']='not-an-integer'
    result=evolution.monitor_active_mutations(user_id=USER,now=200)[0]
    assert result['breach'] is False and result['rollback_evidence']=='invalid_regression_receipt'


def test_bound_paired_activation_survives_selected_store_restart(storage,store,monkeypatch):
    value,_=measured(store);originals=deepcopy(store.rows);supervisor,_=storage
    monkeypatch.setattr(records,'_storage',lambda **kwargs:supervisor.client)
    for (namespace,key),row in originals.items():records.put_record(namespace,key,row,kind='fixture')
    supervisor.stop();supervisor.start()
    assert evolution.activate_mutation(value['mutation_id'],user_id=USER)['status']=='active'


def test_future_paired_measurement_cannot_activate_before_its_observation(store):
    value,receipt=measured(store)
    with pytest.raises(evolution.EvolutionStateError,match='future'):
        evolution.activate_mutation(value['mutation_id'],user_id=USER,now=receipt['created_at']-1,
            allow_unmeasurable=True)
    assert gate._load(value['mutation_id'],user_id=USER)['status']=='confirmed'
