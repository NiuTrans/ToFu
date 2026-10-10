"""Read-only owned unfinished Research actions for cold workbench reopening.

Entry: discover_unfinished_actions. Lookup caches accelerate ordering; original
bindings, settlement and execution process evidence define inspected status.
No private message/tool chunks, writes, worker, replay or budget grant is added.
"""
from lib.identity import require_user_id
from lib.storage.errors import StorageError
from .action_observations import BINDING_NAMESPACE,_record_value,_task_prefix,_digest
from .action_pending import NAMESPACE as PENDING_NAMESPACE
from .persistence import research_direction_hash


def _settled_binding(client,binding):
    from .action_terminal import NAMESPACE as TERMINAL_NAMESPACE
    owner=binding['user_id'];task_id=binding['task_id'];key=_task_prefix(owner,task_id)[1]
    row=client.query('record.get',{'namespace':TERMINAL_NAMESPACE,'key':key})
    if row is not None:
        terminal=_record_value(row,'actionTerminal')
        if (terminal['user_id']!=owner or terminal['task_id']!=task_id
                or terminal['direction_hash']!=binding['direction_hash'] or terminal['lang']!=binding['lang']):
            raise StorageError('database_integrity','Research unfinished settlement identity differs')
        if 'result' in terminal:
            digest=_digest(terminal['result']) if terminal['result'] is not None else None
            if digest!=terminal['result_digest']:
                raise StorageError('database_integrity','Research unfinished settlement digest differs')
        return True
    return False


def _inspect_binding(client,binding,direction,*,journal_binding=False):
    from .execution_owner import ActionExecutionOwners
    from .request_context import RequestContexts
    from .action_runtime import _research_action_runtime
    owner=binding['user_id'];task_id=binding['task_id']
    # Original list rows already passed the binding schema and owner boundary.
    if journal_binding and _settled_binding(client,binding):return None
    repo=ActionExecutionOwners.open(client,user_id=owner,task_id=task_id)
    if repo is None:
        return {'id':task_id,'kind':'research-action','status':'unknown',
            'meta':{'direction':direction,'lang':binding['lang'],'action':'unknown'},'replay_allowed':False}
    if repo.binding!=binding:
        # Cached projections cannot override the immutable original scope.
        return None
    if not journal_binding and _settled_binding(client,binding):return None
    live=_research_action_runtime.get_owned(task_id,user_id=owner)
    definition=RequestContexts.open(client,user_id=owner,task_id=task_id).load_action_definition(1)
    action=definition['action'] if definition else 'unknown'
    if live is not None and live['status'] in ('pending','running'):
        status=live['status']
    else:
        state=repo.inspect_owner()
        status='interrupted' if state['found'] and state['process_state']=='dead' else 'unknown'
    return {'id':task_id,'kind':'research-action','status':status,
        'meta':{'direction':direction,'lang':binding['lang'],'action':action,
                'expected_revision':binding['revision']},'replay_allowed':False}


def discover_unfinished_actions(direction,*,lang='en',user_id):
    from lib.storage import get_storage_client
    owner=require_user_id(user_id,context='research unfinished action owner')
    if not isinstance(direction,str) or not direction.strip() or len(direction)>2000:
        raise ValueError('direction must be non-empty and at most 2000 characters')
    if lang not in ('en','zh'):raise ValueError('lang must be en or zh')
    direction=direction.strip();scope=research_direction_hash(direction)
    client=get_storage_client();candidates={}
    for namespace in (PENDING_NAMESPACE,BINDING_NAMESPACE):
        cursor=''
        try:
            while True:
                rows=client.query('record.list',{'namespace':namespace,'prefix':f'{owner}:',
                    'after_key':cursor,'limit':16})
                if not isinstance(rows,list) or len(rows)>16:
                    raise StorageError('database_integrity','Research unfinished page is invalid')
                for row in rows:
                    key=row.get('key') if isinstance(row,dict) else None
                    if not isinstance(key,str) or key<=cursor:
                        raise StorageError('database_integrity','Research unfinished cursor is invalid')
                    cursor=key
                    binding=_record_value(row,'admission')['binding']
                    if binding['user_id']!=owner or _task_prefix(owner,binding['task_id'])[1]!=key:
                        raise StorageError('database_integrity','Research unfinished owner boundary is invalid')
                    if binding['direction_hash']!=scope or binding['lang']!=lang:continue
                    if binding['task_id'] in candidates:continue
                    task=_inspect_binding(client,binding,direction,journal_binding=namespace==BINDING_NAMESPACE)
                    if task is not None:candidates[task['id']]=task
                    if len(candidates)==2:
                        return {'tasks':list(candidates.values()),'complete':False}
                if len(rows)<16:break
        except StorageError:
            if namespace==BINDING_NAMESPACE:raise
            # The original owner-scoped pass still determines completeness.
            candidates.clear()
    return {'tasks':list(candidates.values()),'complete':True}
