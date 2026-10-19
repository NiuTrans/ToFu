"""Owner-scoped search over the disposable chunk projection, never transcripts.

Entry point: search_conversations. The HTTP page uses opaque query/owner-bound
keyset continuations. SQLite's optional trigram index accelerates long literals;
short queries and other backends inspect bounded header pages and report their
continuation rather than silently treating a scan budget as the end of history.
Authority: contracts/conversation_search_v2.json.
"""
from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.base import Session
from lib.storage_authority_sql.operations_pkg._common import _integer

QUERY_CHARACTERS = 512
QUERY_TERMS = 32
SCAN_PAGE = 256
CHUNK_SCAN_PAGE = 512
CHUNK_SCAN_BATCH = 16
CURSOR_BYTES = 8192


def normalize_query(value: Any) -> str:
    if not isinstance(value, str) or len(value) > QUERY_CHARACTERS or '\x00' in value:
        raise StorageError('database_protocol_error', 'Invalid conversation search query')
    query = value.strip().lower()
    if len(query) > QUERY_CHARACTERS or len(query.split()) > QUERY_TERMS:
        raise StorageError('database_protocol_error', 'Conversation search query is too long')
    return query


def _binding(user_id: int, query: str) -> str:
    return hashlib.sha256(f'{user_id}\0{query}'.encode()).hexdigest()[:24]


def _decode_cursor(value: Any, binding: str) -> tuple[int, int | None, str]:
    if value in (None, ''):
        return 0, None, ''
    try:
        if not isinstance(value, str) or len(value) > CURSOR_BYTES:
            raise ValueError()
        data = json.loads(base64.b64decode(value.encode('ascii'), altchars=b'-_', validate=True))
        if (not isinstance(data, list) or len(data) not in (5, 6) or data[0] != 2
                or data[1] != binding or type(data[2]) is not int or data[2] not in (0, 1)
                or type(data[3]) is not int or not 0 <= data[3] <= 9223372036854775807
                or not isinstance(data[4], str) or not 1 <= len(data[4]) <= 256):
            raise ValueError()
        return data[2], data[3], data[4]
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise StorageError('database_protocol_error', 'Invalid conversation search cursor') from exc


def _cursor(binding: str, phase: int, updated_at: int, conversation_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps(
        [2, binding, phase, updated_at, conversation_id], separators=(',', ':')
    ).encode()).decode('ascii')


def _literal(term: str) -> str:
    return '%' + term.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'


def _fts_available(session: Session) -> bool:
    return session.backend == 'sqlite' and bool(session.fetch_one(
        "SELECT 1 AS present FROM sqlite_schema WHERE type='table' AND name=?",
        ('storage_search_chunks_fts',),
    ))


def _text_predicate(term: str, accelerated: bool, user_id: int) -> tuple[str, list[Any]]:
    if accelerated:
        # MATCH is a bound, quoted literal, never user-supplied FTS syntax.
        phrase = '"' + term.replace('"', '""') + '"'
        return (
            'c.id IN (SELECT s.conversation_id FROM storage_search_chunks_fts AS f '
            'JOIN storage_search_chunks AS s ON s.rowid=f.rowid '
            "WHERE storage_search_chunks_fts MATCH ? AND s.user_id=? AND s.folded_text LIKE ? ESCAPE '!')",
            [phrase, user_id, _literal(term)],
        )
    return (
        'EXISTS (SELECT 1 FROM storage_search_chunks AS s '
        "WHERE s.user_id=c.user_id AND s.conversation_id=c.id AND s.folded_text LIKE ? ESCAPE '!')",
        [_literal(term)],
    )


