"""Post-readiness recovery prioritizes pending lookup, then reconciles journals.

Entry: resume_interrupted_actions. The operator supplies each validated owner
explicitly to ordinary resume. Cached entries grant no authority; immutable
bindings and execution/checkpoint/billing proofs remain the source of truth.
"""
from lib.log import get_logger
from lib.storage.errors import StorageError
from .action_observations import BINDING_NAMESPACE,_record_value,_task_prefix

logger=get_logger(__name__)


def _stopped(stop_event):
    return stop_event is not None and stop_event.is_set()


def _resume_binding(client,row,namespace,stop_event):
    from .action_terminal import NAMESPACE as TERMINAL_NAMESPACE
    from .action_pending import NAMESPACE as PENDING_NAMESPACE,retain_pending,retire_settled
    from .execution_owner import ActionExecutionOwners
    from .action_continuation import resume_action
    from .action_runtime import _research_action_runtime
    binding=_record_value(row,'admission')['binding']
    owner=binding['user_id'];task_id=binding['task_id'];key=row['key']
    if _task_prefix(owner,task_id)[1]!=key:
        raise StorageError('database_integrity','Research startup binding identity is invalid')
    if _research_action_runtime.get_owned(task_id,user_id=owner) is not None:return False
    if client.query('record.get',{'namespace':TERMINAL_NAMESPACE,'key':key}) is not None:
        if namespace==PENDING_NAMESPACE:
            try:retire_settled(client,task_id=task_id,user_id=owner)
            except Exception as exc:logger.warning('Research pending lookup cleanup deferred: %s',exc)
        return False
    owners=ActionExecutionOwners.open(client,user_id=owner,task_id=task_id)
    if owners is not None and owners.binding!=binding:
        raise StorageError('database_integrity','Research pending lookup differs from its journal binding')
    state=owners.inspect_owner() if owners is not None else None
    if state is None or not state['found'] or state['process_state']!='dead':return False
    if _stopped(stop_event):return False
    if namespace==BINDING_NAMESPACE:
        try:retain_pending(client,binding)
        except Exception as exc:logger.warning('Research pending lookup repair deferred: %s',exc)
    if _stopped(stop_event):return False
    result=resume_action(task_id,user_id=owner)
    return result['resumed'] and not result['deduped']


def resume_interrupted_actions(*,stop_event=None):
    from lib.storage import get_storage_client
    from .action_pending import NAMESPACE as PENDING_NAMESPACE
    if _stopped(stop_event):return 0
    client=get_storage_client(write=True);resumed=0
    for namespace in (PENDING_NAMESPACE,BINDING_NAMESPACE):
        cursor=''
        try:
            while True:
                if _stopped(stop_event):return resumed
                rows=client.query('record.list',{'namespace':namespace,'prefix':'',
                    'after_key':cursor,'limit':16})
                if not isinstance(rows,list) or len(rows)>16:
                    raise StorageError('database_integrity','Research startup binding page is invalid')
                for row in rows:
                    if _stopped(stop_event):return resumed
                    key=row.get('key') if isinstance(row,dict) else None
                    if not isinstance(key,str) or not key or key<=cursor:
                        raise StorageError('database_integrity','Research startup binding cursor is invalid')
                    cursor=key
                    try:
                        resumed+=int(_resume_binding(client,row,namespace,stop_event))
                    except Exception as exc:
                        logger.warning('Research action startup continuation refused: %s',exc)
                if len(rows)<16:break
        except Exception as exc:
            if namespace==BINDING_NAMESPACE:raise
            logger.warning('Research pending lookup scan deferred to original journals: %s',exc)
    return resumed
