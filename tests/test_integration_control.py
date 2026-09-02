"""Focused Git-level tests for the token-free integration control plane."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lib import integration_control as control
from lib.storage import StorageSupervisor
from lib.storage.runtime import StorageRuntime
from lib.storage.service import install_runtime_for_test

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
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'worktrees'))
    monkeypatch.setenv('TOFU_INTEGRATION_AUTORUN', '0')
    monkeypatch.delenv('TOFU_INTEGRATION_TEST_CMD', raising=False)
    monkeypatch.delenv('TOFU_INTEGRATION_STABLE_TEST_CMD', raising=False)
    control._STATUS_CACHE.clear()
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    runtime = StorageRuntime(supervisor=supervisor, auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    try:
        yield repo
    finally:
        install_runtime_for_test(None)


def _worktree(repo: Path, path: Path) -> Path:
    _run(repo, 'git', 'worktree', 'add', '--detach', str(path), 'HEAD')
    return path


def _row(repo: Path, task_id: str) -> dict:
    status = control.integration_status(str(repo), user_id=1, use_cache=False)
    return next(item for item in status['workspaces']
                if item['taskId'] == task_id)


def test_gate_argv_expands_immutable_sha_placeholders_without_shell() -> None:
    assert control._gate_argv(
        'python3 scripts/test_select.py --base {base} --target {target}',
        'abc123', 'def456') == [
            'python3', 'scripts/test_select.py', '--base', 'abc123',
            '--target', 'def456',
        ]


def test_checkpoint_captures_untracked_without_touching_writer_index(
        repository: Path, tmp_path: Path) -> None:
    writer = _worktree(repository, tmp_path / 'writer')
    (writer / 'shared.txt').write_text('writer edit\n', encoding='utf-8')
    (writer / 'new.txt').write_text('untracked\n', encoding='utf-8')
    before = _run(writer, 'git', 'status', '--porcelain=v1')

    control.register_workspace(
        str(repository), 'task-a', str(writer), user_id=1)
    result = control.checkpoint_workspace(
        str(repository), 'task-a', user_id=1)

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
    control.register_workspace(str(repository), 'one', str(first), user_id=1)
    control.register_workspace(str(repository), 'two', str(second), user_id=1)
    control.submit_workspace(str(repository), 'one', user_id=1)
    control.submit_workspace(str(repository), 'two', user_id=1)

    with pytest.raises(control.IntegrationError, match='immutable'):
        control.checkpoint_workspace(str(repository), 'one', user_id=1)
    with pytest.raises(control.IntegrationError, match='Only quarantined'):
        control.retry_workspace(str(repository), 'one', user_id=1)

    assert control.process_ready_once() is True
    assert control.process_ready_once() is True
    assert control.process_ready_once() is False
    status = control.integration_status(
        str(repository), user_id=1, use_cache=False)
    candidate = status['refs']['candidate']
    stable_before = status['refs']['stable']
    assert _run(repository, 'git', 'show', f'{candidate}:one.txt') == 'one'
    assert _run(repository, 'git', 'show', f'{candidate}:two.txt') == 'two'
    assert candidate != stable_before
    assert {item['state'] for item in status['workspaces']} == {'merged'}
    assert all(not item['dirty']['scanned'] for item in status['workspaces']), \
        'terminal history must not launch per-worktree Git scans'

    promoted = control.promote_stable(str(repository), user_id=1)
    assert promoted['stableSha'] == candidate
    assert control.integration_status(
        str(repository), user_id=1,
        use_cache=False)['refs']['candidateAheadStable'] == 0


def test_board_completion_gate_requires_checkpoint_in_candidate(
        repository: Path, tmp_path: Path) -> None:
    ordinary = control.board_completion_gate(
        str(repository), 'ordinary', user_id=1)
    assert ordinary == {
        'ok': True, 'integrationRequired': False, 'state': ''}

    writer = _worktree(repository, tmp_path / 'board-gate-writer')
    control.register_workspace(
        str(repository), 'board-gate', str(writer), user_id=1)
    before = control.board_completion_gate(
        str(repository), 'board-gate', user_id=1)
    assert before['ok'] is False
    assert before['state'] == 'running'

    (writer / 'board-gate.txt').write_text('ready\n', encoding='utf-8')
    control.submit_workspace(str(repository), 'board-gate', user_id=1)
    queued = control.board_completion_gate(
        str(repository), 'board-gate', user_id=1)
    assert queued['ok'] is False
    assert queued['state'] == 'ready'

    assert control.process_ready_once()
    merged = control.board_completion_gate(
        str(repository), 'board-gate', user_id=1)
    assert merged['ok'] is True
    assert merged['state'] == 'merged'


def test_board_origin_auto_completes_only_after_candidate_moves(
        repository: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _worktree(repository, tmp_path / 'board-auto-complete-writer')
    completed: list[tuple[str, str, str, int]] = []

    def record_complete(project_path, conv_id, task_id, *, user_id):
        completed.append((project_path, conv_id, task_id, user_id))
        return {'ok': True}

    monkeypatch.setattr(
        'lib.conversations.project_board.complete_task', record_complete)
    control.register_workspace(
        str(repository), 'board-auto', str(writer), user_id=1,
        origin={
            'source': 'board', 'epicId': 'board-auto', 'convId': 'conv-a',
        })
    (writer / 'board-auto.txt').write_text('ready\n', encoding='utf-8')
    control.submit_workspace(str(repository), 'board-auto', user_id=1)
    assert completed == []

    assert control.process_ready_once()

    assert completed == [
        (str(repository), 'conv-a', 'board-auto', 1),
    ]
    assert _row(repository, 'board-auto')['state'] == 'merged'


def test_prune_removes_only_metadata_for_missing_worktree(
        repository: Path, tmp_path: Path) -> None:
    live = _worktree(repository, tmp_path / 'live-writer')
    stale = _worktree(repository, tmp_path / 'stale-writer')
    shutil.rmtree(stale)

    result = control.prune_worktree_metadata(str(repository), user_id=1)

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
    control.register_workspace(str(repository), 'first', str(first), user_id=1)
    control.register_workspace(str(repository), 'second', str(second), user_id=1)
    control.submit_workspace(str(repository), 'first', user_id=1)
    control.submit_workspace(str(repository), 'second', user_id=1)

    control.process_ready_once()
    after_first = control.integration_status(
        str(repository), user_id=1, use_cache=False)
    stable = after_first['refs']['stable']
    control.process_ready_once()
    after_second = control.integration_status(
        str(repository), user_id=1, use_cache=False)

    assert _row(repository, 'first')['state'] == 'merged'
    quarantined = _row(repository, 'second')
    assert quarantined['state'] == 'quarantined'
    assert quarantined['error']
    assert after_second['refs']['stable'] == stable
    assert _run(repository, 'git', 'show',
                f"refs/tofu/quarantine/u1/"
                f"{control._safe_task('second')}:shared.txt") == 'second'


def test_quarantined_repair_reanchors_checkpoint_to_moved_writer_head(
        repository: Path, tmp_path: Path) -> None:
    first = _worktree(repository, tmp_path / 'first-reanchor')
    second = _worktree(repository, tmp_path / 'second-reanchor')
    (first / 'shared.txt').write_text('first\n', encoding='utf-8')
    (second / 'shared.txt').write_text('second\n', encoding='utf-8')
    control.register_workspace(
        str(repository), 'first-reanchor', str(first), user_id=1)
    control.register_workspace(
        str(repository), 'second-reanchor', str(second), user_id=1)
    control.submit_workspace(str(repository), 'first-reanchor', user_id=1)
    control.submit_workspace(str(repository), 'second-reanchor', user_id=1)
    assert control.process_ready_once()
    assert control.process_ready_once()
    assert _row(repository, 'second-reanchor')['state'] == 'quarantined'

    candidate = control.integration_status(
        str(repository), user_id=1, use_cache=False)['refs']['candidate']
    _run(second, 'git', 'reset', '--hard', candidate)
    (second / 'shared.txt').write_text('resolved\n', encoding='utf-8')

    submitted = control.submit_workspace(
        str(repository), 'second-reanchor', user_id=1)
    assert submitted['reanchored'] is True
    assert _run(
        repository, 'git', 'merge-base', '--is-ancestor',
        candidate, submitted['checkpointSha']) == ''
    assert control.process_ready_once()
    repaired = _row(repository, 'second-reanchor')
    assert repaired['state'] == 'merged'
    assert repaired['baseSha'] == candidate


def test_merged_workspace_is_terminal(
        repository: Path, tmp_path: Path) -> None:
    writer = _worktree(repository, tmp_path / 'terminal-writer')
    (writer / 'done.txt').write_text('done\n', encoding='utf-8')
    control.register_workspace(
        str(repository), 'terminal', str(writer), user_id=1)
    control.submit_workspace(str(repository), 'terminal', user_id=1)
    assert control.process_ready_once()
    assert _row(repository, 'terminal')['state'] == 'merged'

    with pytest.raises(control.IntegrationError, match='merged'):
        control.checkpoint_workspace(str(repository), 'terminal', user_id=1)
    with pytest.raises(control.IntegrationError, match='merged'):
        control.submit_workspace(str(repository), 'terminal', user_id=1)
    with pytest.raises(Exception, match='terminal'):
        control.discard_workspace(str(repository), 'terminal', user_id=1)
    with pytest.raises(Exception, match='immutable or terminal'):
        control.register_workspace(
            str(repository), 'terminal', str(writer), user_id=1)


def test_head_candidate_divergence_requires_explicit_promotion_ack(
        repository: Path, tmp_path: Path) -> None:
    writer = _worktree(repository, tmp_path / 'diverged-writer')
    (writer / 'candidate.txt').write_text('candidate\n', encoding='utf-8')
    control.register_workspace(
        str(repository), 'diverged', str(writer), user_id=1)
    control.submit_workspace(str(repository), 'diverged', user_id=1)
    assert control.process_ready_once()

    (repository / 'canonical.txt').write_text('canonical\n', encoding='utf-8')
    _run(repository, 'git', 'add', 'canonical.txt')
    _run(repository, 'git', 'commit', '-m', 'canonical advances independently')
    status = control.integration_status(
        str(repository), user_id=1, use_cache=False)
    assert status['refs']['headCandidateDiverged'] is True
    assert status['refs']['headAheadCandidate'] == 1
    assert status['refs']['candidateAheadHead'] == 1
    assert any('HEAD and candidate have diverged' in warning
               for warning in status['warnings'])

    with pytest.raises(control.IntegrationError, match='explicitly acknowledge'):
        control.promote_stable(str(repository), user_id=1)
    promoted = control.promote_stable(
        str(repository), user_id=1, acknowledge_head_divergence=True)
    assert promoted['headDiverged'] is True


def test_reconcile_committed_head_into_candidate_under_gate(
        repository: Path, tmp_path: Path) -> None:
    writer = _worktree(repository, tmp_path / 'head-reconcile-writer')
    (writer / 'candidate.txt').write_text('candidate\n', encoding='utf-8')
    control.register_workspace(
        str(repository), 'head-reconcile', str(writer), user_id=1)
    control.submit_workspace(
        str(repository), 'head-reconcile', user_id=1)
    assert control.process_ready_once()
    before = control.integration_status(
        str(repository), user_id=1, use_cache=False)
    candidate_before = before['refs']['candidate']
    stable_before = before['refs']['stable']

    (repository / 'canonical.txt').write_text('not committed\n', encoding='utf-8')
    with pytest.raises(control.IntegrationError, match='dirty'):
        control.reconcile_candidate_with_head(
            str(repository), user_id=1)
    assert control.integration_status(
        str(repository), user_id=1,
        use_cache=False)['refs']['candidate'] == candidate_before

    _run(repository, 'git', 'add', 'canonical.txt')
    _run(repository, 'git', 'commit', '-m', 'canonical commit to reconcile')
    reconciled = control.reconcile_candidate_with_head(
        str(repository), user_id=1)
    assert reconciled['changed'] is True
    assert reconciled['mergeCommit'] is True
    assert _run(
        repository, 'git', 'show',
        f"{reconciled['candidateSha']}:candidate.txt") == 'candidate'
    assert _run(
        repository, 'git', 'show',
        f"{reconciled['candidateSha']}:canonical.txt") == 'not committed'

    after = control.integration_status(
        str(repository), user_id=1, use_cache=False)
    assert after['refs']['headCandidateDiverged'] is False
    assert after['refs']['headAheadCandidate'] == 0
    assert after['refs']['stable'] == stable_before
    again = control.reconcile_candidate_with_head(
        str(repository), user_id=1)
    assert again['changed'] is False
    assert again['headAlreadyContained'] is True


def test_reconcile_head_conflict_does_not_move_candidate(
        repository: Path, tmp_path: Path) -> None:
    writer = _worktree(repository, tmp_path / 'head-conflict-writer')
    (writer / 'shared.txt').write_text('candidate version\n', encoding='utf-8')
    control.register_workspace(
        str(repository), 'head-conflict', str(writer), user_id=1)
    control.submit_workspace(
        str(repository), 'head-conflict', user_id=1)
    assert control.process_ready_once()
    candidate_before = control.integration_status(
        str(repository), user_id=1, use_cache=False)['refs']['candidate']

    (repository / 'shared.txt').write_text('canonical version\n', encoding='utf-8')
    _run(repository, 'git', 'add', 'shared.txt')
    _run(repository, 'git', 'commit', '-m', 'conflicting canonical commit')
    with pytest.raises(control.IntegrationError, match='conflicted'):
        control.reconcile_candidate_with_head(
            str(repository), user_id=1)
    assert control.integration_status(
        str(repository), user_id=1,
        use_cache=False)['refs']['candidate'] == candidate_before


def test_declared_write_set_is_enforced_before_candidate_moves(
        repository: Path, tmp_path: Path) -> None:
    writer = _worktree(repository, tmp_path / 'write-set-writer')
    (writer / 'outside.txt').write_text('outside\n', encoding='utf-8')
    control.register_workspace(
        str(repository), 'write-set', str(writer), user_id=1,
        origin={'writeSet': ['allowed/']})
    control.submit_workspace(str(repository), 'write-set', user_id=1)
    candidate_before = control.integration_status(
        str(repository), user_id=1, use_cache=False)['refs']['candidate']

    assert control.process_ready_once()
    row = _row(repository, 'write-set')
    assert row['state'] == 'quarantined'
    assert 'outside the epic declared write-set' in row['error']
    assert control.integration_status(
        str(repository), user_id=1,
        use_cache=False)['refs']['candidate'] == candidate_before


def test_board_write_set_update_replaces_active_integration_scope(
        repository: Path, tmp_path: Path) -> None:
    writer = _worktree(repository, tmp_path / 'write-set-update-writer')
    control.register_workspace(
        str(repository), 'write-set-update', str(writer), user_id=1,
        origin={'source': 'board', 'writeSet': ['old/**']})

    result = control.update_workspace_write_set(
        str(repository), 'write-set-update',
        ['new/**', 'new/**', ' tests/*.py '], user_id=1)

    assert result['updated'] is True
    assert result['writeSet'] == ['new/**', 'tests/*.py']
    row = control._state.get_workspace(
        str(repository), 'write-set-update', user_id=1)
    assert row['origin']['source'] == 'board'
    assert row['origin']['writeSet'] == ['new/**', 'tests/*.py']


def test_forbidden_dependency_path_is_quarantined(
        repository: Path, tmp_path: Path) -> None:
    writer = _worktree(repository, tmp_path / 'forbidden-writer')
    (writer / 'node_modules').symlink_to(tmp_path)
    control.register_workspace(
        str(repository), 'forbidden', str(writer), user_id=1)
    control.submit_workspace(str(repository), 'forbidden', user_id=1)

    assert control.process_ready_once()
    row = _row(repository, 'forbidden')
    assert row['state'] == 'quarantined'
    assert 'forbidden dependency/generated/runtime paths' in row['error']
    assert 'node_modules' in row['error']


def test_forbidden_dependency_path_can_be_removed_as_remediation(
        repository: Path, tmp_path: Path) -> None:
    dependency_dir = repository / 'node_modules'
    dependency_dir.mkdir()
    legacy = dependency_dir / 'legacy.txt'
    legacy.write_text('historical dependency artifact\n', encoding='utf-8')
    _run(repository, 'git', 'add', 'node_modules/legacy.txt')
    _run(repository, 'git', 'commit', '-m', 'seed historical forbidden path')

    writer = _worktree(repository, tmp_path / 'forbidden-removal-writer')
    (writer / 'node_modules' / 'legacy.txt').unlink()
    control.register_workspace(
        str(repository), 'forbidden-removal', str(writer), user_id=1)
    control.submit_workspace(
        str(repository), 'forbidden-removal', user_id=1)

    assert control.process_ready_once()
    row = _row(repository, 'forbidden-removal')
    assert row['state'] == 'merged'
    candidate = control.integration_status(
        str(repository), user_id=1, use_cache=False)['refs']['candidate']
    missing = subprocess.run(
        ['git', 'cat-file', '-e', f'{candidate}:node_modules/legacy.txt'],
        cwd=str(repository), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False)
    assert missing.returncode != 0


def test_semantic_code_change_requires_project_gate_command(
        repository: Path, tmp_path: Path) -> None:
    writer = _worktree(repository, tmp_path / 'semantic-gate-writer')
    (writer / 'logic.py').write_text('VALUE = 1\n', encoding='utf-8')
    control.register_workspace(
        str(repository), 'semantic-gate', str(writer), user_id=1)
    control.submit_workspace(str(repository), 'semantic-gate', user_id=1)

    assert control.process_ready_once()
    row = _row(repository, 'semantic-gate')
    assert row['state'] == 'quarantined'
    assert 'TOFU_INTEGRATION_TEST_CMD' in row['error']
    required = control.integration_status(
        str(repository), user_id=1,
        use_cache=False)['gates']['projectGateRequiredSuffixes']
    assert '.py' in required and '.css' in required and '.json' in required


def test_configured_project_gate_accepts_semantic_change(
        repository: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _worktree(repository, tmp_path / 'configured-gate-writer')
    (writer / 'logic.py').write_text('VALUE = 2\n', encoding='utf-8')
    monkeypatch.setenv(
        'TOFU_INTEGRATION_TEST_CMD',
        f'{sys.executable} -m py_compile logic.py')
    control.register_workspace(
        str(repository), 'configured-gate', str(writer), user_id=1)
    control.submit_workspace(
        str(repository), 'configured-gate', user_id=1)

    assert control.process_ready_once()
    assert _row(repository, 'configured-gate')['state'] == 'merged'


def test_stable_promotion_reuses_project_gate_when_release_gate_is_unset(
        repository: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _worktree(repository, tmp_path / 'promotion-gate-writer')
    (writer / 'logic.py').write_text('VALUE = 4\n', encoding='utf-8')
    monkeypatch.setenv(
        'TOFU_INTEGRATION_TEST_CMD', f'{sys.executable} -c pass')
    control.register_workspace(
        str(repository), 'promotion-gate', str(writer), user_id=1)
    control.submit_workspace(
        str(repository), 'promotion-gate', user_id=1)
    assert control.process_ready_once()

    before = control.integration_status(
        str(repository), user_id=1, use_cache=False)
    assert before['gates']['testCommandConfigured'] is True
    assert before['gates']['stableCommandConfigured'] is False
    promoted = control.promote_stable(str(repository), user_id=1)
    assert promoted['stableSha'] == before['refs']['candidate']


def test_stable_promotion_prefers_configured_release_gate(
        repository: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _worktree(repository, tmp_path / 'release-gate-writer')
    (writer / 'logic.py').write_text('VALUE = 5\n', encoding='utf-8')
    monkeypatch.setenv(
        'TOFU_INTEGRATION_TEST_CMD', f'{sys.executable} -c pass')
    monkeypatch.setenv(
        'TOFU_INTEGRATION_STABLE_TEST_CMD',
        f'{sys.executable} -c "import sys; sys.exit(7)"')
    control.register_workspace(
        str(repository), 'release-gate', str(writer), user_id=1)
    control.submit_workspace(
        str(repository), 'release-gate', user_id=1)
    assert control.process_ready_once()
    before = control.integration_status(
        str(repository), user_id=1, use_cache=False)

    with pytest.raises(control.IntegrationError, match='promotion gate failed'):
        control.promote_stable(str(repository), user_id=1)
    after = control.integration_status(
        str(repository), user_id=1, use_cache=False)
    assert after['refs']['stable'] == before['refs']['stable']
    assert after['refs']['candidate'] == before['refs']['candidate']


def test_builtin_javascript_gate_accepts_browser_esm(
        repository: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _worktree(repository, tmp_path / 'browser-esm-writer')
    (writer / 'dependency.js').write_text(
        'export default 42;\n', encoding='utf-8')
    (writer / 'browser.js').write_text(
        "import value from './dependency.js';\nexport default value;\n",
        encoding='utf-8')
    monkeypatch.setenv(
        'TOFU_INTEGRATION_TEST_CMD', f'{sys.executable} -c pass')
    control.register_workspace(
        str(repository), 'browser-esm', str(writer), user_id=1)
    control.submit_workspace(str(repository), 'browser-esm', user_id=1)

    assert control.process_ready_once()
    assert _row(repository, 'browser-esm')['state'] == 'merged'


def test_builtin_javascript_gate_rejects_invalid_browser_esm(
        repository: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _worktree(repository, tmp_path / 'invalid-browser-esm-writer')
    (writer / 'browser.js').write_text(
        'export default ; )\n', encoding='utf-8')
    monkeypatch.setenv(
        'TOFU_INTEGRATION_TEST_CMD', f'{sys.executable} -c pass')
    control.register_workspace(
        str(repository), 'invalid-browser-esm', str(writer), user_id=1)
    control.submit_workspace(
        str(repository), 'invalid-browser-esm', user_id=1)

    assert control.process_ready_once()
    row = _row(repository, 'invalid-browser-esm')
    assert row['state'] == 'quarantined'
    assert 'browser.js' in row['error']
    assert 'SyntaxError' in row['error']


def test_malformed_project_gate_command_quarantines_with_configuration_error(
        repository: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _worktree(repository, tmp_path / 'malformed-gate-writer')
    (writer / 'logic.py').write_text('VALUE = 3\n', encoding='utf-8')
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'python3 "unterminated')
    control.register_workspace(
        str(repository), 'malformed-gate', str(writer), user_id=1)
    control.submit_workspace(str(repository), 'malformed-gate', user_id=1)

    assert control.process_ready_once()
    row = _row(repository, 'malformed-gate')
    assert row['state'] == 'quarantined'
    assert 'No closing quotation' in row['error']


def test_git_211_scratch_merge_fallback_preserves_both_trees(
        repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _worktree(repository, tmp_path / 'legacy-first')
    second = _worktree(repository, tmp_path / 'legacy-second')
    (first / 'one.txt').write_text('one\n', encoding='utf-8')
    (second / 'two.txt').write_text('two\n', encoding='utf-8')
    control.register_workspace(
        str(repository), 'legacy-one', str(first), user_id=1)
    control.register_workspace(
        str(repository), 'legacy-two', str(second), user_id=1)
    control.submit_workspace(str(repository), 'legacy-one', user_id=1)
    control.submit_workspace(str(repository), 'legacy-two', user_id=1)
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
        str(repository), user_id=1, use_cache=False)['refs']['candidate']
    assert _run(repository, 'git', 'show', f'{candidate}:one.txt') == 'one'
    assert _run(repository, 'git', 'show', f'{candidate}:two.txt') == 'two'


def test_peek_walks_to_git_root_when_called_from_a_subdirectory(
        repository: Path, tmp_path: Path) -> None:
    """Regression pin for the 2026-08-20 review finding: get_workspace
    RAISES IntegrationStateError on a missing row, and the peek loop's
    except-Exception-return-None bailed on the FIRST candidate — the .git
    parent walk was dead code, so a board path that is a subdirectory (or
    symlink) of the repo silently found no workspace and dispatch downgraded
    an isolated epic to shared-tree work on the canonical checkout."""
    writer = _worktree(repository, tmp_path / 'writer')
    control.register_workspace(
        str(repository), 'epic-subdir', str(writer), user_id=1)

    nested = repository / 'nested' / 'deeper'
    nested.mkdir(parents=True)
    row = control.peek_workspace_for_epic(
        str(nested), 'epic-subdir', user_id=1)
    assert row is not None, 'peek must walk to the git toplevel anchor'
    assert row['task_id'] == 'epic-subdir'
    assert row['workspace_path'] == str(writer)

    # A genuine miss still returns None (both candidates miss cleanly).
    assert control.peek_workspace_for_epic(
        str(nested), 'no-such-epic', user_id=1) is None


def test_peek_reports_only_active_writer_states(
        repository: Path, tmp_path: Path) -> None:
    """A ready/merged workspace is sealed or already integrated — injecting
    the ISOLATED brief for it would send a re-dispatched assignee to edit an
    immutable checkout.  Only running/checkpointed rows mean \"an isolated
    writer owns this epic\"."""
    writer = _worktree(repository, tmp_path / 'writer-states')
    control.register_workspace(
        str(repository), 'epic-states', str(writer), user_id=1)
    row = control.peek_workspace_for_epic(
        str(repository), 'epic-states', user_id=1)
    assert row is not None and row['state'] == 'running'

    control.submit_workspace(str(repository), 'epic-states', user_id=1)
    assert _row(repository, 'epic-states')['state'] == 'ready'
    assert control.peek_workspace_for_epic(
        str(repository), 'epic-states', user_id=1) is None

    # Discard is terminal. More work must use a new task id so history remains
    # append-only and an operator action cannot be silently undone.
    control.discard_workspace(str(repository), 'epic-states', user_id=1)
    assert control.peek_workspace_for_epic(
        str(repository), 'epic-states', user_id=1) is None
    with pytest.raises(Exception, match='terminal'):
        control.register_workspace(
            str(repository), 'epic-states', str(writer), user_id=1)
    control.register_workspace(
        str(repository), 'epic-states-v2', str(writer), user_id=1)
    row = control.peek_workspace_for_epic(
        str(repository), 'epic-states-v2', user_id=1)
    assert row is not None and row['state'] == 'running'


