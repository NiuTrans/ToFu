"""Side-effect-free PostgreSQL driver factory for offline data tools.

This module is deliberately a leaf that can be loaded by file path. Importing
``lib.database`` performs application backend discovery, which a check-only
migration/recovery command must not trigger merely to construct an explicitly
addressed external PostgreSQL connection.
"""

from __future__ import annotations


def open_postgres_tool_connection(dsn=None, **kwargs):
    """Open one explicitly configured offline-tool connection."""
    import psycopg2

    if dsn is None:
        return psycopg2.connect(**kwargs)
    return psycopg2.connect(dsn, **kwargs)


def load_external_conversation_archive(conn, conv_id: str) -> dict | None:
    """Load one explicitly addressed legacy PG transcript for diagnostics."""
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            'SELECT messages, settings FROM conversations WHERE id=%s',
            (conv_id,))
        row = cursor.fetchone()
    return dict(row) if row is not None else None


def load_largest_conversation_archives(conn, limit: int) -> list[tuple]:
    """Return a bounded legacy-PG corpus for serializer benchmarking."""
    bounded = max(1, min(int(limit), 1000))
    with conn.cursor() as cursor:
        cursor.execute(
            'SELECT id, messages FROM conversations ORDER BY '
            'octet_length(messages::text) DESC LIMIT %s', (bounded,))
        return list(cursor.fetchall())


def load_recovery_conversation_archives(conn) -> list[dict]:
    """Load portable fields from a reviewed recovery database."""
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            'SELECT id,user_id,title,messages::text AS messages,created_at,'
            'updated_at,settings::text AS settings,msg_count,search_text '
            'FROM conversations')
        return [dict(row) for row in cursor.fetchall()]


def merge_recovery_conversation_archives(
    conn,
    rows,
    *,
    apply: bool,
    verify_ids=(),
) -> dict:
    """Idempotently merge reviewed legacy rows with per-row isolation."""
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM conversations')
        before = int(cursor.fetchone()[0])
        if not apply:
            return {
                'before': before, 'after': before, 'inserted': 0,
                'failed': [], 'verified': {},
            }
        statement = (
            'INSERT INTO conversations '
            '(id,user_id,title,messages,created_at,updated_at,settings,'
            'msg_count,search_text) VALUES '
            '(%(id)s,%(user_id)s,%(title)s,%(messages)s::jsonb,'
            '%(created_at)s,%(updated_at)s,%(settings)s::jsonb,'
            '%(msg_count)s,%(search_text)s) '
            'ON CONFLICT (id,user_id) DO NOTHING')
        inserted = 0
        failed = []
        for row in rows:
            try:
                cursor.execute(statement, row)
                inserted += int(cursor.rowcount or 0)
            except Exception as exc:
                failed.append((str(row.get('id') or ''), str(exc)[:160]))
        cursor.execute('SELECT count(*) FROM conversations')
        after = int(cursor.fetchone()[0])
        verified = {}
        for conv_id in verify_ids:
            cursor.execute(
                'SELECT id,left(title,40),length(messages::text) '
                'FROM conversations WHERE id=%s', (conv_id,))
            verified[str(conv_id)] = cursor.fetchone()
    return {
        'before': before, 'after': after, 'inserted': inserted,
        'failed': failed, 'verified': verified,
    }


__all__ = [
    'load_external_conversation_archive',
    'load_largest_conversation_archives',
    'load_recovery_conversation_archives',
    'merge_recovery_conversation_archives',
    'open_postgres_tool_connection',
]
