"""Bounded owned presentation reads hydrated from existing run output.

Entry: presentations(repository, conversation_id, limit, after). Uses canonical
conversation/attempt query ports and identity-only references. Deleted attempts
are hidden without deleting durable run results; cursors advance over references.
"""
import hashlib
import re

from .contract import STORAGE_NAMESPACES
from .errors import AutomationError
from .presentation_index import presentation_prefix, validate_target


def presentations(repository, conversation_id, attempt_id, *, limit, after=''):
    repository.principal.require_scope('automations:read')
    if not isinstance(conversation_id,str) or not 0 < len(conversation_id) <= 256:
        raise AutomationError('invalid_entity_id','Invalid presentation conversation')
    if type(limit) is not int or not 1 <= limit <= 8:
        raise AutomationError('invalid_page_limit','Invalid presentation page limit')
    if not isinstance(after,str) or (after and not re.fullmatch('[a-f0-9]{64}',after)):
        raise AutomationError('invalid_history_cursor','Invalid presentation cursor')
    if not isinstance(attempt_id,str) or not 0 < len(attempt_id) <= 128:
        raise AutomationError('invalid_entity_id','Invalid presentation attempt')
    client = repository._factory()
    source = client.query('turn.attempt.get',{'attempt_id':attempt_id,'user_id':repository.owner_id})
    if source is None or source['conversationId'] != conversation_id:
        raise AutomationError('automation_not_found','Presentation attempt does not exist')
    prefix = presentation_prefix(repository._prefix,conversation_id,attempt_id)
    payload = {'namespace':STORAGE_NAMESPACES['presentation'],'user_id':repository.owner_id,
               'prefix':prefix,'limit':limit}
    if after: payload['after_key'] = prefix + after
    rows = client.query('record.list',payload)
    if not isinstance(rows,list) or len(rows)>limit:
        raise AutomationError('automation_storage_integrity','Invalid presentation page')
    result, cursor = [], ''
    for row in rows:
        pointer = row.get('value') if isinstance(row,dict) else None
        if (not isinstance(pointer,dict) or type(pointer.get('ownerId')) is not int
                or pointer['ownerId'] != repository.owner_id
                or pointer.get('tenantId') != repository.principal.tenant_id
                or pointer.get('conversationId') != conversation_id
                or pointer.get('attemptId') != attempt_id
                or not isinstance(pointer.get('id'),str)
                or not re.fullmatch('[a-f0-9]{64}',pointer['id'])
                or row.get('key') != prefix + pointer['id']
                or (cursor and pointer['id'] <= cursor)):
            raise AutomationError('automation_storage_integrity','Invalid owned presentation reference')
        cursor = pointer['id']
        attempt = source
        entity = repository.get('run',pointer['runId'])
        run = entity.value if entity else None
        state = run['steps'].get(pointer['stepId']) if run else None
        if (not state or state['status'] != 'succeeded'
                or hashlib.sha256(state['effectKey'].encode()).hexdigest() != pointer['id']
                or any(run['trigger']['evidence'].get(key) != pointer.get(key) for key in
                       ('attemptId','turnId','conversationId'))):
            raise AutomationError('automation_storage_integrity','Presentation has no matching run output')
        validate_target(run['trigger'],attempt)
        result.append({key:value for key,value in pointer.items() if key not in {'ownerId','tenantId'}}
                      | {'output':state['output']})
    return {'items':result,'nextCursor':cursor if len(rows)==limit else ''}