def test_worker_loop_survives_transient_storage_error(monkeypatch):
    """A transient StorageError in claim_next must not kill the poll thread.

    The 2026-08-18 incident: `Storage writer acquisition timed out` escaped
    ``process_ready_once`` (``_claim_next`` is outside its try/except) and the
    'tofu-integration' background thread died permanently. The loop must warn,
    back off, and keep polling.
    """
    from lib.storage import StorageError

    calls = {'n': 0}

    def fake_process_ready_once():
        calls['n'] += 1
        if calls['n'] == 1:
            raise StorageError(
                'database_unavailable',
                'Storage writer acquisition timed out', retryable=True)
        return False

    autorun = iter([True, True, False])
    monkeypatch.setattr(control, '_autorun_enabled', lambda: next(autorun, False))
    monkeypatch.setattr(control, 'process_ready_once', fake_process_ready_once)
    # The peek gate (2026-08-19) consults the storage read pool before the
    # full claim; stub it claimable so the poll reaches process_ready_once
    # without a live store.
    monkeypatch.setattr(control, '_peek_ready', lambda: {'id': 1})
    waits = []
    monkeypatch.setattr(
        control, '_wait_for_worker',
        lambda delay, *_args, **_kwargs: waits.append(delay) or (True, False))

    control._worker_loop()

    assert calls['n'] == 2, 'the loop must keep polling after a StorageError'
    assert waits and waits[0] >= 5.0, 'must back off before retrying'


