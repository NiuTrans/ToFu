"""routes/conversations_search.py — Full-text conversation search endpoint.

Extracted from ``routes/conversations.py``. Registers on the same
``conversations_bp`` Blueprint via side-effect import in
``routes/__init__.py``.
"""

import re
import time

from quart import request

from lib.api_response import api_ok
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


def _snippet_projection_sql(backend: str, placeholders: str) -> str:
    """Project a bounded snippet in SQL instead of returning full search blobs.

    ``search_text`` can be hundreds of megabytes for a long conversation.  A
    final result set of 50 rows must not copy all of those values through the
    driver and then materialize them again in Python merely to keep ~80 chars.
    The database still locates the match in the authoritative full text, but
    only the bounded substring crosses the process boundary.
    """
    if backend == 'pg':
        snippet_expr = (
            'substring(search_text FROM GREATEST(1, match_pos - ?) FOR ?)'
        )
        pos_expr = 'strpos(lower(search_text), ?)'
    else:
        snippet_expr = 'substr(search_text, max(1, match_pos - ?), ?)'
        pos_expr = 'instr(lower(search_text), ?)'
    return (
        'SELECT id, CASE WHEN match_pos > 0 THEN ' + snippet_expr +
        " ELSE '' END AS snippet FROM ("
        'SELECT id, search_text, ' + pos_expr + ' AS match_pos '
        'FROM conversations WHERE user_id=? AND id IN (' + placeholders +
        ')) AS matched'
    )


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

    Snippets are projected to a bounded substring in SQL (max 50 rows); full
    search blobs never cross into Python.
    """
    query = (request.args.get('q') or '').strip().lower()
    if not query or len(query) < 2:
        return api_ok({'items': []})

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
        return api_ok({'items': []})

    # ── Project bounded snippets in SQL ──
    placeholders = ','.join(['?'] * len(result_ids))
    snippet_width = (2 * SNIPPET_RADIUS) + len(query)
    snippet_rows = await async_fetchall(
        _snippet_projection_sql(_BACKEND, placeholders),
        (SNIPPET_RADIUS, snippet_width, query, DEFAULT_USER_ID, *result_ids),
        domain=DOMAIN_CHAT)

    snippet_map = {}
    for r in snippet_rows:
        snip = (r['snippet'] or '').replace('\n', ' ').strip()
        if snip:
            snip = '…' + snip + '…'
        snippet_map[r['id']] = snip

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
    # Coordinated bare-array migration (batch 20): array under ``items``;
    # Api.conversations.search unwraps with an Array.isArray fallback.
    return api_ok({'items': results})
