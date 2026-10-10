"""Incremental translation close drains physical workers before dependencies."""
import threading
import weakref

import pytest

from lib.translate import incremental

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setattr(incremental, '_accumulators', {})
    monkeypatch.setattr(incremental, '_worker_threads', weakref.WeakSet())
    monkeypatch.setattr(incremental, '_shutdown_started', False)
    monkeypatch.setattr(incremental, '_retired_stamps', {})
    monkeypatch.setattr(incremental, '_PREVIEW_MIN_NARRATION_CHARS', 1)
    yield
    incremental.shutdown_incremental_translations()
    assert not incremental._accumulators
    assert not incremental._worker_threads
    assert not incremental._retired_stamps


def task(identifier='fixture'):
    return {'id': identifier, 'convId': 'conversation', '_turnId': 'turn', '_userId': 9,
            'config': {'autoTranslate': True, 'uiLang': 'zh'}}


def test_close_waits_for_cancelled_callback_and_fences_late_push(monkeypatch):
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    pushes = []
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *args, **kwargs: pushes.append(args))
    def translate(self, original, progress_cb=None, **kwargs):
        entered.set()
        assert release.wait(5)
        progress_cb('late-progress')
        return 'late-result', 'fixture-model'
    monkeypatch.setattr(incremental._Accumulator, '_translate', translate)
    value = task()
    assert incremental.submit_round_segment(value, 1, 'source')
    accumulator = incremental._accumulators[value['id']]
    assert entered.wait(5)
    def close():
        incremental.shutdown_incremental_translations()
        returned.set()
    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert not returned.wait(.1)
        assert not incremental.submit_round_segment(task('late'), 1, 'source')
    finally:
        release.set()
        closer.join(5)
    assert returned.is_set() and not closer.is_alive()
    assert not accumulator._thread.is_alive()
    assert not accumulator._queue.snapshot()['spoolOpen']
    assert not pushes and not accumulator.translations
    incremental.shutdown_incremental_translations()


def test_shutdown_wakes_idle_worker_without_waiting_for_idle_timeout():
    accumulator = incremental._Accumulator(task())
    with incremental._accumulators_lock:
        incremental._accumulators[accumulator.task_id] = accumulator
        accumulator.start()
    incremental.shutdown_incremental_translations()
    assert not accumulator._thread.is_alive()
    assert not incremental._worker_threads
    assert not incremental._retired_stamps


def test_worker_cannot_join_its_own_owner(monkeypatch):
    observed = threading.Event()
    def translate(self, original, **kwargs):
        with pytest.raises(RuntimeError, match='cannot join its own owner'):
            incremental.shutdown_incremental_translations()
        assert not incremental._shutdown_started
        observed.set()
        return 'translated', 'fixture'
    monkeypatch.setattr(incremental._Accumulator, '_translate', translate)
    monkeypatch.setattr(incremental._Accumulator, '_push', lambda *args: None)
    assert incremental.submit_round_segment(task(), 1, 'source')
    assert observed.wait(5)


def test_failed_thread_start_leaves_no_registered_worker(monkeypatch):
    def fail(self):
        raise RuntimeError('injected thread start failure')
    monkeypatch.setattr(threading.Thread, 'start', fail)
    value = task()
    assert not incremental.submit_round_segment(value, 1, 'source')
    assert not incremental._accumulators and not incremental._worker_threads
    assert '_incremental_translate_active' not in value


def test_close_joins_retired_cache_persistence_and_refuses_late_handoff(monkeypatch):
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def commit(self, **kwargs):
        calls.append(self.task_id)
        entered.set()
        assert release.wait(5)
        return True
    monkeypatch.setattr(incremental._Accumulator, '_commit_segments_only', commit)
    value = task()
    value[incremental._TASK_PREVIEW_STATE] = {'translations': {'text:1': 'cached'},
        'originals': {'text:1': 'source'}}
    handoff = threading.Thread(target=incremental.finalize_incremental_stamp_only, args=(value,))
    handoff.start()
    assert entered.wait(5)
    def close():
        incremental.shutdown_incremental_translations()
        returned.set()
    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert not returned.wait(.1)
        late = task('late')
        late[incremental._TASK_PREVIEW_STATE] = {'translations': {'text:1': 'cached'}}
        assert not incremental.finalize_incremental_stamp_only(late)
    finally:
        release.set()
        handoff.join(5)
        closer.join(5)
    assert returned.is_set() and not handoff.is_alive() and not closer.is_alive()
    assert calls == ['fixture']
    assert not incremental._retired_stamps


def test_retired_cache_callback_cannot_join_itself(monkeypatch):
    def commit(self, **kwargs):
        with pytest.raises(RuntimeError, match='cannot join its own owner'):
            incremental.shutdown_incremental_translations()
        assert not incremental._shutdown_started
        return True
    monkeypatch.setattr(incremental._Accumulator, '_commit_segments_only', commit)
    value = task()
    value[incremental._TASK_PREVIEW_STATE] = {'translations': {'text:1': 'cached'}}
    assert incremental.finalize_incremental_stamp_only(value)
    assert not incremental._retired_stamps


def test_close_joins_thread_after_accumulator_has_deregistered(monkeypatch):
    retired, release, returned = threading.Event(), threading.Event(), threading.Event()
    original_run = incremental._Accumulator._run
    def delayed_exit(self):
        original_run(self)
        retired.set()
        assert release.wait(5)
    monkeypatch.setattr(incremental._Accumulator, '_run', delayed_exit)
    monkeypatch.setattr(incremental._Accumulator, '_commit_segments_only', lambda *args, **kwargs: False)
    accumulator = incremental._Accumulator(task())
    with incremental._accumulators_lock:
        incremental._accumulators[accumulator.task_id] = accumulator
        accumulator.start()
    assert retired.wait(5)
    assert not incremental._accumulators
    def close():
        incremental.shutdown_incremental_translations()
        returned.set()
    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert not returned.wait(1.1)
    finally:
        release.set()
        closer.join(5)
    assert returned.is_set() and not closer.is_alive()
    assert not accumulator._thread.is_alive()


def test_start_failure_after_physical_activation_remains_joinable(monkeypatch):
    original_start = threading.Thread.start
    def activated_then_failed(self):
        original_start(self)
        raise RuntimeError('injected failure after thread activation')
    monkeypatch.setattr(threading.Thread, 'start', activated_then_failed)
    assert not incremental.submit_round_segment(task(), 1, 'source')
    incremental.shutdown_incremental_translations()
    assert not incremental._worker_threads and not incremental._accumulators