def test_worker_loop_survives_unexpected_error(monkeypatch):
    calls = {'n': 0}

    def fake_process_ready_once():
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('boom')
        return False

    autorun = iter([True, True, False])
    monkeypatch.setattr(control, '_autorun_enabled', lambda: next(autorun, False))
    monkeypatch.setattr(control, 'process_ready_once', fake_process_ready_once)
    monkeypatch.setattr(control, '_peek_ready', lambda: {'id': 1})
    waits = []
    monkeypatch.setattr(
        control, '_wait_for_worker',
        lambda delay, *_args, **_kwargs: waits.append(delay) or (True, False))

    control._worker_loop()

    assert calls['n'] == 2
    assert waits, 'an unexpected error must also back off, not kill the thread'


def test_worker_idle_poll_does_not_repeat_writer_claim(monkeypatch):
    monkeypatch.delenv('TOFU_INTEGRATION_IDLE_POLL_BASE_SECONDS', raising=False)
    monkeypatch.delenv('TOFU_INTEGRATION_IDLE_POLL_MAX_SECONDS', raising=False)
    calls = {'n': 0}
    autorun = iter([True, True, False])
    monkeypatch.setattr(control, '_autorun_enabled', lambda: next(autorun, False))
    monkeypatch.setattr(
        control, 'process_ready_once',
        lambda: calls.__setitem__('n', calls['n'] + 1) or False)
    monkeypatch.setattr(control, '_peek_ready', lambda: None)
    waits = []
    monkeypatch.setattr(
        control, '_wait_for_worker',
        lambda delay, *_args, **_kwargs: waits.append(delay) or (True, False))

    control._worker_loop()

    assert calls['n'] == 1, \
        'boot may recover once, but an idle read-only poll must not fsync again'
    assert waits == [3.0, 6.0]


