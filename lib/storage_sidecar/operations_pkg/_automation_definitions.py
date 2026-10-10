"""Atomic publication, subscription replacement and canonical clock-job ownership.

Entry point: _automation_definition_update. One definition advisory lock covers
revision/request replay, subscription pointers and replacement clock jobs. The
caller transaction rolls back every effect together. Rule interpretation stays
at the shared prepare_definition admission boundary.
"""

from copy import deepcopy
import hashlib
import time

from lib.storage.errors import StorageError
from lib.automations.contract import LIMITS, STORAGE_NAMESPACES, storage_input_validator, storage_schema
from ._common import _integer
from ._records import _record_get, _record_put_owned, _record_delete_owned
from ._worker_jobs import _worker_job_get, _worker_job_enqueue_owned


def _fail(message,code='database_protocol_error'):
    raise StorageError(code,message)


def _automation_definition_update(session,payload):
    from lib.automations.publication import (
        canonical,clock_job_id,request_digest,subscription_key,validate_publication_record,
    )
    if next(storage_input_validator('definitionUpdate').iter_errors(payload),None) is not None:
        _fail('Automation publication does not match its contract')
    limits = storage_schema()['x-limits']
    if len(canonical(payload).encode()) > limits['publicationRequestBytes']:
        _fail('Automation publication exceeds its request bound')
    owner = _integer(payload,'user_id',minimum=1,maximum=LIMITS['safeIntegerMax'])
    now = _integer(payload,'now_ms',minimum=1,maximum=LIMITS['safeIntegerMax'])
    version = _integer(payload,'expected_version',minimum=0,maximum=LIMITS['safeIntegerMax'])
    tenant,key = payload['tenant_id'],payload['definition_key']
    prefix = f'{owner}:{hashlib.sha256(tenant.encode()).hexdigest()[:32]}:'
    if not key.startswith(prefix) or now-int(time.time()*1000) > limits['clockSkewMs']:
        _fail('Automation publication identity or clock is invalid')
    due = payload['first_due_ms']
    if due is not None:
        due = _integer(payload,'first_due_ms',minimum=1,maximum=LIMITS['safeIntegerMax'])
    record = payload['record']
    if record is not None:
        try:
            entity_id = validate_publication_record(record,payload['definition_json'],payload['actions_json'],owner=owner,tenant=tenant)
        except (TypeError,ValueError):
            _fail('Invalid prepared publication binding')
        if record['grant']['approvedBy'] != payload['actor_subject_id']:
            _fail('Publication actor differs from its approval')
        if key != prefix+entity_id or len(canonical(record).encode()) > LIMITS['recordBytes']-limits['publicationMetadataReserveBytes']:
            _fail('Automation publication escaped its identity or record bound')
    digest = request_digest(payload)
    session.lock_key('automation_definition',key)
    namespace = STORAGE_NAMESPACES['definition']
    current = _record_get(session,{'namespace':namespace,'key':key})
    if current:
        old = current['value']
        if (type(old.get('ownerId')) is not int or old['ownerId'] != owner
                or (old.get('tenantId') or '') != tenant or key != prefix+old['id']):
            _fail('Automation publication escaped its owner')
        command = old.get('publicationCommand')
        if command and command['id'] == payload['request_id'] and command['digest'] != digest:
            _fail('Automation publication request evidence changed','database_conflict')
        if (current['version'] == version+1 and command
                and command['id'] == payload['request_id'] and command['digest'] == digest):
            return current
    if (current['version'] if current else 0) != version:
        _fail('Automation definition revision changed','database_conflict')
    if payload['mode'] in {'pause','retire'} and current is None:
        _fail('Automation definition does not exist','database_not_found')
    if payload['mode']=='publish':
        from ._automation_projects import require_project
        require_project(session,owner,tenant,record['definition'].get('projectId'))
    inactive_definition = None
    if payload['mode'] in {'pause','retire'}:
        import json
        try:
            inactive_definition = json.loads(payload['definition_json'])
            expected_definition = deepcopy(current['value']['definition'])
            expected_definition['enabled'] = False
            if (canonical(inactive_definition) != canonical(expected_definition)
                    or canonical(json.loads(payload['actions_json'])) != canonical(current['value']['compiled']['actions'])):
                _fail('State changes cannot modify the published definition or actions')
        except (TypeError,ValueError):
            _fail('Invalid inactive definition binding')
    last_clock_failure = (current['value'].get('lastClockFailure')
        if current and current['value'].get('grant') is not None else None)
    old_state = current['value'].get('triggerState',{}) if current else {}
    old_subscription = old_state.get('subscriptionKey')
    if old_subscription:
        if old_subscription != subscription_key(prefix,current['value']['definition']):
            _fail('Stored subscription identity is inconsistent','database_integrity')
        previous = _record_get(session,{'namespace':STORAGE_NAMESPACES['subscription'],'key':old_subscription})
        if previous:
            _record_delete_owned(session,{'namespace':STORAGE_NAMESPACES['subscription'],'key':old_subscription,'expected_version':previous['version']})
    old_clock = old_state.get('clockJobId')
    if old_clock:
        if old_clock != clock_job_id(key,current['version']):
            _fail('Stored clock identity is inconsistent','database_integrity')
        # Match publication/progress lock order: definition, worker advisory,
        # then the physical job row; claim_next skips this locked row.
        session.lock_key('worker_job',old_clock)
        previous_job = _worker_job_get(session,{'task_id':old_clock,'user_id':owner},lock=True)
        if previous_job is None:
            _fail('Published clock job is missing','database_integrity')
        source=previous_job['payload']
        if (previous_job['tenantId'] != tenant or previous_job['taskKind'] != 'automation-clock'
                or previous_job['idempotencyKey'] != old_clock or not isinstance(source,dict)
                or source.get('definitionKey') != key
                or type(source.get('definitionRevision')) is not int or source['definitionRevision'] != current['version']
                or source.get('definitionId') != current['value']['id']
                or source.get('definitionDigest') != current['value']['compiled']['digest']
                or canonical(source.get('principal')) != canonical(current['value']['grant']['principal'])):
            _fail('Automation clock escaped its immutable binding')
        if previous_job['status']=='failed':
            kind=previous_job['error'].get('kind')
            last_clock_failure={'jobId':old_clock,'revision':current['version'],
                'failedAtMs':previous_job['terminalAtMs'],
                'kind':kind[:storage_schema()['x-clock']['supersession']['failureKindCharacters']]
                    if isinstance(kind,str) else 'automation_clock_failed'}
        deleted=session.execute('DELETE FROM storage_worker_jobs WHERE task_id=? AND user_id=? AND tenant_id=?',
            (old_clock,owner,tenant))
        if deleted!=1:
            _fail('Published clock changed during retirement','database_conflict')
    if payload['mode'] in {'pause','retire'}:
        value = deepcopy(current['value'])
        if payload['mode'] == 'retire':
            value['deleted'] = True
        value['definition'] = inactive_definition
        value['compiled']['digest'] = hashlib.sha256((payload['definition_json']+'\n'+payload['actions_json']).encode()).hexdigest()
    else:
        value = deepcopy(record)
        value.update(schemaVersion=1,id=entity_id,ownerId=owner,
            tenantId=record['grant']['principal']['tenant_id'],deleted=False)
        value['grant']['approvedAt'] = now/1000
    value.pop('lastClockFailure',None)
    if last_clock_failure is not None:
        value['lastClockFailure']=deepcopy(last_clock_failure)
    value['updatedAt'] = now/1000
    value['publicationCommand'] = {'id':payload['request_id'],'digest':digest,
        'expectedVersion':version,'requestedBy':payload['actor_subject_id'],'requestedAt':now/1000}
    value['_commitDigest'] = digest
    state = {'subscriptionKey':None,'clockJobId':None}
    if not value['deleted'] and value['definition']['enabled']:
        state['subscriptionKey'] = subscription_key(prefix,value['definition'])
        if value['definition']['trigger']['kind'] == 'schedule':
            if due is None or due <= now:
                _fail('An enabled schedule requires a future first UTC slot')
            state['clockJobId'] = clock_job_id(key,version+1)
    if due is not None and state['clockJobId'] is None:
        _fail('Only an enabled schedule may enqueue a clock slot')
    value['triggerState'] = state
    if state['subscriptionKey']:
        pointer = {field:value[field] for field in ('id','ownerId','tenantId')}
        pointer.update(definitionRevision=version+1,definitionDigest=value['compiled']['digest'],
            trigger=value['definition']['trigger'],projectId=value['definition'].get('projectId'))
        _record_put_owned(session,{'namespace':STORAGE_NAMESPACES['subscription'],
            'key':state['subscriptionKey'],'value':pointer,'expected_version':0})
    if state['clockJobId']:
        _worker_job_enqueue_owned(session,{'task_id':state['clockJobId'],'user_id':owner,'tenant_id':tenant,
            'task_kind':'automation-clock','payload':{'contract':storage_schema()['$defs']['clockJob']['properties']['contract']['const'],
                'schemaVersion':1,'definitionId':value['id'],
                'definitionRevision':version+1,'definitionDigest':value['compiled']['digest'],
                'definitionKey':key,'publishedAtMs':now,'firstDueMs':due,
                'principal':value['grant']['principal']},'idempotency_key':state['clockJobId'],
            'priority':200,'now_ms':now,'available_at_ms':due})
    saved = _record_put_owned(session,{'namespace':namespace,'key':key,'value':value,'expected_version':version})
    return {**saved,'value':value}
