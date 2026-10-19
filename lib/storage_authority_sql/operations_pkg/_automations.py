"""Atomic automation admission, progress and cancellation over record/job owners.

Entry points: _automation_run_admit, _automation_run_advance, _automation_run_cancel.
Definitions and
the canonical worker queue are read/written in the caller's transaction. Small
active/history pointers avoid scanning terminal run payloads during recovery.
"""

from __future__ import annotations

import hashlib
import json
import time

from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from lib.automations.contract import RUNTIME as RUNTIME, STORAGE_NAMESPACES as STORAGE_NAMESPACES, storage_input_validator as storage_input_validator
from ._common import _integer, _required_text
from ._records import _record_get, _record_put_owned as _record_put, _record_delete_owned as _record_delete
from ._worker_jobs import _worker_job_enqueue_owned, _worker_job_get, _worker_job_claim_state, _worker_job_request_cancel

DEFINITION_NAMESPACE = 'automation.definitions.v1'
RUN_NAMESPACE = 'automation.runs.v1'
ACTIVE_NAMESPACE = 'automation.active.v1'
HISTORY_NAMESPACE = 'automation.history.v1'
TERMINAL = RUNTIME['terminalRunStates']
IMMUTABLE = ('contract', 'id', 'ownerId', 'tenantId', 'definitionId',
             'definitionRevision', 'definitionDigest', 'snapshot', 'actions',
             'stepOrder', 'trigger', 'triggerDigest', 'automationChain',
             'createdAt', 'deadlineAt', 'jobId', 'principal')


def _fail(message, code='database_protocol_error'):
    raise StorageError(code, message)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _envelope(payload, *, advance):
    from lib.automations.conditions import check_json_bounds
    try:
        check_json_bounds(payload, max_depth=44, max_nodes=65536)
    except ValueError:
        _fail('Automation mutation exceeds JSON bounds')
    if next(storage_input_validator('advance' if advance else 'admit').iter_errors(payload), None) is not None:
        _fail('Automation mutation does not match its storage contract')
    fields = {'user_id', 'tenant_id', 'run_key', 'run', 'now_ms'}
    fields |= ({'expected_version', 'worker_id', 'fencing_token'} if advance else
               {'definition_key', 'definition_revision', 'definition_digest'})
    if not advance and 'producer_fence' in payload:
        fields.add('producer_fence')
    if set(payload) != fields:
        _fail('Invalid automation mutation fields')
    owner = _integer(payload, 'user_id', minimum=1)
    tenant = payload['tenant_id']
    if not isinstance(tenant, str) or len(tenant) > 256:
        _fail('Invalid automation tenant')
    prefix = f'{owner}:{hashlib.sha256(tenant.encode()).hexdigest()[:32]}:'
    run = payload['run']
    if (not isinstance(run, dict) or len(_canonical(run).encode()) > 524288
            or type(run.get('ownerId')) is not int or run['ownerId'] != owner
            or (run.get('tenantId') or '') != tenant
            or type(run.get('definitionRevision')) is not int
            or run.get('contract') != 'tofu.automation-run/v1'
            or not isinstance(run.get('id'), str)):
        _fail('Invalid automation run identity')
    if not advance and any('approval' in state for state in run['steps'].values()):
        _fail('Initial automation state cannot carry approval')
    key = _required_text(payload, 'run_key', 512)
    if key != prefix + run['id']:
        _fail('Automation run key escaped its owner')
    job_id = 'automation-run:' + hashlib.sha256(key.encode()).hexdigest()
    if run.get('jobId') != job_id:
        _fail('Automation job identity differs')
    now = _integer(payload, 'now_ms', minimum=1, maximum=9007199254740991)
    if now - int(time.time() * 1000) > 86400000:
        _fail('Automation clock is too far ahead')
    return owner, tenant, prefix, run, key, job_id, now


def _record(session, namespace, key):
    return _record_get(session, {'namespace': namespace, 'key': key})


def _put(session, namespace, key, value, version):
    return _record_put(session, {'namespace': namespace, 'key': key,
                                'value': value, 'expected_version': version})


