"""Conversation search application service: bounded hits plus owned metadata.

Entry point: search_conversation_page. Two semantic reads per page, regardless
of hit count: independent projection search and an owner-scoped metadata batch.
No transcript is hydrated; deleted/foreign stale projection hits are withheld.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError

MAX_QUERY_CHARACTERS = 512
MAX_QUERY_TERMS = 32
PAGE_RESULTS = 50


def search_conversation_page(client: Any, *, user_id: int, query: str,
                             cursor: str | None = None) -> dict[str, Any]:
    if (not isinstance(query, str) or len(query) > MAX_QUERY_CHARACTERS
            or len(query.split()) > MAX_QUERY_TERMS):
        raise ValueError('Search query is too long')
    if '\x00' in query:
        raise ValueError('Search query contains NUL')
    normalized = query.strip().lower()
    if len(normalized) > MAX_QUERY_CHARACTERS:
        raise ValueError('Search query is too long')
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 8192):
        raise ValueError('Invalid search cursor')
    if len(normalized) < 2:
        return {'items': [], 'nextCursor': None, 'coverage': 'ready', 'coverageReasons': []}
    payload = {'user_id': user_id, 'query': normalized, 'limit': PAGE_RESULTS,
               'snippet_radius': 40, 'page': True}
    if cursor:
        payload['cursor'] = cursor
    page = client.query('conversation.search', payload)
    if not isinstance(page, Mapping) or not isinstance(page.get('items'), list):
        raise StorageError('database_protocol_error', 'Invalid conversation search response')
    hits = page['items'][:PAGE_RESULTS]
    ids = list(dict.fromkeys(str(hit['id']) for hit in hits if isinstance(hit, Mapping) and hit.get('id')))
    documents = client.query('conversation.list', {
        'user_id': user_id, 'ids': ids, 'include_messages': False,
        # Empty whitelist projects SQL constant {}, avoiding large settings.
        # The normal open path hydrates the selected conversation settings.
        'settings_keys': [], 'limit': len(ids),
    }) if ids else []
    metadata = {
        str(doc['metadata']['id']): doc['metadata'] for doc in documents
        if isinstance(doc, Mapping) and isinstance(doc.get('metadata'), Mapping)
    }
    items = []
    for hit in hits:
        if not isinstance(hit, Mapping):
            continue
        row = metadata.get(str(hit.get('id') or ''))
        if row is None:
            continue
        field = hit.get('match_field') or 'content'
        items.append({
            'id': str(row['id']), 'matchField': field,
            'matchSnippet': str(hit.get('snippet') or ''),
            'matchRole': hit.get('role'),
            'conversation': {
                'id': str(row['id']), 'title': str(row.get('title') or '')[:200],
                'createdAt': int(row.get('created_at') or 0),
                'updatedAt': int(row.get('updated_at') or 0),
                'msgCount': int(row.get('msg_count') or 0),
                'rev': int(row.get('rev') or 0), 'settings': {},
            },
        })
    return {
        'items': items, 'nextCursor': page.get('next_cursor'),
        'coverage': page.get('coverage') or 'indexing',
        'coverageReasons': list(page.get('coverage_reasons') or [])[:8],
    }
