"""Atomic, scope-local sequence allocation for append-only streams.

Sequence ownership belongs to the database, not to a Python ``Lock``.  The
caller must invoke :func:`allocate_scoped_sequence` inside the same
``write_transaction`` that inserts the stream row.  The counter row then acts
as the portable SQLite/PostgreSQL serialization point across threads,
processes, and hosts.
"""

from __future__ import annotations


_SOURCES = {
    'project_events': ('project_events', 'project_path', 'seq'),
    'project_status_snapshots': (
        'project_status_snapshots', 'project_path', 'seq'),
    'project_watch_responses': ('project_watch_responses', 'item_id', 'seq'),
    'message_queue_position': ('message_queue', 'conv_id', 'position'),
    'attempt_events': ('attempt_events', 'attempt_id', 'seq'),
}


def lock_scoped_sequence(db, namespace: str, scope_key: str) -> None:
    """Acquire a portable transaction-scoped mutex row without incrementing.

    PostgreSQL serializes conflicting UPSERTs on the primary-key row; SQLite's
    outer ``BEGIN IMMEDIATE`` already owns its sole writer slot. The lock is
    released by the caller's surrounding ``write_transaction``.
    """
    if not namespace or not scope_key:
        raise ValueError('namespace and scope_key must not be empty')
    db.execute(
        'INSERT INTO scoped_sequences(namespace, scope_key, value) '
        'VALUES (?, ?, 0) '
        'ON CONFLICT(namespace, scope_key) DO UPDATE SET '
        'value=scoped_sequences.value',
        (namespace, scope_key),
    )


def allocate_scoped_sequence(db, namespace: str, scope_key: str) -> int:
    """Return the next sequence under a database-owned atomic counter.

    ``MAX(seq)`` is observed only as an upgrade/self-heal floor.  Concurrent
    allocators serialize on ``scoped_sequences(namespace, scope_key)`` and the
    UPSERT increments exactly once.  Keeping this call and the append in one
    outer transaction also means a failed append rolls the allocation back.
    """
    try:
        table, scope_column, value_column = _SOURCES[namespace]
    except KeyError as exc:
        raise ValueError(f'unknown scoped sequence namespace: {namespace!r}') from exc
    if not scope_key:
        raise ValueError('scope_key must not be empty')

    floor_row = db.execute(
        f'SELECT COALESCE(MAX({value_column}), 0) + 1 AS next_value '
        f'FROM {table} WHERE {scope_column}=?', (scope_key,)).fetchone()
    floor = int(floor_row['next_value'] if floor_row else 1)
    row = db.execute(
        'INSERT INTO scoped_sequences(namespace, scope_key, value) '
        'VALUES (?, ?, ?) '
        'ON CONFLICT(namespace, scope_key) DO UPDATE SET value=CASE '
        'WHEN scoped_sequences.value < excluded.value THEN excluded.value '
        'ELSE scoped_sequences.value + 1 END '
        'RETURNING value',
        (namespace, scope_key, floor),
    ).fetchone()
    if not row:
        raise RuntimeError('scoped sequence allocation returned no row')
    return int(row['value'])


__all__ = ['allocate_scoped_sequence', 'lock_scoped_sequence']
