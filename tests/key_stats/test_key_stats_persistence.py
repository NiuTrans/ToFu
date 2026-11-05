#!/usr/bin/env python3
"""Durability and cross-process coherence guards for ``lib.key_stats``."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest import mock

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

PROVIDER = 'ipc-provider'
KEY = 'ipc-key'
PAIR = f'{PROVIDER}::{KEY}'


@pytest.fixture
def isolated_stats(monkeypatch, tmp_path):
    import lib.key_stats as ks
    from lib.key_stats import _state

    # Join any earlier worker and keep background flushes out of the test:
    # each spec below drives ``flush_key_stats`` explicitly.
    ks.shutdown_key_stats()
    monkeypatch.setattr(_state, 'KEY_STATS_FLUSH_INTERVAL_SECONDS', 3600.0)
    snapshot = {
        'day': ks._cache['day'],
        'stats': ks._cache['stats'],
        'overrides': ks._cache['overrides'],
        'loaded': ks._cache['loaded'],
    }
    pending_snapshot = list(_state._pending_mutations)
    path = tmp_path / 'key_stats.json'
    monkeypatch.setattr(ks, '_STATS_PATH', str(path))
    monkeypatch.setattr(ks, '_list_siblings', lambda _provider: [PAIR])
    ks._cache.update(day='', stats={}, overrides={}, loaded=False)
    _state._pending_mutations.clear()
    yield ks, path
    # Several specs leave mutations for a deliberately broken store pending;
    # discard them rather than let a later worker retry that store forever.
    _state._pending_mutations.clear()
    ks.shutdown_key_stats()
    ks._cache.update(snapshot)
    _state._pending_mutations[:] = pending_snapshot


def _on_disk(stats_path):
    return json.loads(stats_path.read_text(encoding='utf-8'))


_WORKER = r'''
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, sys.argv[1])
import lib.key_stats as ks

stats_path, ready_path, start_path, operation, identity = sys.argv[2:]
ks._STATS_PATH = stats_path
with ks._lock:
    ks._cache.update(day='', stats={}, overrides={}, loaded=False)
    # All workers load the same pre-mutation snapshot before they are released.
    # A full-snapshot writer therefore loses updates deterministically.
    ks._load_unlocked()
Path(ready_path).touch()
deadline = time.monotonic() + 20
while not os.path.exists(start_path):
    if time.monotonic() > deadline:
        raise RuntimeError('timed out waiting for parent barrier')
    time.sleep(0.01)
if operation == 'record':
    ks.record_outcome('ipc-provider', 'ipc-key', success=True)
else:
    ks.set_key_override('ipc-provider', identity, enabled=False)
'''


def _start_barrier_workers(tmp_path, script, argument_lists):
    start_path = tmp_path / 'start'
    processes = []
    ready_paths = []
    for index, arguments in enumerate(argument_lists):
        ready_path = tmp_path / f'ready-{index}'
        ready_paths.append(ready_path)
        processes.append(subprocess.Popen(
            [sys.executable, '-c', script, ROOT, arguments[0],
             str(ready_path), str(start_path), *arguments[1:]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ))

    deadline = time.monotonic() + 30
    while not all(path.exists() for path in ready_paths):
        failed = [process for process in processes
                  if process.poll() not in (None, 0)]
        if failed or time.monotonic() > deadline:
            outputs = [process.communicate(timeout=2) for process in processes]
            pytest.fail(f'workers failed before barrier: {outputs}')
        time.sleep(0.01)
    return processes, start_path


def test_cross_process_counters_and_overrides_do_not_lose_updates(tmp_path):
    """Every process mutates the latest locked document, never its snapshot.

    Record workers exit without an explicit flush: interpreter exit persists
    their coalesced counters.
    """
    pytest.importorskip('fcntl')

    stats_path = tmp_path / 'shared-key-stats.json'
    specs = ([('record', str(index)) for index in range(8)]
             + [('override', f'key-{index}') for index in range(5)])
    processes, start_path = _start_barrier_workers(
        tmp_path, _WORKER,
        [(str(stats_path), operation, identity)
         for operation, identity in specs])
    start_path.touch()

    outputs = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs

    document = json.loads(stats_path.read_text(encoding='utf-8'))
    assert document['stats'][PAIR]['success'] == 8
    assert document['overrides'] == {
        f'{PROVIDER}::key-{index}': False for index in range(5)
    }


_RECORD_AND_EXIT_WORKER = r'''
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, sys.argv[1])
import lib.key_stats as ks
from lib.key_stats import _state

stats_path, ready_path, start_path, identity, successes, commit_seconds = sys.argv[2:]
ks._STATS_PATH = stats_path
if float(commit_seconds):
    # A slow volume: every commit holds the store's flock this much longer.
    real_write = _state.write_json_atomic

    def slow_write(*args, **kwargs):
        time.sleep(float(commit_seconds))
        return real_write(*args, **kwargs)

    _state.write_json_atomic = slow_write
Path(ready_path).touch()
deadline = time.monotonic() + 20
while not os.path.exists(start_path):
    if time.monotonic() > deadline:
        raise RuntimeError('timed out waiting for parent barrier')
    time.sleep(0.01)
for _ in range(int(successes)):
    ks.record_outcome('ipc-provider', 'ipc-key', success=True)
ks.record_rate_limit('ipc-provider', f'own-{identity}')
'''


@pytest.mark.parametrize('commit_seconds', [
    0.0,
    # Eight queued commits on a slow volume keep the store busy for six
    # seconds, longer than a fixed exit wait would allow the last process.
    pytest.param(0.75, marks=pytest.mark.slow),
])
def test_processes_that_record_and_exit_at_once_lose_no_counters(
        tmp_path, commit_seconds):
    """Every exiting process persists its counters, however long the queue.

    The processes record and exit together without flushing, so their exit
    commits queue on the store's flock behind each other, round after round.
    """
    pytest.importorskip('fcntl')

    stats_path = tmp_path / 'shared-key-stats.json'
    workers, successes = 8, 3
    for round_number in (1, 2, 3):
        round_path = tmp_path / f'round-{round_number}'
        round_path.mkdir()
        processes, start_path = _start_barrier_workers(
            round_path, _RECORD_AND_EXIT_WORKER,
            [(str(stats_path), str(index), str(successes), str(commit_seconds))
             for index in range(workers)])
        start_path.touch()

        outputs = [process.communicate(timeout=120) for process in processes]
        assert all(process.returncode == 0 for process in processes), outputs

        stats = _on_disk(stats_path)['stats']
        assert stats[PAIR]['success'] == round_number * workers * successes
        assert {index: stats.get(f'{PROVIDER}::own-{index}', {}).get('rate_limited')
                for index in range(workers)} == dict.fromkeys(
                    range(workers), round_number)


_COALESCING_WORKER = r'''
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, sys.argv[1])
import lib.key_stats as ks
from lib.key_stats import _state

stats_path, ready_path, start_path, successes, override_key = sys.argv[2:]
ks._STATS_PATH = stats_path
_state.KEY_STATS_FLUSH_INTERVAL_SECONDS = 3600.0
for _ in range(int(successes)):
    ks.record_outcome('ipc-provider', 'ipc-key', success=True)
ks.record_rate_limit('ipc-provider', 'ipc-key')
assert ks.get_today_stats('ipc-provider', 'ipc-key')['success'] >= int(successes)
Path(ready_path).touch()
deadline = time.monotonic() + 20
while not os.path.exists(start_path):
    if time.monotonic() > deadline:
        raise RuntimeError('timed out waiting for parent barrier')
    time.sleep(0.01)
if override_key:
    ks.set_key_override('ipc-provider', override_key, enabled=False)
assert ks.flush_key_stats() is True
'''


def test_coalesced_counters_from_concurrent_processes_merge(
        isolated_stats, tmp_path):
    """Batched counters from several processes add up; none overwrites."""
    pytest.importorskip('fcntl')
    ks, stats_path = isolated_stats
    from lib.json_store import write_json_atomic
    write_json_atomic(stats_path, {
        'day': ks._today(),
        'stats': {PAIR: {'success': 5}},
        'overrides': {f'{PROVIDER}::existing': True},
    })
    processes, start_path = _start_barrier_workers(
        tmp_path, _COALESCING_WORKER,
        [(str(stats_path), '25', 'key-a' if index < 2 else '')
         for index in range(4)])
    for _ in range(10):
        ks.record_outcome(PROVIDER, KEY, success=True)
    start_path.touch()
    outputs = [process.communicate(timeout=60) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs

    assert ks.flush_key_stats() is True
    document = _on_disk(stats_path)
    assert document['stats'][PAIR]['success'] == 5 + 4 * 25 + 10
    assert document['stats'][PAIR]['rate_limited'] == 4
    assert document['overrides'] == {
        f'{PROVIDER}::existing': True, f'{PROVIDER}::key-a': False}
    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 5 + 4 * 25 + 10


def test_cached_reader_observes_external_atomic_replacement(isolated_stats):
    ks, stats_path = isolated_stats
    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 0

    from lib.json_store import write_json_atomic
    write_json_atomic(stats_path, {
        'day': ks._today(),
        'stats': {PAIR: {'success': 7, 'failure': 0}},
        'overrides': {PAIR: False},
    })

    row = ks.get_today_stats(PROVIDER, KEY)
    assert row['success'] == 7
    assert row['override'] is False
    assert row['enabled'] is False


def test_malformed_store_is_preserved_and_memory_fallback_remains_usable(
        isolated_stats):
    ks, stats_path = isolated_stats
    original = b'{not valid json\n'
    stats_path.write_bytes(original)

    ks.record_outcome(PROVIDER, KEY, success=True)

    assert stats_path.read_bytes() == original
    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 1
    assert ks.flush_key_stats() is False
    assert stats_path.read_bytes() == original
    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 1


def test_invalid_nested_shape_is_not_silently_rewritten(isolated_stats):
    ks, stats_path = isolated_stats
    original = json.dumps({
        'day': ks._today(),
        'stats': {PAIR: ['not', 'an', 'entry']},
        'overrides': {},
    }).encode()
    stats_path.write_bytes(original)

    ks.set_key_override(PROVIDER, KEY, enabled=False)

    assert stats_path.read_bytes() == original
    assert ks.get_today_stats(PROVIDER, KEY)['override'] is False


def test_invalid_counter_is_preserved_and_does_not_crash_hot_path(
        isolated_stats):
    ks, stats_path = isolated_stats
    original = json.dumps({
        'day': ks._today(),
        'stats': {PAIR: {'success': {'not': 'a counter'}}},
        'overrides': {},
    }).encode()
    stats_path.write_bytes(original)

    ks.record_outcome(PROVIDER, KEY, success=True)

    assert stats_path.read_bytes() == original
    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 1


def test_repeated_write_failures_replay_all_increments_after_recovery(
        isolated_stats):
    ks, stats_path = isolated_stats
    # Materialise a valid baseline so the injected failure is specifically the
    # atomic replace, not first-file creation or parsing.
    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 0
    assert ks.flush_key_stats() is True

    with mock.patch('lib.json_store.os.replace',
                    side_effect=OSError('injected disk outage')):
        ks.record_outcome(PROVIDER, KEY, success=True)
        ks.record_outcome(PROVIDER, KEY, success=True)
        assert ks.flush_key_stats() is False
        assert ks.flush_key_stats() is False

    on_disk = json.loads(stats_path.read_text(encoding='utf-8'))
    assert (on_disk.get('stats') or {}).get(PAIR) is None
    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 2
    # Retries re-queue one coalesced batch instead of one per attempt.
    from lib.key_stats import _state
    assert len(_state._pending_mutations) == 1

    ks.record_outcome(PROVIDER, KEY, success=True)
    assert ks.flush_key_stats() is True

    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 3
    persisted = json.loads(stats_path.read_text(encoding='utf-8'))
    assert persisted['stats'][PAIR]['success'] == 3


_LOCK_HOLDER = r'''
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from lib.json_store import locked_path

stats_path, ready_path, release_path = sys.argv[2:]
with locked_path(stats_path):
    Path(ready_path).touch()
    deadline = time.monotonic() + 30
    while not Path(release_path).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
'''


def test_success_recording_never_waits_for_store_io(isolated_stats, tmp_path):
    """Another process mid-write (holding the store lock) cannot stall a success.

    Before coalescing, every success read, rewrote and fsynced the store under
    the process-wide ``_lock`` and the cross-process sidecar flock, so the
    request thread waited for whichever process was writing.
    """
    pytest.importorskip('fcntl')
    from lib.llm_dispatch.slot import Slot

    ks, stats_path = isolated_stats
    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 0
    ready_path = tmp_path / 'lock-held'
    release_path = tmp_path / 'lock-release'
    holder = subprocess.Popen(
        [sys.executable, '-c', _LOCK_HOLDER, ROOT, str(stats_path),
         str(ready_path), str(release_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    slot = Slot(key_name=KEY, api_key='synthetic-key', model='test-model',
                provider_id=PROVIDER, capabilities={'text'})
    finished = threading.Event()

    def succeed():
        for _ in range(20):
            slot.record_request()
            slot.record_success(5.0)
        finished.set()

    worker = threading.Thread(target=succeed, daemon=True)
    try:
        deadline = time.monotonic() + 30
        while not ready_path.exists():
            if holder.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f'lock holder failed: {holder.communicate(timeout=5)}')
            time.sleep(0.01)
        worker.start()
        assert finished.wait(2.0), (
            'Slot.record_success waited for key-stats disk I/O owned by '
            'another process')
        row = ks.get_today_stats(PROVIDER, KEY)
        assert row['success'] == 20
        assert ks.is_key_enabled(PROVIDER, KEY) is True
    finally:
        release_path.touch()
        holder.communicate(timeout=30)
        if worker.is_alive() or worker.ident is not None:
            worker.join(30)
    assert ks.flush_key_stats() is True
    assert _on_disk(stats_path)['stats'][PAIR]['success'] == 20


def test_outage_retries_keep_the_pending_queue_bounded(isolated_stats):
    """Telemetry recorded during each failed commit merges, in order."""
    import lib.json_store as json_store

    ks, stats_path = isolated_stats
    assert ks.flush_key_stats() is True
    from lib.key_stats import _state

    def failing_write(*_args, **_kwargs):
        # Simulates traffic that arrives while the commit is in flight.
        ks.record_outcome(PROVIDER, KEY, success=True)
        ks.record_rate_limit(PROVIDER, KEY)
        raise OSError('injected disk outage')

    ks.record_outcome(PROVIDER, KEY, success=True)
    real_write = json_store.write_text_atomic
    json_store.write_text_atomic = failing_write
    try:
        for _ in range(25):
            assert ks.flush_key_stats() is False
    finally:
        json_store.write_text_atomic = real_write
    assert len(_state._pending_mutations) == 1
    row = ks.get_today_stats(PROVIDER, KEY)
    assert (row['success'], row['rate_limited'], row['consecutive_429']) == (
        26, 25, 1)

    assert ks.flush_key_stats() is True
    persisted = _on_disk(stats_path)['stats'][PAIR]
    assert (persisted['success'], persisted['rate_limited'],
            persisted['consecutive_429']) == (26, 25, 1)


def test_commit_in_progress_never_blocks_recording_or_picks(
        isolated_stats, monkeypatch):
    """This process's own commit holds the store, never the hot-path lock."""
    import lib.json_store as json_store

    ks, stats_path = isolated_stats
    ks.record_outcome(PROVIDER, KEY, success=True)
    entered, release = threading.Event(), threading.Event()
    real_write = json_store.write_text_atomic

    def blocked_write(*args, **kwargs):
        entered.set()
        assert release.wait(30)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(json_store, 'write_text_atomic', blocked_write)
    flushed = []
    committer = threading.Thread(
        target=lambda: flushed.append(ks.flush_key_stats()), daemon=True)
    recorded = threading.Event()

    def record_and_pick():
        for _ in range(50):
            ks.record_outcome(PROVIDER, KEY, success=True)
            assert ks.is_key_enabled(PROVIDER, KEY) is True
        recorded.set()

    recorder = threading.Thread(target=record_and_pick, daemon=True)
    committer.start()
    try:
        assert entered.wait(10), 'the commit never reached the store write'
        assert ks._lock.acquire(timeout=1.0), (
            'a store commit held the in-memory lock across disk I/O')
        ks._lock.release()
        recorder.start()
        assert recorded.wait(5.0), (
            'recording or picking waited for an in-progress store commit')
        assert ks.get_today_stats(PROVIDER, KEY)['success'] == 51
    finally:
        release.set()
        committer.join(30)
        if recorder.ident is not None:
            recorder.join(30)
    assert flushed == [True]
    # The committed document replaces the view exactly once: counters that
    # arrived during the commit are replayed, the committed one is not.
    assert _on_disk(stats_path)['stats'][PAIR]['success'] == 1
    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 51
    assert ks.flush_key_stats() is True
    assert _on_disk(stats_path)['stats'][PAIR]['success'] == 51
    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 51


