"""Shared explicit owner scope and short critical sections for source receipts.

Receipts are task-local metadata. This owner grants neither file/tool access nor
replay permission; domain adapters verify their existing execution authorities.
"""
import threading

from lib.identity import PrincipalContext

source_receipt_lock = threading.RLock()


def source_scope(task):
    from lib.tasks_pkg.manager import task_user_id
    task_user_id(task)
    principal = PrincipalContext.from_payload(task['_principalContext'])
    conversation_id = str(task.get('convId') or '')
    if not conversation_id or task.get('_transientRuntime'):
        raise ValueError('Working sources require durable conversation scope')
    return principal.require_owner(context='working source'), principal.tenant_id, conversation_id


def source_task_is_live(task):
    from lib.task_replay.task_replay import TASK_REPLAY_TERMINAL_STATUSES
    return not task.get('aborted') and task.get('status') not in TASK_REPLAY_TERMINAL_STATUSES