def _predicate(query: str, phase: int, accelerated: bool, backend: str, user_id: int) -> tuple[str, list[Any]]:
    # SQLite registers this deterministic Unicode function at the local
    # projection connection boundary. PostgreSQL lower handles Unicode text.
    lower = 'tofu_search_lower' if backend == 'sqlite' else 'lower'
    phrase, phrase_params = _text_predicate(query, accelerated, user_id)
    exact = f"({lower}(c.title) LIKE ? ESCAPE '!' OR {lower}(c.id) LIKE ? ESCAPE '!' OR {phrase})"
    exact_params = [_literal(query), _literal(query), *phrase_params]
    if phase == 0:
        return exact, exact_params
    terms = list(dict.fromkeys(query.split()))
    pieces: list[str] = []
    params: list[Any] = []
    for term in terms:
        content, content_params = _text_predicate(term, accelerated, user_id)
        pieces.append(f"({lower}(c.title) LIKE ? ESCAPE '!' OR {content})")
        params.extend([_literal(term), *content_params])
    return '(' + ' AND '.join(pieces) + ') AND NOT ' + exact, [*params, *exact_params]


def _after(updated_at: int | None, conversation_id: str) -> tuple[str, list[Any]]:
    if updated_at is None:
        return '', []
    return ' AND (c.updated_at_ms<? OR (c.updated_at_ms=? AND c.id<?))', [updated_at, updated_at, conversation_id]


def _snippet(session: Session, row: Mapping[str, Any], user_id: int,
             query: str, radius: int) -> dict[str, Any]:
    conversation_id = str(row['id'])
    title = str(row.get('title') or '')
    if query in title.lower():
        return {'id': conversation_id, 'snippet': '', 'role': None, 'match_field': 'title'}
    if query in conversation_id.lower():
        return {'id': conversation_id, 'snippet': conversation_id, 'role': None, 'match_field': 'id'}
    first = query.split()[0]
    # Original human wording wins among equivalent hits. Only one bounded
    # chunk crosses the database/Python boundary per visible conversation.
    chunk = session.fetch_one(
        'SELECT search_text,role,match_field FROM storage_search_chunks '
        'WHERE user_id=? AND conversation_id=? '
        "AND (folded_text LIKE ? ESCAPE '!' OR folded_text LIKE ? ESCAPE '!') "
        "ORDER BY CASE WHEN folded_text LIKE ? ESCAPE '!' THEN 0 ELSE 1 END, "
        "CASE WHEN role='user' THEN 0 ELSE 1 END, "
        "CASE WHEN match_field='originalContent' THEN 0 ELSE 1 END, ordinal,turn_id,chunk_index LIMIT 1",
        (user_id, conversation_id, _literal(query), _literal(first), _literal(query)),
    )
    if chunk is None:
        # An all-words match can have its first word only in the title.
        return {'id': conversation_id, 'snippet': '', 'role': None, 'match_field': 'title'}
    return _chunk_snippet(chunk, conversation_id, query, radius)


def _chunk_snippet(chunk: Mapping[str, Any], conversation_id: str, query: str, radius: int) -> dict[str, Any]:
    first = query.split()[0]
    text = str(chunk['search_text'] or '')
    folded = text.lower()
    position = folded.find(query)
    term = query
    if position < 0:
        position, term = folded.find(first), first
    snippet = ''
    if position >= 0 and radius > 0:
        # Unicode lower can expand a scalar (e.g. İ). Map the bounded folded
        # position back to source indices so highlighted snippets stay truthful.
        start_index = end_index = len(text)
        offset = 0
        for index, char in enumerate(text):
            next_offset = offset + len(char.lower())
            if offset <= position < next_offset:
                start_index = index
            if offset < position + len(term) <= next_offset:
                end_index = index + 1
                break
            offset = next_offset
        start = max(0, start_index - radius)
        end = min(len(text), end_index + radius)
        snippet = '…' + text[start:end].replace('\n', ' ').strip() + '…'
    return {'id': conversation_id, 'snippet': snippet,
            'role': chunk['role'], 'match_field': chunk['match_field']}


