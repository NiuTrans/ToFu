"""Return Global Router command-chat receipts and visible progress (C5).

The command chat gets an immediate receipt after its message is accepted,
non-deliverable assistant text as each round becomes canonical, progress-board
summaries, the final answer, and an explicit terminal status. Only genuine
human command-chat Turns are mirrored. Reasoning, tool arguments/results and
internal phase labels never cross the channel boundary.

``on_task_event`` is a passive Push observer: it enqueues durable outbox rows
and never performs model or network calls. ``on_task_terminal`` remains on the
task engine's settled-listener seam. Translation happens inside the ordered
outbox, so asynchronous observers cannot reorder the messages.
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

_INSTALLED = False
_INSTALL_LOCK = threading.Lock()
_RECEIPT_LABELS = {
    'started': '收到，总负责人已开始处理。',
    'steered': '收到，已并入总负责人正在处理的任务。',
    'queued': '收到，已排队，前一项处理完后开始。',
}
_TERMINAL_LABELS = {
    'done': '总负责人处理完成。',
    'aborted': '总负责人已中止处理。',
    'error': '总负责人处理失败，请重新发送或到 Tofu 查看详情。',
    'interrupted': '总负责人处理中断，请重新发送或到 Tofu 查看详情。',
}


def install() -> bool:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return False
        from lib.tasks_pkg.manager._terminal import register_terminal_listener
        from lib.agent_core.push import hub
        register_terminal_listener(on_task_terminal)
        hub.add_listener(on_task_event)
        _INSTALLED = True
        return True


def _channel(store, owner: int, conversation_id: str, turn_id: str) -> dict[str, Any] | None:
    from lib.channels.delivery import is_global_router
    if not is_global_router(owner, conversation_id):
        return None
    return reply_origin(owner, conversation_id, turn_id)


def _enqueue(store, owner: int, channel: dict[str, Any], conversation_id: str, turn_id: str,
             kind: str, text: str, key: str, *, reply_to: str | None = None) -> None:
    text = str(text or '').strip()
    if not text:
        return
    from lib.channels import outbox
    from lib.channels.model import ChannelError
    try:
        outbox.enqueue(owner, channel['thread'], text, idempotency_key=key, reply_to=reply_to,
                       origin={'kind': kind, 'conversationId': conversation_id, 'turnId': turn_id},
                       store=store)
    except ChannelError as error:
        logger.warning('[Channels] %s not enqueued turn=%s: %s', kind, turn_id, error.code)


def acknowledge_command(owner: int, conversation_id: str, event, disposition: str) -> None:
    """Receipt after dispatch acceptance, keyed by the provider event, not a Turn."""
    label = _RECEIPT_LABELS.get(disposition)
    if label is None:
        return
    from lib.channels.store import ChannelStore
    store = ChannelStore(int(owner))
    channel = {'channel': event.channel, 'thread': str(event.thread), 'eventIds': [event.event_id]}
    _enqueue(store, int(owner), channel, str(conversation_id), '', 'router_receipt', label,
             'receipt:' + event.event_id, reply_to=event.event_id)


def reply_origin(owner: int, conversation_id: str, turn_id: str) -> dict[str, Any] | None:
    """The ``origin.channel`` of the genuine human input that started ``turn_id``."""
    from lib.turn_initiation import require_human_turn
    from lib.turn_lifecycle import get_turn
    output = get_turn(conversation_id, turn_id, user_id=owner)
    parent = (output or {}).get('parentTurnId')
    if not parent:
        return None
    human = get_turn(conversation_id, parent, user_id=owner)
    try:
        require_human_turn(human)
    except ValueError:
        return None
    channel = ((human.get('projection') or {}).get('origin') or {}).get('channel')
    return channel if isinstance(channel, dict) and channel.get('thread') else None


def _command_receipt_pending(task: dict[str, Any]) -> bool:
    """A queued command starts after the receipt said it was queued, not started."""
    from lib.turn_lifecycle import get_attempt
    attempt_id = str(task.get('_attemptId') or '')
    if not attempt_id:
        return False
    attempt = get_attempt(attempt_id, user_id=int(task['_userId']))
    return str((attempt or {}).get('commandId') or '').startswith('channel-owner-')


def _publish_segments(task: dict[str, Any], store, channel: dict[str, Any],
                      conversation_id: str, turn_id: str) -> None:
    """Mirror canonical non-deliverable text once per block and text revision."""
    segments = task.get('segments')
    if not isinstance(segments, list):
        from lib.tasks_pkg.segments import assemble_segments
        segments = assemble_segments(task)
    from lib.tool_results.tool_round_replay import SUPERSEDED_PROVIDER_ATTEMPT_FIELD
    for segment in segments:
        if (segment.get('type') != 'text' or segment.get('deliverable') is not False
                or segment.get(SUPERSEDED_PROVIDER_ATTEMPT_FIELD)):
            continue
        text = str(segment.get('text') or '').strip()
        block_id = str(segment.get('blockId') or '').strip()
        if not text or not block_id:
            continue
        revision = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
        _enqueue(store, int(task['_userId']), channel, conversation_id, turn_id,
                 'router_content', text, f'{turn_id}:content:{block_id}:{revision}')


def _publish_progress(task: dict[str, Any], store, channel: dict[str, Any],
                      conversation_id: str, turn_id: str, payload: dict[str, Any]) -> None:
    if payload.get('toolName') != 'update_task_progress' or payload.get('status'):
        return
    try:
        content = json.loads(payload.get('toolContent') or '{}')
    except (TypeError, ValueError):
        return
    revision = content.get('revision')
    if not isinstance(revision, int):
        return
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.channels.delivery import global_router_binding
    from lib.conversations.project_identity import normalize_project_path
    binding = global_router_binding(int(task['_userId']), create=False)
    board = CoordinatorTaskProgress(
        int(task['_userId']), normalize_project_path(binding['projectPath'])).read().get('board') or {}
    if board.get('revision') != revision:
        return
    summary = str(board.get('summary') or '').strip()
    if not summary:
        return
    _enqueue(store, int(task['_userId']), channel, conversation_id, turn_id,
             'router_progress', '进度：' + summary, f'{turn_id}:progress:{revision}')


def on_task_event(stream: str, task_id: str, payload: dict[str, Any]) -> None:
    """Observe durable-before-visible chat frames; ignore every other stream."""
    if stream != 'chat' or payload.get('type') not in ('tool_start', 'tool_complete', 'round_committed'):
        return
    try:
        owner = int(payload.get('_ownerUserId') or 0)
        conversation_id = str(payload.get('conversationId') or '')
        turn_id = str(payload.get('turnId') or '')
        if not owner or not conversation_id or not turn_id:
            return
        from lib.channels.store import ChannelStore
        store = ChannelStore(owner)
        account = store.account('dx')
        if not account or not account.get('enabled'):
            return
        channel = _channel(store, owner, conversation_id, turn_id)
        if channel is None:
            return
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        task = chat_task_runtime.get_owned(task_id, user_id=owner)
        if task is None or task.get('_parentTaskId') or task.get('_transientRuntime'):
            return
        if _command_receipt_pending(task):
            _enqueue(store, owner, channel, conversation_id, turn_id, 'router_receipt',
                     _RECEIPT_LABELS['started'], f'{turn_id}:started')
        _publish_progress(task, store, channel, conversation_id, turn_id, payload)
        if payload.get('type') != 'tool_complete' or payload.get('toolName') == 'update_task_progress':
            _publish_segments(task, store, channel, conversation_id, turn_id)
    except Exception:
        logger.warning('[Channels] router progress observer failed task=%s', task_id, exc_info=True)


def _terminal_outcome(task: dict[str, Any]) -> str:
    if task.get('aborted') or task.get('finishReason') == 'aborted':
        return 'aborted'
    if task.get('status') == 'done' and task.get('finishReason') != 'error':
        return 'done'
    if task.get('status') == 'interrupted':
        return 'interrupted'
    return 'error'


def on_task_terminal(task: dict[str, Any]) -> None:
    if task.get('_parentTaskId') or task.get('_transientRuntime'):
        return
    owner = task.get('_userId')
    conversation_id = str(task.get('convId') or '')
    turn_id = str(task.get('_turnId') or (task.get('config') or {}).get('_turnId') or '')
    if not owner or not conversation_id or not turn_id:
        return
    from lib.channels.store import ChannelStore
    store = ChannelStore(int(owner))
    account = store.account('dx')
    if not account or not account.get('enabled'):
        return
    channel = _channel(store, int(owner), conversation_id, turn_id)
    if channel is None:
        return
    outcome = _terminal_outcome(task)
    if outcome == 'done':
        _enqueue(store, int(owner), channel, conversation_id, turn_id, 'router_reply',
                 str(task.get('content') or ''), turn_id)
    _enqueue(store, int(owner), channel, conversation_id, turn_id, 'router_status',
             _TERMINAL_LABELS[outcome], f'{turn_id}:terminal:{outcome}')


__all__ = ['acknowledge_command', 'install', 'on_task_event', 'on_task_terminal', 'reply_origin']
