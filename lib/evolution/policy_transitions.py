"""Durable policy preimages and CAS-fenced application/rollback.

Entry: apply, restore, applied. Original gate policy schema owns vectors. One
owner/mutation row retains the preimage before effect; rollback is appended once.
Exact versioned readback resolves lost ACKs. No model, timer or worker is added.
"""
from copy import deepcopy
import time
import hashlib
import json
from lib.identity import require_user_id
from lib.storage.errors import StorageError
from .contracts import contract_validator, validate_gate_policy, EvolutionStateError
from .comparison import mutation_fingerprint
from .records import entity_key, get_versioned_record, put_record

NAMESPACE = 'evolution.policy_transitions.v1'


def _seal(value):
    material={key:item for key,item in value.items() if key!='material_digest'}
    digest=hashlib.sha256(json.dumps(material,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {**material,'material_digest':digest}


def _keys(mutation, user_id):
    owner=require_user_id(user_id,context='evolution policy transition owner')
    return owner,entity_key(mutation['mutation_id'],owner,kind='policy transition'),entity_key('active',owner,kind='gate policy')


def _read_policy(key):
    from .records import NAMESPACE_GATE_POLICY
    value,version=get_versioned_record(NAMESPACE_GATE_POLICY,key)
    if value is not None:
        try:validate_gate_policy(value)
        except ValueError as exc:raise StorageError('database_integrity','Invalid current gate policy') from exc
    return value,version


def _read(mutation, user_id):
    owner,key,policy_key=_keys(mutation,user_id)
    value,version=get_versioned_record(NAMESPACE,key)
    if value is None:return None,0,key,policy_key
    try:
        contract_validator('evolution_policy_transition_v1.schema.json').validate(value)
        validate_gate_policy(value['before']);validate_gate_policy(value['after'])
        from .gate_policy import DEFAULTS,parse_policy_overrides
        overrides=parse_policy_overrides(mutation.get('payload') or {})
        if (value!=_seal(value) or any(value['after'][key]!=overrides.get(key,value['before'][key]) for key in DEFAULTS)):
            raise ValueError('policy material digest or frozen application differs')
        if (value['user_id']!=owner or value['mutation_id']!=mutation['mutation_id']
                or value['mutation_fingerprint']!=mutation_fingerprint(mutation)
                or value['parent_version']!=mutation['parent_version']
                or value['after']['source_mutation_id']!=mutation['mutation_id']
                or value['after']['version']!=(value['before']['version'] if value['before_storage_version'] else 0)+1
                or version!=(2 if 'rollback' in value else 1)):
            raise ValueError('transition identity/version differs')
        if 'rollback' in value:
            from .gate_policy import DEFAULTS
            restored=value['rollback'];validate_gate_policy(restored)
            source=value['rollback_from'];validate_gate_policy(source)
            if (source['source_mutation_id']!=value['mutation_id']
                    or any(source[key]!=value['after'][key] for key in DEFAULTS)
                    or restored['version']!=source['version']+1
                    or restored['source_mutation_id']!=value['before']['source_mutation_id']
                    or any(restored[key]!=value['before'][key] for key in DEFAULTS)):
                raise ValueError('rollback projection differs from original preimage')
    except Exception as exc:
        raise StorageError('database_integrity','Invalid policy transition original') from exc
    return value,version,key,policy_key


def _write_exact(namespace,key,value,expected):
    try:put_record(namespace,key,value,kind='policy transition',expected_version=expected)
    except StorageError as exc:
        current,version=get_versioned_record(namespace,key)
        if current==value and version==expected+1:return
        raise exc
    current,version=get_versioned_record(namespace,key)
    if current!=value or version!=expected+1:
        raise StorageError('database_integrity','Policy transition was not exactly acknowledged')


def applied(mutation, *, user_id):
    value,_,_,policy_key=_read(mutation,user_id)
    if value is None or 'rollback' in value:return False
    current,version=_read_policy(policy_key)
    return current==value['after'] and version==value['before_storage_version']+1


def apply(mutation, *, user_id, now=None, expected_parent=None):
    from .gate_policy import DEFAULTS,parse_policy_overrides
    from .records import NAMESPACE_GATE_POLICY
    overrides=parse_policy_overrides(mutation.get('payload') or {})
    transition,_,key,policy_key=_read(mutation,user_id)
    if transition is None:
        before,version=_read_policy(policy_key)
        if before is not None and before['source_mutation_id']!=(expected_parent or ''):
            raise EvolutionStateError('effective policy baseline differs from active mutation metadata')
        stamp=int(time.time() if now is None else now)
        frozen=before or {'schema_version':1,**DEFAULTS,'version':1,'source_mutation_id':'','updated_at':stamp}
        after=validate_gate_policy({'schema_version':1,
            **{key:overrides.get(key,frozen[key]) for key in DEFAULTS},
            'version':(before['version'] if before else 0)+1,
            'source_mutation_id':mutation['mutation_id'],'updated_at':stamp})
        owner,_,_=_keys(mutation,user_id)
        transition={'schema_version':1,'user_id':owner,'mutation_id':mutation['mutation_id'],
            'mutation_fingerprint':mutation_fingerprint(mutation),'parent_version':mutation['parent_version'],
            'before_storage_version':version,'before':deepcopy(frozen),'after':after}
        transition=_seal(transition)
        _write_exact(NAMESPACE,key,transition,0)
    if 'rollback' in transition:
        raise EvolutionStateError('rolled back policy cannot replay its application')
    current,version=_read_policy(policy_key)
    expected=transition['before_storage_version']
    if current==transition['after'] and version==expected+1:return deepcopy(current)
    before=transition['before'] if expected else None
    if current!=before or version!=expected:
        raise EvolutionStateError('policy changed after preparation; application refused')
    _write_exact(NAMESPACE_GATE_POLICY,policy_key,transition['after'],expected)
    return deepcopy(transition['after'])


def restore(mutation, *, user_id, now=None):
    from .gate_policy import DEFAULTS
    from .records import NAMESPACE_GATE_POLICY
    transition,version,key,policy_key=_read(mutation,user_id)
    if transition is None:
        raise EvolutionStateError('legacy policy has no frozen rollback preimage; no values were changed')
    current,current_version=_read_policy(policy_key)
    if 'rollback' not in transition:
        if (current is None or current['source_mutation_id']!=mutation['mutation_id']
                or any(current[key]!=transition['after'][key] for key in DEFAULTS)):
            raise EvolutionStateError('another policy owns the current effect; rollback refused')
        rollback=validate_gate_policy({'schema_version':1,
            **{key:transition['before'][key] for key in DEFAULTS},
            'version':current['version']+1,
            'source_mutation_id':transition['before']['source_mutation_id'],
            'updated_at':int(time.time() if now is None else now)})
        transition={**transition,'rollback':rollback,'rollback_from':deepcopy(current),
            'rollback_storage_version':current_version}
        transition=_seal(transition)
        _write_exact(NAMESPACE,key,transition,version)
    expected=transition['rollback_storage_version']
    if current==transition['rollback'] and current_version==expected+1:
        return deepcopy(current)
    if current!=transition['rollback_from'] or current_version!=expected:
        raise EvolutionStateError('policy changed before rollback; refusing overwrite')
    _write_exact(NAMESPACE_GATE_POLICY,policy_key,transition['rollback'],expected)
    return deepcopy(transition['rollback'])
