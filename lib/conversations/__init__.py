"""lib.conversations — conversation-domain helpers that used to live in
``routes/`` but are imported by lib modules.

Relocated here (2026-06) to break the ``lib → routes`` circular-import
coupling: ``lib/database``, ``lib/feishu``, ``lib/scheduler``, and
``lib/tasks_pkg`` all needed ``build_search_text`` and the conversation
meta-cache, and were reaching UP into ``routes.conversations`` /
``routes.common`` to get them. Dependencies now flow ``routes → lib``
only. The route modules re-export these names for backward compatibility.
"""

from lib.conversations.meta_cache import (
    invalidate_meta_cache,
    refresh_meta_cache_if_stale,
)
from lib.conversations.search_index import build_search_text, update_conversation_fts
from lib.conversations.title_gen import generate_conversation_title

__all__ = [
    'build_search_text',
    'update_conversation_fts',
    'invalidate_meta_cache',
    'refresh_meta_cache_if_stale',
    'generate_conversation_title',
]
