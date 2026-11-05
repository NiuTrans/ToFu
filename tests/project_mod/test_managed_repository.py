"""Tofu prepares plain project folders for isolated workers; people never run Git."""
import json
import os
import shutil
import subprocess

import pytest

from lib.project_mod import managed_repository as mr
from lib.project_mod.managed_repository import (
    ContentPolicy, ManagedRepositoryUnavailable, is_shared_link, link_shared_paths,
    prepare_managed_repository,
)

pytestmark = pytest.mark.unit

SMALL = ContentPolicy(large_file_bytes=1024, bulk_directory_files=5)


def git(root, *args, check=True):
    return subprocess.run(['git', '-C', str(root), *args], check=check,
                          capture_output=True, text=True, timeout=30)


def write(path, data=b'x\n'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def tracked(root):
    return set(git(root, 'ls-files').stdout.split())


def manifest(root):
    return json.loads((root / '.git' / mr.MANIFEST_NAME).read_text())


@pytest.fixture
def folder(tmp_path):
    """A research folder as a person leaves it: notes, code, data and environments."""
    root = tmp_path / 'rsi-exam'
    write(root / 'plan.md', b'# plan\n')
    write(root / 'tasks' / 'README.md', b'# tasks\n')
    write(root / 'tasks' / 'grader.py', b'print(1)\n')
    for index in range(6):  # a bulky data directory: shared, not versioned
        write(root / 'tasks' / 'raw' / 'bars' / f'{index}.bin', b'\0' * 8)
    for index in range(6):  # a bulky source directory stays versioned
        write(root / 'src' / f'm{index}.py', b'pass\n')
    write(root / 'tasks' / 'panel.parquet', b'p' * 4096)  # large file: shared
    write(root / '.venv' / 'pyvenv.cfg', b'home = /usr/bin\n')
    write(root / '.venv' / 'lib' / 'site.py', b'pass\n')
    write(root / 'tools' / 'env' / 'pyvenv.cfg', b'home = /usr/bin\n')
    write(root / '.tofu' / 'state.json', b'{}\n')
    write(root / 'tasks' / '__pycache__' / 'grader.cpython-311.pyc', b'\0')
    write(root / 'data' / 'cache.json', b'{}\n')
    return root.resolve()


def test_plain_folder_becomes_a_clean_managed_repository(folder):
    before = {path: path.read_bytes() for path in folder.rglob('*') if path.is_file()}
    result = prepare_managed_repository(str(folder), policy=SMALL)

    assert result.state == 'created' and result.guidance == 'AGENTS.md'
    assert git(folder, 'rev-parse', 'HEAD').stdout.strip() == result.baseline
    assert git(folder, 'symbolic-ref', 'HEAD').stdout.strip() == 'refs/heads/main'
    assert git(folder, 'status', '--porcelain').stdout == ''
    assert git(folder, 'config', '--bool', mr.MANAGED_CONFIG_KEY).stdout.strip() == 'true'
    assert 'Tofu baseline' in git(folder, 'log', '-1', '--format=%B').stdout
    # User files are never modified, moved or deleted.
    assert {path: path.read_bytes() for path in before} == before
    assert not list(folder.glob(mr._STAGING_PREFIX + '*'))

    versioned = tracked(folder)
    assert {'AGENTS.md', 'plan.md', 'tasks/README.md', 'tasks/grader.py',
            *(f'src/m{index}.py' for index in range(6))} == versioned
    assert set(manifest(folder)['shared']) == set(result.shared) == {
        'tasks/raw/bars', 'tasks/panel.parquet', 'tools/env'}

    guide = (folder / 'AGENTS.md').read_text()
    assert 'Nobody needs to run Git commands here.' in guide
    assert '`tasks/raw/bars` — 6 data files' in guide and '`tools/env` — virtual environment' in guide
    assert 'Start with `tasks/README.md`.' in guide and '`.venv/bin/python`' in guide


def test_preparation_is_idempotent_and_refresh_only_extends_sharing(folder):
    first = prepare_managed_repository(str(folder), policy=SMALL)
    again = prepare_managed_repository(str(folder), policy=SMALL)
    assert again.state == 'unchanged' and again.baseline == first.baseline and not again.added_shared

    for index in range(7):
        write(folder / 'downloads' / 'archive' / f'{index}.zip', b'z')
    write(folder / 'notes.md', b'new note\n')
    refreshed = prepare_managed_repository(str(folder), policy=SMALL)
    assert refreshed.state == 'refreshed' and refreshed.added_shared == ('downloads/archive',)
    assert refreshed.baseline == first.baseline  # no further commit
    status = git(folder, 'status', '--porcelain').stdout.splitlines()
    assert status == ['?? notes.md']  # new small files are ordinary untracked input
    cached = prepare_managed_repository(str(folder), refresh=False, policy=SMALL)
    assert cached.state == 'unchanged' and 'downloads/archive' in cached.shared


def test_existing_guidance_is_never_replaced(folder):
    write(folder / 'CLAUDE.md', b'# mine\n')
    result = prepare_managed_repository(str(folder), policy=SMALL)
    assert result.guidance == '' and not (folder / 'AGENTS.md').exists()
    assert (folder / 'CLAUDE.md').read_bytes() == b'# mine\n' and 'CLAUDE.md' in tracked(folder)


def test_paths_hidden_by_the_folders_own_ignore_files_are_shared(folder):
    write(folder / '.gitignore', b'secret-output/\n*.log\n')
    write(folder / 'secret-output' / 'run.txt', b'out\n')
    for index in range(30):  # an unmatched directory of only user-ignored files: one share
        write(folder / 'runs' / f'{index}.log', b'log\n')
    write(folder / 'mixed' / 'train.log', b'log\n')  # beside managed content: shared alone
    write(folder / 'mixed' / '__pycache__' / 'm.cpython-311.pyc', b'\0')
    write(folder / 'mixed' / 'model.py', b'pass\n')
    result = prepare_managed_repository(str(folder), policy=SMALL)
    hidden = {path for path in result.shared if path not in {
        'tasks/raw/bars', 'tasks/panel.parquet', 'tools/env'}}
    assert hidden == {'secret-output', 'runs', 'mixed/train.log'}
    assert {'.gitignore', 'mixed/model.py'} <= tracked(folder)
    assert git(folder, 'status', '--porcelain', '--ignored=no').stdout == ''


def test_a_repository_with_its_own_history_is_left_untouched(tmp_path):
    root = (tmp_path / 'repo').resolve()
    root.mkdir()
    git(root, 'init', '-q', '-b', 'trunk')
    write(root / 'a.txt')
    git(root, 'add', 'a.txt')
    git(root, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '-m', 'mine')
    config = (root / '.git' / 'config').read_bytes()
    result = prepare_managed_repository(str(root), policy=SMALL)
    assert result.state == 'user_repository' and not result.shared
    assert (root / '.git' / 'config').read_bytes() == config
    assert not (root / 'AGENTS.md').exists() and not (root / '.git' / mr.MANIFEST_NAME).exists()
    assert link_shared_paths(root, tmp_path / 'anywhere') == []


def test_an_empty_repository_is_adopted_without_changing_its_branch(tmp_path):
    root = (tmp_path / 'unborn').resolve()
    root.mkdir()
    git(root, 'init', '-q', '-b', 'trunk')
    write(root / 'notes.md')
    result = prepare_managed_repository(str(root), policy=SMALL)
    assert result.state == 'adopted'
    assert git(root, 'rev-parse', 'refs/heads/trunk').stdout.strip() == result.baseline
    assert tracked(root) == {'AGENTS.md', 'notes.md'}
    assert git(root, 'status', '--porcelain').stdout == ''
    assert not (root / '.git' / 'tofu-baseline.index').exists()


def test_staged_files_without_commits_are_refused_not_committed(tmp_path):
    root = (tmp_path / 'staged').resolve()
    root.mkdir()
    git(root, 'init', '-q')
    write(root / 'draft.md')
    git(root, 'add', 'draft.md')
    with pytest.raises(ManagedRepositoryUnavailable, match='staged files but no commits'):
        prepare_managed_repository(str(root), policy=SMALL)
    assert git(root, 'rev-parse', '--verify', '-q', 'HEAD', check=False).returncode != 0
    assert not (root / 'AGENTS.md').exists()


@pytest.mark.parametrize('layout', ['subdirectory', 'alias', 'missing', 'remote'])
def test_roots_tofu_cannot_prepare_explain_a_remedy_without_git(tmp_path, folder, layout):
    prepare_managed_repository(str(folder), policy=SMALL)
    if layout == 'subdirectory':
        path, reason = str(folder / 'tasks'), 'subdirectory of the Git repository'
    elif layout == 'alias':
        alias = tmp_path / 'alias'
        alias.symlink_to(folder, target_is_directory=True)
        path, reason = str(alias), 'through a symlink'
    elif layout == 'missing':
        path, reason = str(tmp_path / 'missing'), 'not an existing local directory'
    else:
        path, reason = 'remote:host:/srv/project', 'is not a local folder'
    with pytest.raises(ManagedRepositoryUnavailable) as caught:
        prepare_managed_repository(path, policy=SMALL)
    assert reason in caught.value.reason
    assert 'git ' not in caught.value.remedy and caught.value.hint().startswith('解决办法')


def test_a_failed_preparation_leaves_the_folder_as_it_was(folder, monkeypatch):
    def broken(*args, **kwargs):
        raise ManagedRepositoryUnavailable('boom', 'retry', '重试')
    monkeypatch.setattr(mr, '_record_baseline', broken)
    with pytest.raises(ManagedRepositoryUnavailable):
        prepare_managed_repository(str(folder), policy=SMALL)
    assert not (folder / '.git').exists() and not (folder / 'AGENTS.md').exists()
    assert not list(folder.glob(mr._STAGING_PREFIX + '*'))


def test_a_crashed_preparation_is_discarded_and_redone(folder):
    stale = folder / (mr._STAGING_PREFIX + 'crashed')
    write(stale / 'objects' / 'junk')
    assert prepare_managed_repository(str(folder), policy=SMALL).state == 'created'
    assert not stale.exists()


def test_worker_checkout_reads_shared_data_through_excluded_links(folder, tmp_path):
    prepare_managed_repository(str(folder), policy=SMALL)
    checkout = tmp_path / 'worker'
    git(folder, 'worktree', 'add', '-q', '--detach', str(checkout), 'HEAD')

    linked = link_shared_paths(folder, checkout)
    assert set(linked) == {'tasks/raw/bars', 'tasks/panel.parquet', 'tools/env', 'data'}
    assert link_shared_paths(folder, checkout) == []  # idempotent
    assert (checkout / 'tasks' / 'raw' / 'bars' / '0.bin').read_bytes() == b'\0' * 8
    assert (checkout / 'tasks' / 'panel.parquet').read_bytes() == b'p' * 4096
    assert all(is_shared_link(folder, checkout, path) for path in linked)
    assert not is_shared_link(folder, checkout, 'tasks/grader.py')
    # Links never enter snapshots: the checkout stays clean and `add -A` stages nothing.
    assert git(checkout, 'status', '--porcelain').stdout == ''
    git(checkout, 'add', '-A')
    assert git(checkout, 'diff', '--cached', '--name-only').stdout == ''
    ignored = git(checkout, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z').stdout
    assert {path for path in ignored.split('\0') if path} == set(linked)

    # Retiring the checkout unlinks; canonical data survives both removal paths.
    git(folder, 'worktree', 'remove', '--force', str(checkout))
    assert (folder / 'tasks' / 'raw' / 'bars' / '0.bin').exists()
    second = tmp_path / 'second'
    git(folder, 'worktree', 'add', '-q', '--detach', str(second), 'HEAD')
    link_shared_paths(folder, second)
    shutil.rmtree(second)
    assert (folder / 'tasks' / 'panel.parquet').exists() and (folder / 'data' / 'cache.json').exists()


def test_link_never_escapes_the_checkout_or_replaces_its_content(folder, tmp_path):
    prepare_managed_repository(str(folder), policy=SMALL)
    checkout = tmp_path / 'worker'
    git(folder, 'worktree', 'add', '-q', '--detach', str(checkout), 'HEAD')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (checkout / 'tools').mkdir()
    (checkout / 'tools' / 'env').symlink_to(outside, target_is_directory=True)
    write(checkout / 'data', b'worker file\n')
    linked = link_shared_paths(folder, checkout)
    assert 'tools/env' not in linked and 'data' not in linked
    assert (checkout / 'data').read_bytes() == b'worker file\n' and not list(outside.iterdir())


def test_plan_shares_bulk_data_but_keeps_source_trees_and_occupied_directories():
    files = [*(f'src/m{index}.py' for index in range(6)), *(f'raw/{index}.csv' for index in range(6)),
             'mixed/keep.py', *(f'mixed/blob{index}.bin' for index in range(6))]
    plan = mr.plan_content(mr.Path('/nonexistent'), files, ['mixed/keep.py'], SMALL)
    assert plan.shared == {'raw': '6 data files'}