def test_store_commit_runs_no_import_or_config_read_under_the_flock(
        isolated_stats, monkeypatch):
    """The cross-process flock covers only the document transaction.

    Every process sharing the store queues on this flock, at exit all at
    once. The face-to-account fold map (a server-config read, a cold import
    on a first commit) is resolved before the lock; the fold still applies
    to the document inside it.
    """
    import builtins
    import contextlib

    import lib
    from lib.json_store import write_json_atomic
    from lib.key_stats import _state

    ks, stats_path = isolated_stats
    write_json_atomic(stats_path, {
        'day': ks._today(),
        'stats': {'gw_anthropic::gw_anthropic_key_0': {'success': 4}},
        'overrides': {},
    })
    holders = []
    real_locked_path = _state.locked_path

    @contextlib.contextmanager
    def observed_locked_path(path):
        with real_locked_path(path):
            holders.append(threading.get_ident())
            try:
                yield
            finally:
                holders.remove(threading.get_ident())

    def under_flock():
        return threading.get_ident() in holders

    imports_under_flock = []
    real_import = builtins.__import__

    def observed_import(name, globals=None, locals=None, fromlist=(), level=0):
        if not under_flock():
            return real_import(name, globals, locals, fromlist, level)
        importer = (globals or {}).get('__name__', '')
        known = len(sys.modules)
        module = real_import(name, globals, locals, fromlist, level)
        # Application imports and actual module loads; the standard
        # library's own `import io` inside os.fdopen is neither.
        if importer.startswith('lib') or len(sys.modules) != known:
            imports_under_flock.append((importer, name))
        return module

    config_reads = []

    def observed_config():
        config_reads.append('under the flock' if under_flock() else 'before it')
        return {'providers': [{
            'id': 'gw', 'base_url': 'https://gw.example.com/v1/openai',
            'api_keys': ['ak0'],
            'faces': {'anthropic': {
                'base_url': 'https://gw.example.com/v1/anthropic',
                'protocol': 'anthropic'}}}]}

    monkeypatch.setattr(_state, 'locked_path', observed_locked_path)
    monkeypatch.setattr(lib, '_load_server_config', observed_config)
    ks.record_outcome(PROVIDER, KEY, success=True)
    monkeypatch.setattr(builtins, '__import__', observed_import)
    committed = ks.flush_key_stats()
    monkeypatch.setattr(builtins, '__import__', real_import)

    assert committed is True
    assert imports_under_flock == []
    assert config_reads == ['before it']
    stats = _on_disk(stats_path)['stats']
    assert stats['gw::gw_key_0']['success'] == 4
    assert 'gw_anthropic::gw_anthropic_key_0' not in stats
    assert stats[PAIR]['success'] == 1


