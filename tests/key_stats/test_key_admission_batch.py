"""Key-health reads stay off the global selection path's critical costs.

A dispatcher pick reads one key-health verdict per candidate slot. The store
freshness check behind ``is_key_enabled`` stats the store file; entering one
``key_admission_batch()`` runs it once, and the reads inside the block skip
it. The caller enters the batch before taking its own locks. The demand-driven
flush worker is started after the in-memory ``_lock`` is released, so a
concurrent admission read never waits for a thread start.
"""
import json
import os
import threading

import pytest

pytestmark = pytest.mark.unit

PROVIDER = 'batch-provider'


@pytest.fixture
def stats(monkeypatch, tmp_path):
    import lib.key_stats as ks
    from lib.key_stats import _state

    ks.shutdown_key_stats()
    monkeypatch.setattr(_state, 'KEY_STATS_FLUSH_INTERVAL_SECONDS', 3600.0)
    snapshot = {key: ks._cache[key] for key in ('day', 'stats', 'overrides', 'loaded')}
    pending = list(_state._pending_mutations)
    monkeypatch.setattr(ks, '_STATS_PATH', str(tmp_path / 'key_stats.json'))
    monkeypatch.setattr(ks, '_list_siblings', lambda provider: [])
    ks._cache.update(day='', stats={}, overrides={}, loaded=False)
    _state._pending_mutations.clear()
    fingerprints = []
    real_fingerprint = _state._file_fingerprint

    def counting_fingerprint(path):
        fingerprints.append(path)
        return real_fingerprint(path)

    monkeypatch.setattr(_state, '_file_fingerprint', counting_fingerprint)
    ks.is_key_enabled(PROVIDER, 'warm-load')
    fingerprints.clear()
    yield ks, fingerprints
    _state._pending_mutations.clear()
    ks.shutdown_key_stats()
    ks._cache.update(snapshot)
    _state._pending_mutations[:] = pending


def test_batch_checks_store_freshness_once_for_many_reads(stats):
    ks, fingerprints = stats
    keys = [f'key-{index}' for index in range(40)]

    assert all(ks.is_key_enabled(PROVIDER, key) for key in keys)
    assert len(fingerprints) == len(keys)

    fingerprints.clear()
    with ks.key_admission_batch():
        assert len(fingerprints) == 1, 'entering the batch checks freshness'
        assert all(ks.is_key_enabled(PROVIDER, key) for key in keys)
    assert len(fingerprints) == 1

    fingerprints.clear()
    assert ks.is_key_enabled(PROVIDER, keys[0])
    assert len(fingerprints) == 1, 'the batch scope must not outlive its block'


def test_batch_still_honours_decisions_recorded_inside_it(stats):
    ks, _ = stats
    with ks.key_admission_batch():
        assert ks.is_key_enabled(PROVIDER, 'decided')
        ks.set_key_override(PROVIDER, 'decided', False)
        assert not ks.is_key_enabled(PROVIDER, 'decided')


def test_batch_is_local_to_its_execution_context(stats):
    ks, fingerprints = stats
    with ks.key_admission_batch():
        ks.is_key_enabled(PROVIDER, 'here')
        worker = threading.Thread(
            target=lambda: [ks.is_key_enabled(PROVIDER, 'there') for _ in range(3)])
        worker.start()
        worker.join(10)
    assert len(fingerprints) == 4


@pytest.mark.parametrize('batched', [True, False])
def test_admission_reads_never_stat_the_store_while_holding_the_lock(
        stats, monkeypatch, batched):
    """The stat releases the GIL; under GIL pressure the holder of ``_lock``
    would then wait a full scheduling round while every reader queues."""
    ks, fingerprints = stats
    from lib.key_stats import _state
    held = []
    counting = _state._file_fingerprint
    monkeypatch.setattr(_state, '_file_fingerprint', lambda path: (
        held.append(_state._lock.locked()) or counting(path)))

    if batched:
        with ks.key_admission_batch():
            assert ks.is_key_enabled(PROVIDER, 'a') and ks.is_key_enabled(PROVIDER, 'b')
    else:
        assert ks.is_key_enabled(PROVIDER, 'a') and ks.is_key_enabled(PROVIDER, 'b')
    assert held == [False] * (1 if batched else 2)


def test_change_committed_by_another_writer_is_seen_by_the_next_batch(
        stats, tmp_path):
    ks, _ = stats
    from lib.key_stats import _state
    with ks.key_admission_batch():
        assert ks.is_key_enabled(PROVIDER, 'remote')
    # Another process commits a manual disable (atomic replace, new inode).
    store = tmp_path / 'key_stats.json'
    replacement = tmp_path / 'key_stats.next'
    replacement.write_text(json.dumps({
        'day': _state._current_day(), 'stats': {},
        'overrides': {_state._pair_key(PROVIDER, 'remote'): False}}))
    os.replace(replacement, store)
    with ks.key_admission_batch():
        assert not ks.is_key_enabled(PROVIDER, 'remote')


@pytest.mark.parametrize('producer', ['telemetry', 'failed_decision'])
def test_flush_worker_starts_after_the_stats_lock_is_released(
        stats, monkeypatch, producer):
    ks, _ = stats
    from lib.key_stats import _state

    started_under_lock = []
    real_start = threading.Thread.start

    def observed_start(thread):
        if thread.name == 'tofu-key-stats-flush':
            started_under_lock.append(_state._lock.locked())
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, 'start', observed_start)
    if producer == 'telemetry':
        ks.record_outcome(PROVIDER, 'flushed', success=True)
    else:
        monkeypatch.setattr(_state, '_persist_path_locked', lambda path: False)
        ks.set_key_override(PROVIDER, 'flushed', False)
    assert started_under_lock == [False]
    assert _state._flusher is not None and _state._flusher.is_alive()


def test_shutdown_tolerates_a_claimed_worker_that_has_not_started(stats, monkeypatch):
    ks, _ = stats
    from lib.key_stats import _state

    claimed = threading.Thread(target=lambda: None, name='tofu-key-stats-flush')
    monkeypatch.setattr(_state, '_flusher', claimed)
    assert ks.shutdown_key_stats() is True
