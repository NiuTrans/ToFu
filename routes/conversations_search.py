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

#: Searches slower than this (seconds) are logged at WARNING so a regression
#: in the index path is visible in app.log without flipping to DEBUG. Fast
#: searches log at DEBUG to keep the steady-state log quiet.
_SLOW_SEARCH_THRESHOLD_S = 0.3


def _head_cap_sql(backend: str) -> str:
    """Return the backend-appropriate ``search_text`` head-cap SQL fragment.

    Both branches cap the substring scan to the first 10000 chars (so a
    megabyte-scale TOASTed value isn't decompressed in full), but the spelling
    is backend-specific and MUST stay so:

      * ``pg``     → ``left(search_text, 10000)`` — matches the expression trgm
        index ``idx_conv_search_head_trgm`` (``lower(left(...,10000))``)
        verbatim; any other spelling defeats the planner → full Seq Scan.
      * anything else (SQLite) → ``substr(search_text, 1, 10000)`` — SQLite has
        NO ``left()`` builtin, so the PG form raised ``no such function: left``,
        the ``except`` swallowed it, and the Phase-2 substring fallback silently
        returned nothing (degraded search on every SQLite deployment).
        ``substr(x, 1, N)`` is the portable equivalent, semantically identical
        on both backends.
    """
    return ('left(search_text, 10000)' if backend == 'pg'
            else 'substr(search_text, 1, 10000)')


def _log_search_timing(query: str, n_results: int, elapsed: float) -> None:
    """Log search latency — WARNING when slow, DEBUG otherwise."""
    if elapsed >= _SLOW_SEARCH_THRESHOLD_S:
        logger.warning('[search_convs] SLOW query=%r results=%d elapsed=%.3fs '
                       '(>= %.1fs threshold — check Phase-1 index path)',
                       query, n_results, elapsed, _SLOW_SEARCH_THRESHOLD_S)
    else:
        logger.debug('[search_convs] query=%r results=%d elapsed=%.3fs',
                     query, n_results, elapsed)


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

    # ── Phase 1: index-backed full-text match ──
    # Both backends keep a GIN-indexed full-text column populated on every
    # write path, so Phase 1 is index-backed and ~10-40x faster than the
    # Phase-2 substring scan:
    #   • SQLite → ``conversations_fts`` FTS5 virtual table + ``MATCH``.
    #   • PG     → ``search_tsv`` tsvector (``idx_conv_search_tsv`` GIN) +
    #              ``to_tsquery('simple', 'w1:* & w2:*')`` prefix match.
    # The SQLite FTS5 SQL is a hard syntax error on PG (no such table/operator)
    # and vice-versa, so each backend takes its own branch. Previously PG had
    # NO Phase 1 at all and fell straight through to a full Seq Scan on every
    # keystroke (~790ms on 2.9k rows, avg 45KB search_text) — that is the slow
    # path this branch eliminates.
    from lib.database import _BACKEND
    _fts_words = re.sub(r'[^\w\s]', '', query, flags=re.UNICODE).split()

    result_ids = []
    if _fts_words:
        if _BACKEND == 'pg':
            # Prefix match on each word so partial typing still hits the index.
            _ts_query = ' & '.join(f'{w}:*' for w in _fts_words)
            try:
                rows = await async_fetchall(
                    """SELECT id FROM conversations
                       WHERE user_id=? AND search_tsv @@ to_tsquery('simple', ?)
                       ORDER BY updated_at DESC LIMIT ?""",
                    (DEFAULT_USER_ID, _ts_query, MAX_RESULTS), domain=DOMAIN_CHAT)
                result_ids = [r['id'] for r in rows]
            except Exception as e:
                logger.debug('[search_convs] tsvector query failed (will fallback): %s', e)
        else:
            _fts_query = ' '.join(f'{w}*' for w in _fts_words)
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

    # ── Phase 2: LIKE fallback for substring matches Phase 1 misses ──
    # Backend-aware head-cap on search_text (see _head_cap_sql): PG keeps
    # ``left(...)`` to hit its expression index; SQLite uses portable
    # ``substr(...)`` because it has no ``left()`` builtin.
    _head_cap = _head_cap_sql(_BACKEND)
    if len(result_ids) < MAX_RESULTS:
        _like_pattern = '%' + query.replace('%', '\\%').replace('_', '\\_') + '%'
        remaining = MAX_RESULTS - len(result_ids)
        try:
            if result_ids:
                placeholders = ','.join(['?'] * len(result_ids))
                rows = await async_fetchall(
                    f"""SELECT id FROM conversations
                        WHERE user_id=? AND lower({_head_cap}) LIKE ?
                          AND id NOT IN ({placeholders})
                        ORDER BY updated_at DESC LIMIT ?""",
                    (DEFAULT_USER_ID, _like_pattern, *result_ids, remaining),
                    domain=DOMAIN_CHAT)
            else:
                rows = await async_fetchall(
                    f"""SELECT id FROM conversations
                       WHERE user_id=? AND lower({_head_cap}) LIKE ?
                       ORDER BY updated_at DESC LIMIT ?""",
                    (DEFAULT_USER_ID, _like_pattern, remaining), domain=DOMAIN_CHAT)
            result_ids.extend(r['id'] for r in rows)
        except Exception as e:
            logger.warning('[search_convs] LIKE fallback failed: %s', e)

    if not result_ids:
        elapsed = time.monotonic() - t0
        _log_search_timing(query, 0, elapsed)
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
    _log_search_timing(query, len(results), elapsed)
    return jsonify(results)
