"""Acknowledged physical Research model intents and terminal usage observations.

Entry: install_model_attempt_journal/read_model_attempt. Reuses ExecutionBudget
and semantic record storage. A journal never authorizes replay, recovery, or
scientific acceptance. Fresh installation refuses existing attempts; restoration
must later prove original policy, ownership and complete attempt continuity.
"""
from copy import deepcopy
import hashlib
import uuid
from lib.agent_core.execution_budget import _ensure_execution_budget
from lib.evolution import records as record_store
from lib.storage.errors import StorageError
from lib.identity import require_user_id
from . import budget_binding as binding
from . import records as research_records

NAMESPACE='research.physical_model_attempts.v1'


def _digest(value):return hashlib.sha256(binding._encode(value)).hexdigest()

def _key(task_id,ordinal,phase,user_id):
    if type(ordinal) is not int or not 1<=ordinal<=9223372036854775807:
        raise ValueError('invalid research model attempt ordinal')
    if phase not in ('dispatch_intent','terminal'):raise ValueError('invalid attempt reference')
    identity=hashlib.sha256(task_id.encode()).hexdigest()
    return record_store.entity_key(f'{identity}-{ordinal:020d}-{phase}',user_id,kind='research model attempt')

def _validate(value):
    binding._validator('attempt_event').validate(value)
    binding._encode(value)

def read_model_attempt(task_id,ordinal,phase,*,user_id):
    policy=binding.read_model_budget(task_id,user_id=user_id)
    if policy is None:raise ValueError('research model budget is not bound')
    value=record_store.get_record(NAMESPACE,_key(task_id,ordinal,phase,user_id))
    if value is None:return None
    _validate(value)
    if (value['binding_sha256']!=_digest(policy) or value['attempt_ordinal']!=ordinal
            or (value['phase']=='dispatch_intent')!=(phase=='dispatch_intent')):
        raise StorageError('database_integrity','Research model attempt binding changed')
    return deepcopy(value)

def _put(key,value):
    _validate(value)
    expected=binding._encode(value)
    def exact():
        saved=record_store.get_record(NAMESPACE,key)
        if saved is None:return False
        _validate(saved)
        return binding._encode(saved)==expected
    try:
        record_store.put_record(NAMESPACE,key,value,kind='research model attempt',expected_version=0)
    except StorageError:
        if not exact():raise
    if not exact():raise StorageError('database_internal','Research model attempt was not exactly acknowledged')

def install_model_attempt_journal(root_task, *, execution_claim=None):
    owner=require_user_id(root_task['_userId'],context='research model attempt owner');task_id=root_task['id']
    if execution_claim is not None:
        if execution_claim.task_id != task_id or execution_claim.user_id != owner:
            raise ValueError('research model journal execution claim differs from account')
        execution_claim.assert_current()
    policy=binding.read_model_budget(task_id,user_id=owner)
    if policy is None:raise ValueError('research model budget is not bound')
    budget=_ensure_execution_budget(root_task)
    if budget.root_id!=task_id or binding._encode(budget.limits)!=binding._encode(policy['execution_limits']):
        raise ValueError('research model budget account differs from its binding')
    recovery = execution_claim.value.get('recovery') if execution_claim is not None else None
    if recovery is not None:
        summary = summarize_model_attempts(task_id, user_id=owner)
        if (not summary['recorded_usage_complete']
                or summary['reported_tokens'] != recovery['consumed_tokens']
                or summary['last_attempt_ordinal'] != recovery['last_attempt_ordinal']):
            raise StorageError('database_conflict', 'Discovery recovery usage changed before account restoration')
    if recovery is None and any(read_model_attempt(task_id,1,phase,user_id=owner) is not None
           for phase in ('dispatch_intent','terminal')):
        raise StorageError('database_conflict','Research model attempts require explicit recovery; replay refused')
    producer=uuid.uuid4().hex;failed=[]
    def write_observation(event):
        if event['root_task_id']!=task_id or require_user_id(root_task['_userId'],context='research model attempt owner')!=owner:
            raise ValueError('research model account owner changed')
        current=binding.read_model_budget(task_id,user_id=owner)
        if current!=policy or binding._encode(budget.limits)!=binding._encode(policy['execution_limits']):
            raise ValueError('research model budget policy changed')
        intent=event['phase']=='dispatch_intent'
        if failed and (intent or event['attempt_ordinal'] != failed[0][0]):
            raise StorageError('database_unavailable',
                'Research model journal is fenced after a failed observation')
        if intent:
            if execution_claim is not None:
                execution_claim.assert_current()
            _,source,_,_=binding._source(task_id,owner)
            if source['status'] not in research_records.ACTIVE:
                raise ValueError('terminal research task cannot dispatch a model')
        value={'schema_version':1,'binding_sha256':_digest(policy),'producer_id':producer,
            **{key:item for key,item in event.items() if key!='root_task_id'}}
        phase='dispatch_intent' if intent else 'terminal'
        prior=read_model_attempt(task_id,event['attempt_ordinal'],'dispatch_intent',user_id=owner)
        if prior is not None and prior['producer_id']!=producer:
            raise StorageError('database_conflict','Research model attempt belongs to another producer')
        if not intent and event['phase']=='settled' and prior is None:
            raise StorageError('database_integrity','Sent research model attempt lacks acknowledged intent')
        _put(_key(task_id,event['attempt_ordinal'],phase,owner),value)
        if intent and execution_claim is not None:
            execution_claim.assert_current()
    def observe(event):
        try:
            return write_observation(event)
        except Exception as error:
            if not failed:failed.append((event['attempt_ordinal'],type(error).__name__))
            raise
    budget.bind_model_attempt_observer(observe)
    if recovery is not None:
        execution_claim.assert_current()
        budget.restore_model_account(root_task_id=task_id,
            consumed_tokens=recovery['consumed_tokens'],
            last_attempt_ordinal=recovery['last_attempt_ordinal'], usage_complete=True)
    return budget