def test_worker_idle_poll_budget_backs_off_without_delaying_local_wakes(
        monkeypatch):
    monkeypatch.delenv('TOFU_INTEGRATION_IDLE_POLL_BASE_SECONDS', raising=False)
    monkeypatch.delenv('TOFU_INTEGRATION_IDLE_POLL_MAX_SECONDS', raising=False)

    delays = [control._idle_poll_bounds()[0]]
    for _ in range(5):
        delays.append(control._next_idle_poll_delay(delays[-1]))

    assert delays == [3.0, 6.0, 12.0, 24.0, 48.0, 60.0]
    old_empty_queries_per_day = 86_400 / 3
    new_empty_queries_per_day = 86_400 / delays[-1]
    assert new_empty_queries_per_day <= 1_440
    assert 1 - new_empty_queries_per_day / old_empty_queries_per_day >= 0.95


def test_worker_start_stop_is_bounded_and_releases_exact_owner(monkeypatch):
    monkeypatch.setattr(control, '_WORKER', None)
    monkeypatch.setattr(control, '_WORKER_STOP_REQUESTED', False)
    monkeypatch.setattr(control, '_WORKER_WAKE_GENERATION', 0)
    monkeypatch.setattr(control, '_WORKER_AUTHORITY_ARMED', True)
    monkeypatch.setattr(control, '_autorun_enabled', lambda: True)
    monkeypatch.setattr(control, 'process_ready_once', lambda: False)
    monkeypatch.setattr(control, '_peek_ready', lambda: None)

    assert control._start_or_wake_worker() is True
    worker = control._WORKER
    assert worker is not None and worker.is_alive()
    assert control.stop_worker(timeout=1.0) is True
    assert control._WORKER is None


