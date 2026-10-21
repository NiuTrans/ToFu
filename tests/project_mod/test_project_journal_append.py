"""A commit costs one record, not the whole history, and stays crash-safe.

Problem: every edit rewrote the entire undo index under the project admission.
At 8.8k records that section took ~1s, so a second conversation editing an
unrelated file could wait out its 30s admission budget.
Decision: small records append to `modifications.journal` (one write + fsync);
only a large pre-image or a bounded log folds the index. Readers replay
index + fold artifact + log.
"""
import json
import os
from pathlib import Path

import pytest

from lib.project_mod import modifications as mods
from lib.project_mod.session_lock import locked_session

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(mods, '_JOURNAL_INDEX_COMPACT', {})
    monkeypatch.setattr(mods, '_JOURNAL_VALIDATED', {})
    monkeypatch.setattr(mods, '_JOURNAL_CHANGE_IDS', {})
    mods._mods_cache.clear()
    mods._mods_cache_identity.clear()
    mods._mods_cache_bytes.clear()
    mods._state.update(sessionId=None, modifications=[])
    yield


def session_of(root):
    return mods._get_session_dir(str(root))


def index_path(session):
    return Path(session, 'modifications.json')


def tail_path(session):
    return Path(session, 'modifications.journal')


def record(path, content='', change_id=None):
    row = {'type': 'write_file', 'path': path, 'existed': True}
    if content:
        row['originalContent'] = content
    if change_id:
        row['changeId'] = change_id
    return row


def test_small_records_append_without_rewriting_the_index(tmp_path):
    session = session_of(tmp_path)
    assert mods._record_modification(str(tmp_path), 'write_file', 'first') is True
    published = index_path(session).read_bytes()
    stamp = index_path(session).stat().st_mtime_ns
    for name in ('a.txt', 'b.txt', 'c.txt'):
        assert mods._record_modification(str(tmp_path), 'write_file', name) is True
        assert index_path(session).read_bytes() == published
        assert index_path(session).stat().st_mtime_ns == stamp
    assert [row['path'] for row in mods.get_modifications(str(tmp_path))] == [
        'first', 'a.txt', 'b.txt', 'c.txt']
    written = [json.loads(line)['path'] for line in tail_path(session).read_text().splitlines()]
    assert written == ['a.txt', 'b.txt', 'c.txt']


def test_commit_work_does_not_scale_with_history_size(tmp_path, monkeypatch):
    session = session_of(tmp_path)
    mods._flush_to_disk(session, [record(f'old-{index}') for index in range(500)])
    index_before = index_path(session).stat().st_size
    folds = []
    fold = mods._flush_to_disk
    monkeypatch.setattr(mods, '_flush_to_disk',
                        lambda *a, **kw: (folds.append(a[0]), fold(*a, **kw))[1])
    for index in range(10):
        assert mods._record_modification(str(tmp_path), 'write_file', f'new-{index}') is True
    assert folds == []
    assert index_path(session).stat().st_size == index_before
    assert len(mods.get_modifications(str(tmp_path))) == 510


def test_torn_final_append_is_not_trusted_and_recovery_keeps_earlier_commits(tmp_path):
    session = session_of(tmp_path)
    assert mods._record_modification(str(tmp_path), 'write_file', 'committed') is True
    with open(tail_path(session), 'ab') as handle:
        handle.write(b'{"type":"write_file","path":"half-writ')
        handle.flush()
        os.fsync(handle.fileno())
    assert [row['path'] for row in mods._load_from_disk(session)] == ['committed']
    assert mods._record_modification(str(tmp_path), 'write_file', 'after-crash') is True
    assert [row['path'] for row in mods.get_modifications(str(tmp_path))] == [
        'committed', 'after-crash']


def test_failed_append_is_rolled_back_and_never_acknowledged(tmp_path, monkeypatch):
    session = session_of(tmp_path)
    for name in ('first', 'second'):
        assert mods._record_modification(str(tmp_path), 'write_file', name) is True
    original = mods.get_modifications(str(tmp_path))
    assert tail_path(session).exists()
    before = tail_path(session).read_bytes()

    def fail(*_args):
        raise OSError('injected disk full')

    monkeypatch.setattr(mods, '_append_to_tail', fail)
    assert mods._record_modification(str(tmp_path), 'write_file', 'third') is False
    assert tail_path(session).read_bytes() == before
    mods._mods_cache.clear()
    assert mods.get_modifications(str(tmp_path)) == original


def test_bounded_tail_folds_into_the_index_and_keeps_order(tmp_path, monkeypatch):
    session = session_of(tmp_path)
    monkeypatch.setattr(mods, '_JOURNAL_TAIL_MAX_BYTES', 400)
    names = [f'f-{index}' for index in range(20)]
    sizes = []
    for name in names:
        assert mods._record_modification(str(tmp_path), 'write_file', name) is True
        sizes.append(tail_path(session).stat().st_size if tail_path(session).exists() else 0)
    # The log is folded before it can grow past its bound; the index absorbs the
    # prefix and readers still see every record in commit order.
    assert max(sizes) <= 400
    assert len(json.loads(index_path(session).read_text())['modifications']) > 0
    assert [row['path'] for row in mods.get_modifications(str(tmp_path))] == names


