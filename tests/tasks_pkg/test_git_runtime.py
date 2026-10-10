"""Git admission skips broken binaries without retrying repository operations."""
import os
import subprocess

import pytest

from lib import git_runtime
from lib.tasks_pkg import workspace_delivery

pytestmark = pytest.mark.unit


def test_incompatible_path_git_does_not_block_workspace(tmp_path, monkeypatch):
    broken = tmp_path / 'conda'
    broken.mkdir()
    executable = broken / 'git'
    executable.write_text('#!/bin/sh\necho "GLIBC_2.28 not found" >&2\nexit 1\n')
    executable.chmod(0o755)
    repository = tmp_path / 'repository'
    repository.mkdir()
    good_git = git_runtime.git_executable()
    subprocess.run([good_git, '-C', str(repository), 'init'], check=True,
                   capture_output=True)
    monkeypatch.setenv('PATH', str(broken) + os.pathsep + os.environ['PATH'])
    assert git_runtime.git_executable() != str(executable)
    assert workspace_delivery._checkout(str(repository)) == (str(repository), str(repository))


def test_no_runnable_git_retains_loader_failure_and_can_recover(tmp_path, monkeypatch):
    broken = tmp_path / 'git'
    broken.write_text('#!/bin/sh\necho "GLIBC_2.28 not found" >&2\nexit 1\n')
    broken.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(os, 'defpath', str(tmp_path))
    with pytest.raises(RuntimeError, match='GLIBC_2.28 not found'):
        git_runtime.git_executable()
    broken.write_text('#!/bin/sh\necho "git version synthetic"\n')
    assert git_runtime.git_executable() == str(broken)


def test_repository_failure_keeps_git_stderr(tmp_path):
    (tmp_path / '.git').mkdir()
    with pytest.raises(workspace_delivery.WorkspaceBindingError, match='(?i)not a git repository'):
        workspace_delivery._checkout(str(tmp_path))
