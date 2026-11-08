"""Private-index acceleration must retain full snapshot and isolation semantics."""
import os
import subprocess
import time

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


def _settle_stat_cache(root):
    """Record old mtimes so no entry is racily clean (Git would rehash it)."""
    past = time.time() - 600
    for path in root.glob('*.txt'):
        os.utime(path, (past, past))
    git(root, 'update-index', '-q', '--refresh')


def test_first_observation_hashes_only_changed_files(repository, tmp_path):
    root = repository
    for number in range(20):
        (root / f'stable-{number}.txt').write_text(f'stable {number}\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'unchanged tracked tree')
    hashed = tmp_path / 'hashed.log'
    # A clean filter runs exactly when Git reads a file's bytes to hash them,
    # which on a network volume is the cost that grows with the whole tree.
    (root / '.gitattributes').write_text('*.txt filter=observe\n')
    git(root, 'config', 'filter.observe.clean', f"echo %f >> '{hashed}'; cat")
    _settle_stat_cache(root)
    hashed.write_text('')
    (root / 'changed.txt').write_text('modified\n')
    (root / 'borrowed.txt').write_text('untracked input\n')
    parent = git(root, 'rev-parse', 'HEAD')
    actual = ic._alternate_index_tree(root, parent, str(tmp_path / 'private-index'))
    assert set(hashed.read_text().split()) == {'changed.txt', 'borrowed.txt'}
    env = {**os.environ, 'GIT_INDEX_FILE': str(tmp_path / 'reference-index')}
    git(root, 'read-tree', parent, env=env)
    git(root, 'add', '-A', '--', '.', env=env)
    assert actual == git(root, 'write-tree', env=env)


def test_capture_never_rewrites_the_writer_index(repository, tmp_path):
    root = repository
    _settle_stat_cache(root)
    # A stat-only change makes porcelain diff refresh, i.e. rewrite, the index
    # it reads; in a shared checkout that index belongs to other sessions.
    now = time.time()
    os.utime(root / 'stable.txt', (now, now))
    (root / 'changed.txt').write_text('modified\n')
    index = root / '.git' / 'index'
    before = index.read_bytes(), index.stat().st_mtime_ns
    ic._alternate_index_tree(root, git(root, 'rev-parse', 'HEAD'), str(tmp_path / 'private-index'))
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before

@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
def test_borrowed_stat_cache_never_hides_working_bytes(repository, tmp_path, flag):
    root = repository
    git(root, 'update-index', flag, 'changed.txt')
    (root / 'changed.txt').write_text('locally modified\n')
    parent = git(root, 'rev-parse', 'HEAD')
    actual = ic._alternate_index_tree(root, parent, str(tmp_path / 'private-index'))
    env = {**os.environ, 'GIT_INDEX_FILE': str(tmp_path / 'reference-index')}
    git(root, 'read-tree', parent, env=env)
    git(root, 'add', '-A', '--', '.', env=env)
    assert actual == git(root, 'write-tree', env=env)
    assert git(root, 'cat-file', '-p', actual + ':changed.txt') == 'locally modified'

# Commands that lstat every working-tree path. On a network volume each walk
# costs one metadata round trip per tracked file, so their count is the cost.
def _walks_working_tree(args):
    command = args[0]
    return (command in {'add', 'status'}
            or (command == 'diff' and '--cached' not in args)
            or (command == 'ls-files' and '--others' in args))


@pytest.mark.parametrize('dirty', [False, True])
def test_snapshot_walks_the_working_tree_once_per_observation(
        repository, monkeypatch, dirty):
    import threading
    root = repository
    if dirty:
        (root / 'changed.txt').write_text('modified\n')
        (root / 'borrowed.txt').write_text('untracked input\n')
    walks = []
    original = ic._git

    def observe(checkout, args, **kwargs):
        if _walks_working_tree(args):
            walks.append(args[0])
        return original(checkout, args, **kwargs)

    monkeypatch.setattr(ic, '_git', observe)
    parent = git(root, 'rev-parse', 'HEAD')
    snapshot, baseline = ic._task_input_checkpoint(
        root, parent, 'walk-count', threading.Event())
    # Two agreeing observations prove the input stable; each is one walk.
    assert walks == ['add', 'add']
    assert baseline == parent
    env = {**os.environ, 'GIT_INDEX_FILE': str(root.parent / 'reference-index')}
    git(root, 'read-tree', parent, env=env)
    git(root, 'add', '-A', '--', '.', env=env)
    assert git(root, 'rev-parse', snapshot + '^{tree}') == git(root, 'write-tree', env=env)
    assert (snapshot == parent) is (not dirty)


@pytest.mark.parametrize('path', ['node_modules/pkg/index.js', 'pkg/cache.pyc'])
def test_capture_refuses_forbidden_paths_without_touching_the_writer_index(
        repository, tmp_path, path):
    root = repository
    target = root / path
    target.parent.mkdir(parents=True)
    target.write_text('runtime bytes\n')
    index = root / '.git' / 'index'
    before = index.read_bytes()
    with pytest.raises(ic.IntegrationError, match='forbidden'):
        ic._alternate_index_tree(root, git(root, 'rev-parse', 'HEAD'),
                                 str(tmp_path / 'private-index'))
    assert index.read_bytes() == before
    assert git(root, 'status', '--porcelain', '--', path) == '?? ' + path
