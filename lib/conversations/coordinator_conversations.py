"""Passive project conversation inventory, including never-started Worker shells.

One bounded metadata-only catalog read per page, on the selected storage authority.
A cursor traverses the owner catalog rather than inventing a worker registry or
loading all transcripts. Nonmatching pages retain their cursor; coverage never
pretends that a partial page is the complete project. No mutation or execution.
"""
from __future__ import annotations

import base64
import hashlib
import json

from .project_identity import normalize_project_path
from .repository import ConversationRepository

PAGE_SIZE = 64
_SETTINGS = ('projectPath', '_coordinatorSource', 'archived', 'readOnly')


def _scope_key(owner: int, project: str) -> str:
    return hashlib.sha256(json.dumps([owner, project], separators=(',', ':')).encode()).hexdigest()


def _decode_cursor(cursor: str | None, scope: str) -> tuple[int | None, str]:
    if cursor is None or cursor == '':
        return None, ''
    if not isinstance(cursor, str) or len(cursor) > 1024:
        raise ValueError('Invalid project conversation cursor')
    try:
        row = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
        if (not isinstance(row, list) or len(row) != 4 or row[0] != scope
                or type(row[1]) is not int or row[1] != 1
                or type(row[2]) is not int or row[2] < 0
                or not isinstance(row[3], str) or not 1 <= len(row[3]) <= 256):
            raise ValueError('Invalid project conversation cursor')
        return row[2], row[3]
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError('Invalid project conversation cursor') from exc


def conversation_page(owner: int, project: str, cursor: str | None = None, *, repository=None) -> dict:
    if type(owner) is not int or owner < 1:
        raise ValueError('A positive owner is required')
    project = normalize_project_path(project)
    if not project:
        raise ValueError('A project path is required')
    scope = _scope_key(owner, project)
    timestamp, identity = _decode_cursor(cursor, scope)
    repository = repository or ConversationRepository()
    page = repository.list_catalog_page(user_id=owner, limit=PAGE_SIZE,
        before_updated_at=timestamp, before_id=identity, settings_keys=list(_SETTINGS))
    result = []
    for snapshot in page.items:
        row = snapshot.metadata
        settings = row.get('settings') or {}
        if (settings.get('archived') or settings.get('readOnly')
                or normalize_project_path(settings.get('projectPath') or '') != project):
            continue
        source = settings.get('_coordinatorSource')
        result.append({'conversationId': row['id'], 'title': str(row.get('title') or row['id'])[:200],
            'turnCount': max(0, int(row.get('msg_count') or 0)),
            'coordinatorId': source if isinstance(source, str) and 0 < len(source) <= 256 else None})
    next_cursor = None
    if page.has_more:
        if not page.items:
            raise RuntimeError('Conversation catalog returned a nonadvancing page')
        last = page.items[-1].metadata
        boundary = (int(last['updated_at']), str(last['id']))
        if timestamp is not None and boundary >= (timestamp, identity):
            raise RuntimeError('Conversation catalog returned a nonadvancing cursor')
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            [scope, 1, *boundary], separators=(',', ':')).encode()).decode().rstrip('=')
    return {'conversations': result, 'nextCursor': next_cursor,
            'coverage': 'page' if next_cursor else 'complete'}
