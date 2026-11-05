"""Incomplete Git metadata/configuration observations never authorize a workspace."""
from __future__ import annotations

import subprocess
import pytest
from lib.project_mod import activity_supervised_process as supervised
from lib.tasks_pkg import workspace_delivery as delivery

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('returncode,stdout,stderr', [
    (2, '', 'config read failed'), (1, '', 'config read failed'),
    (1, 'unexpected output', ''), (0, '', ''), (0, 'first\nsecond\n', ''),
])
def test_failed_config_read_never_authorizes_workspace(
        tmp_path, monkeypatch, returncode, stdout, stderr):
    common = tmp_path / '.git'
    common.mkdir()

    calls = []

    def run(command, *, cwd, env, stall_seconds, cancellation=None):
        calls.append(command)
        # A slow volume decides how long a healthy read takes; only the shared
        # no-progress window, never a wall-clock deadline, may end it.
        assert stall_seconds == delivery.GIT_STALL_SECONDS
        if 'rev-parse' in command:
            return subprocess.CompletedProcess(command, 0,
                stdout=f'false\n{common}\n', stderr='')
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(delivery, 'git_executable', lambda: '/synthetic/git')
    monkeypatch.setattr(supervised, 'run_activity_supervised', run)
    with pytest.raises(delivery.WorkspaceBindingError):
        delivery._checkout(str(tmp_path))
    assert [command[-1] for command in calls] == ['--git-common-dir', 'core.worktree']


@pytest.mark.parametrize('returncode, stdout', [(1, ''), (0, '/only-one-line\n')])
def test_failed_or_incomplete_observation_never_authorizes_workspace(
        tmp_path, monkeypatch, returncode, stdout):
    (tmp_path / '.git').mkdir()
    monkeypatch.setattr(delivery, 'git_executable', lambda: '/synthetic/git')
    monkeypatch.setattr(supervised, 'run_activity_supervised', lambda command, **kwargs:
        subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr='failed'))
    with pytest.raises(delivery.WorkspaceBindingError):
        delivery._checkout(str(tmp_path))