def _index(session, prefix, run):
    pointer = {'id': run['id'], 'ownerId': run['ownerId'], 'tenantId': run['tenantId'],
               'definitionId': run['definitionId'], 'jobId': run['jobId'],
               'status': run['status'], 'createdAt': run['createdAt']}
    active_key = prefix + run['id']
    active = _record(session, ACTIVE_NAMESPACE, active_key)
    if run['status'] in TERMINAL:
        if active:
            _record_delete(session, {'namespace': ACTIVE_NAMESPACE, 'key': active_key,
                                    'expected_version': active['version']})
    else:
        _put(session, ACTIVE_NAMESPACE, active_key, pointer, active['version'] if active else 0)
    history_key = prefix + run['definitionId'] + ':' + f'{int(run["createdAt"] * 1000):016d}:' + run['id']
    history = _record(session, HISTORY_NAMESPACE, history_key)
    _put(session, HISTORY_NAMESPACE, history_key, pointer, history['version'] if history else 0)


def _automation_run_admit(session, payload):
    owner, tenant, prefix, run, key, job_id, now = _envelope(payload, advance=False)
    session.lock_key('automation_run', key)
    existing = _record(session, RUN_NAMESPACE, key)
    if existing:
        if existing['value'].get('triggerDigest') != run.get('triggerDigest'):
            _fail('Automation occurrence evidence changed', 'database_conflict')
        job = _worker_job_get(session, {'task_id': job_id, 'user_id': owner})
        if job is None:
            _fail('Accepted automation has no durable job', 'database_integrity')
        return {'created': False, 'run': existing, 'job': job}
    if run.get('status') != 'queued' or run.get('fence') is not None:
        _fail('A new automation must be queued without a claim')
    raw_principal = run.get('principal')
    try:
        principal = PrincipalContext.from_payload(raw_principal)
        if (principal.to_payload() != raw_principal or principal.require_owner() != owner
                or (principal.tenant_id or '') != tenant or 'admin' in principal.scopes):
            raise ValueError('Invalid grant')
        for scope in {'automations:read', 'automations:execute'}:
            principal.require_scope(scope)
        for action in run['actions'].values():
            for scope in action['requiredScopes']:
                principal.require_scope(scope)
    except (TypeError, ValueError, PermissionError, KeyError, AttributeError):
        _fail('Automation requires an explicit least-privilege grant')
    definition_key = _required_text(payload, 'definition_key', 512)
    if definition_key != prefix + run.get('definitionId', ''):
        _fail('Automation definition key escaped its owner')
    revision = _integer(payload, 'definition_revision', minimum=1)
    session.lock_key('automation_definition', definition_key)
    definition = _record(session, DEFINITION_NAMESPACE, definition_key)
    digest = _required_text(payload, 'definition_digest', 64)
    if (definition is None or definition['version'] != revision
            or definition['value'].get('compiled', {}).get('digest') != digest
            or run.get('definitionRevision') != revision or run.get('definitionDigest') != digest
            or definition['value'].get('deleted')
            or (not definition['value']['definition']['enabled'] and run['trigger']['kind'] != 'manual')):
        _fail('Automation definition changed before admission', 'database_conflict')
    from ._automation_projects import require_project
    require_project(session,owner,tenant,definition['value']['definition'].get('projectId'))
    project=definition['value']['definition'].get('projectId')
    if project is not None and run['trigger'].get('projectId')!=project:
        _fail('Automation trigger escaped its project binding')
    if (_canonical(run['snapshot']) != _canonical(definition['value']['definition'])
            or _canonical(run['actions']) != _canonical(definition['value']['compiled']['actions'])
            or _canonical(run['stepOrder']) != _canonical(definition['value']['compiled']['stepOrder'])
            or _canonical(raw_principal) != _canonical(definition['value'].get('grant', {}).get('principal'))):
        _fail('Automation snapshot differs from admitted definition')
    producer = payload.get('producer_fence')
    if run['trigger']['kind'] == 'schedule':
        clock_id = definition['value'].get('triggerState',{}).get('clockJobId')
        if not producer or producer['jobId'] != clock_id:
            _fail('A scheduled occurrence requires its published clock producer','database_conflict')
        clock = _worker_job_get(session,{'task_id':clock_id,'user_id':owner},lock=True)
        clock_proof = _worker_job_claim_state(session,{'task_id':clock_id,
            'worker_id':producer['workerId'],'fencing_token':producer['token'],'now_ms':now})
        if (clock is None or clock['taskKind'] != 'automation-clock' or clock['tenantId'] != tenant
                or clock['payload'].get('definitionKey') != payload['definition_key']
                or clock['payload'].get('definitionRevision') != revision
                or not clock_proof['ok'] or clock_proof['cancelSequence']):
            _fail('Automation clock producer claim is stale','database_conflict')
    elif run['trigger']['kind']=='task.started' and (producer is not None or run['trigger']['id'].startswith('task-start:')):
        if producer is None:
            _fail('A canonical task-start occurrence requires its event producer','database_conflict')
        event_job=_worker_job_get(session,{'task_id':producer['jobId'],'user_id':owner},lock=True)
        event_proof=_worker_job_claim_state(session,{'task_id':producer['jobId'],
            'worker_id':producer['workerId'],'fencing_token':producer['token'],'now_ms':now})
        if (event_job is None or event_job['taskKind']!='automation-event' or event_job['tenantId']!=tenant
                or _canonical(event_job['payload'].get('event'))!=_canonical(run['trigger'])
                or not event_proof['ok'] or event_proof['cancelSequence']):
            _fail('Automation event producer claim is stale','database_conflict')
    elif producer is not None:
        _fail('Only a scheduled or canonical task-start occurrence may carry a producer fence')
    saved = _put(session, RUN_NAMESPACE, key, run, 0)
    job = _worker_job_enqueue_owned(session, {
        'task_id': job_id, 'user_id': owner, 'tenant_id': tenant,
        'task_kind': 'automation-run', 'idempotency_key': job_id,
        'payload': {'contract': 'tofu.automation-job/v1', 'runId': run['id'],
                    'principal': raw_principal},
        'priority': 200, 'available_at_ms': now, 'now_ms': now,
    })['job']
    _index(session, prefix, run)
    return {'created': True, 'run': {**saved, 'value': run}, 'job': job}


