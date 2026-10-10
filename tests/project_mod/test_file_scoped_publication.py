"""Real file leases and journal writes release before presentation callbacks."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import subprocess
import threading

import pytest

from lib import file_history, write_freshness
from lib.project_mod import modifications as mods, post_commit, session_lock
from lib.project_mod.workspace_lease import workspace_lease_fds
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    base = tmp_path / 'checkout'
    base.mkdir()
    (base / 'first.txt').write_text('A')
    (base / 'second.txt').write_text('A')
    subprocess.run(['git', 'init', '-q', str(base)], check=True)
    subprocess.run(['git', '-C', str(base), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(base), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(mods, '_live_change_listeners', [])
    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution', lambda target, base, path, **kw: (base, path))
    monkeypatch.setattr(_ops, '_resolve_write_path', lambda base, path, **kw: os.path.join(base, path))
    monkeypatch.setattr(_ops, '_touch_for_vscode', lambda *a: None)
    write_freshness._reset_for_tests()
    yield str(base)
    write_freshness._reset_for_tests()
    assert not post_commit._lanes


def test_same_file_listener_observes_current_freshness_and_can_write_again(project, monkeypatch):
    path = os.path.join(project, 'first.txt')
    write_freshness.record('same-conv', path)
    nested = []

    def listener(task, entry):
        assert session_lock.session_state(mods._get_session_dir(project)) is None
        assert workspace_lease_fds() == ()
        assert not write_freshness.is_stale('same-conv', path)
        if task == 'outer':
            nested.append(_ops.tool_write_file(project, 'first.txt', 'C',
                                              conv_id='same-conv', task_id='inner'))

    monkeypatch.setattr(mods, '_live_change_listeners', [listener])
    result = _ops.tool_write_file(project, 'first.txt', 'B', conv_id='same-conv', task_id='outer')
    assert result['ok']
    assert len(nested) == 1 and nested[0]['ok'], nested
    assert Path(path).read_text() == 'C'
    assert not write_freshness.is_stale('same-conv', path)
    assert [row['taskId'] for row in mods.get_modifications(project)] == ['outer', 'inner']
    assert file_history.make_snapshot(project, task_id='after', rel_paths=['first.txt'])


def test_cross_file_listeners_write_each_other_without_file_lock_wait_cycle(project, monkeypatch):
    barrier = threading.Barrier(2)
    nested = []
    guard = threading.Lock()
    for name in ('first.txt', 'second.txt'):
        write_freshness.record('cross-conv', os.path.join(project, name))

    def listener(task, entry):
        assert session_lock.session_state(mods._get_session_dir(project)) is None
        assert workspace_lease_fds() == ()
        if task not in {'left', 'right'}:
            return
        barrier.wait(timeout=5)
        other = 'second.txt' if task == 'left' else 'first.txt'
        result = _ops.tool_write_file(project, other, 'from-' + task,
                                     conv_id='cross-conv', task_id='nested-' + task)
        with guard:
            nested.append(result)

    monkeypatch.setattr(mods, '_live_change_listeners', [listener])
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(_ops.tool_write_file, project, 'first.txt', 'B',
                           conv_id='cross-conv', task_id='left')
        right = pool.submit(_ops.tool_write_file, project, 'second.txt', 'B',
                            conv_id='cross-conv', task_id='right')
        assert left.result(timeout=10)['ok']
        assert right.result(timeout=10)['ok']
    assert len(nested) == 2 and all(row['ok'] for row in nested), nested
    assert Path(project, 'first.txt').read_text() == 'from-right'
    assert Path(project, 'second.txt').read_text() == 'from-left'
    assert len(mods.get_modifications(project)) == 4
    for name in ('first.txt', 'second.txt'):
        assert not write_freshness.is_stale('cross-conv', os.path.join(project, name))
    assert workspace_lease_fds() == ()
