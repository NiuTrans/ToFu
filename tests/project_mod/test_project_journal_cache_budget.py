"""Reconstructible history is byte-bounded; durable journals are never trimmed."""
from collections import OrderedDict
import json
import os
import threading
import tracemalloc

import pytest

from lib.project_mod import modifications as mods
from lib.project_mod.session_lock import session_state

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(mods, '_mods_cache', OrderedDict())
    monkeypatch.setattr(mods, '_mods_cache_identity', {})
    monkeypatch.setattr(mods, '_mods_cache_bytes', {})
    monkeypatch.setattr(mods, '_state', {'sessionId': None, 'modifications': []})
    monkeypatch.setattr(mods, '_MODS_CACHE_CAPACITY', 16)
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_BYTES', 16 * 1024)
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_SESSION_BYTES', 8 * 1024)


def row(path, content=''):
    return {'type': 'write_file', 'path': path, 'originalContent': content,
            'existed': True}


def journal(directory, rows):
    directory.mkdir()
    path = directory / 'modifications.json'
    path.write_text(json.dumps({'modifications': rows}))
    return str(directory), path


def retained_size(value, seen=None):
    """Independent oracle: every distinct object reachable from ``value`` once."""
    import sys
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(retained_size(key, seen) + retained_size(child, seen)
                    for key, child in value.items())
    elif isinstance(value, list):
        size += sum(retained_size(child, seen) for child in value)
    return size


@pytest.mark.parametrize('budget_ratio, admitted', [(1.25, True), (0.9, False)])
def test_budget_admits_a_history_by_its_retained_size(tmp_path, monkeypatch, budget_ratio, admitted):
    # Decoded index rows share their key strings; the budget bounds memory, so
    # repeated keys must not be charged as if every row owned a copy.
    rows = [{'type': 'write_file', 'path': f'src/module_{index}.py', 'basePath': '/project',
             'convId': f'conv-{index % 5}', 'taskId': f'task-{index % 11}',
             'changeId': f'{index:032x}', 'existed': True, 'timestamp': 1.7e9 + index}
            for index in range(400)]
    session, _ = journal(tmp_path / 'shared-keys', rows)
    budget = int(retained_size(mods._load_from_disk(session)) * budget_ratio)
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_BYTES', budget)
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_SESSION_BYTES', budget)
    assert mods._cache_get(session) == rows
    assert (session in mods._mods_cache) is admitted
    if admitted:
        assert mods._mods_cache_bytes[session] <= budget


def test_byte_budget_evicts_lru_and_reconstructs_without_touching_disk(tmp_path, monkeypatch):
    sessions = [journal(tmp_path / name, [row(name, name * 1000)])
                for name in ('a', 'b', 'c')]
    weights = [mods._mods_cache_weight(session, mods._load_from_disk(session),
                                       mods._journal_identity(session))
               for session, _ in sessions]
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_BYTES', max(weights) * 2)
    original = [path.read_bytes() for _, path in sessions]
    first, second, third = [session for session, _ in sessions]
    first_rows = mods._cache_get(first)
    mods._cache_get(second)
    assert mods._cache_get(first) is first_rows  # Refresh LRU, not journal data.
    mods._cache_get(third)
    assert list(mods._mods_cache) == [first, third]
    assert sum(mods._mods_cache_bytes.values()) <= mods._MODS_CACHE_MAX_BYTES
    assert set(mods._mods_cache_identity) == set(mods._mods_cache_bytes) == set(mods._mods_cache)
    assert mods._cache_get(second) == [row('b', 'b' * 1000)]
    assert [path.read_bytes() for _, path in sessions] == original


def test_oversize_session_is_readable_but_not_retained_by_active_view(tmp_path):
    session, path = journal(tmp_path / 'large', [row('large', 'x' * (32 * 1024))])
    original = path.read_bytes()
    mods._state.update(sessionId=session, modifications=[row('stale')])
    first = mods._cache_get(session)
    second = mods._cache_get(session)
    assert first == second == json.loads(original)['modifications']
    assert first is not second  # No unbounded warm resident snapshot.
    with mods._lock:
        mods._sync_primary_view(session, first)
    assert mods._state['sessionId'] == session
    assert mods._state['modifications'] == []
    assert not mods._mods_cache
    assert not mods._mods_cache_identity
    assert not mods._mods_cache_bytes
    assert path.read_bytes() == original


def test_oversized_history_is_weighed_once_until_its_index_is_rewritten(tmp_path, monkeypatch):
    session, path = journal(tmp_path / 'grows', [row('large', 'x' * (16 * 1024))])
    log = path.parent / 'modifications.journal'
    walks = []
    weigh = mods._mods_cache_weight
    monkeypatch.setattr(mods, '_mods_cache_weight',
                        lambda *args: (walks.append(args[0]), weigh(*args))[1])
    assert [r['path'] for r in mods._cache_get(session)] == ['large']
    assert walks == [session] and not mods._mods_cache
    # Appends only add rows, so a longer log on the same index stays oversized.
    for name in ('first', 'second'):
        with open(log, 'a') as handle:
            handle.write(json.dumps(row(name)) + '\n')
        assert mods._cache_get(session)[-1]['path'] == name
    assert walks == [session] and not mods._mods_cache
    mods._atomic_json_write(str(path), {'modifications': [row('small')]})
    assert [r['path'] for r in mods._cache_get(session)] == ['small', 'first', 'second']
    assert walks == [session, session] and session in mods._mods_cache


