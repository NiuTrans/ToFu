"""Immutable child birth evidence in the existing task-result authority."""
from collections.abc import Mapping
import time

from lib.storage.errors import StorageError
from ._common import _required_text, _integer
from ._records import _record_get


def require_child(session, scope, conversation_id, user_id):
    identity = resolve_child(session, {'archive_scope': scope, 'user_id': user_id})
    if (identity.get('archive_scope') != scope
            or identity.get('conversation_id') != conversation_id
            or identity.get('user_id') != user_id):
        raise StorageError('database_not_found', 'Child invocation not found')
    return identity


def register_child(session, payload):
    user_id = _integer(payload, 'user_id', minimum=1)
    conv = _required_text(payload, 'conversation_id')
    parent = _required_text(payload, 'parent_task_id')
    child = _required_text(payload, 'child_task_id')
    scope = f'invocation:{user_id}:{child}'
    identity = dict(user_id=user_id, conversation_id=conv,
                    parent_task_id=parent, child_task_id=child, archive_scope=scope)
    session.lock_key('conversation', f'{user_id}:{conv}')
    if session.fetch_one('SELECT 1 AS present FROM storage_conversations WHERE id=? AND user_id=?', (conv, user_id)) is None:
        raise StorageError('database_not_found', 'Conversation not found')
    parent_row = _record_get(session, {'namespace': 'task_results', 'key': parent})
    value = (parent_row or {}).get('value') or {}
    if value.get('user_id') != user_id or value.get('conv_id') != conv:
        bound = session.fetch_one(
            'SELECT 1 AS present FROM storage_generation_attempts AS a '
            'JOIN storage_conversations AS c ON c.id=a.conversation_id '
            'WHERE a.task_id=? AND a.conversation_id=? AND c.user_id=?',
            (parent, conv, user_id))
        if bound is None:
            raise StorageError('database_not_found', 'Parent task not found')
    # Child task ID itself is the immutable global identity, independent of scope.
    session.lock_key('record', f'task_results:{child}')
    current_child = _record_get(session, {'namespace': 'task_results', 'key': child})
    if current_child:
        if current_child['value'].get('child_invocation') != identity:
            raise StorageError('database_conflict', 'Child identity is immutable')
    else:
        from ._records import _encoded_task_result_document
        document = {'task_id': child, 'user_id': user_id, 'conv_id': conv,
                    'status': 'registered', 'created_at': int(time.time()),
                    'child_invocation': identity}
        session.execute(
            'INSERT INTO storage_records(namespace,record_key,value_json,version,updated_at_ms) '
            'VALUES (?,?,?,?,?)',
            ('task_results', child, _encoded_task_result_document(document), 1,
             int(time.time() * 1000)))
    return identity


def resolve_child(session, payload):
    scope = _required_text(payload, 'archive_scope')
    user_id = _integer(payload, 'user_id', minimum=1)
    # Syntax locates a record; exact persisted identity authorizes it.
    prefix = f'invocation:{user_id}:'
    child = scope[len(prefix):] if scope.startswith(prefix) else ''
    row = _record_get(session, {'namespace': 'task_results', 'key': child or scope})
    identity = ((row or {}).get('value') or {}).get('child_invocation') or {}
    if identity.get('archive_scope') != scope or identity.get('user_id') != user_id:
        raise StorageError('database_not_found', 'Child invocation not found')
    return identity


def guard_child_record(session, key, incoming):
    current = _record_get(session, {'namespace': 'task_results', 'key': key})
    identity = ((current or {}).get('value') or {}).get('child_invocation')

    if not identity and isinstance(incoming, Mapping) and incoming.get('child_invocation'):
        raise StorageError('database_protocol_error', 'Child birth requires invocation registration')
    if identity and (not isinstance(incoming, Mapping)
                     or incoming.get('child_invocation') != identity
                     or incoming.get('task_id') != identity['child_task_id']
                     or incoming.get('user_id') != identity['user_id']
                     or incoming.get('conv_id') != identity['conversation_id']):
        raise StorageError('database_conflict', 'Child identity is immutable')
