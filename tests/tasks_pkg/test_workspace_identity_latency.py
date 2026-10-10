"""One Git observation preserves canonical/linked identity and typed failures."""
from __future__ import annotations

from pathlib import Path
import subprocess
import pytest
from lib.tasks_pkg import workspace_delivery as delivery

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('linked', [False, True])
def test_repository_identity_uses_one_observation(tmp_path, monkeypatch, linked):
    canonical = tmp_path / 'canonical'
    canonical.mkdir()
    common = canonical / '.git'
    common.mkdir()
    checkout = tmp_path / 'linked' if linked else canonical
    if linked:
        checkout.mkdir()
        (checkout / '.git').write_text(f'gitdir: {common}/worktrees/linked')
    nested = checkout / 'nested'
    nested.mkdir()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[-2:] == ['--show-toplevel', '--git-common-dir']
        assert kwargs['check'] is False and kwargs['timeout'] == 10
        return subprocess.CompletedProcess(command, 0,
            stdout=f'{checkout}\n{common}\n', stderr='')

    monkeypatch.setattr(delivery, 'git_executable', lambda: '/synthetic/git')
    monkeypatch.setattr(delivery.subprocess, 'run', run)
    assert delivery._checkout(str(nested)) == (str(checkout), str(canonical))
    assert len(calls) == 1


@pytest.mark.parametrize('returncode, stdout', [(1, ''), (0, '/only-one-line\n')])
def test_failed_or_incomplete_observation_never_authorizes_workspace(
        tmp_path, monkeypatch, returncode, stdout):
    (tmp_path / '.git').mkdir()
    monkeypatch.setattr(delivery, 'git_executable', lambda: '/synthetic/git')
    monkeypatch.setattr(delivery.subprocess, 'run', lambda command, **kwargs:
        subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr='failed'))
    with pytest.raises(delivery.WorkspaceBindingError):
        delivery._checkout(str(tmp_path))
