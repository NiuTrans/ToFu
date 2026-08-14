"""Focused Git-level tests for the token-free integration control plane."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lib import integration_control as control

pytestmark = pytest.mark.unit


def _run(cwd: Path, *args: str) -> str:
    cp = subprocess.run(
        list(args), cwd=str(cwd), text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / 'repo'
    repo.mkdir()
    _run(repo, 'git', 'init')
    _run(repo, 'git', 'config', 'user.name', 'Integration Test')
    _run(repo, 'git', 'config', 'user.email', 'integration@test.invalid')
    (repo / 'shared.txt').write_text('base\n', encoding='utf-8')
    _run(repo, 'git', 'add', 'shared.txt')
    _run(repo, 'git', 'commit', '-m', 'base')
    monkeypatch.setenv('TOFU_INTEGRATION_DB', str(tmp_path / 'control.sqlite3'))
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'worktrees'))
    monkeypatch.setenv('TOFU_INTEGRATION_AUTORUN', '0')
    monkeypatch.delenv('TOFU_INTEGRATION_TEST_CMD', raising=False)
    monkeypatch.delenv('TOFU_INTEGRATION_STABLE_TEST_CMD', raising=False)
    control._STATUS_CACHE.clear()
    return repo


def _worktree(repo: Path, path: Path) -> Path:
    _run(repo, 'git', 'worktree', 'add', '--detach', str(path), 'HEAD')
    return path


def _row(repo: Path, task_id: str) -> dict:
    status = control.integration_status(str(repo), use_cache=False)
    return next(item for item in status['workspaces']
                if item['taskId'] == task_id)


def test_checkpoint_captures_untracked_without_touching_writer_index(
        repository: Path, tmp_path: Path) -> None:
    writer = _worktree(repository, tmp_path / 'writer')
    (writer / 'shared.txt').write_text('writer edit\n', encoding='utf-8')
    (writer / 'new.txt').write_text('untracked\n', encoding='utf-8')
    before = _run(writer, 'git', 'status', '--porcelain=v1')

    control.register_workspace(str(repository), 'task-a', str(writer))
    result = control.checkpoint_workspace(str(repository), 'task-a')

    assert _run(writer, 'git', 'status', '--porcelain=v1') == before
    assert _run(writer, 'git', 'diff', '--cached', '--name-only') == ''
    assert _run(repository, 'git', 'show', f"{result['checkpointSha']}:new.txt") == 'untracked'
    assert _run(repository, 'git', 'show', f"{result['checkpointSha']}:shared.txt") == 'writer edit'


def test_disjoint_submissions_merge_then_promote_stable(
        repository: Path, tmp_path: Path) -> None:
    first = _worktree(repository, tmp_path / 'first')
    second = _worktree(repository, tmp_path / 'second')
    (first / 'one.txt').write_text('one\n', encoding='utf-8')
    (second / 'two.txt').write_text('two\n', encoding='utf-8')
    control.register_workspace(str(repository), 'one', str(first))
    control.register_workspace(str(repository), 'two', str(second))
    control.submit_workspace(str(repository), 'one')
    control.submit_workspace(str(repository), 'two')

    with pytest.raises(control.IntegrationError, match='immutable'):
        control.checkpoint_workspace(str(repository), 'one')
    with pytest.raises(control.IntegrationError, match='Only quarantined'):
        control.retry_workspace(str(repository), 'one')

    assert control.process_ready_once() is True
    assert control.process_ready_once() is True
    assert control.process_ready_once() is False
    status = control.integration_status(str(repository), use_cache=False)
    candidate = status['refs']['candidate']
    stable_before = status['refs']['stable']
    assert _run(repository, 'git', 'show', f'{candidate}:one.txt') == 'one'
    assert _run(repository, 'git', 'show', f'{candidate}:two.txt') == 'two'
    assert candidate != stable_before
    assert {item['state'] for item in status['workspaces']} == {'merged'}

    promoted = control.promote_stable(str(repository))
    assert promoted['stableSha'] == candidate
    assert control.integration_status(
        str(repository), use_cache=False)['refs']['candidateAheadStable'] == 0


def test_prune_removes_only_metadata_for_missing_worktree(
        repository: Path, tmp_path: Path) -> None:
    live = _worktree(repository, tmp_path / 'live-writer')
    stale = _worktree(repository, tmp_path / 'stale-writer')
    shutil.rmtree(stale)

    result = control.prune_worktree_metadata(str(repository))

    assert result['removed'] == 1
    assert live.exists()
    inventory = control._worktree_inventory(repository)
    assert str(live) in {item['path'] for item in inventory}
    assert str(stale) not in {item['path'] for item in inventory}


def test_conflict_is_quarantined_without_advancing_stable(
        repository: Path, tmp_path: Path) -> None:
    first = _worktree(repository, tmp_path / 'first-conflict')
    second = _worktree(repository, tmp_path / 'second-conflict')
    (first / 'shared.txt').write_text('first\n', encoding='utf-8')
    (second / 'shared.txt').write_text('second\n', encoding='utf-8')
    control.register_workspace(str(repository), 'first', str(first))
    control.register_workspace(str(repository), 'second', str(second))
    control.submit_workspace(str(repository), 'first')
    control.submit_workspace(str(repository), 'second')

    control.process_ready_once()
    after_first = control.integration_status(str(repository), use_cache=False)
    stable = after_first['refs']['stable']
    control.process_ready_once()
    after_second = control.integration_status(str(repository), use_cache=False)

    assert _row(repository, 'first')['state'] == 'merged'
    quarantined = _row(repository, 'second')
    assert quarantined['state'] == 'quarantined'
    assert quarantined['error']
    assert after_second['refs']['stable'] == stable
    assert _run(repository, 'git', 'show',
                f"refs/tofu/quarantine/{control._safe_task('second')}:shared.txt") == 'second'


def test_git_211_scratch_merge_fallback_preserves_both_trees(
        repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _worktree(repository, tmp_path / 'legacy-first')
    second = _worktree(repository, tmp_path / 'legacy-second')
    (first / 'one.txt').write_text('one\n', encoding='utf-8')
    (second / 'two.txt').write_text('two\n', encoding='utf-8')
    control.register_workspace(str(repository), 'legacy-one', str(first))
    control.register_workspace(str(repository), 'legacy-two', str(second))
    control.submit_workspace(str(repository), 'legacy-one')
    control.submit_workspace(str(repository), 'legacy-two')
    assert control.process_ready_once()

    real_git = control._git

    def legacy_git(cwd, args, **kwargs):
        if args[:2] == ['merge-tree', '--write-tree']:
            return subprocess.CompletedProcess(
                ['git', *args], 129, '', 'error: unknown option `write-tree`')
        return real_git(cwd, args, **kwargs)

    monkeypatch.setattr(control, '_git', legacy_git)
    assert control.process_ready_once()
    candidate = control.integration_status(
        str(repository), use_cache=False)['refs']['candidate']
    assert _run(repository, 'git', 'show', f'{candidate}:one.txt') == 'one'
    assert _run(repository, 'git', 'show', f'{candidate}:two.txt') == 'two'
