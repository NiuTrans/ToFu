"""Immutable child birth evidence in the existing task-result authority."""
from collections.abc import Mapping
import json
import time

from lib.storage.errors import StorageError
from ._common import _required_text, _integer
from ._records import _record_get


def worker_root_for_task(session, task_id, *, user_id=None):
    """Follow immutable births, without inflating transcript/tool payloads.

    The stored parent chain selects authority. Omitting the caller's claim or
    using an arbitrary child ID cannot turn a descendant into legacy work.
    """
    current, seen, conversation = task_id, set(), None
    while True:
        if current in seen:
            raise StorageError('database_integrity', 'Child invocation ancestry is cyclic')
        seen.add(current)
        if current.startswith('conversation-attempt:'):
            return current
        row = session.fetch_one(
            'SELECT value_json FROM storage_records WHERE namespace=? AND record_key=?',
            ('task_results', current))
        if row is None:
            return None
        from ._common import _load
        value = _load(row['value_json'])
        identity = value.get('child_invocation') if isinstance(value, Mapping) else None
        if not identity:
            return None
        if not isinstance(identity, Mapping):
            raise StorageError('database_integrity', 'Child invocation identity is malformed')
        owner = identity.get('user_id')
        if type(owner) is not int or owner < 1 or (user_id is not None and owner != user_id):
            raise StorageError('database_not_found', 'Task not found')
        user_id = owner
        conv, parent = identity.get('conversation_id'), identity.get('parent_task_id')
        if (identity.get('child_task_id') != current or value.get('user_id') != owner
                or value.get('conv_id') != conv or not isinstance(conv, str) or not conv
                or (conversation is not None and conversation != conv)
                or not isinstance(parent, str) or not parent):
            raise StorageError('database_integrity', 'Child invocation ancestry is malformed')
        conversation, current = conv, parent


def worker_root_for_event_stream(session, task_id, *, user_id=None):
    """Inspector names identify a registered child, never a separate executor."""
    parent, separator, child = task_id.partition('#agent:')
    if not separator:
        return worker_root_for_task(session, task_id, user_id=user_id)
    parent_root = worker_root_for_task(session, parent, user_id=user_id)
    child_root = worker_root_for_task(session, child, user_id=user_id)
    if parent_root is None and child_root is None:
        return None
    from ._common import _load
    row = session.fetch_one(
        'SELECT value_json FROM storage_records WHERE namespace=? AND record_key=?',
        ('task_results', child))
    value = _load(row['value_json']) if row is not None else {}
    birth = value.get('child_invocation') or {}
    if (parent_root != child_root or parent_root is None
            or birth.get('parent_task_id') != parent or birth.get('child_task_id') != child):
        raise StorageError('database_protocol_error', 'Inspector stream differs from child birth')
    return parent_root


def starts_new_tool_effect(session, task_id, incoming, *, intent=None):
    """Only newly introduced operation IDs need effect-entry authority."""
    if 'tool_rounds' not in incoming:
        if intent is not None:
            raise StorageError('database_protocol_error', 'Tool entry requires its exact invocation receipt')
        return False
    from ._common import _load
    from ._records import _hydrated_task_result_value

    def rows(value):
        raw = value.get('tool_rounds') or []
        try:
            result = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError) as exc:
            raise StorageError('database_protocol_error', 'Invalid child tool receipts') from exc
        if not isinstance(result, list):
            raise StorageError('database_protocol_error', 'Invalid child tool receipts')
        return result

    current = session.fetch_one(
        'SELECT value_json FROM storage_records WHERE namespace=? AND record_key=?',
        ('task_results', task_id))
    value = (_hydrated_task_result_value(_load(current['value_json']), fields={'tool_rounds'})
             if current is not None else {})
    if intent is not None:
        selected = [row for row in rows(incoming) if isinstance(row, Mapping)
                    and isinstance(effect := row.get('executionEffect'), Mapping)
                    and effect.get('operationId') == intent]
        if (len(selected) != 1 or selected[0].get('taskId') != task_id
                or selected[0]['executionEffect'].get('scope') != 'invocation'
                or selected[0]['executionEffect'].get('state') != 'entered'
                or selected[0]['executionEffect'].get('reconciliation') is not None):
            raise StorageError('database_protocol_error', 'Tool entry requires its exact invocation receipt')
        previous = [row for row in rows(value) if isinstance(row, Mapping)
                    and isinstance(effect := row.get('executionEffect'), Mapping)
                    and effect.get('operationId') == intent]
        if previous and previous != selected:
            raise StorageError('database_conflict', 'Tool effect admission changed its occurrence')
        return True
    def operations(value, *, entered_only=False):
        result = set()
        for row in rows(value):
            effect = row.get('executionEffect') if isinstance(row, Mapping) else None
            if not isinstance(effect, Mapping):
                continue
            operation = effect.get('operationId')
            if not isinstance(operation, str) or not operation:
                raise StorageError('database_protocol_error', 'Invalid child effect identity')
            if not entered_only or effect.get('state') == 'entered':
                result.add(operation)
        return result

    return bool(operations(incoming, entered_only=True) - operations(value))


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
    if child == parent or child.startswith('conversation-attempt:'):
        raise StorageError('database_conflict', 'Child identity conflicts with its ancestry')
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
    root = worker_root_for_task(session, parent, user_id=user_id)
    if root is not None or payload.get('worker_claim') is not None:
        from ._worker_jobs import _require_conversation_publication_claim
        _require_conversation_publication_claim(session,
            attempt_id=(root or parent).removeprefix('conversation-attempt:'),
            task_id=root or parent, user_id=user_id, proof=payload.get('worker_claim'))
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