def test_growing_payloads_keep_bounded_metadata_cache_and_exact_durable_content(tmp_path):
    session, path = journal(tmp_path / 'growing', [row('small')])
    mods._state['sessionId'] = session
    assert mods._cache_get(session) == [row('small')]
    assert mods._state['modifications'] == [row('small')]
    large = row('large', '\U0001f642' * (32 * 1024))
    mods._locked_rmw(session, lambda rows: rows.append(large))
    from lib.project_mod.undo_payloads import hydrate_record
    stored = json.loads(path.read_text())['modifications']
    assert [hydrate_record(session, item) for item in stored] == [row('small'), large]
    assert mods._cache_get(session) == stored
    assert sum(mods._mods_cache_bytes.values()) <= mods._MODS_CACHE_MAX_BYTES
    assert session in mods._mods_cache
    assert mods._state['modifications']


def test_eviction_and_invalidation_release_active_projection(tmp_path, monkeypatch):
    monkeypatch.setattr(mods, '_MODS_CACHE_CAPACITY', 1)
    first, _ = journal(tmp_path / 'first', [row('first')])
    second, _ = journal(tmp_path / 'second', [row('second')])
    mods._state['sessionId'] = first
    mods._cache_get(first)
    assert mods._state['modifications']
    mods._cache_get(second)
    assert mods._state['modifications'] == []
    mods._state['sessionId'] = second
    rows = mods._cache_get(second)
    with mods._lock:
        mods._sync_primary_view(second, rows)
    mods._invalidate_cache(second)
    assert mods._state['modifications'] == []
    assert not mods._mods_cache_bytes


def test_replacement_identity_and_invalid_load_do_not_reuse_stale_projection(tmp_path):
    session, path = journal(tmp_path / 'identity', [row('first')])
    mods._state['sessionId'] = session
    first = mods._cache_get(session)
    assert mods._cache_get(session) is first
    replacement = path.with_suffix('.replacement')
    replacement.write_text(json.dumps({'modifications': [row('other')]}))
    os.replace(replacement, path)
    assert mods._cache_get(session) == [row('other')]
    replacement.write_text('{corrupt')
    os.replace(replacement, path)
    with pytest.raises(ValueError):
        mods._cache_get(session)
    assert path.read_text() == '{corrupt'
    assert not mods._mods_cache
    assert not mods._mods_cache_bytes
    assert mods._state['modifications'] == []


def test_cache_weight_and_filesystem_work_hold_session_not_global_lock(tmp_path, monkeypatch):
    session, path = journal(tmp_path / 'locks', [row('first')])

    class MemoryLock:
        def __init__(self):
            self.lock = threading.RLock()
            self.depth = 0

        def __enter__(self):
            self.lock.acquire()
            self.depth += 1

        def __exit__(self, *_args):
            self.depth -= 1
            self.lock.release()

    lock = MemoryLock()
    monkeypatch.setattr(mods, '_lock', lock)
    seen = set()

    def checked(name, function):
        def run(*args, **kwargs):
            assert lock.depth == 0
            assert session_state(session) is not None
            seen.add(name)
            return function(*args, **kwargs)
        return run

    for name in ('_journal_identity', '_clean_stale_tmp', '_load_from_disk',
                 '_mods_cache_weight', '_atomic_json_write'):
        monkeypatch.setattr(mods, name, checked(name, getattr(mods, name)))
    assert mods._cache_get(session) == [row('first')]
    mods._locked_rmw(session, lambda rows: rows.append(row('second')))
    assert len(json.loads(path.read_text())['modifications']) == 2
    assert seen == {'_journal_identity', '_clean_stale_tmp', '_load_from_disk',
                    '_mods_cache_weight', '_atomic_json_write'}


def test_compact_streaming_write_has_bounded_batches_and_allocation(tmp_path, monkeypatch):
    payload = {'modifications': [row(str(i), ('\u4e2d\\"\n' * 256)) for i in range(1800)]}
    expected = json.dumps(payload, separators=(',', ':')).encode('ascii')
    path = tmp_path / 'modifications.json'
    sizes = []
    fdopen = mods.os.fdopen

    class ObservedWriter:
        def __init__(self, *args, **kwargs):
            self.handle = fdopen(*args, **kwargs)

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def write(self, text):
            sizes.append(len(text))
            return self.handle.write(text)

        def flush(self):
            return self.handle.flush()

        def fileno(self):
            return self.handle.fileno()

    def no_full_serialization(*_args, **_kwargs):
        raise AssertionError('whole-document dumps is not a streaming writer')

    monkeypatch.setattr(mods.os, 'fdopen', ObservedWriter)
    monkeypatch.setattr(mods.json, 'dumps', no_full_serialization)
    tracemalloc.start()
    try:
        mods._atomic_json_write(str(path), payload)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert path.read_bytes() == expected
    assert max(sizes) <= mods._JSON_WRITE_BUFFER_BYTES
    assert len(sizes) == (len(expected) + mods._JSON_WRITE_BUFFER_BYTES - 1) // mods._JSON_WRITE_BUFFER_BYTES
    assert peak < 1024 * 1024
    assert not list(tmp_path.glob('.modifications_*.tmp'))
    print(f'streaming journal: bytes={len(expected)} peak={peak} writes={len(sizes)}')


@pytest.mark.parametrize('failure', ['serialize', 'fsync', 'replace'])
def test_stream_failure_preserves_old_journal_and_cleans_temporary(tmp_path, monkeypatch, failure):
    path = tmp_path / 'modifications.json'
    original = b'{"modifications":[]}'
    path.write_bytes(original)
    payload = {'modifications': [row('new', 'x' * (256 * 1024))]}
    if failure == 'serialize':
        payload['unsupported'] = object()
        expected_error = TypeError
    else:
        def fail(*_args, **_kwargs):
            raise OSError(f'injected {failure} failure')
        monkeypatch.setattr(mods.os, failure, fail)
        expected_error = OSError
    with pytest.raises(expected_error):
        mods._atomic_json_write(str(path), payload)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob('.modifications_*.tmp'))
