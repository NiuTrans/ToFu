"""Executable safety basis for ordinary apply/commit, not a clean-index claim.

Contract: docs/runbooks/GIT_WORKSPACE_GUARD.md. Each repository is private;
no command touches the application's checkout or its index.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from lib.project_mod.command_analysis import _git_guard_classify
from lib.project_mod.run_command import command_write_lease_claims

pytestmark = pytest.mark.unit

PATCH = (
    'diff --git a/one.txt b/one.txt\n'
    '--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-old-one\n+new-one\n'
    'diff --git a/two.txt b/two.txt\n'
    '--- a/two.txt\n+++ b/two.txt\n@@ -1 +1 @@\n-old-two\n+new-two\n'
)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    env = {
        **os.environ, 'HOME': str(tmp_path), 'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_AUTHOR_NAME': 'test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
        'GIT_COMMITTER_NAME': 'test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
    }
    for key in list(env):
        if key in {'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'}:
            env.pop(key)

    def git(*args, check=True, input=None):
        return subprocess.run(
            ['git', *args], cwd=root, env=env, input=input,
            text=True, capture_output=True, check=check, timeout=10,
        )

    # Portable to older deployed Git versions without `init -b`.
    git('init', '--quiet')
    git('symbolic-ref', 'HEAD', 'refs/heads/main')
    (root / 'one.txt').write_text('old-one\n')
    (root / 'two.txt').write_text('old-two\n')
    git('add', 'one.txt', 'two.txt')
    git('commit', '--quiet', '-m', 'base')
    return root, git


@pytest.mark.parametrize('arguments', [
    ('--check',), ('--stat',), ('--numstat',), ('--summary',),
    ('--check', '--index'), ('--check', '--cached'),
])
def test_apply_reports_leave_files_index_and_head_unchanged(repo, arguments):
    root, git = repo
    before_head = git('rev-parse', 'HEAD').stdout
    before_index = (root / '.git' / 'index').read_bytes()
    git('apply', *arguments, input=PATCH)
    assert (root / 'one.txt').read_text() == 'old-one\n'
    assert (root / 'two.txt').read_text() == 'old-two\n'
    assert (root / '.git' / 'index').read_bytes() == before_index
    assert git('rev-parse', 'HEAD').stdout == before_head


@pytest.mark.parametrize('arguments', [(), ('--cached',), ('--index',)])
def test_default_apply_conflict_does_not_partially_apply_other_file(repo, arguments):
    root, git = repo
    (root / 'two.txt').write_text('foreign-work\n')
    if '--cached' in arguments:
        git('add', 'two.txt')
    before_index = (root / '.git' / 'index').read_bytes()
    before_head = git('rev-parse', 'HEAD').stdout
    failed = git('apply', *arguments, input=PATCH, check=False)
    assert failed.returncode != 0
    assert (root / 'one.txt').read_text() == 'old-one\n'
    assert (root / 'two.txt').read_text() == 'foreign-work\n'
    assert (root / '.git' / 'index').read_bytes() == before_index
    assert git('rev-parse', 'HEAD').stdout == before_head


def test_cached_apply_preserves_worktree_and_normal_commit_appends_history(repo):
    root, git = repo
    previous = git('rev-parse', 'HEAD').stdout.strip()
    git('apply', '--cached', input=PATCH)
    assert (root / 'one.txt').read_text() == 'old-one\n'
    assert git('show', ':one.txt').stdout == 'new-one\n'
    git('commit', '--quiet', '-m', 'append patch')
    assert git('rev-parse', 'HEAD^').stdout.strip() == previous
    assert git('merge-base', '--is-ancestor', previous, 'HEAD').returncode == 0
    assert git('show', f'{previous}:one.txt').stdout == 'old-one\n'


@pytest.mark.parametrize('command', [
    'git apply patch.diff', 'git apply --cached patch.diff',
    'git apply --index patch.diff', 'git commit -m append',
    'git apply patch.diff && git add one.txt && git commit -m append',
])
def test_ordinary_write_classification_still_requires_checkout_lease(repo, command):
    root, _git = repo
    assert _git_guard_classify(command) == 'safe_write'
    assert ('root', str(root)) in command_write_lease_claims(str(root), command)


@pytest.mark.parametrize('command', [
    'git apply patch.diff', 'git apply --cached patch.diff',
    'git apply --index patch.diff', 'git apply --stat --apply patch.diff',
    'git commit -m append', 'git apply --stat "$(touch marker)"',
    'git apply --check `touch marker`',
])
def test_apply_write_and_dynamic_reports_are_refused_in_readonly_root(repo, monkeypatch, command):
    from lib.project_mod import config, tools
    root, _git = repo
    monkeypatch.setattr(config, 'is_readonly_path', lambda *args, **kwargs: True)
    def no_spawn(*args, **kwargs):
        pytest.fail('readonly refusal must precede command execution')
    monkeypatch.setattr(tools, '_exec_run_command_with_writer', no_spawn)
    result = tools._exec_run_command(
        {'command': command}, str(root), '', '', {},
    )
    assert 'READ-ONLY' in result
    assert not (root / 'marker').exists()


@pytest.mark.parametrize('command', [
    'git apply --check patch.diff', 'git apply --stat patch.diff',
])
def test_literal_reports_retain_read_authorization_without_expanding_lease_free_grammar(
        repo, monkeypatch, command):
    from lib.project_mod import config, tools
    from lib.project_mod.command_analysis import _lease_free_git_command
    root, _git = repo
    monkeypatch.setattr(config, 'is_readonly_path', lambda *args, **kwargs: True)
    monkeypatch.setattr(tools, '_exec_run_command_with_writer', lambda *args, **kwargs: 'admitted')
    result = tools._exec_run_command({'command': command}, str(root), '', '', {})
    assert result == 'admitted'
    assert _lease_free_git_command(command) is None
