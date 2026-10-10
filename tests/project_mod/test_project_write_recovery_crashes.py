"""Real tool publication interrupted at each durable boundary recovers on read."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from lib.project_mod import modifications as mods
from lib.project_mod.write_intents import IntentError, locked_intents

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    from lib import file_history
    base = tmp_path / 'project'
    base.mkdir()
    (base / 'file.txt').write_bytes(b'old\r\n')
    sessions = tmp_path / 'sessions'
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(sessions))
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: None)
    return base, sessions


def crash_tool(base, sessions, phase, action='write'):
    program = r'''
import os, sys
from lib.project_mod import modifications as mods
from lib.project_mod.write_tools import _ops
from lib.project_mod.write_intents import IntentStore
from lib import file_history
base, sessions, phase, action = sys.argv[1:]
mods.SESSIONS_DIR = sessions
file_history.track_edit = lambda *a, **kw: None
_ops._should_record_modification = lambda *a, **kw: True
_ops._mod_attribution = lambda target, root, path, **kw: (root, path)
_ops._resolve_write_path = lambda root, path, **kw: os.path.join(root, path)
_ops._touch_for_vscode = lambda *a: None
_ops._record_write_freshness = lambda *a, **kw: None
replace = os.replace
target = os.path.join(base, 'file.txt')
def interrupted_replace(source, destination, *args, **kwargs):
    if destination == target and phase == 'prepared':
        os._exit(23)
    result = replace(source, destination, *args, **kwargs)
    if destination == target and phase == 'replaced':
        os._exit(23)
    return result
os.replace = interrupted_replace
if phase == 'journalled':
    IntentStore.acknowledge = lambda *args: os._exit(23)
kwargs = dict(conv_id='conv', task_id='task')
if action == 'write': _ops.tool_write_file(base, 'file.txt', 'new\n', **kwargs)
elif action == 'upload': _ops.save_uploaded_file(base, 'file.txt', b'new\n', on_conflict='overwrite', **kwargs)
elif action == 'diff': _ops.tool_apply_diff(base, 'file.txt', 'old', 'new', **kwargs)
elif action == 'insert': _ops.tool_insert_content(base, 'file.txt', 'old', 'new\n', **kwargs)
elif action == 'diffs': _ops.tool_apply_diffs(base, [{'path':'file.txt','search':'old','replace':'new'}], **kwargs)
elif action == 'inserts': _ops.tool_insert_contents(base, [{'path':'file.txt','anchor':'old','content':'new\n'}], **kwargs)
else: _ops.tool_edit_file(base, [{'path':'file.txt','operation':'replace','anchor':'old','content':'new'}], **kwargs)
raise SystemExit('tool returned without reaching the injected crash')
'''
    result = subprocess.run([sys.executable, '-c', program, str(base), str(sessions), phase, action],
                            capture_output=True, timeout=90)
    assert result.returncode == 23, result.stderr.decode(errors='replace')


@pytest.mark.parametrize('phase', ['prepared', 'replaced', 'journalled'])
def test_real_write_recovers_each_crash_boundary(project, phase):
    base, sessions = project
    crash_tool(base, sessions, phase)
    assert mods.resolve_base_path(task_id='task') == str(base)
    records = mods.get_modifications(str(base))
    session = mods._get_session_dir(str(base))
    with locked_intents(session) as store:
        assert store.read() == []
    assert not list(base.glob('.tofu_atomic_*'))
    if phase == 'prepared':
        assert records == []
        assert (base / 'file.txt').read_bytes() == b'old\r\n'
    else:
        assert len(records) == 1
        assert mods.get_modifications(str(base)) == records
        result = mods.undo_task_modifications(str(base), 'task')
        assert result['undone'] == 1 and result['failed'] == 0
        assert (base / 'file.txt').read_bytes() == b'old\r\n'


@pytest.mark.parametrize('action', ['upload', 'diff', 'insert', 'diffs', 'inserts', 'edit'])
def test_each_tool_uses_recoverable_publication(project, action):
    base, sessions = project
    crash_tool(base, sessions, 'replaced', action)
    result = mods.undo_task_modifications(str(base), 'task')
    assert result['undone'] == 1 and result['failed'] == 0
    assert (base / 'file.txt').read_bytes() == b'old\r\n'


def test_later_user_edit_is_never_replaced_by_crash_recovery(project):
    base, sessions = project
    crash_tool(base, sessions, 'replaced')
    (base / 'file.txt').write_bytes(b'user edit after crash')
    with pytest.raises(IntentError, match='write-recovery-conflict'):
        mods.get_modifications(str(base))
    with locked_intents(mods._get_session_dir(str(base))) as store:
        assert len(store.read()) == 1
    assert (base / 'file.txt').read_bytes() == b'user edit after crash'


def test_symlinked_project_root_keeps_its_existing_journal_identity(project, tmp_path):
    base, sessions = project
    alias = tmp_path / 'project-alias'
    alias.symlink_to(base, target_is_directory=True)
    crash_tool(alias, sessions, 'replaced')
    assert mods.resolve_base_path(task_id='task') == str(alias)
    rows = mods.get_modifications(str(alias))
    assert len(rows) == 1 and rows[0]['basePath'] == str(alias)
    result = mods.undo_task_modifications(str(alias), 'task')
    assert result['undone'] == 1 and result['failed'] == 0
    assert (base / 'file.txt').read_bytes() == b'old\r\n'


def test_retargeted_project_alias_is_a_recovery_conflict(project, tmp_path):
    base, sessions = project
    alias = tmp_path / 'project-alias'
    alias.symlink_to(base, target_is_directory=True)
    crash_tool(alias, sessions, 'replaced')
    other = tmp_path / 'other-project'
    other.mkdir()
    (other / 'file.txt').write_bytes(b'other user file')
    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)
    with pytest.raises(IntentError, match='write-recovery-conflict'):
        mods.get_modifications(str(alias))
    assert (base / 'file.txt').read_bytes() == b'new\n'
    assert (other / 'file.txt').read_bytes() == b'other user file'


def configure_writer(monkeypatch):
    from lib.project_mod.write_tools import _ops
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution', lambda target, base, path, **kw: (base, path))
    monkeypatch.setattr(_ops, '_resolve_write_path', lambda base, path, **kw: os.path.join(base, path))
    monkeypatch.setattr(_ops, '_touch_for_vscode', lambda *a: None)
    monkeypatch.setattr(_ops, '_record_write_freshness', lambda *a, **kw: None)
    return _ops


@pytest.mark.parametrize('fault', ['intent-flush', 'replacement'])
def test_failed_prepare_or_replace_aborts_without_changing_original(project, monkeypatch, fault):
    base, _ = project
    ops = configure_writer(monkeypatch)
    if fault == 'intent-flush':
        write = mods._atomic_json_write
        failed = False
        def uncertain_write(path, data):
            nonlocal failed
            write(path, data)
            if 'write-intents-v1' in path and not failed:
                failed = True
                raise OSError('injected uncertain intent publication')
        monkeypatch.setattr(mods, '_atomic_json_write', uncertain_write)
    else:
        replace = os.replace
        def fail_replace(source, target, *args, **kwargs):
            if target == str(base / 'file.txt'):
                raise OSError('injected target replacement failure')
            return replace(source, target, *args, **kwargs)
        monkeypatch.setattr(os, 'replace', fail_replace)
    result = ops.tool_write_file(str(base), 'file.txt', 'new\n', task_id='task')
    assert result['ok'] is False
    assert (base / 'file.txt').read_bytes() == b'old\r\n'
    assert mods.get_modifications(str(base)) == []
    with locked_intents(mods._get_session_dir(str(base))) as store:
        assert store.read() == []
    assert not list(base.glob('.tofu_atomic_*'))


def test_cleanup_failure_after_journal_commit_never_repeats_mutation(project, monkeypatch):
    from lib.project_mod.write_intents import IntentStore
    base, _ = project
    ops = configure_writer(monkeypatch)
    acknowledge = IntentStore.acknowledge
    def fail_ack(*args):
        raise OSError('injected acknowledgement cleanup failure')
    monkeypatch.setattr(IntentStore, 'acknowledge', fail_ack)
    result = ops.tool_write_file(str(base), 'file.txt', 'new\n', task_id='task')
    assert result['ok'] is True  # File and undo journal both committed.
    monkeypatch.setattr(IntentStore, 'acknowledge', acknowledge)
    records = mods.get_modifications(str(base))
    assert len(records) == 1
    assert mods.get_modifications(str(base)) == records
    assert (base / 'file.txt').read_bytes() == b'new\n'
    with locked_intents(mods._get_session_dir(str(base))) as store:
        assert store.read() == []


@pytest.mark.parametrize('action', ['write', 'upload'])
def test_new_target_parent_failure_stops_file_publication_and_retries(project, monkeypatch, action):
    base, _ = project
    ops = configure_writer(monkeypatch)
    sync = mods._sync_journal_directory
    def fail_parent(path):
        if Path(path) == base:
            raise OSError('injected new target ancestor failure')
        sync(path)
    monkeypatch.setattr(mods, '_sync_journal_directory', fail_parent)
    def write():
        if action == 'write':
            return ops.tool_write_file(str(base), 'new/nested/file.txt', 'created')
        return ops.save_uploaded_file(str(base), 'new/nested/file.txt', b'created')
    result = write()
    assert result['ok'] is False
    assert not (base / 'new/nested/file.txt').exists()
    monkeypatch.setattr(mods, '_sync_journal_directory', sync)
    result = write()
    assert result['ok'] is True
    assert (base / 'new/nested/file.txt').read_bytes() == b'created'
