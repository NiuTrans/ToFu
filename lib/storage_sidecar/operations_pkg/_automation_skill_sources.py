"""Atomic owned skill-source setting CAS and exact semantic replay.

Entry point: _automation_skill_source_update. Uses the selected SQL authority's
record row and source advisory lock. Vault values never enter this transaction.
"""
import time

from lib.automations.contract import storage_schema
from lib.skills.automation.source_records import (
    SOURCE_NAMESPACE,source_payload,source_digest,source_record,
)
from lib.storage.errors import StorageError
from ._records import _record_get,_record_put_owned


def _automation_skill_source_update(session,payload):
    try:source_payload(payload)
    except (ValueError,TypeError,KeyError) as exc:
        raise StorageError('database_protocol_error','Invalid owned skill source setting') from exc
    if payload['now_ms']-int(time.time()*1000)>storage_schema()['x-limits']['clockSkewMs']:
        raise StorageError('database_protocol_error','Source clock is too far ahead')
    key=payload['source_key']
    session.lock_key('automation_skill_source',key)
    current=_record_get(session,{'namespace':SOURCE_NAMESPACE,'key':key})
    try:record=source_record(payload,current)
    except (ValueError,TypeError,KeyError) as exc:
        raise StorageError('database_integrity','Stored skill source identity differs') from exc
    if current is not None:
        current.pop('key',None)
    version=payload['expected_version'];digest=source_digest(payload)
    if current is not None:
        command=current['value']['command']
        if command['id']==payload['request_id'] and command['digest']!=digest:
            raise StorageError('database_conflict','Skill source registration request changed')
        if current['version']==version+1 and command['id']==payload['request_id'] and command['digest']==digest:
            return current
    _record_put_owned(session,{'namespace':SOURCE_NAMESPACE,'key':key,'value':record,
        'expected_version':version,'user_id':payload['user_id']})
    accepted=_record_get(session,{'namespace':SOURCE_NAMESPACE,'key':key})
    accepted.pop('key',None)
    return accepted