def test_coalesced_counters_survive_a_flush_and_a_restart(isolated_stats):
    ks, stats_path = isolated_stats
    for _ in range(3):
        ks.record_outcome(PROVIDER, KEY, success=True)
    ks.record_outcome(PROVIDER, KEY, success=False, error='upstream boom')
    ks.record_rate_limit(PROVIDER, KEY)
    ks.record_rate_limit(PROVIDER, KEY)
    ks.record_gateway_error(PROVIDER, KEY)
    ks.record_outcome(PROVIDER, KEY, success=False, error='key revoked',
                      credential_failure=True)
    ks.record_rate_limit(PROVIDER, KEY)
    expected = {
        'success': 3, 'failure': 2, 'credential_failures': 1,
        'rate_limited': 3, 'consecutive_429': 1, 'gateway_errors': 1,
    }
    assert not stats_path.exists(), 'recording must not write the store'
    before_flush = ks.get_today_stats(PROVIDER, KEY)

    assert ks.flush_key_stats() is True

    persisted = _on_disk(stats_path)['stats'][PAIR]
    assert {field: persisted[field] for field in expected} == expected
    assert persisted['last_error'] == 'key revoked'
    assert persisted['last_credential_error'] == 'key revoked'
    after_flush = ks.get_today_stats(PROVIDER, KEY)
    ks._cache.update(day='', stats={}, overrides={}, loaded=False)
    restarted = ks.get_today_stats(PROVIDER, KEY)
    for row in (before_flush, after_flush, restarted):
        assert {field: row[field] for field in expected} == expected
        assert row['last_error'] == 'key revoked'

    unchanged = stats_path.read_bytes()
    assert ks.flush_key_stats() is True
    assert stats_path.read_bytes() == unchanged


