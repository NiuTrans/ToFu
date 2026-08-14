"""Conversation reference — search/list surface.

Holds the DB access helper, keyword-clause builder, and ``list_conversations``
(the searchable listing of *other* conversations, optionally project-scoped).
"""

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)

DEFAULT_USER_ID = 1  # mirrors routes/common.py


def _get_db():
    """Get a DB connection — works both inside Flask request context and background threads."""
    try:
        from quart import has_app_context
        if has_app_context():
            from lib.database import get_db
            return get_db(DOMAIN_CHAT)
    except Exception as e:
        logger.debug("Flask app context not available, using thread DB: %s", e, exc_info=True)
    return get_thread_db(DOMAIN_CHAT)


def _keyword_clause(keyword, params):
    """Build the keyword WHERE-fragment, appending bind params in place.

    On PostgreSQL, match the GIN-indexed ``search_tsv`` tsvector (prefix
    query, the same index ``routes/conversations_search.py`` uses) so a
    content search stays index-backed instead of degrading to a full
    ``search_text LIKE '%kw%'`` scan (~3s on a few thousand rows). The title
    is OR-ed in via LIKE so short/partial titles still match. On SQLite (and
    if the tsvector query can't be built) fall back to the portable
    title-or-search_text LIKE.
    """
    import re as _re
    like = f'%{keyword}%'
    try:
        from lib.database import _BACKEND
    except Exception as e:  # pragma: no cover - import shape guard
        logger.debug('[ConvRef] _BACKEND import failed, assuming sqlite: %s', e)
        _BACKEND = 'sqlite'

    if _BACKEND == 'pg':
        words = _re.sub(r'[^\w\s]', '', keyword, flags=_re.UNICODE).split()
        if words:
            ts_query = ' & '.join(f'{w}:*' for w in words)
            # tsvector prefix match (indexed) OR title substring.
            params.append(ts_query)
            params.append(like)
            return "(search_tsv @@ to_tsquery('simple', ?) OR title LIKE ?)"

    # SQLite / no-word fallback: portable substring match on title + body.
    params.append(like)
    params.append(like)
    return '(title LIKE ? OR search_text LIKE ?)'


def list_conversations(keyword=None, limit=20, scope='auto',
                       project_path=None, current_conv_id=None,
                       user_id=None):
    """List other conversations, optionally scoped to the current project.

    Args:
        keyword: optional filter. Matches the conversation TITLE *and* its
            indexed ``search_text`` (message bodies), so the model can find a
            conversation by what was discussed, not just its title.
        limit: max rows (1-50).
        scope: ``'project'`` → only conversations whose ``settings.projectPath``
            equals ``project_path``; ``'all'`` → every conversation;
            ``'auto'`` (default) → project-scoped when a ``project_path`` is
            available, else all.
        project_path: the current task's project path (supplied by the tool
            handler). Required for project scoping; ignored otherwise.
        current_conv_id: the active conversation, excluded from results.
        user_id: the OWNING principal whose conversations may be listed.
            ``None`` falls back to :data:`DEFAULT_USER_ID` so a single-user
            install behaves byte-identically. Callers on a request thread pass
            ``routes.common._request_user_id()``; background task threads pass
            ``task_user_id(task)``. Hard-coding the default here would make
            every tenant read user 1's conversations.

    Returns a formatted string with conversation metadata.
    """
    limit = min(max(1, int(limit or 20)), 50)
    db = _get_db()

    effective_scope = scope or 'auto'
    if effective_scope == 'auto':
        effective_scope = 'project' if project_path else 'all'
    if effective_scope == 'project' and not project_path:
        # Asked to scope by project but we have no path — degrade to all.
        effective_scope = 'all'

    where = ['user_id=?']
    params = [DEFAULT_USER_ID if user_id is None else user_id]

    if effective_scope == 'project':
        # json_extract is rewritten to the PG jsonb accessor by _sql_translate,
        # so this one statement works on both SQLite and PostgreSQL.
        where.append("json_extract(settings, '$.projectPath') = ?")
        params.append(project_path)

    if keyword:
        where.append(_keyword_clause(keyword, params))

    if current_conv_id:
        where.append('id <> ?')
        params.append(current_conv_id)

    params.append(limit)
    sql = (
        'SELECT id, title, created_at, updated_at, msg_count '
        'FROM conversations WHERE ' + ' AND '.join(where) +
        ' ORDER BY updated_at DESC LIMIT ?'
    )
    rows = db.execute(sql, tuple(params)).fetchall()

    scope_note = ''
    if effective_scope == 'project' and project_path:
        scope_note = f" in this project ({project_path})"

    if not rows:
        if keyword:
            return (f"No conversations found matching '{keyword}'{scope_note}. "
                    f"Try a different keyword, or pass scope='all' to search "
                    f"every conversation.")
        return f"No other conversations found{scope_note}."

    lines = [f"Found {len(rows)} conversation(s){scope_note}:\n"]
    for r in rows:
        title = r['title'] or '(untitled)'
        msg_count = r['msg_count'] or 0
        conv_id = r['id']
        updated = r['updated_at'] or r['created_at'] or 0

        # Format timestamp
        if updated:
            from datetime import datetime, timezone
            try:
                dt = datetime.fromtimestamp(updated / 1000, tz=timezone.utc)
                time_str = dt.strftime('%Y-%m-%d %H:%M UTC')
            except (ValueError, OSError):
                logger.debug('Failed to parse timestamp %s for conversation', updated, exc_info=True)
                time_str = str(updated)
        else:
            time_str = 'unknown'

        lines.append(f"• [{conv_id}] \"{title}\" — {msg_count} messages, updated {time_str}")

    lines.append("\nUse get_conversation(conversation_id=\"<id>\") to retrieve full content.")
    return '\n'.join(lines)
