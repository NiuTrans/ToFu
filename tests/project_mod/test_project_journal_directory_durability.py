"""Directory publication and uncertain-write cache recovery for undo journals."""
import json
import os
import stat

import pytest
from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


def test_json_publication_syncs_file_then_directory(tmp_path, monkeypatch):
    calls = []
    sync = os.fsync
    def observe(fd):
        calls.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
        return sync(fd)
    monkeypatch.setattr(os, 'fsync', observe)
    mods._atomic_json_write(str(tmp_path / 'journal.json'), {'value': 1})
    assert calls == ['file', 'directory']


def test_journal_removal_syncs_directory(tmp_path, monkeypatch):
    path = tmp_path / 'modifications.json'
    path.write_text('{}')
    calls = []
    sync = os.fsync
    def observe(fd):
        calls.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        return sync(fd)
    monkeypatch.setattr(os, 'fsync', observe)
    mods._flush_to_disk(str(tmp_path), [])
    assert not path.exists()
    assert calls == [True]


def test_failed_post_replace_sync_cannot_erase_new_record_on_next_append(tmp_path, monkeypatch):
    session = str(tmp_path)
    original = mods._atomic_json_write
    def uncertain_write(path, data):
        original(path, data)
        raise OSError('injected failure after rename')
    monkeypatch.setattr(mods, '_atomic_json_write', uncertain_write)
    try:
        with pytest.raises(OSError, match='after rename'):
            mods._locked_rmw(session, lambda rows: rows.append({'type': 'write_file', 'path': 'first'}))
        assert session not in mods._mods_cache
        monkeypatch.setattr(mods, '_atomic_json_write', original)
        mods._locked_rmw(session, lambda rows: rows.append({'type': 'write_file', 'path': 'second'}))
        durable = json.loads((tmp_path / 'modifications.json').read_text())['modifications']
        assert [row['path'] for row in durable] == ['first', 'second']
    finally:
        mods._mods_cache.pop(session, None)


@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_directory_flush_failure_is_visible_and_releases_descriptor(tmp_path, monkeypatch, failure):
    descriptors = []
    sync = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            descriptors.append(fd)
            raise failure('injected directory flush failure')
        return sync(fd)
    monkeypatch.setattr(os, 'fsync', fail_directory)
    target = tmp_path / 'journal.json'
    with pytest.raises(failure, match='directory flush'):
        mods._atomic_json_write(str(target), {'value': 1})
    assert json.loads(target.read_text()) == {'value': 1}
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert not list(tmp_path.glob('.modifications_*'))
