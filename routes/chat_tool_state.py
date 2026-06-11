"""routes/chat_tool_state.py — Lightweight tool-state PATCH endpoint.

Extracted from ``routes/chat.py``. Patches the ``settings`` column of a
conversation without touching messages or search-text indexes; safe to
call frequently (every tool toggle).
"""

import asyncio
import json


from lib.database import DOMAIN_CHAT, async_fetchone, db_execute_with_retry
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
        row = await async_fetchone(
            'SELECT settings FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)

        if not row:
            # Conv not in DB yet (no messages sent) — that's OK, skip
            return api_ok({'skipped': True})
        try:
            settings = json.loads(row['settings'] or '{}')
        except (json.JSONDecodeError, TypeError) as _e_audit:
            logger.debug('[chat_tool_state] chat_tool_state caught %s: %s', type(_e_audit).__name__, _e_audit)
            settings = {}

        settings.update(data)
        settings_json = json.dumps(settings, ensure_ascii=False)

        def _write():
            # db_execute_with_retry needs a real pooled connection (it takes a
            # `db` arg and cannot accept the async facade), so run it off-loop
            # via a strict checkout→use→return cycle.
            from lib.database._core import _pool_get, _pool_put
            db = _pool_get()
            try:
                db_execute_with_retry(db, '''
                    UPDATE conversations SET settings=? WHERE id=? AND user_id=?
                ''', (settings_json, conv_id, DEFAULT_USER_ID))
            finally:
                _pool_put(db)

        await asyncio.to_thread(_write)

        logger.debug('[ToolState] conv=%s patched %d keys: %s',
                     conv_id[:8], len(data), list(data.keys())[:10])
        return api_ok()
    except Exception as e:
        logger.error('[ToolState] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')