def _fast_page(session: Session, *, user_id: int, query: str, limit: int,
                 radius: int, cursor: Any) -> dict[str, Any]:
    binding = _binding(user_id, query)
    phase, updated_at, conversation_id = _decode_cursor(cursor, binding)
    words = list(dict.fromkeys(query.split()))
    if len(query) < 2:
        return {'items': [], 'next_cursor': None, 'incomplete': False}
    items: list[dict[str, Any]] = []
    while phase <= (1 if len(words) > 1 else 0):
        after, after_params = _after(updated_at, conversation_id)
        predicate, predicate_params = _predicate(query, phase, True, session.backend, user_id)
        rows = session.fetch_all(
            'SELECT c.id,c.title,c.updated_at_ms FROM storage_search_conversations AS c '
            'WHERE c.user_id=?' + after + ' AND (' + predicate + ') '
            'ORDER BY c.updated_at_ms DESC,c.id DESC LIMIT ?',
            tuple([user_id, *after_params, *predicate_params, limit - len(items) + 1]),
        )
        more = len(rows) > limit - len(items)
        rows = rows[:limit - len(items)]
        for row in rows:
            items.append(_snippet(session, row, user_id, query, radius))
        if rows:
            updated_at, conversation_id = int(rows[-1]['updated_at_ms']), str(rows[-1]['id'])
        if more:
            return {'items': items, 'next_cursor': _cursor(binding, phase, updated_at, conversation_id),
                    'incomplete': False}
        phase += 1
        updated_at, conversation_id = None, ''
        # Cursor representation of the beginning of the second phase: time/id
        # sentinel is avoided by completing its first bounded query here.
        if len(items) >= limit and phase <= 1 and len(words) > 1:
            return {'items': items, 'next_cursor': _cursor(binding, phase, 9223372036854775807, '\uffff'),
                    'incomplete': False}
    return {'items': items, 'next_cursor': None, 'incomplete': False}


def _scan_cursor(binding, phase, updated_at, conversation_id, pending=None):
    return base64.urlsafe_b64encode(json.dumps(
        [2, binding, phase, updated_at, conversation_id, {'scan': 1, 'pending': pending}],
        separators=(',', ':')).encode()).decode('ascii')


def _scan_state(value, query):
    if not value:
        return None
    data = json.loads(base64.b64decode(value.encode('ascii'), altchars=b'-_', validate=True))
    if len(data) == 5:
        return None
    extension = data[5]
    pending = extension.get('pending') if isinstance(extension, dict) else None
    valid = (isinstance(extension, dict) and set(extension) == {'scan', 'pending'}
             and type(extension.get('scan')) is int and extension.get('scan') == 1)
    if pending is not None:
        valid = valid and isinstance(pending, dict) and set(pending) in (
            {'after', 'mask', 'best'}, {'after', 'mask', 'best', 'phrase'})
        if valid and 'phrase' in pending:
            valid = type(pending['phrase']) is bool
        if valid:
            valid = type(pending['mask']) is int and 0 <= pending['mask'] < (1 << len(set(query.split())))
            for key in ('after', 'best'):
                point = pending[key]
                valid = valid and (point is None or (isinstance(point, list) and len(point) == 3
                    and type(point[0]) is int and 0 <= point[0] <= 9223372036854775807
                    and isinstance(point[1], str) and len(point[1]) <= 1024
                    and type(point[2]) is int and 0 <= point[2] <= 9223372036854775807))
    if not valid:
        raise StorageError('database_protocol_error', 'Invalid conversation search cursor')
    return extension


def _chunk_rank(chunk, query):
    return (query not in str(chunk['search_text'] or '').lower(), chunk['role'] != 'user',
            chunk['match_field'] != 'originalContent', int(chunk['ordinal']),
            str(chunk['turn_id']), int(chunk['chunk_index']))


