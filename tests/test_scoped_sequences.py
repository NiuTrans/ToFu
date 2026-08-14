"""Database-owned stream sequence concurrency and rollback guarantees."""

from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from lib.database import (
    DOMAIN_CHAT,
    allocate_scoped_sequence,
    pooled_write_transaction,
)


pytestmark = pytest.mark.unit


def _append_event(scope: str, index: int) -> int:
    with pooled_write_transaction(
            DOMAIN_CHAT, label='scoped-sequence-concurrency-test') as db:
        seq = allocate_scoped_sequence(db, 'project_events', scope)
        db.execute(
            'INSERT INTO project_events '
            '(project_path, seq, event_id, conv_id, task_id, kind, title, '
            'summary, payload, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (scope, seq, f'e{index}', '', '', 'note', '', '', '{}',
             int(time.time() * 1000)))
        return seq


def test_concurrent_allocators_mint_each_sequence_exactly_once(flask_app):
    scope = f'/sequence-test/{time.time_ns()}'
    with ThreadPoolExecutor(max_workers=8) as pool:
        seqs = list(pool.map(lambda i: _append_event(scope, i), range(24)))
    assert sorted(seqs) == list(range(1, 25))


def test_failed_append_rolls_back_its_sequence_allocation(flask_app):
    scope = f'/sequence-rollback/{time.time_ns()}'
    with pytest.raises(RuntimeError, match='abort append'):
        with pooled_write_transaction(
                DOMAIN_CHAT, label='scoped-sequence-rollback-test') as db:
            assert allocate_scoped_sequence(db, 'project_events', scope) == 1
            raise RuntimeError('abort append')
    assert _append_event(scope, 1) == 1
