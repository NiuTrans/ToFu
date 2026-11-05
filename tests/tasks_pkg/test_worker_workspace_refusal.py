"""Dispatched workers need an isolatable root: Tofu prepares plain folders, refuses the rest early."""
import subprocess

import pytest

from lib.error_envelope import task_start_failure_cause
from lib.tasks_pkg.workspace_delivery import (
    IsolationRefusal, WorkspaceBindingError, isolated_workspace_refusal,
)

pytestmark = pytest.mark.unit


def git(root, *args):
    subprocess.run(['git', '-C', str(root), *args], check=True,
                   capture_output=True, text=True, timeout=20)


@pytest.fixture
def committed(tmp_path):
    root = (tmp_path / 'repo')
    root.mkdir()
    root = root.resolve()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.name', 'Refusal test')
    git(root, 'config', 'user.email', 'refusal@example.invalid')
    (root / 'README.md').write_text('seed\n')
    git(root, 'add', 'README.md')
    git(root, 'commit', '-q', '-m', 'seed')
    return root


def test_primary_checkout_top_level_with_a_commit_is_isolatable(committed):
    assert isolated_workspace_refusal(str(committed)) is None


def test_plain_directory_is_prepared_so_people_never_run_git(tmp_path):
    plain = (tmp_path / 'rsi-exam')
    plain.mkdir()
    plain = plain.resolve()
    (plain / 'plan.md').write_text('# plan\n')
    assert isolated_workspace_refusal(str(plain)) is None
    assert (plain / '.git').is_dir() and (plain / 'AGENTS.md').is_file()
    assert (plain / 'plan.md').read_text() == '# plan\n'
    head = subprocess.run(['git', '-C', str(plain), 'rev-parse', 'HEAD:plan.md'],
                          capture_output=True, text=True, timeout=20)
    assert head.returncode == 0
    assert isolated_workspace_refusal(str(plain)) is None  # idempotent


def test_repository_without_commits_gets_its_first_commit(tmp_path):
    root = (tmp_path / 'unborn')
    root.mkdir()
    git(root, 'init', '-q')
    assert isolated_workspace_refusal(str(root.resolve())) is None
    git(root, 'rev-parse', '--verify', 'HEAD^{commit}')


def test_a_root_tofu_cannot_prepare_carries_a_bilingual_remedy(tmp_path):
    root = (tmp_path / 'staged')
    root.mkdir()
    git(root, 'init', '-q')
    (root / 'draft.md').write_text('draft\n')
    git(root, 'add', 'draft.md')
    refusal = isolated_workspace_refusal(str(root.resolve()))
    assert isinstance(refusal, IsolationRefusal)
    assert 'staged files but no commits' in refusal.reason
    assert refusal.message() == f'{refusal.reason} {refusal.remedy}'
    hint = refusal.hint()
    assert hint.startswith('解决办法 / How to fix:\n• ') and hint.endswith(refusal.remedy)


def test_subdirectory_symlink_and_linked_worktree_name_the_real_checkout(committed, tmp_path):
    sub = committed / 'pkg'
    sub.mkdir()
    refusal = isolated_workspace_refusal(str(sub))
    assert 'subdirectory of the Git repository' in refusal.reason
    assert f'Open {committed} as the project' in refusal.remedy

    alias = tmp_path / 'alias'
    alias.symlink_to(committed, target_is_directory=True)
    refusal = isolated_workspace_refusal(str(alias))
    assert 'through a symlink' in refusal.reason and str(committed) in refusal.remedy

    linked = tmp_path / 'linked'
    git(committed, 'worktree', 'add', '-q', '-b', 'side', str(linked))
    refusal = isolated_workspace_refusal(str(linked.resolve()))
    assert 'linked Git worktree' in refusal.reason and str(committed) in refusal.remedy


@pytest.mark.parametrize('path', ['remote:host:/srv/project', '/definitely/missing/project'])
def test_missing_or_remote_root_is_refused(path):
    assert 'not an existing local directory' in isolated_workspace_refusal(path).reason


def test_task_start_failure_renders_the_refusal_remedy():
    refusal = isolated_workspace_refusal('/definitely/missing/project')
    error = WorkspaceBindingError(refusal.message(), user_hint=refusal.hint())
    cause = task_start_failure_cause(error)
    assert cause['hint'] == refusal.hint()
    assert cause['detail'].startswith('Project root /definitely/missing/project is not')
    assert task_start_failure_cause(WorkspaceBindingError('plain failure'))['hint'] is None
