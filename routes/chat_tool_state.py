"""routes/chat_tool_state.py — Lightweight tool-state PATCH endpoint.

Extracted from ``routes/chat.py``. Patches the ``settings`` column of a
conversation without touching messages or search-text indexes; safe to
call frequently (every tool toggle).
"""

import asyncio

from lib.log import get_logger
from lib.api_response import api_bad_request, api_internal_error, api_ok
from lib.request_parser import async_parse_body
from routes.api_v1.chat import api_v1_chat_bp  # noqa: E402
from routes.api_v1.auth import require_scope
from routes.common import DEFAULT_USER_ID

logger = get_logger(__name__)


@api_v1_chat_bp.route('/api/v1/chat/tool-state/<conv_id>', methods=['PATCH'], endpoint='ui_chat_tool_state')
@require_scope('chat')
async def chat_tool_state(conv_id):
    """Lightweight tool-state sync: merge tool settings into conversation settings.

    Unlike the full PUT /api/conversations/<id>, this only touches the settings
    column — no messages, no msg_count, no search_text update.
    Safe to call frequently (e.g. on every tool toggle).

    Body: { model?, searchMode?, fetchEnabled?, browserEnabled?, projectPath?, ... }
    """
    data = await async_parse_body()
    if not data:
        return api_bad_request('No settings provided')

    try:
        def _write():
            # Serialized read-merge-write (see settings_store): a tool-toggle
            # PATCH must not clobber a concurrent activeTaskId / autopilot
            # settings write on the same row. Runs off-loop via a strict
            # pooled checkout→use→return cycle. Returns True when the conv row
            # exists, False when it's not persisted yet.
            from lib.conversations import set_conversation_settings
            from lib.database import DOMAIN_CHAT, pooled_db
            with pooled_db(DOMAIN_CHAT) as db:
                res = set_conversation_settings(
                    conv_id, data, user_id=DEFAULT_USER_ID, db=db)
                return res is not None

        existed = await asyncio.to_thread(_write)
        if not existed:
            # Conv not in DB yet (no messages sent) — that's OK, skip
            return api_ok({'skipped': True})

        logger.debug('[ToolState] conv=%s patched %d keys: %s',
                     conv_id[:8], len(data), list(data.keys())[:10])
        return api_ok()
    except Exception as e:
        logger.error('[ToolState] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')