def test_an_oversized_record_folds_instead_of_bloating_the_log(tmp_path):
    session = session_of(tmp_path)
    oversized = b'x' * (mods._JOURNAL_APPEND_MAX_RECORD_BYTES + 1)
    assert mods._tail_appendable(session, {}, oversized) is False
    assert mods._tail_appendable(session, {}, b'x' * 128) is True
    assert mods._record_modification(str(tmp_path), 'write_file', 'small') is True
    assert mods._record_modification(
        str(tmp_path), 'write_file', 'big', original_content='y' * 4096) is True
    assert [row['path'] for row in mods.get_modifications(str(tmp_path))] == ['small', 'big']


def test_crash_before_index_publication_replays_the_fold_artifact(tmp_path):
    session = session_of(tmp_path)
    for name in ('folded', 'appended'):
        assert mods._record_modification(str(tmp_path), 'write_file', name) is True
    tail = tail_path(session)
    fold = Path(session, 'modifications.journal.fold')
    os.replace(tail, fold)
    # The index still lacks the appended record: the artifact is its only copy.
    assert [row['path'] for row in json.loads(index_path(session).read_text())['modifications']] == ['folded']
    assert [row['path'] for row in mods._load_from_disk(session)] == ['folded', 'appended']


def test_crash_after_index_publication_does_not_replay_the_artifact(tmp_path):
    session = session_of(tmp_path)
    assert mods._record_modification(str(tmp_path), 'write_file', 'first') is True
    assert mods._record_modification(str(tmp_path), 'write_file', 'second') is True
    tail = tail_path(session)
    fold = Path(session, 'modifications.journal.fold')
    os.replace(tail, fold)
    # Publish an index that records absorbing this exact artifact, then crash
    # before unlinking it.
    envelope = json.loads(index_path(session).read_text())
    envelope['modifications'].append({'type': 'write_file', 'path': 'second',
                                      'existed': True})
    envelope['absorbedFold'] = list(mods._fold_identity(session))
    mods._atomic_json_write(str(index_path(session)), envelope)
    assert [row['path'] for row in mods._load_from_disk(session)] == ['first', 'second']


def test_repeated_change_identity_is_idempotent_across_the_log(tmp_path):
    session = session_of(tmp_path)
    identity = 'a' * 32
    assert mods._commit_record(session, record('once', 'body', identity), identity) is True
    assert mods._commit_record(session, record('once', 'body', identity), identity) is False
    assert [row['path'] for row in mods._load_from_disk(session)] == ['once']


def test_undo_compaction_removes_every_journal_component(tmp_path):
    session = session_of(tmp_path)
    for name in ('a', 'b'):
        assert mods._record_modification(str(tmp_path), 'write_file', name) is True
    mods._locked_rmw(session, lambda rows: [])
    assert not index_path(session).exists()
    assert not tail_path(session).exists()
    assert not Path(session, 'modifications.journal.fold').exists()
    assert mods.get_modifications(str(tmp_path)) == []


def test_edit_admission_validates_by_identity_instead_of_reparsing(tmp_path, monkeypatch):
    """A single-file edit must not pay for the whole journal on every call."""
    session = session_of(tmp_path)
    mods._flush_to_disk(session, [record(f'old-{index}') for index in range(300)])
    mods._invalidate_cache(session)
    loads = []
    load = mods._load_from_disk
    monkeypatch.setattr(mods, '_load_from_disk', lambda s: (loads.append(s), load(s))[1])
    # First admission validates once; subsequent ones rely on the sealed prefix.
    mods._assert_journal_ready(session)
    validated = len(loads)
    for _ in range(20):
        mods._assert_journal_ready(session)
    assert validated == 1
    assert len(loads) == 1
    # An append advances the validated offset without a re-parse either.
    assert mods._record_modification(str(tmp_path), 'write_file', 'next') is True
    for _ in range(5):
        mods._assert_journal_ready(session)
    assert len(loads) == 1
    assert len(mods.get_modifications(str(tmp_path))) == 301


def test_concurrent_unrelated_file_commits_never_exhaust_the_admission_budget(tmp_path):
    """The reported failure: a second writer timed out on an unrelated file."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    base = str(tmp_path)
    session = session_of(tmp_path)
    mods._flush_to_disk(session, [record(f'history-{index}') for index in range(400)])
    mods._invalidate_cache(session)
    import time
    from contextlib import contextmanager

    waits = []
    enter = mods.locked_session

    @contextmanager
    def measured(directory):
        started = time.monotonic()
        with enter(directory) as state:
            waits.append(time.monotonic() - started)
            yield state

    mods.locked_session = measured
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(
                lambda index: mods._record_modification(
                    base, 'write_file', f'parallel-{index}'), range(16)))
    finally:
        mods.locked_session = enter
    assert all(outcomes)
    assert len(mods.get_modifications(base)) == 416
    assert max(waits) < 5.0, f'admission contention regressed: {max(waits):.2f}s'
    assert threading.active_count() >= 1
