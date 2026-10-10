"""Accepted backlog owns every payload and releases reconstructible storage."""
import queue
import threading
import tracemalloc

import pytest

from lib.translate._operation_buffer import IncrementalOperationBuffer

pytestmark = pytest.mark.unit


def test_large_backlog_retains_all_segments_with_metadata_only_memory():
    text = 'fixture:' + 'x' * (32 * 1024 - 8)
    buffer = IncrementalOperationBuffer(2)
    tracemalloc.start()
    try:
        for index in range(2085):
            assert buffer.put_segment(index, text) == 0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    try:
        snapshot = buffer.snapshot()
        assert snapshot['depth'] == 2085
        assert snapshot['pendingTextBytes'] == 2085 * len(text.encode())
        assert snapshot['spoolBytes'] == snapshot['pendingTextBytes']
        assert snapshot['droppedSegments'] == 0
        assert peak < 8 * 1024 * 1024
        buffer.put_terminal(('stamp',), replace=True)
        for index in range(2085):
            assert buffer.get(0) == ('segment', index, text)
        assert buffer.get(0) == ('stamp',)
        assert not buffer.snapshot()['spoolOpen']
        assert buffer.snapshot()['spoolBytes'] == 0
    finally:
        buffer.close()


def test_repeated_keys_coalesce_and_reclaim_obsolete_bytes():
    buffer = IncrementalOperationBuffer()
    try:
        for index in range(1000):
            buffer.put_segment('round', f'{index:04}:' + '译' * 2000)
        state = buffer.snapshot()
        assert state['depth'] == 1
        assert state['spoolBytes'] <= state['pendingTextBytes'] * 2
        assert buffer.get(0) == ('segment', 'round', '0999:' + '译' * 2000)
        assert buffer.snapshot()['spoolBytes'] == 0
    finally:
        buffer.close()


def test_failed_append_preserves_previous_accepted_work():
    buffer = IncrementalOperationBuffer()
    buffer.put_segment('old', 'accepted')
    original = buffer._spool
    class FailedWriter:
        def __getattr__(self, name):
            return getattr(original, name)
        def write(self, raw):
            raise OSError('injected write failure')
    buffer._spool = FailedWriter()
    try:
        with pytest.raises(OSError):
            buffer.put_segment('new', 'unaccepted')
        assert buffer.snapshot()['depth'] == 1
        assert buffer.get(0) == ('segment', 'old', 'accepted')
    finally:
        buffer.close()
    assert original.closed


def test_housekeeping_failure_keeps_latest_accepted_work(monkeypatch):
    buffer = IncrementalOperationBuffer()
    buffer.put_segment('round', 'old')
    def unavailable(*args, **kwargs):
        raise OSError('injected temporary file failure')
    monkeypatch.setattr('lib.translate._operation_buffer.tempfile.TemporaryFile', unavailable)
    try:
        assert buffer.put_segment('round', 'new') == 0
        assert buffer.put_segment('round', 'last') == 0
        assert buffer.get(0) == ('segment', 'round', 'last')
    finally:
        buffer.close()


def test_explicit_cancel_discards_backlog_and_releases_spool():
    buffer = IncrementalOperationBuffer()
    for index in range(300):
        buffer.put_segment(index, 'private-fixture')
    stopped = object()
    assert buffer.put_terminal(stopped, replace=True, discard_segments=True) == 300
    assert buffer.snapshot()['pendingTextBytes'] == 0
    assert buffer.get(0) is stopped
    assert not buffer.snapshot()['spoolOpen']
    assert buffer.put_segment('late', 'unowned') == -1


def test_close_wakes_consumer_and_rejects_new_work():
    buffer = IncrementalOperationBuffer()
    entered, finished = threading.Event(), threading.Event()
    def consume():
        entered.set()
        with pytest.raises(queue.Empty):
            buffer.get(30)
        finished.set()
    worker = threading.Thread(target=consume)
    worker.start()
    assert entered.wait(5)
    buffer.close()
    worker.join(5)
    assert finished.is_set() and not worker.is_alive()
    assert buffer.put_terminal(('stamp',)) == -1


def test_independent_accumulators_start_while_other_translations_are_blocked(monkeypatch):
    from lib.translate import incremental
    release, healthy = threading.Event(), threading.Event()
    condition = threading.Condition()
    entered = []
    tasks = []
    accumulators = []
    monkeypatch.setattr(incremental, '_accumulators', {})
    original_start = incremental._Accumulator.start
    def capture_start(self):
        accumulators.append(self)
        original_start(self)
    monkeypatch.setattr(incremental._Accumulator, 'start', capture_start)
    monkeypatch.setattr(incremental, '_PREVIEW_MIN_NARRATION_CHARS', 1)
    monkeypatch.setattr(incremental._Accumulator, '_push', lambda *args: None)
    def translate(self, original, **kwargs):
        if original == 'healthy':
            healthy.set()
        else:
            with condition:
                entered.append(self.task_id)
                condition.notify_all()
            assert release.wait(10)
        return 'fixture-translation', 'fixture-model'
    monkeypatch.setattr(incremental._Accumulator, '_translate', translate)
    try:
        for index in range(66):
            task = {'id': f'demand-{index}', 'convId': f'conversation-{index}',
                    '_turnId': f'turn-{index}', '_userId': 9 + index,
                    'config': {'autoTranslate': True, 'uiLang': 'zh'}}
            tasks.append(task)
            assert incremental.submit_round_segment(task, 1, 'healthy' if index == 65 else 'blocked')
        with condition:
            assert condition.wait_for(lambda: len(entered) == 65, timeout=10)
        assert healthy.wait(5)
        accumulators[-1]._thread.join(5)
        assert not accumulators[-1]._thread.is_alive()
        assert len(incremental._accumulators) == 65
    finally:
        release.set()
        for task in tasks:
            incremental.cancel_incremental(task)
        for accumulator in accumulators:
            accumulator._thread.join(10)
    assert not incremental._accumulators
    assert all(not accumulator._thread.is_alive() for accumulator in accumulators)
    assert all(not accumulator._queue.snapshot()['spoolOpen'] for accumulator in accumulators)