def test_manual_decisions_and_billing_stops_are_durable_before_returning(
        isolated_stats):
    ks, stats_path = isolated_stats
    for _ in range(3):
        ks.record_rate_limit(PROVIDER, KEY)

    ks.set_key_override(PROVIDER, KEY, False)
    document = _on_disk(stats_path)
    assert document['overrides'] == {PAIR: False}
    # Earlier telemetry commits first, in the same transaction.
    assert document['stats'][PAIR]['rate_limited'] == 3
    assert document['stats'][PAIR]['consecutive_429'] == 3

    ks.set_key_override(PROVIDER, KEY, True)
    ks.record_rate_limit(PROVIDER, KEY)
    document = _on_disk(stats_path)
    assert document['overrides'] == {PAIR: True}
    assert document['stats'][PAIR]['consecutive_429'] == 0

    ks.mark_key_exhausted(PROVIDER, KEY, reason='HTTP 402', model='m1')
    document = _on_disk(stats_path)
    assert document['stats'][PAIR]['exhausted_models'] == {'m1': 'HTTP 402'}
    # The re-enable reset the streak at its own position in the sequence.
    assert document['stats'][PAIR]['consecutive_429'] == 1
    assert document['stats'][PAIR]['rate_limited'] == 4

    ks.clear_key_override(PROVIDER, KEY)
    assert PAIR not in _on_disk(stats_path)['overrides']


