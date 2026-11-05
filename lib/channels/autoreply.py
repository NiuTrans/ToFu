"""Auto-reply: a Global Router answer to a channel command returns to its thread (C5).

Responsibility: observe settled tasks through the existing terminal-listener
seam; when the owner's Global Router finishes a Turn whose input was a genuine
human command-chat message (``origin.channel``), enqueue the final assistant
answer to that thread with idempotency key = the Turn ID. The listener does
bounded storage work only; sending belongs to the outbox executor.

Entry points: ``install()`` (idempotent), ``on_task_terminal(task)``.
Dependencies: ``lib.tasks_pkg.manager._terminal`` (seam), ``lib.turn_lifecycle``,
``lib.channels.outbox``.
"""
from __future__ import annotations

import threading
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

_INSTALLED = False
_INSTALL_LOCK = threading.Lock()


def install() -> bool:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return False
        from lib.tasks_pkg.manager._terminal import register_terminal_listener
        register_terminal_listener(on_task_terminal)
        _INSTALLED = True
        return True


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


def on_task_terminal(task: dict[str, Any]) -> None:
    if (task.get('status') != 'done' or task.get('aborted')
            or task.get('finishReason') in ('aborted', 'error')
            or task.get('_parentTaskId') or task.get('_transientRuntime')):
        return
    owner = task.get('_userId')
    conversation_id = task.get('convId')
    turn_id = task.get('_turnId') or (task.get('config') or {}).get('_turnId')
    text = str(task.get('content') or '').strip()
    if not owner or not conversation_id or not turn_id or not text:
        return
    from lib.channels.store import ChannelStore
    store = ChannelStore(int(owner))
    account = store.account('dx')
    if not account or not account.get('enabled'):
        return
    from lib.channels.delivery import is_global_router
    if not is_global_router(int(owner), str(conversation_id)):
        return
    channel = reply_origin(int(owner), str(conversation_id), str(turn_id))
    if channel is None:
        return
    from lib.channels import outbox
    from lib.channels.model import ChannelError
    try:
        outbox.enqueue(int(owner), channel['thread'], text, idempotency_key=str(turn_id),
                       origin={'kind': 'router_reply', 'conversationId': str(conversation_id),
                               'turnId': str(turn_id)}, store=store)
    except ChannelError as error:
        logger.warning('[Channels] router reply not enqueued turn=%s: %s', turn_id, error.code)


__all__ = ['install', 'on_task_terminal', 'reply_origin']