def _portable_page(session, *, user_id, query, limit, radius, cursor):
    """Bound *both* candidate count and text work, including one huge chat.

    Accumulated word bits plus chunk key survive an empty page. No transcript,
    unbounded SQL EXISTS scan, or corpus-sized Python set is needed.
    """
    binding = _binding(user_id, query)
    phase, updated_at, conversation_id = _decode_cursor(cursor, binding)
    extension = _scan_state(cursor, query)
    pending = extension['pending'] if extension else None
    words = list(dict.fromkeys(query.split()))
    all_bits = (1 << len(words)) - 1
    items, headers_read, chunks_read = [], 0, 0
    last_phase = int(len(words) > 1)
    while phase <= last_phase:
        if headers_read >= SCAN_PAGE or chunks_read >= CHUNK_SCAN_PAGE:
            return {'items': items, 'next_cursor': _scan_cursor(binding, phase,
                    updated_at if updated_at is not None else 9223372036854775807,
                    conversation_id or '\uffff', pending), 'incomplete': True}
        if pending:
            headers = session.fetch_all(
                'SELECT id,title,updated_at_ms FROM storage_search_conversations WHERE user_id=? AND id=?',
                (user_id, conversation_id))
            if not headers:
                pending = None
                continue
        else:
            after, params = _after(updated_at, conversation_id)
            headers = session.fetch_all(
                'SELECT c.id,c.title,c.updated_at_ms FROM storage_search_conversations c WHERE c.user_id=?'
                + after + ' ORDER BY c.updated_at_ms DESC,c.id DESC LIMIT ?',
                tuple([user_id, *params, SCAN_PAGE - headers_read]))
        if not headers:
            phase += 1
            updated_at, conversation_id = None, ''
            continue
        for header in headers:
            headers_read += 1
            cid = str(header['id'])
            title = str(header.get('title') or '').lower()
            title_hit, id_hit = query in title, query in cid.lower()
            phrase_hit = title_hit or id_hit or bool(pending and pending.get('phrase'))
            mask = pending['mask'] if pending else sum(1 << i for i, word in enumerate(words) if word in title)
            chunk_after = pending['after'] if pending else None
            best = pending['best'] if pending else None
            best_chunk = None
            if best is not None:
                best_chunk = session.fetch_one(
                    'SELECT ordinal,turn_id,chunk_index,search_text,role,match_field FROM storage_search_chunks '
                    'WHERE user_id=? AND conversation_id=? AND turn_id=? AND chunk_index=?',
                    (user_id, cid, best[1], best[2]))
                chunks_read += 1
            complete = title_hit or id_hit
            while not complete:
                budget = min(CHUNK_SCAN_BATCH, CHUNK_SCAN_PAGE - chunks_read)
                if budget <= 0:
                    return {'items': items, 'next_cursor': _scan_cursor(binding, phase,
                            int(header['updated_at_ms']), cid,
                            {'after': chunk_after, 'mask': mask, 'best': best, 'phrase': phrase_hit}), 'incomplete': True}
                params = [user_id, cid]
                after = ''
                if chunk_after:
                    after = ' AND (ordinal,turn_id,chunk_index)>(?,?,?)'
                    params.extend(chunk_after)
                params.append(budget)
                chunk_rows = session.fetch_all(
                    'SELECT ordinal,turn_id,chunk_index,search_text,role,match_field FROM storage_search_chunks '
                    'WHERE user_id=? AND conversation_id=?' + after
                    + ' ORDER BY ordinal,turn_id,chunk_index LIMIT ?', tuple(params))
                chunks_read += len(chunk_rows)
                for chunk in chunk_rows:
                    text = str(chunk['search_text'] or '').lower()
                    point = [int(chunk['ordinal']), str(chunk['turn_id']), int(chunk['chunk_index'])]
                    mask |= sum(1 << i for i, word in enumerate(words) if word in text)
                    phrase_hit |= query in text
                    if words[0] in text and (best_chunk is None
                            or _chunk_rank(chunk, query) < _chunk_rank(best_chunk, query)):
                        best = point
                        best_chunk = chunk
                if chunk_rows:
                    last = chunk_rows[-1]
                    chunk_after = [int(last['ordinal']), str(last['turn_id']), int(last['chunk_index'])]
                if len(chunk_rows) < budget:
                    complete = True
            updated_at, conversation_id = int(header['updated_at_ms']), cid
            pending = None
            matched = phrase_hit if phase == 0 else mask == all_bits and not phrase_hit
            if matched:
                if title_hit or (best is None and not id_hit):
                    hit = {'id': cid, 'snippet': '', 'role': None, 'match_field': 'title'}
                elif id_hit:
                    hit = {'id': cid, 'snippet': cid, 'role': None, 'match_field': 'id'}
                else:
                    chunk = best_chunk or session.fetch_one(
                        'SELECT search_text,role,match_field FROM storage_search_chunks '
                        'WHERE user_id=? AND conversation_id=? AND turn_id=? AND chunk_index=?',
                        (user_id, cid, best[1], best[2]))
                    hit = _chunk_snippet(chunk, cid, query, radius) if chunk else None
                if hit:
                    items.append(hit)
            if len(items) >= limit or chunks_read >= CHUNK_SCAN_PAGE:
                return {'items': items, 'next_cursor': _scan_cursor(binding, phase, updated_at, cid),
                        'incomplete': chunks_read >= CHUNK_SCAN_PAGE}
    return {'items': items, 'next_cursor': None, 'incomplete': False}


