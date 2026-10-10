"""Accepted event payloads overflow to owned files without count/byte refusal."""
from __future__ import annotations

import gc
import threading
import time
import weakref

import pytest

from lib.storage import event_batcher as module
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


class Client:
    def __init__(self, blocked=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.rows = []
        if not blocked:
            self.release.set()

    def command(self, operation, payload, *args, **kwargs):
        self.entered.set()
        assert self.release.wait(10)
        self.rows.extend(payload['events'])
        return {'results': [{'inserted': True, 'task_id': row['task_id'],
                             'sequence': row['sequence']} for row in payload['events']],
                'inserted': len(payload['events'])}


def _files(monkeypatch):
    files = []
    create = module.tempfile.TemporaryFile

    def temporary(*args, **kwargs):
        file = create(*args, **kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(module.tempfile, 'TemporaryFile', temporary)
    return files


def test_backlog_above_legacy_slots_spills_and_flushes_exact_frozen_prefix(monkeypatch):
    files = _files(monkeypatch)
    client = Client(blocked=True)
    batcher = module.StorageEventBatcher(lambda **kwargs: client,
        queue_capacity=1, queue_byte_capacity=1, max_batch=8,
        max_batch_bytes=256 * 1024, coalesce_ms=0)
    class Event(dict):
        pass

    references = []
    started = time.monotonic()
    try:
        batcher.append('active', 0, {'text': 'first'}, wait=False)
        assert client.entered.wait(1)
        for sequence in range(1, 33):
            event = Event(text='x' * (64 * 1024))
            references.append(weakref.ref(event))
            assert batcher.append('burst', sequence, event, wait=False)['accepted']
            event['text'] = 'mutated after acceptance'
            del event
        gc.collect()
        assert all(reference() is None for reference in references)
        metrics = batcher.metrics
        assert metrics['queue_depth'] == 32
        assert metrics['queue_rejections'] == 0 and metrics['queue_capacity'] == 0
        assert metrics['frame_buffer_memory_bytes'] <= 1
        assert metrics['frame_buffer_file_bytes'] >= 32 * 64 * 1024
        print(f'32 accepted file-backed events / 2 MiB: '
              f'{(time.monotonic() - started) * 1000:.2f} ms before writer release')
        client.release.set()
        assert batcher.flush(timeout=5)
        assert [row['sequence'] for row in client.rows] == list(range(33))
        assert all(row['event']['text'] == 'x' * (64 * 1024) for row in client.rows[1:])
        assert batcher.metrics['frame_bytes_inflight'] == 0
        assert all(file.closed for file in files)
    finally:
        client.release.set()
        assert batcher.close(timeout=5)


def test_real_spool_creation_failure_is_unaccepted_and_releases_representation(monkeypatch):
    client = Client()
    batcher = module.StorageEventBatcher(lambda **kwargs: client,
        queue_capacity=1, queue_byte_capacity=1, coalesce_ms=0)
    create = module.tempfile.TemporaryFile

    def fail(*args, **kwargs):
        raise OSError('injected physical spool failure')

    try:
        monkeypatch.setattr(module.tempfile, 'TemporaryFile', fail)
        with pytest.raises(StorageError) as raised:
            batcher.append('failed', 0, {'text': 'payload'}, wait=False)
        assert raised.value.code == 'database_unavailable'
        assert raised.value.request_not_dispatched
        assert not client.rows and batcher.metrics['frame_bytes_inflight'] == 0
        assert batcher.metrics['queue_depth'] == 0
        monkeypatch.setattr(module.tempfile, 'TemporaryFile', create)
        assert batcher.append('good', 1, {'text': 'next'})['inserted']
        assert batcher.flush()
        assert [row['task_id'] for row in client.rows] == ['good']
    finally:
        assert batcher.close()


def test_confirmation_cancellation_closes_only_unstarted_spill(monkeypatch):
    files = _files(monkeypatch)
    client = Client(blocked=True)
    batcher = module.StorageEventBatcher(lambda **kwargs: client,
        queue_capacity=1, queue_byte_capacity=1, coalesce_ms=0)
    try:
        batcher.append('active', 0, {'text': 'active'}, wait=False)
        assert client.entered.wait(1)
        with pytest.raises(StorageError, match='confirmation timed out'):
            batcher.append('cancelled', 1, {'text': 'cancel me'}, timeout=.01)
        assert len(files) == 2 and files[1].closed and not files[0].closed
        assert batcher.metrics['queue_depth'] == 0
        client.release.set()
        with pytest.raises(StorageError, match='confirmation timed out'):
            batcher.flush(timeout=5)
        assert [row['task_id'] for row in client.rows] == ['active']
        assert all(file.closed for file in files)
    finally:
        client.release.set()
        assert batcher.close()


def test_read_failure_settles_prefix_without_dispatch_or_leaked_file(monkeypatch):
    files = _files(monkeypatch)
    client = Client()
    batcher = module.StorageEventBatcher(lambda **kwargs: client,
        queue_byte_capacity=1, coalesce_ms=0)

    def fail(*args, **kwargs):
        raise OSError('injected physical read failure')

    monkeypatch.setattr(module.json, 'load', fail)
    try:
        with pytest.raises(StorageError) as raised:
            batcher.append('failed', 1, {'text': 'read failure'})
        assert raised.value.code == 'database_unavailable' and raised.value.request_not_dispatched
        with pytest.raises(StorageError) as flushed:
            batcher.flush()
        assert flushed.value.code == 'database_unavailable'
        assert not client.rows and all(file.closed for file in files)
        assert batcher.metrics['frame_bytes_inflight'] == 0
    finally:
        assert batcher.close()


def test_close_retains_ownership_of_unpublished_file_preparation(monkeypatch):
    client = Client()
    batcher = module.StorageEventBatcher(lambda **kwargs: client,
        queue_byte_capacity=1, coalesce_ms=0)
    create = module.tempfile.TemporaryFile
    entered, release = threading.Event(), threading.Event()
    files, errors = [], []

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        file = create(*args, **kwargs)
        files.append(file)
        return file

    def append():
        try:
            batcher.append('late', 1, {'text': 'not published'}, wait=False)
        except StorageError as error:
            errors.append(error)

    monkeypatch.setattr(module.tempfile, 'TemporaryFile', delayed)
    producer = threading.Thread(target=append)
    try:
        producer.start()
        assert entered.wait(1)
        assert not batcher.close(timeout=.1)
        assert batcher.metrics['preparing_payloads'] == 1
        assert batcher.metrics['frame_bytes_inflight'] > 0
        release.set()
        producer.join(3)
        assert not producer.is_alive()
        assert len(errors) == 1 and errors[0].code == 'database_unavailable'
        assert batcher.close(timeout=3)
        assert not client.rows and all(file.closed for file in files)
        assert batcher.metrics['preparing_payloads'] == 0
        assert batcher.metrics['frame_bytes_inflight'] == 0
    finally:
        release.set()
        producer.join(5)
        assert batcher.close(timeout=5)
