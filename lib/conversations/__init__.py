"""lib.conversations — conversation-domain helpers that used to live in
``routes/`` but are imported by lib modules.

Dependencies flow ``routes → lib`` only. Durable storage reads live in
``repository``; wake hints live in ``change_notifications``; settings writes
live in ``settings_store``.
"""

from lib.conversations.change_notifications import notify_conv_changed
from lib.conversations.search_index import build_search_text
from lib.conversations.settings_store import (
    set_conversation_settings,
    update_conversation_settings,
)
from lib.conversations.title_gen import first_user_text, generate_conversation_title

__all__ = [
    'build_search_text',
    'notify_conv_changed',
    'generate_conversation_title',
    'first_user_text',
    'update_conversation_settings',
    'set_conversation_settings',
]
