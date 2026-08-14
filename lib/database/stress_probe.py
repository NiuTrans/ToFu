"""Data-layer implementation for the isolated concurrency stress diagnostic."""

from __future__ import annotations

import time

from lib.database import DOMAIN_CHAT, pooled_db, write_transaction


def initialize() -> None:
    with pooled_db(DOMAIN_CHAT) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS database_stress_probe (
                id INTEGER PRIMARY KEY,
                thread_id INTEGER NOT NULL,
                value TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        ''')


def run_operation(thread_id: int, operation_index: int) -> None:
    with pooled_db(DOMAIN_CHAT) as db:
        if operation_index % 3 == 1:
            db.execute(
                'SELECT COUNT(*) FROM database_stress_probe WHERE thread_id=?',
                (thread_id,)).fetchone()
            return
        with write_transaction(db, label='database stress probe operation'):
            if operation_index % 3 == 0:
                db.execute(
                    'INSERT INTO database_stress_probe '
                    '(thread_id, value, created_at) VALUES (?, ?, ?)',
                    (thread_id, f'thread-{thread_id}-op-{operation_index}',
                     time.time()))
            else:
                db.execute(
                    'UPDATE database_stress_probe SET value=? '
                    'WHERE thread_id=? AND id=('
                    'SELECT MAX(id) FROM database_stress_probe '
                    'WHERE thread_id=?)',
                    (f'updated-{operation_index}', thread_id, thread_id))


def row_count() -> int:
    with pooled_db(DOMAIN_CHAT) as db:
        row = db.execute(
            'SELECT COUNT(*) FROM database_stress_probe').fetchone()
        return int(row[0])


__all__ = ['initialize', 'row_count', 'run_operation']
