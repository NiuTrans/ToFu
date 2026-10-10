"""Atomic owner approval and canonical wakeup; no executor authority is granted.

Entry point: _automation_run_approve. Run revision, immutable argument/action
proof and wake observation commit together; transport failure never fakes approval.
"""
import hashlib
import time

from lib.automations.approval import prepare_approval, request_digest
from lib.automations.contract import storage_input_validator
from lib.automations.errors import AutomationConflict
from ._automations import _fail, _record, _put, _index, RUN_NAMESPACE
from ._worker_jobs import _worker_job_wake_owned


def _automation_run_approve(session, payload):
    if next(storage_input_validator('approve').iter_errors(payload), None) is not None:
        _fail('Automation approval does not match its storage contract')
    owner, tenant, key = payload['user_id'], payload['tenant_id'], payload['run_key']
    prefix = f'{owner}:{hashlib.sha256(tenant.encode()).hexdigest()[:32]}:'
    if not key.startswith(prefix) or payload['now_ms'] - int(time.time()*1000) > 86400000:
        _fail('Automation approval identity or clock is invalid')
    session.lock_key('automation_run', key)
    current = _record(session, RUN_NAMESPACE, key)
    if current is None:
        _fail('Automation run does not exist', 'database_not_found')
    run = current['value']
    if (key != prefix + run['id'] or run['ownerId'] != owner
            or (run['tenantId'] or '') != tenant
            or run['jobId'] != 'automation-run:' + hashlib.sha256(key.encode()).hexdigest()):
        _fail('Automation approval escaped its owner')
    previous = run['steps'].get(payload['step_id'], {}).get('approval')
    if previous:
        if (previous['requestId'] != payload['request_id']
                or previous['requestDigest'] != request_digest(payload)):
            _fail('Approval request evidence changed', 'database_conflict')
        return {'accepted': True, 'alreadyApproved': True, 'run': current}
    if current['version'] != payload['expected_version']:
        _fail('Automation approval revision changed', 'database_conflict')
    try:
        updated = prepare_approval(run, payload)
    except AutomationConflict as exc:
        _fail(str(exc), 'database_conflict')
    except (ValueError, PermissionError, KeyError) as exc:
        _fail(str(exc))
    job = _worker_job_wake_owned(session, run['jobId'], owner, tenant, payload['now_ms'])
    if job is None or job['cancelSequence']:
        _fail('Approval requires a noncancelled canonical job', 'database_conflict')
    saved = _put(session, RUN_NAMESPACE, key, updated, current['version'])
    _index(session, prefix, updated)
    return {'accepted': True, 'alreadyApproved': False, 'run': {**saved, 'value': updated}}

