"""Owner-scoped active invocation projection from the existing worker authority.

Used by API task visibility and restart protection. No local execution carrier,
lease clock guess, cache or second task registry participates in this read.
"""
from lib.identity import require_user_id
from lib.storage.errors import StorageError


def list_active_conversation_tasks(*, user_id=None):
    from lib.conversation_sync.worker_execution import conversation_registration
    from lib.storage import get_storage_client
    owner = require_user_id(user_id, context='worker activity') if user_id is not None else None
    kind = conversation_registration().task_kind
    rows = get_storage_client().query('worker_job.active.list',
        {'user_id': owner, 'task_kinds': [kind]})
    fields = {'taskId', 'userId', 'taskKind', 'conversationId', 'status', 'createdAtMs'}
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or set(row) != fields
        or not isinstance(row['taskId'], str) or not row['taskId']
        or type(row['userId']) is not int or row['userId'] <= 0
        or (owner is not None and row['userId'] != owner)
        or row['taskKind'] != kind or not isinstance(row['conversationId'], str)
        or row['status'] not in {'queued', 'running', 'suspended'}
        or type(row['createdAtMs']) is not int or row['createdAtMs'] < 0
        for row in rows
    ):
        raise StorageError('database_protocol_error', 'Invalid active conversation worker projection')
    return rows
