"""Worker wake publication survives the adapter's existing COMMIT fault paths."""
import time

import pytest

from lib.storage.errors import StorageError
from tests.storage_authority_sql.test_sqlite_commit_delivery import (
    CommitFault, backend as backend, command, effect, totals,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('receipt', [False, True])
def test_lost_commit_ack_still_publishes_worker_hint(backend, receipt):
    control = backend.worker_control
    before = control.watch({'cursor': None}, time.monotonic() + 5)
    conversation_before = control.conversations.watch({'cursor': None}, time.monotonic())
    fault = CommitFault(backend._writer._connection, phase='after')
    backend._writer._connection = fault
    calls = []

    def enqueue(session):
        session.worker_hints.queue_changed = True
        session.worker_hints.conversation_changes.add((41, 'changed'))
        session.worker_hints.cancelled_tasks.add('job')
        return effect('one', calls)(session)

    if receipt:
        assert command(backend, 'one', enqueue, receipt=True) == 1
    else:
        with pytest.raises(StorageError):
            command(backend, 'one', enqueue, receipt=False)
    after = control.watch({'cursor': before['cursor']}, time.monotonic() + 5)
    assert after['cursor']['queue'] > before['cursor']['queue']
    assert after['cursor']['cancel'] > before['cursor']['cancel']
    assert totals(backend) == [{'name': 'one', 'total': 1}]
    assert calls == ['one']
    assert control.conversations.watch({'cursor': conversation_before['cursor']}, time.monotonic())['cursor'] != conversation_before['cursor']


def test_failed_statement_does_not_publish_uncommitted_hints(backend):
    control = backend.worker_control
    before = control.watch({'cursor': None}, time.monotonic() + 5)
    conversation_before = control.conversations.watch({'cursor': None}, time.monotonic())

    def rejected(session):
        session.worker_hints.queue_changed = True
        session.worker_hints.conversation_changes.add((41, 'changed'))
        raise StorageError('database_conflict', 'rejected before commit')

    with pytest.raises(StorageError):
        command(backend, 'one', rejected, receipt=False)
    after = control.watch({'cursor': before['cursor']}, time.monotonic())
    assert after['cursor'] == before['cursor']
    assert control.conversations.watch({'cursor': conversation_before['cursor']}, time.monotonic())['cursor'] == conversation_before['cursor']