def test_shutdown_joins_the_flush_worker_and_persists_pending(isolated_stats):
    from lib.key_stats import _state

    ks, stats_path = isolated_stats
    ks.record_outcome(PROVIDER, KEY, success=True)
    ks.record_outcome(PROVIDER, KEY, success=True)
    worker = _state._flusher
    assert worker is not None and worker.is_alive()
    assert not stats_path.exists()

    assert ks.shutdown_key_stats() is True

    assert not worker.is_alive()
    assert _state._flusher is None
    assert _state._pending_mutations == []
    assert _on_disk(stats_path)['stats'][PAIR]['success'] == 2


def test_shutdown_waits_out_an_in_flight_commit_and_persists_what_follows(
        isolated_stats, monkeypatch):
    """Exit never abandons a commit it can still complete.

    The worker's commit is held inside the store's flock, as a queue of
    other processes' commits would hold it, while the exiting thread's clock
    runs past any fixed wait. Shutdown must outlast the commit, then persist
    the counter recorded after that commit took its batch.
    """
    from lib.key_stats import _state

    ks, stats_path = isolated_stats
    in_commit, release = threading.Event(), threading.Event()
    real_write = _state.write_json_atomic

    def held_write(*args, **kwargs):
        in_commit.set()
        assert release.wait(30)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(_state, 'write_json_atomic', held_write)
    monkeypatch.setattr(_state, 'KEY_STATS_FLUSH_INTERVAL_SECONDS', 0.01)
    ks.record_outcome(PROVIDER, KEY, success=True)
    assert in_commit.wait(10), 'the flush worker never started its commit'
    ks.record_outcome(PROVIDER, KEY, success=True)
    results = []
    exiting = threading.Thread(
        target=lambda: results.append(ks.shutdown_key_stats()), daemon=True)
    real_monotonic = time.monotonic
    leaps = []

    def exiting_thread_clock():
        if threading.current_thread() is not exiting:
            return real_monotonic()
        leaps.append(3600.0)
        return real_monotonic() + sum(leaps)

    monkeypatch.setattr(time, 'monotonic', exiting_thread_clock)
    exiting.start()
    try:
        exiting.join(0.5)
        assert exiting.is_alive() and results == [], (
            'shutdown gave up on a commit that was still in flight')
    finally:
        release.set()
        exiting.join(30)
    assert results == [True]
    assert _state._pending_mutations == []
    assert _on_disk(stats_path)['stats'][PAIR]['success'] == 2


