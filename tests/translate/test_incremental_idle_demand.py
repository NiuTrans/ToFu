"""New arrivals replace idle-retired carriers without losing paid cache."""
import threading
import queue
import weakref

import pytest

from lib.translate import incremental

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setattr(incremental, '_accumulators', {})
    monkeypatch.setattr(incremental, '_worker_threads', weakref.WeakSet())
    monkeypatch.setattr(incremental, '_retired_stamps', {})
    monkeypatch.setattr(incremental, '_shutdown_started', False)
    monkeypatch.setattr(incremental, '_PREVIEW_MIN_NARRATION_CHARS', 1)
    monkeypatch.setattr(incremental._Accumulator, '_push', lambda *args: None)
    monkeypatch.setattr(incremental._Accumulator, '_translate',
        lambda self, source, **kwargs: ('translated:' + source, 'fixture'))
    yield
    incremental.shutdown_incremental_translations()
    assert not incremental._accumulators and not incremental._worker_threads


def task(identifier='fixture'):
    return {'id': identifier, 'convId': 'conversation', '_turnId': 'turn', '_userId': 9,
            'config': {'autoTranslate': True, 'uiLang': 'zh'}}


def test_completed_jobs_leave_zero_idle_workers_and_no_idle_cache_commit(monkeypatch):
    workers, tasks = [], []
    original_start = incremental._Accumulator.start
    def start(self):
        workers.append(self)
        original_start(self)
    monkeypatch.setattr(incremental._Accumulator, 'start', start)
    monkeypatch.setattr(incremental._Accumulator, '_commit_segments_only',
        lambda *args, **kwargs: pytest.fail('idle retirement must not copy/commit the full cache'))
    for index in range(128):
        value = task(f'fixture-{index}')
        tasks.append(value)
        assert incremental.submit_round_segment(value, 1, 'source')
    for worker in workers:
        worker._thread.join(5)
    assert len(workers) == 128
    assert not incremental._accumulators
    assert all(not worker._thread.is_alive() for worker in workers)
    assert all(worker.translations for worker in workers)
    for value in tasks:incremental.cancel_incremental(value)
    assert all(not worker._cache.snapshot()['fileOpen'] for worker in workers)


def test_arrival_in_retirement_gap_recreates_and_reuses_exact_cache(monkeypatch):
    value = task()
    old = incremental._Accumulator(value)
    old._cache.put_pair('text:prior', 'old source', 'paid translation')
    with pytest.raises(queue.Empty):old._queue.get(0)
    incremental._accumulators[value['id']] = old
    workers = []
    original_start = incremental._Accumulator.start
    def start(self):
        workers.append(self)
        original_start(self)
    monkeypatch.setattr(incremental._Accumulator, 'start', start)
    assert incremental.submit_round_segment(value, 2, 'new source')
    new = workers[0]
    new._thread.join(5)
    assert new is not old and new._cache is old._cache
    assert new.translations['text:prior'] == 'paid translation'
    assert 'translated:new source' in new.translations.values()
    incremental.cancel_incremental(value)


def test_partial_start_cannot_dispatch_accepted_input(monkeypatch):
    calls = []
    monkeypatch.setattr(incremental._Accumulator, '_translate',
        lambda *args, **kwargs: calls.append(True) or ('translated', 'fixture'))
    original_start = threading.Thread.start
    def fail_after_activation(self):
        original_start(self)
        raise RuntimeError('injected partial start')
    monkeypatch.setattr(threading.Thread, 'start', fail_after_activation)
    assert not incremental.submit_round_segment(task(), 1, 'source')
    incremental.shutdown_incremental_translations()
    assert calls == []


def test_terminal_and_cancel_fences_prevent_new_carriers(monkeypatch):
    monkeypatch.setattr('lib.translate.commit.commit_translation_to_turn', lambda *args, **kwargs: None)
    finished = task('finished')
    cached = incremental._Accumulator(finished)
    cached._cache.put_pair('key', 'source', 'paid')
    assert incremental.finalize_incremental_stamp_only(finished)
    assert not incremental.submit_round_segment(finished, 2, 'late')
    cancelled = task('cancelled')
    incremental.cancel_incremental(cancelled)
    assert not incremental.submit_round_segment(cancelled, 2, 'late')
    assert not incremental._accumulators


def test_failed_producer_does_not_withdraw_other_accepted_work(monkeypatch):
    value = task()
    failed_entered, fail_release, model_entered, model_release = (
        threading.Event() for _ in range(4))
    original_submit = incremental._Accumulator.submit
    def submit(self, key, source):
        if source == 'fails':
            failed_entered.set()
            assert fail_release.wait(5)
            raise OSError('injected producer failure')
        return original_submit(self, key, source)
    def translate(self, source, **kwargs):
        model_entered.set()
        assert model_release.wait(5)
        return 'accepted translation', 'fixture'
    monkeypatch.setattr(incremental._Accumulator, 'submit', submit)
    monkeypatch.setattr(incremental._Accumulator, '_translate', translate)
    result = []
    producer = threading.Thread(target=lambda: result.append(
        incremental.submit_round_segment(value, 1, 'fails')))
    producer.start()
    assert failed_entered.wait(5)
    try:
        assert incremental.submit_round_segment(value, 2, 'accepted')
        accepted = incremental._accumulators[value['id']]
        assert model_entered.wait(5)
        fail_release.set()
        producer.join(5)
        assert result == [False]
        assert incremental._accumulators[value['id']] is accepted
    finally:
        fail_release.set()
        model_release.set()
        producer.join(5)
    accepted._thread.join(5)
    assert 'accepted translation' in accepted.translations.values()
    incremental.cancel_incremental(value)
