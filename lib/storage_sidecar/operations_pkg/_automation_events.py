"""Live-fenced event cursor and pure transport retirement in one transaction.

Entry point: _automation_event_settle. Frozen producer evidence never changes;
accepted runs have independent owners. Failure diagnostics are durable records.
"""
from copy import deepcopy
import hashlib
import json
import time

from lib.automations.contract import STORAGE_NAMESPACES, storage_input_validator, storage_schema
from lib.automations.events import initial_delivery_cursor, validate_event_job, validate_delivery_advance
from lib.storage.errors import StorageError
from ._records import _record_get, _record_put_owned
from ._worker_jobs import _worker_job_get, _worker_job_claim_state


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def _automation_event_settle(session,payload):
    from lib.automations.conditions import check_json_bounds
    try:
        check_json_bounds(payload,max_depth=24,max_nodes=16384)
        if len(canonical(payload).encode())>storage_schema()['x-event']['settlementRequestBytes']:
            raise ValueError('Event settlement exceeds its bound')
    except ValueError as exc:
        raise StorageError('database_protocol_error','Event settlement exceeds its bounds') from exc
    if next(storage_input_validator('eventSettle').iter_errors(payload),None):
        raise StorageError('database_protocol_error','Invalid event settlement request')
    owner,tenant,job_id=payload['user_id'],payload['tenant_id'],payload['job_id']
    expected=deepcopy(payload['expected_payload']);expected.setdefault('delivery',initial_delivery_cursor())
    mode,next_cursor=payload['mode'],payload['next_delivery'];now=payload['now_ms']
    try:
        prefixes=validate_event_job(owner,tenant,job_id,expected)
        if mode=='advance':
            validate_delivery_advance(prefixes,expected['delivery'],next_cursor)
        elif next_cursor is not None or (mode=='succeeded' and expected['delivery']['index']!=len(prefixes)):
            raise ValueError('Unfinished event delivery cannot succeed')
        if bool(payload['error_kind']) != (mode=='failed'):
            raise ValueError('Only failed delivery carries an error kind')
    except (ValueError,TypeError,KeyError) as exc:
        raise StorageError('database_protocol_error','Invalid event settlement binding') from exc
    if now-int(time.time()*1000)>storage_schema()['x-limits']['clockSkewMs']:
        raise StorageError('database_protocol_error','Invalid event settlement clock')
    session.lock_key('worker_job',job_id)
    job=_worker_job_get(session,{'task_id':job_id,'user_id':owner},lock=True)
    failure_key=f'{owner}:{hashlib.sha256(tenant.encode()).hexdigest()[:32]}:{job_id}'
    if job is None:
        if mode=='advance':return {'ok':False,'error':'producer_retired'}
        failure=_record_get(session,{'namespace':STORAGE_NAMESPACES['eventFailure'],'key':failure_key})
        if mode=='failed' and failure and failure['value']['kind']!=payload['error_kind']:
            raise StorageError('database_conflict','Event failure evidence changed')
        return {'ok':True,'applied':False,'retired':True}
    current=deepcopy(job['payload']);current.setdefault('delivery',initial_delivery_cursor())
    if job['tenantId']!=tenant or job['taskKind']!='automation-event' or job['idempotencyKey']!=job_id:
        raise StorageError('database_protocol_error','Event transport escaped its owner')
    if mode=='advance' and canonical(current)==canonical({**expected,'delivery':next_cursor}):
        return {'ok':True,'applied':False,'retired':False,'payload':current}
    if canonical(current)!=canonical(expected):
        raise StorageError('database_conflict','Event cursor changed')
    proof=_worker_job_claim_state(session,{'task_id':job_id,'worker_id':payload['worker_id'],
        'fencing_token':payload['fencing_token'],'now_ms':now})
    if not proof['ok']:return {'ok':False,'error':'producer_fence_lost'}
    if mode in {'advance','succeeded','failed'} and proof['cancelSequence']:
        return {'ok':False,'error':'cancellation_pending'}
    if mode=='cancelled' and not proof['cancelSequence']:
        raise StorageError('database_protocol_error','Event cancellation requires owner intent')
    if mode=='advance':
        current['delivery']=deepcopy(next_cursor)
        changed=session.execute('UPDATE storage_worker_jobs SET payload_json=?,updated_at_ms=? '
            'WHERE task_id=? AND user_id=? AND tenant_id=?', (canonical(current),now,job_id,owner,tenant))
        if changed!=1:raise StorageError('database_conflict','Event transport changed')
        return {'ok':True,'applied':True,'retired':False,'payload':current}
    if mode=='failed':
        diagnostic={'id':job_id,'ownerId':owner,'tenantId':tenant,'event':expected['event'],
            'delivery':expected['delivery'],'kind':payload['error_kind'],'failedAt':now/1000}
        _record_put_owned(session,{'namespace':STORAGE_NAMESPACES['eventFailure'],
            'key':failure_key,'value':diagnostic,'expected_version':0})
    deleted=session.execute('DELETE FROM storage_worker_jobs WHERE task_id=? AND user_id=? AND tenant_id=?',
        (job_id,owner,tenant))
    if deleted!=1:raise StorageError('database_conflict','Event transport changed during retirement')
    return {'ok':True,'applied':True,'retired':True}
