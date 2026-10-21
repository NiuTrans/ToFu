"""History publication follows the durable write, including failure paths."""
import pytest

from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


def test_failed_record_write_does_not_publish_cache_or_listener(tmp_path, monkeypatch):
    base = str(tmp_path)
    events = []
    assert mods._record_modification(base, 'write_file', 'first')
    original = mods.get_modifications(base)
    def fail(*args, **kwargs):
        raise OSError('injected disk full')
    # Both durable commit mechanisms must fail closed: the append fast path and
    # the whole-index fold.
    monkeypatch.setattr(mods, '_append_to_tail', fail)
    monkeypatch.setattr(mods, '_atomic_json_write', fail)
    monkeypatch.setattr(mods, '_live_change_listeners', [lambda *args: events.append(args)])
    assert mods._record_modification(base, 'write_file', 'second', task_id='task') is False
    assert mods.get_modifications(base) == original
    assert events == []
    session_dir = mods._get_session_dir(base)
    mods._mods_cache.pop(session_dir, None)
    assert mods.get_modifications(base) == original


def test_failed_journal_removal_does_not_drop_cache_evidence(tmp_path, monkeypatch):
    base = str(tmp_path)
    assert mods._record_modification(base, 'write_file', 'first')
    original = mods.get_modifications(base)
    session_dir = mods._get_session_dir(base)
    def fail(path):
        raise OSError('injected unlink failure')
    monkeypatch.setattr(mods.os, 'remove', fail)
    with pytest.raises(OSError, match='injected'):
        mods._locked_rmw(session_dir, lambda rows: [])
    assert mods.get_modifications(base) == original


def test_command_surfaces_history_failure_after_successful_execution(tmp_path, monkeypatch):
    from lib.project_mod import tools, run_command
    monkeypatch.setattr(run_command, '_record_modification', lambda *args, **kwargs: False)
    result = tools.execute_tool('run_command', {'command': 'echo content > created'}, str(tmp_path))
    assert '[exit code: 0]' in result
    assert 'File history unavailable' in result
    assert (tmp_path / 'created').read_text() == 'content\n'
