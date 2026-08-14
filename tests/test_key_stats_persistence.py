#!/usr/bin/env python3
"""Durability and cross-process coherence guards for ``lib.key_stats``."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

PROVIDER = 'ipc-provider'
KEY = 'ipc-key'
PAIR = f'{PROVIDER}::{KEY}'


@pytest.fixture
def isolated_stats(monkeypatch, tmp_path):
    import lib.key_stats as ks
    from lib.key_stats import _state

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
    ks._cache.update(snapshot)
    _state._pending_mutations[:] = pending_snapshot


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


def test_cross_process_counters_and_overrides_do_not_lose_updates(tmp_path):
    """Every process mutates the latest locked document, never its snapshot."""
    pytest.importorskip('fcntl')

    stats_path = tmp_path / 'shared-key-stats.json'
    start_path = tmp_path / 'start'
    specs = ([('record', str(index)) for index in range(8)]
             + [('override', f'key-{index}') for index in range(5)])
    processes = []
    ready_paths = []
    for index, (operation, identity) in enumerate(specs):
        ready_path = tmp_path / f'ready-{index}'
        ready_paths.append(ready_path)
        processes.append(subprocess.Popen(
            [sys.executable, '-c', _WORKER, ROOT, str(stats_path),
             str(ready_path), str(start_path), operation, identity],
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
    start_path.touch()

    outputs = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs

    document = json.loads(stats_path.read_text(encoding='utf-8'))
    assert document['stats'][PAIR]['success'] == 8
    assert document['overrides'] == {
        f'{PROVIDER}::key-{index}': False for index in range(5)
    }


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

    with mock.patch('lib.json_store.os.replace',
                    side_effect=OSError('injected disk outage')):
        ks.record_outcome(PROVIDER, KEY, success=True)
        ks.record_outcome(PROVIDER, KEY, success=True)

    on_disk = json.loads(stats_path.read_text(encoding='utf-8'))
    assert (on_disk.get('stats') or {}).get(PAIR) is None
    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 2

    ks.record_outcome(PROVIDER, KEY, success=True)

    assert ks.get_today_stats(PROVIDER, KEY)['success'] == 3
    persisted = json.loads(stats_path.read_text(encoding='utf-8'))
    assert persisted['stats'][PAIR]['success'] == 3
