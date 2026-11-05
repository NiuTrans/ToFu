"""Read-only history projections never take the project journal admission.

The admission orders the writers of one project's undo journal. A metadata
read (run cards, settlement, completion diagnostics) must neither wait for a
writer nor make writers wait while it parses a large history, and it must
still return one consistent history when a writer appends or folds meanwhile.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import threading

import pytest

from lib import file_history
from lib.project_mod import modifications as mods, session_lock as locks

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    base.mkdir()
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: None)
    monkeypatch.setattr(mods, '_live_change_listeners', [])
    return str(base)


def record(base, path):
    assert mods._record_modification(base, 'write_file', path,
                                     conv_id='conv', task_id='task') is True


def history(base):
    return [row['path'] for row in mods.get_modifications(base, include_content=False)]


def test_history_reads_never_wait_for_a_held_journal_admission(project, monkeypatch):
    record(project, 'first.txt')
    record(project, 'second.txt')
    session = mods._get_session_dir(project)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0.05)
    held, release = threading.Event(), threading.Event()

    def writer():
        with locks.locked_session(session):
            held.set()
            assert release.wait(10)

    def reads():
        return (history(project),
                [row['path'] for row in mods.get_task_modification_paths(
                    project, 'task', conv_id='conv', limit=10)],
                mods.get_conv_ids_with_modifications(project))

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(writer)
        try:
            assert held.wait(10)
            mods._invalidate_cache(session)
            cold = reads()
            warm = reads()
        finally:
            release.set()
        holder.result(timeout=5)
    expected = (['first.txt', 'second.txt'], ['first.txt', 'second.txt'], ['conv'])
    assert cold == warm == expected


def test_read_overlapping_a_fold_returns_one_consistent_history(project, monkeypatch):
    for name in ('a', 'b', 'c'):
        record(project, name)
    session = mods._get_session_dir(project)
    # The first record folded into the index; the later ones are log appends.
    assert os.path.getsize(os.path.join(session, 'modifications.journal')) > 0
    mods._invalidate_cache(session)
    read_tail = mods._read_tail
    folded = []

    def fold_before_the_first_log_read(session_dir, name, **kwargs):
        if name == 'modifications.journal' and not folded:
            folded.append(name)
            # Another writer folds the log into the index between this
            # reader's index read and its log read.
            mods._locked_rmw(session_dir, lambda rows: None)
        return read_tail(session_dir, name, **kwargs)

    monkeypatch.setattr(mods, '_read_tail', fold_before_the_first_log_read)
    assert history(project) == ['a', 'b', 'c']
    assert folded


def test_reader_never_publishes_its_snapshot_over_a_newer_commit(project, monkeypatch):
    record(project, 'a')
    record(project, 'b')
    session = mods._get_session_dir(project)
    mods._invalidate_cache(session)
    weigh = mods._mods_cache_weight
    committed = []

    def commit_while_the_reader_publishes(session_dir, rows, identity):
        if not committed:
            committed.append(True)
            record(project, 'c')
        return weigh(session_dir, rows, identity)

    monkeypatch.setattr(mods, '_mods_cache_weight', commit_while_the_reader_publishes)
    assert history(project) == ['a', 'b']
    assert committed
    assert mods._mods_cache_identity.get(session) in (None, mods._journal_identity(session))
    assert history(project) == ['a', 'b', 'c']


def test_cold_read_warms_the_cache_for_the_next_read(project, monkeypatch):
    record(project, 'a')
    record(project, 'b')
    session = mods._get_session_dir(project)
    mods._invalidate_cache(session)
    assert history(project) == ['a', 'b']
    for name in ('_read_history', '_load_from_disk'):
        monkeypatch.setattr(mods, name, lambda *a, **kw: pytest.fail('warm history re-read'))
    assert history(project) == ['a', 'b']


def test_reads_after_unextended_appends_parse_only_the_appended_lines(project, monkeypatch):
    import json
    from pathlib import Path
    from uuid import uuid4
    for name in ('a', 'b'):
        record(project, name)
    session = mods._get_session_dir(project)
    assert history(project) == ['a', 'b'] and session in mods._mods_cache
    log = Path(session) / 'modifications.journal'
    template = json.loads(log.read_bytes().splitlines()[-1])

    def append(data):
        # Another process's commit, or an in-process commit whose cache
        # extension has not landed yet: the log grows, the cache entry does not.
        with open(log, 'ab') as handle:
            handle.write(data)

    full_reads = []
    read_history = mods._read_history
    monkeypatch.setattr(mods, '_read_history',
                        lambda *a, **kw: (full_reads.append(a[0]), read_history(*a, **kw))[1])
    append(json.dumps({**template, 'path': 'c', 'changeId': uuid4().hex}).encode() + b'\n')
    assert history(project) == ['a', 'b', 'c']
    append(b'{"type": "write_file", "pa')  # A torn append is never a committed record.
    assert history(project) == ['a', 'b', 'c']
    assert full_reads == []
    assert history(project) == [row['path'] for row in read_history(session)[1]]
    append(b'th": "torn"}\n{"bad json"\n')
    with pytest.raises(ValueError):
        history(project)


def test_oversized_history_serves_conversations_without_reparsing_it(project, monkeypatch):
    def write(index, conv):
        assert mods._record_modification(project, 'write_file', f'file-{index}.txt', conv_id=conv,
                                         task_id=f'task-{index % 4}') is True

    for index in range(24):
        write(index, f'conv-{index % 3}')
    session = mods._get_session_dir(project)
    mods._locked_rmw(session, lambda rows: None)  # Fold: these rows now live in the index.
    for index in range(24, 30):
        write(index, f'conv-{index % 3}')
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_BYTES', 1)
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_SESSION_BYTES', 1)
    mods._invalidate_cache(session)
    read_history = mods._read_history

    def expected(conv):
        return [row for row in read_history(session)[1] if row.get('convId') == conv]

    def read(conv):
        return mods.get_modifications(project, conv_id=conv, include_content=False)

    assert read('conv-0') == expected('conv-0')  # Learns that the history exceeds the budget.
    full_reads = []
    monkeypatch.setattr(mods, '_read_history',
                        lambda *a, **kw: (full_reads.append(a[0]), read_history(*a, **kw))[1])
    for conv in ('conv-0', 'conv-1', 'conv-2', 'unknown'):
        assert read(conv) == expected(conv)
    for index in range(30, 36):
        write(index, 'conv-1')
        assert read('conv-1') == expected('conv-1')
    assert mods.get_task_modification_paths(project, 'task-2', conv_id='conv-1', limit=100) == [
        {'path': row['path'], 'rootPath': row['basePath']}
        for row in expected('conv-1') if row['taskId'] == 'task-2']
    assert full_reads == []
    mods._locked_rmw(session, lambda rows: None)  # A replaced index is located once more.
    write(36, 'conv-2')
    assert read('conv-2') == expected('conv-2')
    assert read('conv-0') == expected('conv-0')
