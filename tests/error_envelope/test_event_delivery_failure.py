"""Local event durability failures must never blame machine admission quotas."""
import pytest

from lib.error_envelope import from_exception
from lib.storage.errors import StorageError
from lib.tasks_pkg.event_delivery import (
    EventDeliveryCancelled, EventDeliveryError, EventDeliveryRevoked,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('cause', [
    StorageError('turn_projection_stale', 'projection changed'),
    StorageError('database_integrity', 'checksum mismatch'),
    RuntimeError('event worker failed to start'),
    TimeoutError('durability barrier timed out'),
])
def test_delivery_failure_names_its_subsystem_and_retains_cause(cause):
    error = EventDeliveryError('Earlier task event is not durable')
    error.__cause__ = cause
    envelope = from_exception(error, model='kimi-k3')
    assert envelope['kind'] == 'event_delivery_failed'
    assert envelope['source'] == 'task-event-delivery'
    assert envelope['model'] == ''
    assert str(cause) in envelope['raw']
    assert 'memory headroom' not in envelope['hint']
    assert 'active-task slots' not in envelope['hint']
    assert '并发任务数' not in envelope['hint']


@pytest.mark.parametrize('exception', [EventDeliveryCancelled, EventDeliveryRevoked])
def test_cancellation_and_revocation_keep_their_existing_verdict(exception):
    assert from_exception(exception('publication ended'))['kind'] == 'aborted'
