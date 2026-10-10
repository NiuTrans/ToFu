"""One immutable discovery executor claim per owned model-budget binding.

Entry: claim_discovery_execution/inspect_discovery_execution. Reuses process
incarnation evidence and semantic records. No expiry or replay. Recovery requires proven process death, complete
retained usage and an exact versioned ownership transition. Runtime wiring must check the claim before effects.
"""
from copy import deepcopy
import hashlib
import uuid
from lib.process_owner import process_owner,owner_state
from lib.storage.errors import StorageError
from lib.evolution import records as record_store
from . import budget_binding as binding
from . import records as research_records

NAMESPACE='research.discovery_execution_owners.v1'
HISTORY_NAMESPACE='research.discovery_execution_owner_history.v1'

def _load(task_id,user_id):
    owner,source,_,_=binding._source(task_id,user_id)
    policy=binding.read_model_budget(task_id,user_id=owner)
    if policy is None:raise ValueError('discovery execution requires an acknowledged model budget')
    digest=hashlib.sha256(binding._encode(policy)).hexdigest()
    key=record_store.entity_key(hashlib.sha256(task_id.encode()).hexdigest(),owner,kind='discovery executor')
    value=record_store.get_record(NAMESPACE,key)
    if value is not None:
        binding._validator('discovery_execution_owner').validate(value)
        binding._encode(value)
        if value.get('recovery') is not None:
            prior = record_store.get_record(HISTORY_NAMESPACE, key + '-' + value['recovery']['previous_nonce'])
            if (not isinstance(prior, dict) or prior.get('binding_sha256') != digest
                    or prior.get('nonce') != value['recovery']['previous_nonce']
                    or prior.get('nonce') == value['nonce']):
                raise StorageError('database_integrity', 'Discovery recovery predecessor is missing or changed')
            binding._validator('discovery_execution_owner').validate(prior)
        if value['binding_sha256']!=digest:
            raise StorageError('database_integrity','Discovery executor budget binding changed')
    return owner,source,digest,key,value

class DiscoveryExecutionClaim:
    def __init__(self,task_id,user_id,value):
        self.task_id,self.user_id,self.value=task_id,user_id,deepcopy(value)

    def assert_current(self):
        _,source,_,_,value=_load(self.task_id,self.user_id)
        if (source['status'] not in research_records.ACTIVE or value!=self.value
                or (value['process'] is not None and process_owner()!=value['process'])):
            raise StorageError('database_conflict','Discovery executor no longer owns its claim')

def claim_discovery_execution(task_id,*,user_id):
    owner,source,digest,key,existing=_load(task_id,user_id)
    if source['status'] not in research_records.ACTIVE:
        raise ValueError('terminal discovery task cannot claim execution')
    if existing is not None:
        raise StorageError('database_conflict','Discovery executor already claimed; takeover refused')
    value={'schema_version':1,'binding_sha256':digest,'nonce':uuid.uuid4().hex,
           'process':process_owner(),'replay_allowed':False}
    binding._validator('discovery_execution_owner').validate(value)
    expected=binding._encode(value)
    def exact():
        current=record_store.get_record(NAMESPACE,key)
        return current is not None and binding._encode(current)==expected
    try:
        record_store.put_record(NAMESPACE,key,value,kind='discovery executor',expected_version=0)
    except StorageError:
        if not exact():raise
    if not exact():raise StorageError('database_internal','Discovery executor claim was not exactly acknowledged')
    claim=DiscoveryExecutionClaim(task_id,owner,value)
    claim.assert_current()
    return claim

def inspect_discovery_execution(task_id,*,user_id):
    _,_,_,_,value=_load(task_id,user_id)
    return {'found':value is not None,'process_state':owner_state(value['process']) if value else 'unknown',
            'replay_allowed':False}


def recover_discovery_execution(task_id, *, user_id):
    """Claim continuation after old process death; never replay old attempts."""
    owner, source, digest, key, old = _load(task_id, user_id)
    if source['status'] not in research_records.ACTIVE:
        raise ValueError('terminal discovery cannot automatically recover')
    if old is None or owner_state(old['process']) != 'dead':
        raise StorageError('database_conflict', 'Discovery recovery requires proven old process death')
    from .model_attempt_journal import summarize_model_attempts
    summary = summarize_model_attempts(task_id, user_id=owner)
    if not summary['recorded_usage_complete']:
        raise StorageError('database_conflict', 'Discovery recovery requires complete retained model usage')
    current, version = record_store.get_versioned_record(NAMESPACE, key)
    if current != old or owner_state(old['process']) != 'dead':
        raise StorageError('database_conflict', 'Discovery recovery ownership changed during inspection')
    process = process_owner()
    if process is None:
        raise StorageError('database_conflict', 'Discovery recovery requires current process identity')
    value = {'schema_version':1, 'binding_sha256':digest, 'nonce':uuid.uuid4().hex,
             'process':process, 'replay_allowed':False, 'recovery':{
                 'previous_nonce':old['nonce'], 'consumed_tokens':summary['reported_tokens'],
                 'last_attempt_ordinal':summary['last_attempt_ordinal']}}
    binding._validator('discovery_execution_owner').validate(value)
    history_key = key + '-' + old['nonce']
    def archived():
        return record_store.get_record(HISTORY_NAMESPACE, history_key) == old
    prior = record_store.get_record(HISTORY_NAMESPACE, history_key)
    if prior is not None and prior != old:
        raise StorageError('database_integrity', 'Discovery owner history conflicts')
    if prior is None:
        try:
            record_store.put_record(HISTORY_NAMESPACE, history_key, old,
                kind='discovery owner history', expected_version=0)
        except StorageError:
            if not archived():raise
    if not archived():
        raise StorageError('database_internal', 'Discovery owner history was not exactly acknowledged')
    expected = binding._encode(value)
    def exact():
        observed = record_store.get_record(NAMESPACE, key)
        return observed is not None and binding._encode(observed) == expected
    try:
        record_store.put_record(NAMESPACE, key, value,
            kind='discovery recovery owner', expected_version=version)
    except StorageError:
        if not exact():raise
    if not exact():
        raise StorageError('database_internal', 'Discovery recovery owner was not exactly acknowledged')
    claim = DiscoveryExecutionClaim(task_id, owner, value)
    claim.assert_current()
    return claim


def can_recover_discovery_execution(task_id, *, user_id):
    """Read-only eligibility hint; actual recovery repeats the proof and CAS."""
    _, source, _, _, old = _load(task_id, user_id)
    if source['status'] not in research_records.ACTIVE or old is None or owner_state(old['process']) != 'dead':
        return False
    from .model_attempt_journal import summarize_model_attempts
    if not summarize_model_attempts(task_id, user_id=user_id)['recorded_usage_complete']:
        return False
    _, current_source, _, _, current = _load(task_id,user_id)
    return (current_source['status'] in research_records.ACTIVE and current == old
            and owner_state(old['process']) == 'dead')
