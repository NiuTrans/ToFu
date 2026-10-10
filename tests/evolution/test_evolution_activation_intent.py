"""Policy activation intent survives interruption and fences competing rejection."""
from copy import deepcopy
import pytest
from lib import evolution
from lib.evolution import gate, records, policy_transitions
from lib.storage.errors import StorageError
from tests.evolution.test_evolution_policy_rollback import candidate, activate, USER
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit

@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def interrupt(store, monkeypatch, mutation, phase):
    command=store.command
    def fail(operation,payload,command_id):
        if (phase=='journal' and payload['namespace']==policy_transitions.NAMESPACE
            or phase=='effect' and payload['namespace']==records.NAMESPACE_GATE_POLICY
            or phase=='metadata' and payload['namespace']==records.NAMESPACE_MUTATIONS
                and payload['value']['mutation_id']==mutation['mutation_id']
                and payload['value']['status']=='active'):
            raise StorageError('database_internal','interrupted activation')
        return command(operation,payload,command_id)
    monkeypatch.setattr(store,'command',fail)
    with pytest.raises(StorageError):evolution.activate_mutation(mutation['mutation_id'],user_id=USER)
    monkeypatch.setattr(store,'command',command)


@pytest.mark.parametrize('phase',['journal','effect','metadata'])
def test_interrupted_admitted_policy_refuses_rejection_and_maintenance_finishes(store,monkeypatch,phase):
    value=candidate(store,{'min_lift':0.4})
    interrupt(store,monkeypatch,value,phase)
    pending=gate._load(value['mutation_id'],user_id=USER)
    assert pending['status']=='confirmed' and 'activation_intent' in pending
    before=deepcopy(store.rows)
    with pytest.raises(evolution.EvolutionStateError,match='already admitted'):
        evolution.reject_mutation(value['mutation_id'],user_id=USER)
    assert store.rows==before
    # Receipt refreshes cannot revoke the already persisted admitted operation.
    key=records.entity_key(value['mutation_id'],USER,kind='regression receipt')
    store.rows[records.NAMESPACE_REGRESSIONS,key]['verdict']='fail'
    report=evolution.run_maintenance_cycle(user_id=USER,include_refetch=False)
    assert report['steps']['mutation_recovery']=={'ok':True,'result':[value['mutation_id']]}
    completed=gate._load(value['mutation_id'],user_id=USER)
    assert completed['status']=='active' and 'activation_intent' not in completed
    assert completed['updated_at']==pending['activation_intent']['started_at']
    assert evolution.get_thresholds(user_id=USER)['min_lift']==0.4


def test_rejection_winning_before_intent_leaves_no_policy_effect(store,monkeypatch):
    value=candidate(store,{'min_lift':0.4});original=gate.put_record;competed=False
    def interleave(namespace,key,row,**kwargs):
        nonlocal competed
        if 'activation_intent' in row and not competed:
            competed=True
            evolution.reject_mutation(value['mutation_id'],user_id=USER)
        return original(namespace,key,row,**kwargs)
    monkeypatch.setattr(gate,'put_record',interleave)
    with pytest.raises(StorageError):evolution.activate_mutation(value['mutation_id'],user_id=USER)
    assert gate._load(value['mutation_id'],user_id=USER)['status']=='rejected'
    assert not any(namespace in (policy_transitions.NAMESPACE,records.NAMESPACE_GATE_POLICY) for namespace,key in store.rows)


def test_lost_intent_ack_recovers_exact_once(store,monkeypatch):
    value=candidate(store,{'min_lift':0.4});command=store.command;lost=[]
    def lose(operation,payload,command_id):
        result=command(operation,payload,command_id)
        if 'activation_intent' in payload.get('value',{}) and not lost:
            lost.append(True);raise StorageError('database_timeout','intent acknowledgement lost')
        return result
    monkeypatch.setattr(store,'command',lose)
    assert evolution.activate_mutation(value['mutation_id'],user_id=USER)['status']=='active'
    assert lost==[True]


def test_unadmitted_failed_receipt_never_mints_intent(store):
    value=candidate(store,{'min_lift':0.4})
    key=records.entity_key(value['mutation_id'],USER,kind='regression receipt')
    store.rows[records.NAMESPACE_REGRESSIONS,key]['verdict']='fail'
    with pytest.raises(evolution.EvolutionStateError):evolution.activate_mutation(value['mutation_id'],user_id=USER)
    assert 'activation_intent' not in gate._load(value['mutation_id'],user_id=USER)
    assert gate.recover_policy_activations(user_id=USER)==[]


