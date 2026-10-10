"""Journal admission isolates optional work and bounds history I/O per operation."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading

import pytest

from lib import file_history
from lib.project_mod import modifications as mods, session_lock as locks
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    base.mkdir()
    for name in ('first.txt', 'second.txt'):
        (base / name).write_bytes(b'old\r\n')
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: None)
    monkeypatch.setattr(mods, '_live_change_listeners', [])
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution', lambda target, base, path, **kw: (base, path))
    monkeypatch.setattr(_ops, '_resolve_write_path', lambda base, path, **kw: os.path.join(base, path))
    monkeypatch.setattr(_ops, '_touch_for_vscode', lambda *a: None)
    monkeypatch.setattr(_ops, '_record_write_freshness', lambda *a, **kw: None)
    return str(base)


def write(base, name='first.txt', content='new\n'):
    return _ops.tool_write_file(base, name, content, conv_id='conv', task_id=name)


def test_slow_listener_does_not_block_other_write_or_history(project, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    session = mods._get_session_dir(project)

    def slow(path):
        if path == 'first.txt':
            assert locks.session_state(session) is None
            entered.set()
            assert release.wait(10)

    monkeypatch.setattr(mods, '_live_change_listeners', [lambda task, entry: slow(entry['path'])])
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0.05)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, project)
        try:
            assert entered.wait(10)
            assert [row['path'] for row in mods.get_modifications(project)] == ['first.txt']
            second = pool.submit(write, project, 'second.txt')
            assert second.result(timeout=5)['ok'] is True
        finally:
            release.set()
        assert first.result(timeout=5)['ok'] is True
    assert len(mods.get_modifications(project)) == 2
    assert mods.undo_task_modifications(project, 'first.txt')['undone'] == 1
    assert Path(project, 'first.txt').read_bytes() == b'old\r\n'


def test_effects_run_once_after_outer_release_and_can_reenter(tmp_path):
    calls = []
    session = str(tmp_path)

    def effect():
        assert locks.session_state(session) is None
        with locks.locked_session(session):
            calls.append('reentered')

    with pytest.raises(RuntimeError, match='body failed'):
        with locks.locked_session(session):
            with locks.locked_session(session):
                locks.after_session_release(session, effect)
            assert calls == []
            raise RuntimeError('body failed')
    assert calls == ['reentered']
    assert not locks._active_descriptors


def test_first_write_loads_once_and_unchanged_reads_stay_cold(project, monkeypatch):
    loads = []
    load = mods._load_from_disk
    monkeypatch.setattr(mods, '_load_from_disk', lambda session: (loads.append(session), load(session))[1])
    assert write(project)['ok'] is True
    assert len(loads) == 1
    for _ in range(4):
        assert len(mods.get_modifications(project)) == 1
    assert len(loads) == 1


def test_repeated_write_reuses_identity_validated_cache_without_reload(project, monkeypatch):
    """A writer's own committed journal is reused under identity, so the
    critical section does not re-read the full history on every write."""
    loads = []
    load = mods._load_from_disk
    monkeypatch.setattr(mods, '_load_from_disk', lambda session: (loads.append(session), load(session))[1])
    assert write(project, 'a.txt')['ok'] is True
    assert len(loads) == 1
    assert write(project, 'b.txt')['ok'] is True
    assert write(project, 'c.txt')['ok'] is True
    assert len(loads) == 1  # subsequent writes reused the validated cache
    assert len(mods.get_modifications(project)) == 3


def test_rmw_reloads_after_external_replacement_and_rejects_corruption(project, monkeypatch):
    """A cross-process/external commit changes the journal identity, so the
    next write reloads from disk; a torn journal fails loudly, never reused."""
    session = mods._get_session_dir(project)
    assert write(project, 'a.txt')['ok'] is True
    assert len(mods.get_modifications(project)) == 1
    journal = Path(session, 'modifications.json')
    rows = json.loads(journal.read_text())['modifications']
    # External writer appends via atomic replacement (identity changes).
    replacement = journal.with_suffix('.replacement')
    replacement.write_text(json.dumps({'modifications': rows + [{**rows[0], 'path': 'external.txt', 'changeId': 'f' * 32}]}))
    os.replace(replacement, journal)
    assert write(project, 'b.txt')['ok'] is True
    paths = {row['path'] for row in mods.get_modifications(project)}
    assert paths == {'a.txt', 'external.txt', 'b.txt'}
    # Corrupt journal: write surfaces the failure, never silently resets history.
    replacement.write_text('{corrupt')
    os.replace(replacement, journal)
    assert write(project, 'c.txt')['ok'] is False
    assert journal.read_text() == '{corrupt'


def test_cached_history_reloads_external_replacement_and_rejects_corruption(project):
    assert write(project)['ok'] is True
    assert len(mods.get_modifications(project)) == 1
    journal = Path(mods._get_session_dir(project), 'modifications.json')
    rows = json.loads(journal.read_text())['modifications']
    replacement = journal.with_suffix('.replacement')
    replacement.write_text(json.dumps({'modifications': rows + [{**rows[0], 'path': 'external.txt'}]}))
    os.replace(replacement, journal)
    assert len(mods.get_modifications(project)) == 2
    replacement.write_text('{corrupt')
    os.replace(replacement, journal)
    with pytest.raises(ValueError):
        mods.get_modifications(project)
    assert journal.read_text() == '{corrupt'


@pytest.mark.parametrize('action', ['write', 'upload', 'diff', 'insert', 'diffs', 'inserts', 'edit'])
def test_busy_is_typed_retryable_and_never_mutates(project, monkeypatch, action):
    session = mods._get_session_dir(project)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0)

    def attempt():
        if action == 'write':
            return write(project)
        if action == 'upload':
            return _ops.save_uploaded_file(project, 'first.txt', b'new', on_conflict='overwrite')
        if action == 'diff':
            return _ops.tool_apply_diff(project, 'first.txt', 'old', 'new')
        if action == 'insert':
            return _ops.tool_insert_content(project, 'first.txt', 'old', 'new')
        if action == 'diffs':
            return _ops.tool_apply_diffs(project, [{'path': 'first.txt', 'search': 'old', 'replace': 'new'}])
        if action == 'inserts':
            return _ops.tool_insert_contents(project, [{'path': 'first.txt', 'anchor': 'old', 'content': 'new'}])
        return _ops.tool_edit_file(project, [{'path': 'first.txt', 'operation': 'replace', 'anchor': 'old', 'content': 'new'}])

    with locks.locked_session(session), ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(attempt).result(timeout=5)
    if isinstance(result, str):
        assert '[journal-busy]' in result
        assert 'no file mutation was performed' in result
        assert 're-read' in result
    else:
        assert result['ok'] is False
        assert result['errorKind'] == 'journal-busy'
        assert result['mutationApplied'] is False
        assert result['retryable'] is True
        assert result['waitedSeconds'] >= 0
        assert 're-read' in result['error']
    assert Path(project, 'first.txt').read_bytes() == b'old\r\n'
    assert mods.get_modifications(project) == []
    assert not list(Path(project).glob('.tofu_atomic_*'))


def test_cross_project_disk_write_does_not_hold_global_cache_lock(project, tmp_path, monkeypatch):
    other = tmp_path / 'other'
    other.mkdir()
    entered, release = threading.Event(), threading.Event()
    first_session = mods._get_session_dir(project)
    flush = mods._flush_to_disk

    def slow(session, rows):
        if session == first_session:
            entered.set()
            assert release.wait(10)
        return flush(session, rows)

    monkeypatch.setattr(mods, '_flush_to_disk', slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(mods._record_modification, project, 'write_file', 'first')
        try:
            assert entered.wait(10)
            second = pool.submit(mods._record_modification, str(other), 'write_file', 'other')
            assert second.result(timeout=5) is True
        finally:
            release.set()
        assert first.result(timeout=5) is True


def test_parallel_distinct_file_writes_keep_all_undo_records(project):
    names = [f'parallel-{index}.txt' for index in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda name: write(project, name), names))
    assert all(outcome['ok'] for outcome in outcomes)
    assert {row['path'] for row in mods.get_modifications(project)} == set(names)
    for name in names:
        assert mods.undo_task_modifications(project, name)['undone'] == 1
        assert not Path(project, name).exists()


def test_stale_computed_write_is_rejected_after_another_commit(project):
    assert write(project)['ok'] is True
    with pytest.raises(OSError, match='Original file changed'):
        _ops._write_with_undo(str(Path(project, 'first.txt')), 'stale result', project,
                              'first.txt', 'old\r\n', tracked=True,
                              attribution=(project, 'first.txt'))
    assert Path(project, 'first.txt').read_bytes() == b'new\n'
    assert len(mods.get_modifications(project)) == 1


def test_cross_project_cold_read_does_not_hold_global_cache_lock(project, tmp_path, monkeypatch):
    other = tmp_path / 'other-read'
    other.mkdir()
    first_session = mods._get_session_dir(project)
    mods._invalidate_cache(first_session)
    entered, release = threading.Event(), threading.Event()
    load = mods._load_from_disk

    def slow(session):
        if session == first_session:
            entered.set()
            assert release.wait(10)
        return load(session)

    monkeypatch.setattr(mods, '_load_from_disk', slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(mods.get_modifications, project)
        try:
            assert entered.wait(10)
            second = pool.submit(mods._record_modification, str(other), 'write_file', 'other')
            assert second.result(timeout=5) is True
        finally:
            release.set()
        assert first.result(timeout=5) == []


def test_cold_cache_reader_never_holds_cache_lock_while_waiting_for_admission(project, monkeypatch):
    session = mods._get_session_dir(project)
    attempting, release = threading.Event(), threading.Event()
    from contextlib import contextmanager
    locked = mods.locked_session

    @contextmanager
    def observed(directory):
        if threading.current_thread() is not threading.main_thread():
            attempting.set()
            assert release.wait(10)
        with locked(directory) as state:
            yield state

    monkeypatch.setattr(mods, 'locked_session', observed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with locks.locked_session(session):
            reader = pool.submit(mods._cache_get, session)
            try:
                assert attempting.wait(10)
                acquired = mods._lock.acquire(timeout=1)
                assert acquired, 'reader held global cache lock before session admission'
                mods._lock.release()
            finally:
                release.set()
        assert reader.result(timeout=5) == []


def test_transient_contention_waits_then_commits_without_replaying(project, monkeypatch):
    import fcntl
    session = mods._get_session_dir(project)
    attempted = threading.Event()
    flock = fcntl.flock

    def observe(fd, operation):
        try:
            return flock(fd, operation)
        except BlockingIOError:
            attempted.set()
            raise

    monkeypatch.setattr(fcntl, 'flock', observe)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with locks.locked_session(session):
            writing = pool.submit(write, project)
            assert attempted.wait(10)
            assert not writing.done()
        assert writing.result(timeout=5)['ok'] is True
    assert Path(project, 'first.txt').read_bytes() == b'new\n'
    assert len(mods.get_modifications(project)) == 1
