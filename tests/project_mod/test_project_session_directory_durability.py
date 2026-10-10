"""A new journal directory must be reachable after a successful flush."""
from pathlib import Path

import pytest

from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


def test_each_new_ancestor_is_flushed_before_child_or_marker(tmp_path, monkeypatch):
    sessions = tmp_path / 'new-data' / 'sessions'
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(sessions))
    events = []
    real_mkdir = mods.os.mkdir
    real_sync = mods._sync_journal_directory

    def mkdir(path, *args, **kwargs):
        result = real_mkdir(path, *args, **kwargs)
        events.append(('mkdir', Path(path)))
        return result

    def sync(path):
        real_sync(path)
        events.append(('sync', Path(path)))

    monkeypatch.setattr(mods.os, 'mkdir', mkdir)
    monkeypatch.setattr(mods, '_sync_journal_directory', sync)
    monkeypatch.setattr(mods, '_remember_project_root', lambda *args: events.append(('marker', Path(args[0]))))
    session = Path(mods._get_session_dir(str(tmp_path / 'project')))
    for directory in (sessions.parent, sessions, session):
        created = events.index(('mkdir', directory))
        flushed = events.index(('sync', directory.parent), created)
        next_step = ('marker', session) if directory == session else ('mkdir', sessions if directory == sessions.parent else session)
        assert created < flushed < events.index(next_step)


@pytest.mark.parametrize('failed_level', ['data', 'sessions', 'session'])
def test_failed_ancestor_flush_stops_publication_and_retry_reflushes(tmp_path, monkeypatch, failed_level):
    sessions = tmp_path / 'data' / 'sessions'
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(sessions))
    parent = {'data': tmp_path, 'sessions': sessions.parent, 'session': sessions}[failed_level]
    events = []
    real_sync = mods._sync_journal_directory

    def fail(path):
        if Path(path) == parent:
            raise OSError('injected ancestor flush failure')
        real_sync(path)

    monkeypatch.setattr(mods, '_sync_journal_directory', fail)
    monkeypatch.setattr(mods, '_remember_project_root', lambda *args: events.append('marker'))
    with pytest.raises(OSError, match='ancestor flush'):
        mods._get_session_dir(str(tmp_path / 'project'))
    assert events == []

    def recover(path):
        real_sync(path)
        events.append(Path(path))

    monkeypatch.setattr(mods, '_sync_journal_directory', recover)
    assert Path(mods._get_session_dir(str(tmp_path / 'project'))).is_dir()
    assert parent in events
    assert events.index(parent) < events.index('marker')


def test_existing_non_directory_is_never_accepted(tmp_path, monkeypatch):
    sessions = tmp_path / 'sessions'
    sessions.write_text('keep')
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(sessions))
    with pytest.raises(OSError):
        mods._get_session_dir(str(tmp_path / 'project'))
    assert sessions.read_text() == 'keep'


def test_existing_session_has_one_parent_flush_and_no_creation(tmp_path, monkeypatch):
    directory = tmp_path / 'sessions' / 'existing'
    directory.mkdir(parents=True)
    calls = []
    sync = mods._sync_journal_directory

    def observe(path):
        calls.append(Path(path))
        sync(path)

    def unexpected_mkdir(*args, **kwargs):
        raise AssertionError('existing directory must not be recreated')

    monkeypatch.setattr(mods, '_sync_journal_directory', observe)
    monkeypatch.setattr(mods.os, 'mkdir', unexpected_mkdir)
    mods._ensure_durable_session_directory(str(directory))
    assert calls == [directory.parent]