def summarize_model_attempts(task_id,*,user_id,page_size=64):
    """Fold bounded pages of retained attempts; never authorize resume or replay.

    Cumulative account snapshots may arrive out of ordinal order when requests
    settle concurrently. Keep their maximum alongside summed per-attempt reports;
    no missing settlement, ordinal or usage becomes zero-cost evidence.
    """
    policy=binding.read_model_budget(task_id,user_id=user_id)
    if policy is None:raise ValueError('research model budget is not bound')
    digest=_digest(policy);prefix=hashlib.sha256(task_id.encode()).hexdigest()+'-'
    cursor='';ordinal=0;intent=None;terminal=None
    attempts=reported=account_max=pending=unknown=gaps=0
    def close_group():
        nonlocal attempts,reported,account_max,pending,unknown
        if not ordinal:return
        attempts+=1
        if terminal is None:
            pending+=1
            return
        if terminal['phase']=='settled' and intent is None:
            raise StorageError('database_integrity','Research settlement lacks its send intent')
        if intent is not None and any(terminal[k]!=intent[k] for k in
                ('producer_id','attempt_task_id','reserved_tokens')):
            raise StorageError('database_integrity','Research terminal attempt identity changed')
        reported+=terminal['reported_tokens']
        account_max=max(account_max,terminal['account_observed_tokens'])
        unknown+=not terminal['usage_complete']
    while True:
        rows=record_store.list_record_page(NAMESPACE,user_id,kind='research model attempt',
            limit=page_size,after_key=cursor,entity_prefix=prefix)
        for row in rows:
            value=row['value'];_validate(value)
            phase='dispatch_intent' if value['phase']=='dispatch_intent' else 'terminal'
            if value['binding_sha256']!=digest or row['key']!=_key(task_id,value['attempt_ordinal'],phase,user_id):
                raise StorageError('database_integrity','Research model attempt page binding changed')
            if value['attempt_ordinal']!=ordinal:
                close_group()
                gaps+=value['attempt_ordinal']-ordinal-1
                ordinal=value['attempt_ordinal'];intent=terminal=None
            if phase=='dispatch_intent':intent=value
            else:terminal=value
            account_max=max(account_max,value['account_observed_tokens'])
        if len(rows)<page_size:break
        cursor=rows[-1]['key']
    close_group()
    return {'coverage_kind':'retained_attempt_events','writer_fence_required_for_recovery':True,
        'attempt_count':attempts,
        'last_attempt_ordinal':ordinal,'reported_tokens':reported,
        'account_token_lower_bound':max(reported,account_max),'unsettled_attempts':pending,
        'unknown_usage_attempts':unknown,'missing_ordinals':gaps,
        'recorded_usage_complete':bool(attempts) and not (pending or unknown or gaps)
            and account_max==reported}