def _search_page(session: Session, *, user_id: int, query: str, limit: int,
                 radius: int, cursor: Any) -> dict[str, Any]:
    from lib.storage_authority_sql.adapters.search_support import accelerator_budget, AcceleratorBudgetExceeded
    _decode_cursor(cursor, _binding(user_id, query))
    if len(query) < 2:
        return {'items': [], 'next_cursor': None, 'incomplete': False}
    scan = _scan_state(cursor, query)
    if scan is None and _fts_available(session) and all(len(term) >= 3 for term in query.split()):
        try:
            with accelerator_budget(session):
                return _fast_page(session, user_id=user_id, query=query, limit=limit, radius=radius, cursor=cursor)
        except AcceleratorBudgetExceeded:
            pass  # The next cursor stays on the resumable path, avoiding repeated FTS restarts.
    return _portable_page(session, user_id=user_id, query=query, limit=limit, radius=radius, cursor=cursor)


def search_conversations(session: Session, payload: Mapping[str, Any]) -> Any:
    from lib.storage_authority_sql.adapters.search_support import prepare_search_session
    prepare_search_session(session)
    query = normalize_query(payload.get('query'))
    user_id = _integer(payload, 'user_id', minimum=1)
    limit = _integer(payload, 'limit', default=50, minimum=1, maximum=200)
    radius = _integer(payload, 'snippet_radius', default=40, minimum=0, maximum=400)
    page_mode = payload.get('page', False)
    if not isinstance(page_mode, bool):
        raise StorageError('database_protocol_error', 'Invalid conversation search page mode')
    result = _search_page(session, user_id=user_id, query=query, limit=limit,
                          radius=radius, cursor=payload.get('cursor'))
    if page_mode:
        return result
    # Existing internal array consumers keep their wire shape. The paginated
    # browser API is the owner of exhaustive continuation and coverage notices.
    return result['items']


def attach_search_coverage(result: Any, status: Mapping[str, Any]) -> Any:
    """Attach non-sensitive worker coverage only to the explicitly paged shape."""
    if not isinstance(result, dict) or 'next_cursor' not in result:
        return result
    reasons = list(status.get('coverage_reasons') or [])[:8]
    metrics = status.get('metrics') or {}
    if metrics.get('oversize_skipped') and 'oversize_source' not in reasons:
        reasons.append('oversize_source')
    if status.get('last_worker_error'):
        reasons.append('index_worker_error')
    if result.get('incomplete'):
        reasons.append('scan_continuation')
    coverage = ('partial' if reasons else
                'ready' if status.get('backfill_complete') else 'indexing')
    return {**result, 'coverage': coverage, 'coverage_reasons': list(dict.fromkeys(reasons))[:8]}
