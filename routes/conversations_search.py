"""routes/conversations_search.py — Full-text conversation search endpoint.

Extracted from ``routes/conversations.py``. Registers on the same
``conversations_bp`` Blueprint via side-effect import in
``routes/__init__.py``.
"""

import re
import time

from flask import jsonify, request

from lib.database import DOMAIN_CHAT, async_fetchall
from lib.log import get_logger
from routes.common import DEFAULT_USER_ID
from routes.conversations import conversations_bp

logger = get_logger(__name__)


@conversations_bp.route('/api/v1/conversations/search', methods=['GET'])
async def search_convs():
    """Server-side full-text search through conversation messages.

    Two-phase approach:
      Phase 1: FTS5 MATCH for tokenized word matching (fast via inverted index).
      Phase 2: If <50 results, LIKE fallback on search_text to catch
               substring matches that FTS5 tokenization misses.

    Snippets are extracted in Python from the final result set (max 50 rows).
    """
    query = (request.args.get('q') or '').strip().lower()
    if not query or len(query) < 2:
        return jsonify([])

    t0 = time.monotonic()

    MAX_RESULTS = 50
    SNIPPET_RADIUS = 40

    # ── Phase 1: FTS5 MATCH search (SQLite only) ──
    # The ``conversations_fts`` virtual table + ``MATCH`` operator are SQLite
    # FTS5 features; on PostgreSQL that SQL is a hard syntax error (no such
    # table/operator). Previously this query ran unconditionally and threw on
    # every PG search — silently swallowed, leaving only the portable Phase-2
    # LIKE scan. Gate it on the active backend so PG skips straight to Phase 2
    # (correct + no error-log noise) and SQLite keeps its fast inverted-index path.
    from lib.database import _BACKEND
    _fts_words = re.sub(r'[^\w\s]', '', query, flags=re.UNICODE).split()
    _fts_query = ' '.join(f'{w}*' for w in _fts_words if w)

    result_ids = []
    if _fts_query and _BACKEND != 'pg':
        try:
            rows = await async_fetchall(
                """SELECT c.id FROM conversations c
                   JOIN conversations_fts f ON f.rowid = c.rowid
                   WHERE c.user_id=? AND f.search_text MATCH ?
                   ORDER BY c.updated_at DESC LIMIT ?""",
                (DEFAULT_USER_ID, _fts_query, MAX_RESULTS), domain=DOMAIN_CHAT)
            result_ids = [r['id'] for r in rows]
        except Exception as e:
            logger.debug('[search_convs] FTS5 query failed (will fallback): %s', e)

    # ── Phase 2: LIKE fallback for substring matches FTS5 misses ──
    if len(result_ids) < MAX_RESULTS:
        _like_pattern = '%' + query.replace('%', '\\%').replace('_', '\\_') + '%'
        remaining = MAX_RESULTS - len(result_ids)
        try:
            if result_ids:
                placeholders = ','.join(['?'] * len(result_ids))
                rows = await async_fetchall(
                    f"""SELECT id FROM conversations
                        WHERE user_id=? AND lower(search_text) LIKE ?
                          AND id NOT IN ({placeholders})
                        ORDER BY updated_at DESC LIMIT ?""",
                    (DEFAULT_USER_ID, _like_pattern, *result_ids, remaining),
                    domain=DOMAIN_CHAT)
            else:
                rows = await async_fetchall(
                    """SELECT id FROM conversations
                       WHERE user_id=? AND lower(search_text) LIKE ?
                       ORDER BY updated_at DESC LIMIT ?""",
                    (DEFAULT_USER_ID, _like_pattern, remaining), domain=DOMAIN_CHAT)
            result_ids.extend(r['id'] for r in rows)
        except Exception as e:
            logger.warning('[search_convs] LIKE fallback failed: %s', e)

    if not result_ids:
        elapsed = time.monotonic() - t0
        logger.debug('[search_convs] query=%r, results=0, elapsed=%.3fs', query, elapsed)
        return jsonify([])

    # ── Extract snippets in Python (portable — no PG substring/position) ──
    placeholders = ','.join(['?'] * len(result_ids))
    snippet_rows = await async_fetchall(
        f"SELECT id, search_text FROM conversations WHERE id IN ({placeholders})",
        tuple(result_ids), domain=DOMAIN_CHAT)

    snippet_map = {}
    for r in snippet_rows:
        text = r['search_text'] or ''
        pos = text.lower().find(query)
        if pos >= 0:
            start = max(0, pos - SNIPPET_RADIUS)
            end = min(len(text), pos + len(query) + SNIPPET_RADIUS)
            snip = text[start:end].replace('\n', ' ').strip()
            if snip:
                snip = '…' + snip + '…'
            snippet_map[r['id']] = snip
        else:
            snippet_map[r['id']] = ''

    results = [
        {
            'id': cid,
            'matchField': 'content',
            'matchSnippet': snippet_map.get(cid, ''),
            'matchRole': 'assistant',
        }
        for cid in result_ids
    ]

    elapsed = time.monotonic() - t0
    logger.debug('[search_convs] query=%r, results=%d, elapsed=%.3fs',
                 query, len(results), elapsed)
    return jsonify(results)
