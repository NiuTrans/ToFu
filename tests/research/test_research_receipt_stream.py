"""Complete receipt streams preserve chronology, snapshot authority and cleanup."""
from copy import deepcopy
import tempfile

import pytest

from lib.research.receipt_pages import NAMESPACE, empty_reference
from lib.research.workspace import save_workspace, load_workspace, open_run_receipts
from lib.storage.errors import StorageError
from tests.research.test_research_receipt_pages import repository, fact
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_receipt_publication import initial, prepare, DIRECTION
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def track_spools(monkeypatch):
    created = []
    create = tempfile.TemporaryFile
    def track(*args, **kwargs):
        handle = create(*args, **kwargs); created.append(handle); return handle
    monkeypatch.setattr('lib.research.receipt_pages.tempfile.TemporaryFile', track)
    return created


def test_complete_history_stream_has_original_order_and_detached_tail():
    repo = repository(MemoryRecords())
    reference = repo.prepare(empty_reference(), (fact(n) for n in range(1000)))
    tail = [fact(n) for n in range(1000, 1032)]
    with repo.open_receipts(reference, tail) as receipts:
        tail[0]['result_digest'] = 'mutated-after-open'
        assert list(receipts) == [fact(n) for n in range(1032)]
    assert list(receipts) == []


def test_corrupt_ancestor_rejects_stream_before_first_receipt_and_closes_spool(monkeypatch):
    client = MemoryRecords(); repo = repository(client)
    first = repo.prepare(empty_reference(), [fact(1)])
    final = repo.prepare(first, [fact(2)])
    del client.rows[(NAMESPACE, repo.prefix + first['head'])]
    created = track_spools(monkeypatch); entered = False
    with pytest.raises(StorageError, match='missing or invalid'):
        with repo.open_receipts(final) as receipts:
            entered = True
    assert not entered and len(created) == 1 and created[0].closed


@pytest.mark.parametrize('failure', [False, True])
def test_early_exit_or_consumer_error_closes_iterator_and_spool(monkeypatch, failure):
    repo = repository(MemoryRecords()); reference = repo.prepare(empty_reference(), [fact(1), fact(2)])
    created = track_spools(monkeypatch)
    try:
        with repo.open_receipts(reference) as receipts:
            assert next(receipts) == fact(1)
            if failure: raise RuntimeError('consumer failed')
    except RuntimeError:
        assert failure
    assert created[0].closed and list(receipts) == []


def test_inline_iterable_is_bounded_before_materialization():
    repo = repository(MemoryRecords()); consumed = []
    def tail():
        for n in range(100000):
            consumed.append(n); yield fact(n)
    with pytest.raises(ValueError, match='inline tail exceeds'):
        with repo.open_receipts(empty_reference(), tail()):
            pytest.fail('oversized inline tail must not open')
    assert len(consumed) == 65


def test_spool_write_failure_closes_file_without_exposing_partial_history(monkeypatch):
    repo = repository(MemoryRecords()); reference = repo.prepare(empty_reference(), [fact(1)])
    created = []; create = tempfile.TemporaryFile
    class FailingFile:
        def __init__(self): self.handle = create(mode='w+b'); created.append(self.handle)
        def __enter__(self): return self
        def __exit__(self, *args): self.handle.close()
        def write(self, payload): raise OSError('injected disk full')
    monkeypatch.setattr('lib.research.receipt_pages.tempfile.TemporaryFile', lambda **kwargs: FailingFile())
    with pytest.raises(OSError, match='disk full'):
        with repo.open_receipts(reference): pytest.fail('failed spool must not expose receipts')
    assert created[0].closed


def test_working_run_stream_reads_acknowledged_snapshot_after_restart(storage):
    supervisor, _ = storage
    saved = save_workspace(DIRECTION, 'en', initial(), expected_revision=0, user_id=1)
    saved = save_workspace(DIRECTION, 'en', saved, expected_revision=1, user_id=1,
                           receipt_publication=prepare(supervisor.client, saved))
    supervisor.stop(); supervisor.start()
    with open_run_receipts(DIRECTION, user_id=1, run_id='long-experiment') as stream:
        assert stream['revision'] == 2 and stream['count'] == 40
        later = deepcopy(load_workspace(DIRECTION, user_id=1))
        later['runs'][0]['tool_receipts'].append(fact(40))
        save_workspace(DIRECTION, 'en', later, expected_revision=2, user_id=1)
        assert list(stream['receipts']) == [fact(n) for n in range(40)]
    with open_run_receipts(DIRECTION, user_id=1, run_id='long-experiment') as latest:
        assert latest['revision'] == 3 and latest['count'] == 41
        assert list(latest['receipts']) == [fact(n) for n in range(41)]
    with pytest.raises((ValueError, StorageError), match='not found|authority_scope_mismatch'):
        with open_run_receipts(DIRECTION, user_id=2, run_id='long-experiment'): pass


def test_unpaged_legacy_run_stream_needs_no_dependency_binding(storage):
    supervisor, _ = storage
    draft = initial(); draft['runs'][0]['hypothesis_digest'] = ''
    draft['runs'][0]['protocol_digest'] = ''
    save_workspace(DIRECTION, 'en', draft, expected_revision=0, user_id=1)
    with open_run_receipts(DIRECTION, user_id=1, run_id='long-experiment') as stream:
        assert next(stream['receipts']) == fact(0)
    assert list(stream['receipts']) == []


@pytest.mark.parametrize('timestamp', [1.0, True])
def test_stream_does_not_equate_noncanonical_numeric_types(timestamp):
    row = dict(fact(1), observed_at=timestamp)
    with pytest.raises(ValueError, match='canonical complete'):
        with repository(MemoryRecords()).open_receipts(empty_reference(), [row]):
            pytest.fail('noncanonical input cannot be exposed')
