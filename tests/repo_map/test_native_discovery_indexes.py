"""Installed discovery parity for large indexes and submodule boundaries.

Git is the executable oracle; every repository and configuration is test-owned.
The same cases exercise both native transports without selecting a default.
"""
import sys

import pytest

from tests.repo_map.test_native_discovery import git_fixture as git_fixture, oracle, write

pytestmark = pytest.mark.unit


@pytest.fixture(params=('ffi', 'process'))
def native_discovery(request):
    package = pytest.importorskip('tofu_repo_kernel')
    if request.param == 'process':
        if sys.platform != 'linux':
            pytest.skip('sealed process discovery requires Linux')
        return package.load_discovery_process()
    return package.load_discovery()


def assert_read_only_parity(native, git, root, index):
    before = index.read_bytes()
    expected = oracle(git, root)
    assert native.list_files(root) == expected
    assert index.read_bytes() == before
    return expected


def test_large_compressed_index_keeps_deleted_tracked_and_new_files(
        native_discovery, git_fixture, tmp_path):
    git, root = git_fixture, tmp_path / 'repository'
    root.mkdir()
    git(root, 'init', '-q')
    blob = git(root, 'hash-object', '-w', '--stdin', input=b'fixture\n').stdout.strip()
    names = [f'modules/group{i // 64:04d}/file{i:05d}.py'.encode()
             for i in range(32_768)]
    records = b''.join(b'100644 ' + blob + b'\t' + name + b'\n' for name in names)
    git(root, 'update-index', '--index-info', input=records)
    git(root, 'update-index', '--index-version', '4')
    write(root, '.gitignore', '*.tmp\nignored/\n')
    for index in range(64):
        write(root, f'modules/group{index:04d}/fresh.py')
    write(root, 'drop.tmp')
    write(root, 'ignored/private.py')
    index_path = root / '.git/index'
    expected = assert_read_only_parity(native_discovery, git, root, index_path)
    assert set(names).issubset(expected)
    assert len(expected) == len(names) + 65
    assert_read_only_parity(native_discovery, git, root / 'modules/group0007', index_path)
    # Index edits are observed on the next call; no repository snapshot persists.
    git(root, 'update-index', '--force-remove', names[0].decode())
    assert names[0] not in assert_read_only_parity(native_discovery, git, root, index_path)


def test_conflict_stages_and_skip_worktree_preserve_git_file_set(
        native_discovery, git_fixture, tmp_path):
    git, root = git_fixture, tmp_path / 'repository'
    root.mkdir()
    git(root, 'init', '-q')
    blobs = [git(root, 'hash-object', '-w', '--stdin', input=value).stdout.strip()
             for value in (b'base\n', b'ours\n', b'theirs\n')]
    records = b''.join(b'100644 ' + blob + b' ' + str(stage).encode() + b'\tconflict.py\n'
                       for stage, blob in enumerate(blobs, 1))
    records += b'100644 ' + blobs[0] + b'\tmissing.py\n'
    git(root, 'update-index', '--index-info', input=records)
    git(root, 'update-index', '--skip-worktree', 'missing.py')
    write(root, 'conflict.py', 'unresolved working tree\n')
    assert assert_read_only_parity(native_discovery, git, root, root / '.git/index') == [
        b'conflict.py', b'missing.py']
    git(root, 'add', 'conflict.py')
    assert_read_only_parity(native_discovery, git, root, root / '.git/index')


def test_initialized_and_deinitialized_submodules_keep_repository_boundaries(
        native_discovery, git_fixture, tmp_path):
    git = git_fixture
    source, root = tmp_path / 'source', tmp_path / 'parent'
    for directory in (source, root):
        directory.mkdir()
        git(directory, 'init', '-q')
    write(source, 'tracked.py')
    git(source, 'add', 'tracked.py')
    git(source, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'fixture')
    git(root, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q',
        str(source), 'dependencies/library')
    child = root / 'dependencies/library'
    write(child, 'new.py')
    write(child, 'tracked.py', 'modified\n')
    index = root / '.git/index'
    expected = assert_read_only_parity(native_discovery, git, root, index)
    assert b'dependencies/library' in expected
    assert not any(path.startswith(b'dependencies/library/') for path in expected)
    child_index = root / '.git/modules/dependencies/library/index'
    assert assert_read_only_parity(native_discovery, git, child, child_index) == [
        b'new.py', b'tracked.py']
    git(root, 'submodule', 'deinit', '-f', '--', 'dependencies/library')
    assert assert_read_only_parity(native_discovery, git, root, index) == expected
    child.rmdir()
    assert assert_read_only_parity(native_discovery, git, root, index) == expected


def test_plain_directory_refusal_does_not_cache_repository_absence(
        native_discovery, git_fixture, tmp_path):
    from tofu_repo_kernel._discovery import DiscoveryError

    git, root = git_fixture, tmp_path / 'plain'
    root.mkdir()
    write(root, 'public.py')
    write(root, 'private.py')
    with pytest.raises(DiscoveryError) as failure:
        native_discovery.list_files(root)
    # Status 2 is a general discovery failure, not a dedicated NoRepo outcome.
    assert failure.value.status == 2
    git(root, 'init', '-q')
    write(root, '.gitignore', 'private.py\n')
    assert native_discovery.list_files(root) == oracle(git, root) == [
        b'.gitignore', b'public.py']


def test_bare_repository_refuses_without_inventing_a_worktree(
        native_discovery, git_fixture, tmp_path):
    from tofu_repo_kernel._discovery import DiscoveryError

    root = tmp_path / 'bare.git'
    root.mkdir()
    git_fixture(root, 'init', '--bare', '-q')
    with pytest.raises(DiscoveryError) as failure:
        native_discovery.list_files(root)
    assert failure.value.status == 2
    assert not (root / 'index').exists()