def test_worker_stop_timeout_retains_owner_and_blocks_duplicate(monkeypatch):
    class StuckWorker:
        def __init__(self):
            self.joined = []

        def is_alive(self):
            return True

        def join(self, timeout):
            self.joined.append(timeout)

    worker = StuckWorker()
    monkeypatch.setattr(control, '_WORKER', worker)
    monkeypatch.setattr(control, '_WORKER_STOP_REQUESTED', False)
    monkeypatch.setattr(control, '_WORKER_WAKE_GENERATION', 0)
    monkeypatch.setattr(control, '_WORKER_AUTHORITY_ARMED', True)

    assert control.stop_worker(timeout=0.125) is False
    assert worker.joined == [0.125]
    assert control._WORKER is worker
    assert control._WORKER_STOP_REQUESTED is True
    assert control._start_or_wake_worker() is False


def test_unarmed_api_process_cannot_start_integration_worker(monkeypatch):
    monkeypatch.setattr(control, '_WORKER', None)
    monkeypatch.setattr(control, '_WORKER_STOP_REQUESTED', False)
    monkeypatch.setattr(control, '_WORKER_AUTHORITY_ARMED', False)
    monkeypatch.setattr(control, '_autorun_enabled', lambda: True)

    assert control._start_or_wake_worker() is False
    assert control._WORKER is None


