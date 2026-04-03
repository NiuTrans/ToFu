"""lib/feishu/events.py — Event handlers for Feishu webhook events.

Processes incoming message events and menu click events from the Lark SDK
dispatcher, routing them to the appropriate command or pipeline handler.
"""

import json
import logging
import threading
import time

from lib.feishu._state import ALLOWED_USERS, get_user_lock
from lib.feishu.commands import MENU_MAP, dispatch_command
from lib.feishu.conversation import clear_pending, get_pending
from lib.feishu.messaging import send_text
from lib.feishu.pipeline import run_task_pipeline

logger = logging.getLogger(__name__)

__all__ = ['handle_message_event', 'handle_menu_event']

# Message deduplication — Feishu may send duplicates on retries
_processed_msgs = {}
_processed_lock = threading.Lock()
_DEDUP_TTL = 300  # seconds


def _is_duplicate(msg_id: str) -> bool:
    """Check if we've already processed this message (dedup)."""
    now = time.time()
    with _processed_lock:
        # Garbage-collect old entries
        expired = [k for k, v in _processed_msgs.items() if now - v > _DEDUP_TTL]
        for k in expired:
            del _processed_msgs[k]
        if msg_id in _processed_msgs:
            return True
        _processed_msgs[msg_id] = now
        return False


def _check_allowed(open_id: str) -> bool:
    """Check if user is in the allowed-users list (empty = allow all)."""
    if not ALLOWED_USERS:
        return True
    return open_id in ALLOWED_USERS


def handle_message_event(event_data) -> None:
    """Handle im.message.receive_v1 — user sent a message to the bot.

    This is the main entry point called by the Lark SDK event dispatcher.
    """
    try:
        # ── Extract fields from event ──
        message_id = ''
        text = ''
        open_id = ''
        chat_id = ''
        msg_type = 'text'

        if hasattr(event_data, 'event') and event_data.event:
            evt = event_data.event
            msg = getattr(evt, 'message', None)
            if msg:
                message_id = getattr(msg, 'message_id', '') or ''
                msg_type = getattr(msg, 'message_type', 'text') or 'text'
                content_str = getattr(msg, 'content', '') or ''
                chat_id = getattr(msg, 'chat_id', '') or ''
                try:
                    content = json.loads(content_str) if content_str else {}
                    text = content.get('text', '').strip()
                except Exception as e:
                    logger.warning('[FeishuBot] Failed to parse SDK message content JSON, falling back to raw text: %s', e, exc_info=True)
                    text = content_str.strip()
            sender = getattr(evt, 'sender', None)
            if sender:
                sender_id = getattr(sender, 'sender_id', None)
                if sender_id:
                    open_id = getattr(sender_id, 'open_id', '') or ''
        elif isinstance(event_data, dict):
            evt = event_data.get('event', event_data)
            msg = evt.get('message', {})
            message_id = msg.get('message_id', '')
            msg_type = msg.get('message_type', 'text')
            content_str = msg.get('content', '')
            chat_id = msg.get('chat_id', '')
            try:
                content = json.loads(content_str) if content_str else {}
                text = content.get('text', '').strip()
            except Exception as e:
                logger.warning('[FeishuBot] Failed to parse dict message content JSON, falling back to raw text: %s', e, exc_info=True)
                text = content_str.strip()
            open_id = evt.get('sender', {}).get('sender_id', {}).get('open_id', '')

        if not text:
            logger.debug('[FeishuBot] Empty message from %s — ignoring', open_id[:8])
            return

        if not _check_allowed(open_id):
            logger.warning('[FeishuBot] Unauthorized user: %s', open_id[:12])
            send_text(message_id, '⛔ 你没有权限使用此机器人', chat_id=chat_id)
            return

        if _is_duplicate(message_id):
            logger.debug('[FeishuBot] Duplicate message %s — skipping', message_id[:12])
            return

        logger.debug('[FeishuBot] Message from %s: %s', open_id[:8], text[:80])

        # ── Process with per-user lock ──
        user_lock = get_user_lock(open_id)
        if not user_lock.acquire(timeout=120):
            send_text(
                message_id,
                '⏳ 上一条消息还在处理中，请稍候...',
                chat_id=chat_id, open_id=open_id,
            )
            return

        try:
            # Check pending state (e.g., project selection awaiting input)
            pending = get_pending(open_id)
            if pending:
                clear_pending(open_id)

            # Try slash command first
            cmd_response = dispatch_command(open_id, text)
            if cmd_response is not None:
                send_text(message_id, cmd_response, chat_id=chat_id, open_id=open_id)
                return

            # Regular message → run LLM pipeline
            response = run_task_pipeline(open_id, text)
            send_text(message_id, response, chat_id=chat_id, open_id=open_id)

        finally:
            user_lock.release()

    except Exception as e:
        logger.error('[FeishuBot] handle_message_event failed: %s', e, exc_info=True)
        try:
            send_text(message_id, f'❌ 内部错误: {e}', chat_id=chat_id, open_id=open_id)
        except Exception as e:
            logger.debug('[FeishuBot] Failed to send error notification back to user: %s', e, exc_info=True)


def handle_menu_event(event_data) -> None:
    """Handle application.bot.menu_v6 event — user clicked a bot menu item."""
    try:
        event_key = ''
        open_id = ''
        chat_id = ''

        if hasattr(event_data, 'event') and event_data.event:
            evt = event_data.event
            event_key = getattr(evt, 'event_key', '') or ''
            operator = getattr(evt, 'operator', None)
            if operator:
                operator_id = getattr(operator, 'operator_id', None)
                if operator_id:
                    open_id = getattr(operator_id, 'open_id', '') or ''
            chat_id = getattr(evt, 'chat_id', '') or ''
        elif isinstance(event_data, dict):
            evt = event_data.get('event', event_data)
            event_key = evt.get('event_key', '')
            open_id = evt.get('operator', {}).get('operator_id', {}).get('open_id', '')
            chat_id = evt.get('chat_id', '')

        if not event_key:
            return

        # Map menu key to command text
        cmd_text = MENU_MAP.get(event_key, f'/{event_key}')
        logger.debug('[FeishuBot] Menu event: key=%s user=%s', event_key, open_id[:8])

        if not _check_allowed(open_id):
            send_text(None, '⛔ 你没有权限使用此机器人', chat_id=chat_id, open_id=open_id)
            return

        cmd_response = dispatch_command(open_id, cmd_text)
        if cmd_response:
            send_text(None, cmd_response, chat_id=chat_id, open_id=open_id)

    except Exception as e:
        logger.error('[FeishuBot] handle_menu_event failed: %s', e, exc_info=True)
