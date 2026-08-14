"""Semantic reads for persisted task-result recovery state."""

from __future__ import annotations

import json

from lib.database import write_transaction
from lib.log import get_logger


logger = get_logger(__name__)


def list_completion_candidates(db, conv_id: str) -> list[dict]:
    """Return normalized task content/metadata used by repair tooling."""
    rows = db.execute(
        'SELECT task_id, content, metadata FROM task_results WHERE conv_id=?',
        (conv_id,)).fetchall()
    output = []
    for row in rows:
        metadata = row['metadata']
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug('[TaskResults] malformed recovery metadata: %s', exc)
                metadata = {}
        output.append({
            'task_id': row['task_id'],
            'content': row['content'] or '',
            'metadata': metadata if isinstance(metadata, dict) else {},
        })
    return output


def seed_benchmark_checkpoint(
    db, *, task_id: str, conv_id: str, content: str
) -> None:
    """Create/update a synthetic checkpoint for an isolated benchmark DB."""
    from lib.database._core_schema import TASK_RESULTS, upsert

    with write_transaction(db, label='seed task checkpoint benchmark'):
        upsert(
            db, TASK_RESULTS,
            {'task_id': task_id, 'conv_id': conv_id, 'content': content,
             'thinking': '', 'status': 'running'},
            conflict_cols=['task_id'],
            insert_cols=[
                'task_id', 'conv_id', 'content', 'thinking', 'status'],
            update_cols=['content'], commit=False, retry=False)


def read_checkpoint_summary(db, task_id: str) -> dict | None:
    row = db.execute(
        'SELECT content, thinking, status FROM task_results WHERE task_id=?',
        (task_id,)).fetchone()
    return dict(row) if row is not None else None


def list_recent_metadata(
    db, *, contains: str, limit: int = 4000
) -> list[dict]:
    """Return bounded recent task metadata for read-only diagnostics."""
    if not contains:
        raise ValueError('metadata search text is required')
    bounded = max(1, min(int(limit), 10_000))
    rows = db.execute(
        'SELECT task_id, conv_id, created_at, metadata FROM task_results '
        'WHERE metadata LIKE ? ORDER BY created_at DESC LIMIT ?',
        (f'%{contains}%', bounded)).fetchall()
    return [dict(row) for row in rows]


def cleanup_benchmark_task(db, task_id: str) -> None:
    with write_transaction(db, label='cleanup task checkpoint benchmark'):
        db.execute('DELETE FROM task_events WHERE task_id=?', (task_id,))
        db.execute('DELETE FROM task_results WHERE task_id=?', (task_id,))


__all__ = [
    'cleanup_benchmark_task', 'list_completion_candidates',
    'list_recent_metadata', 'read_checkpoint_summary',
    'seed_benchmark_checkpoint',
]
