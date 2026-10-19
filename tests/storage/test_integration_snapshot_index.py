"""Private-index acceleration must retain full snapshot and isolation semantics."""
import os
import subprocess

import pytest

from lib import integration_control as ic


pytestmark = pytest.mark.unit

def git(root, *args, env=None):
    return subprocess.run(['git', *args], cwd=root, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init')
    git(root, 'config', 'user.name', 'Snapshot Test')
    git(root, 'config', 'user.email', 'snapshot@example.invalid')
    (root / 'stable.txt').write_text('stable\n')
    (root / 'changed.txt').write_text('original\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'baseline')
    return root


def test_snapshot_reseed_preserves_matching_stat_cache(repository, tmp_path, monkeypatch):
    root = repository
    index = str(tmp_path / 'private-index')
    parent = git(root, 'rev-parse', 'HEAD')
    ic._alternate_index_tree(root, parent, index)
    env = {**os.environ, 'GIT_INDEX_FILE': index}
    before = git(root, 'ls-files', '--debug', '--', 'stable.txt', env=env)
    assert 'ctime: 0:0' not in before
    observed = []
    original = ic._git

    def observe(checkout, args, **kwargs):
        result = original(checkout, args, **kwargs)
        if args[0] == 'read-tree':
            observed.append(git(root, 'ls-files', '--debug', '--', 'stable.txt', env=env))
        return result

    monkeypatch.setattr(ic, '_git', observe)
    ic._alternate_index_tree(root, parent, index)
    assert observed == [before]


@pytest.mark.parametrize('mutation', ['dirty', 'ignore', 'new-parent', 'delete', 'mode', 'symlink'])
def test_snapshot_reseed_matches_fresh_index(repository, tmp_path, mutation):
    root = repository
    index = str(tmp_path / 'private-index')
    parent = git(root, 'rev-parse', 'HEAD')
    (root / 'borrowed.txt').write_text('untracked input\n')
    ic._alternate_index_tree(root, parent, index)
    if mutation == 'dirty':
        (root / 'changed.txt').write_text('modified\n')
    elif mutation == 'ignore':
        (root / '.gitignore').write_text('borrowed.txt\n')
    elif mutation == 'new-parent':
        (root / '.gitignore').write_text('ignored.txt\n')
        (root / 'ignored.txt').write_text('new tracked ignored content\n')
        git(root, 'add', '-f', '.gitignore', 'ignored.txt')
        git(root, 'commit', '-qm', 'concurrent commit')
        parent = git(root, 'rev-parse', 'HEAD')
    elif mutation == 'delete':
        (root / 'changed.txt').unlink()
        (root / 'borrowed.txt').unlink()
    elif mutation == 'mode':
        (root / 'changed.txt').chmod(0o755)
    else:
        (root / 'changed.txt').unlink()
        (root / 'changed.txt').symlink_to('stable.txt')
    real_index = (root / '.git' / 'index').read_bytes()
    actual = ic._alternate_index_tree(root, parent, index)
    env = {**os.environ, 'GIT_INDEX_FILE': str(tmp_path / 'reference-index')}
    git(root, 'read-tree', parent, env=env)
    git(root, 'add', '-A', '--', '.', env=env)
    expected = git(root, 'write-tree', env=env)
    assert actual == expected
    assert (root / '.git' / 'index').read_bytes() == real_index
    assert git(root, 'rev-parse', 'HEAD') == parent
