"""A durable append keeps a validated warm history without rereading its prefix."""
from collections import OrderedDict
import json
from pathlib import Path

import pytest

from lib.project_mod import modifications as mods
from lib.project_mod.session_lock import locked_session

pytestmark = pytest.mark.unit


@pytest.fixture
def history(tmp_path, monkeypatch):
    for name in ('_mods_cache_identity', '_mods_cache_bytes',
                 '_JOURNAL_INDEX_COMPACT', '_JOURNAL_VALIDATED', '_JOURNAL_CHANGE_IDS'):
        monkeypatch.setattr(mods, name, {})
    monkeypatch.setattr(mods, '_mods_cache', OrderedDict())
    monkeypatch.setattr(mods, '_MODS_CACHE_CAPACITY', 16)
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_BYTES', 32 * 1024 * 1024)
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_SESSION_BYTES', 32 * 1024 * 1024)
    session = str(tmp_path / 'journal')
    Path(session).mkdir()
    monkeypatch.setattr(mods, '_state', {'sessionId': session, 'modifications': []})
    rows = [{'type': 'write_file', 'path': f'old-{i}', 'existed': True}
            for i in range(1000)]
    mods._flush_to_disk(session, rows)
    with locked_session(session):
        mods._assert_journal_ready(session)
    assert mods._cache_get(session) == rows
    return session, rows


def test_append_and_following_reads_do_not_revisit_warm_history(history, monkeypatch):
    session, expected = history
    full_weight = mods._mods_cache_weight

    def forbidden(*args, **kwargs):
        pytest.fail('A small append revisited the validated historical prefix')

    monkeypatch.setattr(mods, '_load_from_disk', forbidden)
    monkeypatch.setattr(mods, '_mods_cache_weight', forbidden)
    for index in range(12):
        row = {'type': 'write_file', 'path': f'new-{index}', 'existed': True}
        assert mods._commit_record(session, row)
        expected.append(row)
        assert mods._cache_get(session) == expected
        assert mods._state['modifications'] == expected
        assert mods._mods_cache_bytes[session] >= full_weight(
            session, expected, mods._journal_identity(session))


def test_append_reclaims_only_cache_when_resident_budget_is_exceeded(history, monkeypatch):
    session, expected = history
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_SESSION_BYTES', mods._mods_cache_bytes[session])
    row = {'type': 'write_file', 'path': 'new', 'existed': True}
    assert mods._commit_record(session, row)
    assert session not in mods._mods_cache
    assert mods._state['modifications'] == []
    assert mods._load_from_disk(session) == expected + [row]


def test_inactive_append_preserves_other_project_and_can_become_primary(history):
    session, expected = history
    other = [{'type': 'write_file', 'path': 'other-project'}]
    mods._state.update(sessionId='other-session', modifications=other)
    for index in range(20):
        row = {'type': 'write_file', 'path': f'inactive-{index}', 'existed': True}
        assert mods._commit_record(session, row)
        expected.append(row)
    assert mods._state['modifications'] == other
    with mods._lock:
        mods._sync_primary_view(session, mods._mods_cache[session])
    row = {'type': 'write_file', 'path': 'now-primary', 'existed': True}
    assert mods._commit_record(session, row)
    assert mods._state['modifications'] == expected + [row]
    assert mods._mods_cache_bytes[session] >= mods._mods_cache_weight(
        session, mods._mods_cache[session], mods._journal_identity(session))


def test_append_does_not_resurrect_an_entry_evicted_during_commit(history, monkeypatch):
    session, expected = history
    append = mods._append_to_tail

    def append_then_evict(*args):
        result = append(*args)
        mods._invalidate_cache(session)
        return result

    monkeypatch.setattr(mods, '_append_to_tail', append_then_evict)
    row = {'type': 'write_file', 'path': 'new', 'existed': True}
    assert mods._commit_record(session, row)
    assert session not in mods._mods_cache
    assert mods._cache_get(session) == expected + [row]


def test_warm_append_refuses_an_externally_replaced_invalid_journal(history):
    session, _ = history
    path = Path(session, 'modifications.json')
    invalid = json.dumps({'modifications': [{'type': 'write_file'}]})
    path.write_text(invalid)
    with pytest.raises(ValueError, match='Invalid modification journal'):
        mods._commit_record(session, {'type': 'write_file', 'path': 'new'})
    assert path.read_text() == invalid


def test_append_does_not_label_an_old_prefix_with_a_replacement_identity(history, monkeypatch):
    session, _ = history
    append = mods._append_to_tail
    foreign = {'type': 'write_file', 'path': 'external', 'existed': True}

    def append_then_replace_index(*args):
        result = append(*args)
        path = Path(session, 'replacement.json')
        path.write_text(json.dumps({'modifications': [foreign]}))
        path.replace(Path(session, 'modifications.json'))
        return result

    monkeypatch.setattr(mods, '_append_to_tail', append_then_replace_index)
    row = {'type': 'write_file', 'path': 'new', 'existed': True}
    assert mods._commit_record(session, row)
    assert session not in mods._mods_cache
    assert session not in mods._JOURNAL_VALIDATED
    assert mods._cache_get(session) == [foreign, row]


def test_append_catches_a_stale_warm_prefix_up_instead_of_dropping_it(history, monkeypatch):
    session, expected = history
    foreign = {'type': 'write_file', 'path': 'other-process', 'existed': True}
    # Another process committed after this cache was filled; it never saw it.
    with open(Path(session, 'modifications.journal'), 'ab') as handle:
        handle.write(json.dumps(foreign).encode() + b'\n')
    expected.append(foreign)
    weigh = mods._mods_cache_weight
    monkeypatch.setattr(mods, '_load_from_disk', lambda *a: pytest.fail('history re-read'))
    monkeypatch.setattr(mods, '_mods_cache_weight', lambda *a: pytest.fail('history re-weighed'))
    row = {'type': 'write_file', 'path': 'mine', 'existed': True}
    assert mods._commit_record(session, row)
    expected.append(row)
    assert mods._mods_cache_identity.get(session) == mods._journal_identity(session)
    assert mods._cache_get(session) == expected
    assert mods._state['modifications'] == expected
    assert mods._mods_cache_bytes[session] >= weigh(
        session, expected, mods._journal_identity(session))
