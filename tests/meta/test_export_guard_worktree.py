"""The privacy drift gate runs in linked worktrees as well as ordinary clones."""
import subprocess

import pytest

from tests.meta import test_gitignore_covers_export_excludes as guard

pytestmark = pytest.mark.unit


def test_linked_worktree_is_not_mistaken_for_an_export(tmp_path, monkeypatch):
    repository = tmp_path / 'repository'
    repository.mkdir()
    subprocess.run(['git', 'init', '--template=', str(repository)], check=True,
        capture_output=True)
    subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Test',
        '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'initial'],
        check=True, capture_output=True)
    worktree = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(repository), 'worktree', 'add', '--detach', str(worktree)],
        check=True, capture_output=True)
    assert (worktree / '.git').is_file()
    monkeypatch.setattr(guard, '_ROOT', str(worktree))
    try:
        guard._require_git_repo()
    except pytest.skip.Exception:
        pytest.fail('a valid linked Git worktree must execute the privacy drift guard')


def test_invalid_git_inventory_cannot_become_an_empty_snapshot(tmp_path, monkeypatch):
    (tmp_path / '.git').write_text('gitdir: missing-git-directory\n')
    monkeypatch.setattr(guard, '_ROOT', str(tmp_path))
    with pytest.raises(subprocess.CalledProcessError):
        guard._tracked_files()
