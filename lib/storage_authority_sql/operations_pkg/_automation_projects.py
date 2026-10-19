"""Atomic automation project binding CAS, replay and active ownership proof.

Entry points: _automation_project_update and require_project. Selected SQL
transaction owns registration; root paths are metadata, never filesystem grants.
"""
import hashlib
import time

from lib.automations.contract import STORAGE_NAMESPACES, storage_schema
from lib.automations.projects import project_payload,project_digest,project_record,project_id
from lib.storage.errors import StorageError
from ._records import _record_get,_record_put_owned


def _automation_project_update(session,payload):
    try:
        project_payload(payload)
    except (ValueError,TypeError,KeyError) as exc:
        raise StorageError('database_protocol_error','Invalid project registration') from exc
    if payload['now_ms']-int(time.time()*1000)>storage_schema()['x-limits']['clockSkewMs']:
        raise StorageError('database_protocol_error','Project clock is too far ahead')
    key=payload['project_key'];namespace=STORAGE_NAMESPACES['target']
    session.lock_key('automation_project',key)
    current=_record_get(session,{'namespace':namespace,'key':key})
    try:
        record=project_record(payload,current)
    except (ValueError,TypeError,KeyError) as exc:
        raise StorageError('database_integrity','Project identity differs') from exc
    version=payload['expected_version'];digest=project_digest(payload)
    if current is not None:
        command=current['value'].get('command',{})
        if command.get('id')==payload['request_id'] and command.get('digest')!=digest:
            raise StorageError('database_conflict','Project registration request changed')
        if (current['version']==version+1 and command.get('id')==payload['request_id']
                and command.get('digest')==digest):
            return current
    _record_put_owned(session,{'namespace':namespace,'key':key,
        'value':record,'expected_version':version,'user_id':payload['user_id']})
    return _record_get(session,{'namespace':namespace,'key':key})


def require_project(session,owner,tenant,identity):
    if identity is None:
        return
    prefix=f'{owner}:{hashlib.sha256(tenant.encode()).hexdigest()[:32]}:'
    key=prefix+identity
    session.lock_key('automation_project',key)
    row=_record_get(session,{'namespace':STORAGE_NAMESPACES['target'],'key':key})
    value=row['value'] if row else {}
    if (value.get('contract')!=storage_schema()['x-project']['contract']
            or value.get('id')!=identity or type(value.get('ownerId')) is not int
            or value['ownerId']!=owner or value.get('tenantId')!=(tenant or None)
            or not isinstance(value.get('root'),str) or project_id(value['root'])!=identity
            or value.get('active') is not True):
        raise StorageError('database_conflict','Owned automation project is unavailable')
