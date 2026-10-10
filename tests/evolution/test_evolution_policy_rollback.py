"""Policy rollback restores the exact frozen vector across ACK and process loss."""
from copy import deepcopy
import json
import pytest
from lib import evolution
from lib.evolution import gate,gate_policy,records,policy_transitions
from lib.storage.errors import StorageError
from tests.evolution.test_evolution_gate import _settled_episode,USER
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark=pytest.mark.unit

@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def candidate(store, overrides):
    eid=_settled_episode(store)
    value=evolution.propose_mutation('budget_policy',{'policy':json.dumps(overrides)},[eid],user_id=USER)
    evolution.confirm_mutation(value['mutation_id'],user_id=USER)
    evolution.build_holdout_suite(user_id=USER)
    evolution.run_regression(value,user_id=USER,probe=lambda mutation,item:'pass')
    return value


def activate(store, overrides):
    value=candidate(store,overrides)
    return evolution.activate_mutation(value['mutation_id'],user_id=USER)


def test_partial_overrides_restore_full_parent_and_then_frozen_defaults(store):
    first=activate(store,{'min_lift':0.4})
    before=evolution.get_thresholds(user_id=USER)
    second=activate(store,{'regression_margin':0.2})
    assert evolution.get_thresholds(user_id=USER)['regression_margin']==0.2
    evolution.rollback_mutation(second['mutation_id'],user_id=USER)
    assert evolution.get_thresholds(user_id=USER)==before
    assert gate._load(first['mutation_id'],user_id=USER)['status']=='active'
    evolution.rollback_mutation(first['mutation_id'],user_id=USER)
    assert evolution.get_thresholds(user_id=USER)==gate_policy.DEFAULTS


def test_failed_effect_keeps_parent_active_and_can_retry_same_preparation(store,monkeypatch):
    first=activate(store,{'min_lift':0.3});second=candidate(store,{'min_lift':0.4})
    command=store.command
    def fail_effect(operation,payload,command_id):
        if payload['namespace']==records.NAMESPACE_GATE_POLICY:raise StorageError('database_internal','effect refused')
        return command(operation,payload,command_id)
    monkeypatch.setattr(store,'command',fail_effect)
    with pytest.raises(StorageError):evolution.activate_mutation(second['mutation_id'],user_id=USER)
    assert gate._load(first['mutation_id'],user_id=USER)['status']=='active'
    assert gate._load(second['mutation_id'],user_id=USER)['status']=='confirmed'
    assert evolution.get_thresholds(user_id=USER)['min_lift']==0.3
    monkeypatch.setattr(store,'command',command)
    evolution.activate_mutation(second['mutation_id'],user_id=USER)
    assert evolution.get_thresholds(user_id=USER)['min_lift']==0.4


@pytest.mark.parametrize('phase',['journal','effect','rollback_journal','rollback_effect'])
def test_committed_lost_ack_uses_exact_readback_without_repeating_policy_effect(store,monkeypatch,phase):
    value=candidate(store,{'min_lift':0.4})
    command=store.command;lost=[]
    def lose(operation,payload,command_id):
        result=command(operation,payload,command_id)
        journal=payload['namespace']==policy_transitions.NAMESPACE
        effect=payload['namespace']==records.NAMESPACE_GATE_POLICY
        rollback='rollback' in payload.get('value',{})
        wanted=(phase=='journal' and journal and not rollback or phase=='effect' and effect
            or phase=='rollback_journal' and journal and rollback or phase=='rollback_effect' and effect)
        if wanted and not lost:
            lost.append(True);raise StorageError('database_timeout','committed acknowledgement lost')
        return result
    if phase.startswith('rollback'):
        evolution.activate_mutation(value['mutation_id'],user_id=USER)
        monkeypatch.setattr(store,'command',lose)
        evolution.rollback_mutation(value['mutation_id'],user_id=USER)
        assert evolution.get_thresholds(user_id=USER)==gate_policy.DEFAULTS
    else:
        monkeypatch.setattr(store,'command',lose)
        evolution.activate_mutation(value['mutation_id'],user_id=USER)
        assert evolution.get_thresholds(user_id=USER)['min_lift']==0.4
    assert lost==[True]


def test_rollback_metadata_failure_resumes_from_durable_restored_effect(store,monkeypatch):
    first=activate(store,{'min_lift':0.3});second=activate(store,{'regression_margin':0.2})
    command=store.command
    def fail_metadata(operation,payload,command_id):
        if payload['namespace']==records.NAMESPACE_MUTATIONS and payload['value']['status']=='rolled_back':
            raise StorageError('database_internal','metadata refused')
        return command(operation,payload,command_id)
    monkeypatch.setattr(store,'command',fail_metadata)
    with pytest.raises(StorageError):evolution.rollback_mutation(second['mutation_id'],user_id=USER)
    assert evolution.get_thresholds(user_id=USER)['regression_margin']==gate_policy.DEFAULTS['regression_margin']
    monkeypatch.setattr(store,'command',command)
    evolution.rollback_mutation(second['mutation_id'],user_id=USER)
    assert gate._load(first['mutation_id'],user_id=USER)['status']=='active'
    assert gate._load(second['mutation_id'],user_id=USER)['status']=='rolled_back'