def _automation_run_advance(session, payload):
    owner, tenant, prefix, run, key, job_id, now = _envelope(payload, advance=True)
    session.lock_key('automation_run', key)
    existing = _record(session, RUN_NAMESPACE, key)
    version = _integer(payload, 'expected_version', minimum=1)
    if (existing and existing['version'] == version + 1
            and _canonical(existing['value']) == _canonical(run)):
        return existing
    # Lock the canonical claim through progress publication. Advisory locks
    # on the run cannot serialize with a claim_next row-lock takeover.
    job = _worker_job_get(session, {'task_id': job_id, 'user_id': owner}, lock=True)
    fence = _integer(payload, 'fencing_token', minimum=1)
    worker = _required_text(payload, 'worker_id', 200)
    proof = _worker_job_claim_state(session, {'task_id': job_id, 'worker_id': worker,
                                            'fencing_token': fence, 'now_ms': now})
    if (job is None or job['tenantId'] != tenant or not proof['ok']
            or run.get('fence') != {'jobId': job_id, 'workerId': worker, 'token': fence}):
        _fail('Automation worker claim is stale', 'database_conflict')
    existing = _record(session, RUN_NAMESPACE, key)
    if existing is None or existing['value']['status'] in TERMINAL:
        _fail('Automation run is missing or terminal', 'database_conflict')
    if existing['version'] != version:
        _fail('Automation run revision changed', 'database_conflict')
    if any(_canonical(existing['value'].get(k)) != _canonical(run.get(k)) for k in IMMUTABLE):
        _fail('Automation immutable evidence changed')
    current = existing['value']
    from lib.automations.approval import validate_approval_progress
    try:
        validate_approval_progress(current, run)
    except (ValueError, KeyError) as exc:
        _fail(str(exc))
    if (current.get('cancelCommand') != run.get('cancelCommand')
            or (current.get('cancelCommand') and current.get('cancelReason') != run.get('cancelReason'))):
        _fail('Automation cancellation evidence changed')
    if current.get('cancelCommand'):
        if run['status'] not in {'cancelling', 'cancelled'}:
            _fail('Owner cancellation requires cancellation settlement', 'database_conflict')
        if run['status'] == 'cancelled' and any(
                state['status'] in RUNTIME['activeEffectStepStates'] for state in run['steps'].values()):
            _fail('Cancellation cannot settle an unresolved external effect')
    if proof['cancelSequence'] and run['status'] not in {'cancelling', 'cancelled', 'failed'}:
        _fail('Automation cancellation requires settlement', 'database_conflict')
    version = _integer(payload, 'expected_version', minimum=1)
    from ._automation_presentations import publish_presentations
    publish_presentations(session, prefix, current, run)
    saved = _put(session, RUN_NAMESPACE, key, run, version)
    _index(session, prefix, run)
    return {**saved, 'value': run}