_EXITING_WORKER = r'''
import os
import sys

sys.path.insert(0, sys.argv[1])
import lib.key_stats as ks
from lib.key_stats import _state

ks._STATS_PATH = sys.argv[2]
_state.KEY_STATS_FLUSH_INTERVAL_SECONDS = 3600.0
for _ in range(3):
    ks.record_outcome('ipc-provider', 'ipc-key', success=True)
assert not os.path.exists(sys.argv[2])
'''


def test_process_exit_persists_unwritten_counters(tmp_path):
    stats_path = tmp_path / 'exit-key-stats.json'
    completed = subprocess.run(
        [sys.executable, '-c', _EXITING_WORKER, ROOT, str(stats_path)],
        capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert _on_disk(stats_path)['stats'][PAIR]['success'] == 3


def test_background_worker_flushes_within_its_interval_and_retires(
        isolated_stats, monkeypatch):
    from lib.key_stats import _state

    ks, stats_path = isolated_stats
    monkeypatch.setattr(_state, 'KEY_STATS_FLUSH_INTERVAL_SECONDS', 0.05)
    ks.record_outcome(PROVIDER, KEY, success=True)
    worker = _state._flusher
    assert worker is not None

    deadline = time.monotonic() + 10
    while True:
        if stats_path.exists() and (_on_disk(stats_path).get('stats') or {}).get(
                PAIR, {}).get('success') == 1:
            break
        assert time.monotonic() < deadline, 'background flush never ran'
        time.sleep(0.02)
    worker.join(10)
    assert not worker.is_alive(), 'an idle flush worker must retire'
    assert _state._flusher is None


def test_unwritten_telemetry_of_a_past_day_never_counts_toward_the_next(
        isolated_stats, monkeypatch):
    ks, stats_path = isolated_stats
    monkeypatch.setattr(ks, '_today', lambda: '2099-03-01')
    ks.record_outcome(PROVIDER, KEY, success=True)
    ks.set_key_override(PROVIDER, KEY, False)
    ks.record_outcome(PROVIDER, KEY, success=True)
    assert _on_disk(stats_path)['stats'][PAIR]['success'] == 1

    monkeypatch.setattr(ks, '_today', lambda: '2099-03-02')
    assert ks.flush_key_stats() is True

    document = _on_disk(stats_path)
    assert document['day'] == '2099-03-02'
    assert document['stats'] == {}
    assert document['overrides'] == {PAIR: False}
    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 0


def test_reads_never_write_the_store(isolated_stats):
    ks, stats_path = isolated_stats
    stale = json.dumps({
        'day': '2000-01-01',
        'stats': {PAIR: {'success': 9}},
        'overrides': {PAIR: True},
    }).encode()
    stats_path.write_bytes(stale)

    row = ks.get_today_stats(PROVIDER, KEY)
    assert row['success'] == 0
    assert row['override'] is True
    assert ks.is_key_enabled(PROVIDER, KEY) is True
    assert PAIR in ks.get_all_stats()['keys']
    assert stats_path.read_bytes() == stale

    # The rollover is persisted by the next flush instead.
    assert ks.flush_key_stats() is True
    document = _on_disk(stats_path)
    assert document['day'] == ks._today()
    assert document['stats'] == {}
    assert document['overrides'] == {PAIR: True}


_FORKING_WORKER = r'''
import os
import sys
import warnings

sys.path.insert(0, sys.argv[1])
import lib.key_stats as ks
from lib.key_stats import _state

warnings.simplefilter('ignore', DeprecationWarning)
ks._STATS_PATH = sys.argv[2]
_state.KEY_STATS_FLUSH_INTERVAL_SECONDS = 3600.0
ks.record_outcome('ipc-provider', 'ipc-key', success=True)
ks.record_outcome('ipc-provider', 'ipc-key', success=True)
pid = os.fork()
if pid == 0:
    committed = False
    try:
        ks.record_outcome('ipc-provider', 'ipc-key', success=True)
        committed = ks.flush_key_stats(timeout=10)
    finally:
        os._exit(0 if committed else 1)
_, status = os.waitpid(pid, 0)
assert os.waitstatus_to_exitcode(status) == 0
assert ks.flush_key_stats() is True
'''


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='requires os.fork')
def test_forked_child_never_replays_its_parents_unwritten_counters(tmp_path):
    stats_path = tmp_path / 'fork-key-stats.json'
    completed = subprocess.run(
        [sys.executable, '-c', _FORKING_WORKER, ROOT, str(stats_path)],
        capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert _on_disk(stats_path)['stats'][PAIR]['success'] == 3
