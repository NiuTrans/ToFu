"""Reconstructible pending-action lookup, backed by immutable Research journals.

Entries: retain_pending / retire_settled. An index entry grants no execution,
replay or budget. Startup validates it against the original binding and claim.
Only this disposable namespace is deleted; evidence and settlements stay intact.
"""
from copy import deepcopy
import uuid
from lib.storage.errors import StorageError
from .action_observations import _validate,_record_value,_task_prefix,_digest

NAMESPACE='research.pending_action_bindings.v1'


def retain_pending(client,binding):
    _validate(binding,'binding')
    _,key=_task_prefix(binding['user_id'],binding['task_id'])
    value={'schema_version':1,'binding':deepcopy(binding)}
    def exact():
        row=client.query('record.get',{'namespace':NAMESPACE,'key':key})
        return row is not None and _record_value(row,'admission')==value
    row=client.query('record.get',{'namespace':NAMESPACE,'key':key})
    if row is not None:
        if _record_value(row,'admission')!=value:
            raise StorageError('database_integrity','Research pending lookup binding conflicts')
        return
    # A rebuilt cache row must not replay the receipt of an earlier deleted row.
    try:
        client.command('record.put',{'namespace':NAMESPACE,'key':key,'value':value,
            'expected_version':0},'research-pending:'+uuid.uuid4().hex)
    except StorageError:
        if not exact():raise
    if not exact():
        raise StorageError('database_integrity','Research pending lookup was not exactly acknowledged')


def retire_settled(client,*,task_id,user_id):
    from .action_terminal import NAMESPACE as TERMINAL_NAMESPACE
    owner,key=_task_prefix(user_id,task_id)
    row=client.query('record.get',{'namespace':TERMINAL_NAMESPACE,'key':key})
    if row is None:return False
    terminal=_record_value(row,'actionTerminal')
    if terminal['user_id']!=owner or terminal['task_id']!=task_id:
        raise StorageError('database_integrity','Research pending retirement settlement identity differs')
    if 'result' in terminal:
        digest=_digest(terminal['result']) if terminal['result'] is not None else None
        if digest!=terminal['result_digest']:
            raise StorageError('database_integrity','Research pending retirement settlement digest differs')
    if client.query('record.get',{'namespace':NAMESPACE,'key':key}) is None:return True
    try:
        client.command('record.delete',{'namespace':NAMESPACE,'key':key},
            'research-pending-retire:'+uuid.uuid4().hex)
    except StorageError:
        if client.query('record.get',{'namespace':NAMESPACE,'key':key}) is not None:raise
    if client.query('record.get',{'namespace':NAMESPACE,'key':key}) is not None:
        raise StorageError('database_integrity','Research pending lookup retirement was not acknowledged')
    return True