def _automation_run_cancel(session, payload):
    from lib.automations.transitions import request_cancel
    if next(storage_input_validator('cancel').iter_errors(payload), None) is not None:
        _fail('Automation cancellation does not match its storage contract')
    owner = _integer(payload, 'user_id', minimum=1, maximum=9007199254740991)
    tenant, key = payload['tenant_id'], payload['run_key']
    now = _integer(payload, 'now_ms', minimum=1, maximum=9007199254740991)
    prefix = f'{owner}:{hashlib.sha256(tenant.encode()).hexdigest()[:32]}:'
    if not key.startswith(prefix) or now - int(time.time() * 1000) > 86400000:
        _fail('Automation cancellation identity or clock is invalid')
    session.lock_key('automation_run', key)
    current = _record(session, RUN_NAMESPACE, key)
    if current is None:
        _fail('Automation run does not exist', 'database_not_found')
    run = current['value']
    if (key != prefix + run['id'] or run['ownerId'] != owner
            or (run.get('tenantId') or '') != tenant
            or run['jobId'] != 'automation-run:' + hashlib.sha256(key.encode()).hexdigest()):
        _fail('Automation cancellation escaped its owner')
    command = run.get('cancelCommand')
    if command and command['id'] == payload['request_id'] and command['reason'] != payload['reason']:
        _fail('Automation cancellation request evidence changed', 'database_conflict')
    if run['status'] in TERMINAL:
        return {'accepted': False, 'alreadyTerminal': True, 'run': current}
    if command:
        job = _worker_job_get(session, {'task_id': run['jobId'], 'user_id': owner})
        if job is None or not job['cancelSequence']:
            _fail('Automation cancellation has no durable worker command', 'database_integrity')
        return {'accepted': True, 'alreadyRequested': True, 'run': current}
    # The job owner acquires its advisory lock before its row lock. Keep the
    # same ordering as defer/cancel; acquiring that row first could deadlock.
    cancellation = _worker_job_request_cancel(session, {
        'task_id': run['jobId'], 'user_id': owner, 'reason': payload['reason'], 'now_ms': now,
    })
    if (cancellation is None or not cancellation['accepted']
            or cancellation['job']['tenantId'] != tenant):
        _fail('An active automation has no cancellable durable job', 'database_integrity')
    cancelled = request_cancel(run, reason=payload['reason'], now=now / 1000)
    if cancelled['status'] == 'cancelling' and cancellation['job']['status'] == 'cancelled':
        _fail('An active external effect cannot have a queued job', 'database_integrity')
    cancelled['cancelCommand'] = {'id': payload['request_id'], 'reason': payload['reason'], 'requestedAt': now / 1000}
    saved = _put(session, RUN_NAMESPACE, key, cancelled, current['version'])
    _index(session, prefix, cancelled)
    return {'accepted': True, 'alreadyRequested': False, 'run': {**saved, 'value': cancelled}}