def test_local_submit_wakes_live_authorized_worker_without_restarting(
        monkeypatch):
    class LiveWorker:
        def is_alive(self):
            return True

    worker = LiveWorker()
    monkeypatch.setattr(control, '_WORKER', worker)
    monkeypatch.setattr(control, '_WORKER_STOP_REQUESTED', False)
    monkeypatch.setattr(control, '_WORKER_WAKE_GENERATION', 7)
    monkeypatch.setattr(control, '_WORKER_AUTHORITY_ARMED', True)
    monkeypatch.setattr(control, '_autorun_enabled', lambda: True)

    assert control._start_or_wake_worker() is True
    assert control._WORKER is worker
    assert control._WORKER_WAKE_GENERATION == 8


def test_status_does_not_start_or_initialize_the_worker(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_started() -> bool:
        raise AssertionError('status must remain read-only')

    monkeypatch.setattr(control, 'ensure_worker_started', fail_if_started)

    status = control.integration_status(
        str(repository), user_id=1, use_cache=False)

    assert status['ok'] is True


def test_candidate_cas_requeue_invalidates_status_and_pushes(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _worktree(repository, tmp_path / 'cas-writer')
    control.register_workspace(
        str(repository), 'epic-cas', str(writer), user_id=1)
    (writer / 'notes.txt').write_text('candidate race\n', encoding='utf-8')
    control.submit_workspace(str(repository), 'epic-cas', user_id=1)
    claimed = control._claim_next()
    assert claimed is not None

    real_git = control._git

    def fail_candidate_cas(cwd, args, **kwargs):
        if args[:2] == ['update-ref', control._CANDIDATE_REF]:
            return subprocess.CompletedProcess(
                ['git', *args], returncode=1, stdout='', stderr='moved')
        return real_git(cwd, args, **kwargs)

    pushed: list[tuple[str, int]] = []
    monkeypatch.setattr(control, '_git', fail_candidate_cas)
    monkeypatch.setattr(
        control, '_push',
        lambda project_root, *, user_id: pushed.append((project_root, user_id)),
    )

    control._integrate_row(claimed)

    row = control._state.get_workspace(
        str(repository), 'epic-cas', user_id=1)
    assert row['state'] == 'ready'
    assert pushed == [(str(repository), 1)]