def test_pending_effect_restoration_survives_selected_store_restart(storage,store,monkeypatch):
    from tests.evolution.test_evolution_gate import _settled_episode
    _settled_episode(store);originals=deepcopy(store.rows);supervisor,_=storage
    monkeypatch.setattr(records,'_storage',lambda **kwargs:supervisor.client)
    for (namespace,key),row in originals.items():records.put_record(namespace,key,row,kind='fixture')
    first=activate(store,{'min_lift':0.3});second=candidate(store,{'min_lift':0.4})
    save=gate._save
    def fail_target(row,**kwargs):
        if row['mutation_id']==second['mutation_id'] and row['status']=='active':
            raise StorageError('database_internal','target metadata interrupted')
        return save(row,**kwargs)
    with monkeypatch.context() as local:
        local.setattr(gate,'_save',fail_target)
        with pytest.raises(StorageError):evolution.activate_mutation(second['mutation_id'],user_id=USER)
    supervisor.stop();supervisor.start()
    assert gate.recover_policy_activations(user_id=USER)==[second['mutation_id']]
    assert gate._load(first['mutation_id'],user_id=USER)['status']=='rolled_back'
    assert gate._load(second['mutation_id'],user_id=USER)['status']=='active'
    assert evolution.get_thresholds(user_id=USER)['min_lift']==0.4


def test_changed_admitted_controls_are_refused_without_new_effect(store,monkeypatch):
    value=candidate(store,{'min_lift':0.4});interrupt(store,monkeypatch,value,'journal')
    key=records.entity_key(value['mutation_id'],USER,kind='mutation')
    store.rows[records.NAMESPACE_MUTATIONS,key]['payload']['policy']='{"min_lift":0.8}'
    before=deepcopy(store.rows)
    with pytest.raises(evolution.EvolutionStateError,match='controls changed'):
        gate.recover_policy_activations(user_id=USER)
    assert store.rows==before


def test_recovery_uses_original_history_beyond_public_projection_and_owner_scope(store,monkeypatch):
    value=candidate(store,{'min_lift':0.4});interrupt(store,monkeypatch,value,'journal')
    pending=gate._load(value['mutation_id'],user_id=USER)
    for index in range(130):
        other=dict(value,mutation_id=f'later-rejected-{index}',status='rejected',
            version=index+2,updated_at=pending['updated_at']+1)
        records.put_record(records.NAMESPACE_MUTATIONS,
            records.entity_key(other['mutation_id'],USER,kind='mutation'),other,kind='fixture')
    assert value['mutation_id'] not in {row['mutation_id'] for row in evolution.list_mutations(user_id=USER)}
    before=deepcopy(store.rows)
    assert gate.recover_policy_activations(user_id=USER+1)==[]
    assert store.rows==before
    assert gate.recover_policy_activations(user_id=USER)==[value['mutation_id']]


def test_recovery_error_is_visible_and_does_not_stop_other_maintenance_steps(store,monkeypatch):
    value=candidate(store,{'min_lift':0.4});interrupt(store,monkeypatch,value,'journal')
    key=records.entity_key(value['mutation_id'],USER,kind='mutation')
    store.rows[records.NAMESPACE_MUTATIONS,key]['activation_intent']['context_fingerprint']='0'*64
    report=evolution.run_maintenance_cycle(user_id=USER,include_refetch=False)
    assert report['ok'] is False
    assert report['steps']['mutation_recovery']['ok'] is False
    assert report['steps']['priors']['ok'] and report['steps']['monitoring']['ok']
    assert gate._load(value['mutation_id'],user_id=USER)['status']=='confirmed'


@pytest.mark.parametrize('patch',[{'kind':'routing_change'},{'status':'active'},
    {'activation_intent':{'started_at':True,'context_fingerprint':'0'*64}}])
def test_activation_intent_contract_rejects_wrong_kind_status_or_boolean_clock(store,monkeypatch,patch):
    from lib.evolution.contracts import validate_mutation
    value=candidate(store,{'min_lift':0.4});interrupt(store,monkeypatch,value,'journal')
    pending=gate._load(value['mutation_id'],user_id=USER)
    with pytest.raises(ValueError):validate_mutation({**pending,**patch})
