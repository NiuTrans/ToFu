"""Delivery identity batches reads without caching repository authority."""
from types import SimpleNamespace

import pytest

from lib import integration_control as ic
from lib.project_mod import delivery

pytestmark = pytest.mark.unit


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    ic._git(root, ['init'], check=True)
    ic._git(root, ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                   'commit', '--allow-empty', '-m', 'base'], check=True)
    return root


@pytest.mark.parametrize('linked', [False, True])
def test_identity_subprocess_count_and_relative_cwd(repository, tmp_path, monkeypatch, linked):
    root = repository
    workspace = root
    if linked:
        workspace = tmp_path / 'linked'
        ic._git(root, ['worktree', 'add', '--detach', str(workspace)], check=True)
    nested = workspace / 'nested'
    nested.mkdir()
    original = ic._git
    calls = []

    def observe(cwd, args, **kwargs):
        calls.append(args)
        return original(cwd, args, **kwargs)

    monkeypatch.setattr(ic, '_git', observe)
    assert delivery._canonical(str(nested)) == (root, workspace)
    assert len(calls) == (3 if linked else 2)


@pytest.mark.parametrize('output', ['', '/root\n', '/root\n\n', '/root\n/common\nextra\n'])
def test_incomplete_identity_refuses(repository, monkeypatch, output):
    monkeypatch.setattr(ic, '_git', lambda *a, **kw: SimpleNamespace(stdout=output))
    with pytest.raises(ic.IntegrationError, match='Incomplete'):
        delivery._canonical(str(repository))


def test_linked_identity_rechecks_canonical_repository(repository, tmp_path, monkeypatch):
    workspace = tmp_path / 'linked'
    ic._git(repository, ['worktree', 'add', '--detach', str(workspace)], check=True)
    assert delivery._canonical(str(workspace)) == (repository, workspace)
    # A later observation cannot reuse earlier canonical identity. Simulate
    # replacement during the worktree listing, before canonical validation.
    original = ic._git

    def changed(cwd, args, **kwargs):
        result = original(cwd, args, **kwargs)
        if args == ['worktree', 'list', '--porcelain']:
            monkeypatch.setattr(ic, '_common_dir', lambda path: tmp_path / 'other.git')
        return result

    monkeypatch.setattr(ic, '_git', changed)
    with pytest.raises(ic.IntegrationError, match='repository differ'):
        delivery._canonical(str(workspace))


def test_git_failure_propagates(repository, monkeypatch):
    def fail(*args, **kwargs):
        raise ic.IntegrationError('identity unavailable')
    monkeypatch.setattr(ic, '_git', fail)
    with pytest.raises(ic.IntegrationError, match='identity unavailable'):
        delivery._canonical(str(repository))
