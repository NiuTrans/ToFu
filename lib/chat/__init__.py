"""lib.chat — chat-domain helpers that used to live in ``routes/chat.py``
but are imported by lib modules (message_queue, etc.).

Relocated here (2026-06) to break the ``lib → routes`` circular-import
coupling: ``lib/message_queue`` reached UP into ``routes.chat`` for
``_append_user_msg_idempotent`` and ``_resolve_conv_refs``. Dependencies
now flow ``routes → lib`` only. ``routes/chat.py`` re-exports these names
for backward compatibility.
"""

from lib.chat.messages import (
    append_user_msg_idempotent,
    resolve_conv_refs,
)
from lib.chat.persistence import (
    append_pending_user_msg,
    extract_db_meta,
    extract_task_meta,
    load_or_create_conv,
    persist_conv_messages,
)
from lib.chat.turn_builder import (
    auto_translate_user,
    translate_user_text_to_english,
    build_tool_history_round,
    build_user_msg_from_payload,
    clear_send_translate_status,
    get_send_translate_status,
    scan_continue_checkpoint,
    set_send_translate_status,
)

__all__ = [
    'append_user_msg_idempotent',
    'resolve_conv_refs',
    'append_pending_user_msg',
    'extract_db_meta',
    'extract_task_meta',
    'load_or_create_conv',
    'persist_conv_messages',
    'auto_translate_user',
    'translate_user_text_to_english',
    'build_tool_history_round',
    'build_user_msg_from_payload',
    'clear_send_translate_status',
    'get_send_translate_status',
    'scan_continue_checkpoint',
    'set_send_translate_status',
]