def test_unrelated_policy_change_is_not_overwritten_by_rollback(store):
    value=activate(store,{'min_lift':0.4})
    key=records.entity_key('active',USER,kind='gate policy')
    changed=deepcopy(store.rows[records.NAMESPACE_GATE_POLICY,key]);changed['source_mutation_id']='another-owner-claim'
    records.put_record(records.NAMESPACE_GATE_POLICY,key,changed,kind='gate policy')
    with pytest.raises(evolution.EvolutionStateError):evolution.rollback_mutation(value['mutation_id'],user_id=USER)
    assert store.rows[records.NAMESPACE_GATE_POLICY,key]==changed
    assert gate._load(value['mutation_id'],user_id=USER)['status']=='active'


def test_restart_retains_preimage_and_restores_exact_policy(storage,store,monkeypatch):
    # Seed valid owned originals, then use only the selected semantic authority.
    eid=_settled_episode(store);originals=deepcopy(store.rows)
    supervisor,_=storage
    monkeypatch.setattr(records,'_storage',lambda **kwargs:supervisor.client)
    for (namespace,key),value in originals.items():records.put_record(namespace,key,value,kind='fixture')
    first=activate(store,{'min_lift':0.3})
    before=evolution.get_thresholds(user_id=USER)
    second=activate(store,{'regression_margin':0.2})
    supervisor.stop();supervisor.start()
    evolution.rollback_mutation(second['mutation_id'],user_id=USER)
    assert evolution.get_thresholds(user_id=USER)==before
    assert gate._load(first['mutation_id'],user_id=USER)['status']=='active'


def test_partial_activation_metadata_failure_repairs_without_reapplying_effect(store,monkeypatch):
    first=activate(store,{'min_lift':0.3});second=candidate(store,{'min_lift':0.4})
    command=store.command
    def fail_target(operation,payload,command_id):
        if payload['namespace']==records.NAMESPACE_MUTATIONS and payload['value']['mutation_id']==second['mutation_id'] and payload['value']['status']=='active':
            raise StorageError('database_internal','target metadata refused')
        return command(operation,payload,command_id)
    monkeypatch.setattr(store,'command',fail_target)
    with pytest.raises(StorageError):evolution.activate_mutation(second['mutation_id'],user_id=USER)
    assert gate._load(first['mutation_id'],user_id=USER)['status']=='rolled_back'
    assert gate._load(second['mutation_id'],user_id=USER)['status']=='confirmed'
    version=store.versions[records.NAMESPACE_GATE_POLICY,records.entity_key('active',USER,kind='gate policy')]
    monkeypatch.setattr(store,'command',command)
    evolution.activate_mutation(second['mutation_id'],user_id=USER)
    assert store.versions[records.NAMESPACE_GATE_POLICY,records.entity_key('active',USER,kind='gate policy')]==version


def test_parent_metadata_failure_can_complete_interrupted_rollback(store,monkeypatch):
    first=activate(store,{'min_lift':0.3});second=activate(store,{'regression_margin':0.2})
    command=store.command
    def fail_parent(operation,payload,command_id):
        if payload['namespace']==records.NAMESPACE_MUTATIONS and payload['value']['mutation_id']==first['mutation_id'] and payload['value']['status']=='active':
            raise StorageError('database_internal','parent metadata refused')
        return command(operation,payload,command_id)
    monkeypatch.setattr(store,'command',fail_parent)
    with pytest.raises(StorageError):evolution.rollback_mutation(second['mutation_id'],user_id=USER)
    assert gate._load(second['mutation_id'],user_id=USER)['status']=='rolled_back'
    assert gate._load(first['mutation_id'],user_id=USER)['status']=='rolled_back'
    monkeypatch.setattr(store,'command',command)
    evolution.rollback_mutation(second['mutation_id'],user_id=USER)
    assert gate._load(first['mutation_id'],user_id=USER)['status']=='active'


def test_legacy_policy_without_preimage_refuses_before_changes(store):
    value=activate(store,{'min_lift':0.4})
    key=records.entity_key(value['mutation_id'],USER,kind='policy transition')
    store.rows.pop((policy_transitions.NAMESPACE,key))
    before=deepcopy(store.rows)
    with pytest.raises(evolution.EvolutionStateError,match='preimage'):
        evolution.rollback_mutation(value['mutation_id'],user_id=USER)
    assert store.rows==before


@pytest.mark.parametrize('field',['before','after','rollback'])
def test_corrupt_transition_vectors_refuse_before_policy_or_metadata_change(store,field):
    value=activate(store,{'min_lift':0.4})
    key=records.entity_key(value['mutation_id'],USER,kind='policy transition')
    if field=='rollback':evolution.rollback_mutation(value['mutation_id'],user_id=USER)
    transition=store.rows[policy_transitions.NAMESPACE,key]
    transition[field]['min_lift']=0.25
    before=deepcopy(store.rows)
    with pytest.raises(StorageError):evolution.rollback_mutation(value['mutation_id'],user_id=USER)
    assert store.rows==before
