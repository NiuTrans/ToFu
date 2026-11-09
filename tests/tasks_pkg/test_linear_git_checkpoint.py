"""Task-end workspace Git checkpoint behavior.

The checkpoint is deliberately downstream of tool execution. These tests pin
dirty-baseline adoption, linear commits, bounded Git-only serialization,
concurrent-byte preservation, and the invariant that checkpoint failures never
reject or rewrite a project tool result.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from lib import linear_git_checkpoint as checkpoint

# serial: git serialization timing under parallel lanes — timing/resource-sensitive and cannot safely
# overlap the parallel unit lane (TESTING_STRATEGY serial policy).
pytestmark = [pytest.mark.unit, pytest.mark.serial]

_AUDIT_SYNTHETIC_REPO_PATHS = {'tests/test_calc.py'}
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['git', '-c', 'user.name=test', '-c', 'user.email=test@tofu.local',
         *args],
        cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True,
    )


def _task(task_id: str, *, status: str = 'done', error: str = '',
          aborted: bool = False) -> dict:
    return {
        'id': task_id, 'convId': f'conv-{task_id}', '_userId': 7,
        'status': status, 'error': error, 'aborted': aborted,
        'finishReason': 'error' if error else 'stop',
    }


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'README.md').write_text('base\n', encoding='utf-8')
    (repo / 'app.py').write_text('VALUE = 1\n', encoding='utf-8')
    _git(repo, 'init', '-q')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'base')
    monkeypatch.setenv('TOFU_LINEAR_GIT_CHECKPOINT', '1')
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_ROOT', str(repo))
    monkeypatch.setenv(
        'TOFU_LINEAR_GIT_CHECKPOINT_LOCK_WAIT_SECONDS', '2')
    monkeypatch.delenv(
        'TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD', raising=False)
    monkeypatch.delenv('TOFU_INTEGRATION_TEST_CMD', raising=False)
    lock_root = tmp_path / 'locks'

    def _private_lock_path(root: Path) -> Path:
        lock_root.mkdir(parents=True, exist_ok=True)
        return lock_root / f'{root.name}.lock'

    monkeypatch.setattr(checkpoint, '_lock_path', _private_lock_path)
    return repo


def _settle(repo: Path, task: dict) -> dict | None:
    return checkpoint.settle_task_checkpoint(
        task, user_id=7, project_path=str(repo), project_paths=None)


def _head(repo: Path) -> str:
    return _git(repo, 'rev-parse', 'HEAD').stdout.strip()


def _stable(repo: Path) -> str:
    return _git(repo, 'rev-parse', checkpoint.STABLE_REF).stdout.strip()


@pytest.mark.parametrize('state,eligible,reason', [
    ('fresh', True, 'initial linear baseline activation'),
    ('owned', True, 'owns stable at HEAD'),
    ('candidate', False, 'Integration candidate exists'),
    ('unowned-stable', False, 'no linear baseline ownership'),
    ('missing-stable', False, 'stable ref is missing'),
    ('stable-behind', True, 'owns stable behind HEAD'),
    ('stable-diverged', False, 'Stable is outside HEAD history'),
    ('foreign-baseline', False, 'outside the linear baseline history'),
    ('rewritten-history', True, 'unrelated history will be retired'),
    ('detached', False, 'Detached HEAD'),
    ('unborn', False, 'Unborn HEAD'),
])
def test_publication_mode_resolution_is_read_only(repository, state, eligible, reason):
    head = _head(repository)
    _git(repository, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    if state in {'owned', 'candidate', 'stable-behind', 'stable-diverged', 'foreign-baseline'}:
        for ref in (checkpoint.STABLE_REF, checkpoint.CHECKPOINT_BASELINE_REF):
            _git(repository, 'update-ref', ref, head)
    tree = _git(repository, 'rev-parse', 'HEAD^{tree}').stdout.strip()
    if state == 'candidate':
        _git(repository, 'update-ref', 'refs/tofu/candidate', head)
    elif state == 'unowned-stable':
        _git(repository, 'update-ref', checkpoint.STABLE_REF, head)
    elif state == 'missing-stable':
        _git(repository, 'update-ref', checkpoint.CHECKPOINT_BASELINE_REF, head)
    elif state == 'stable-behind':
        (repository / 'README.md').write_text('external commit\n')
        _git(repository, 'add', 'README.md')
        _git(repository, 'commit', '-qm', 'external head advance')
    elif state == 'stable-diverged':
        side = _git(repository, 'commit-tree', tree, '-p', head, '-m', 'side stable').stdout.strip()
        _git(repository, 'update-ref', checkpoint.STABLE_REF, side)
    elif state == 'foreign-baseline':
        # Diverged but related: shared history keeps the fail-closed decision.
        foreign = _git(repository, 'commit-tree', tree, '-p', head, '-m', 'foreign baseline').stdout.strip()
        _git(repository, 'update-ref', checkpoint.CHECKPOINT_BASELINE_REF, foreign)
    elif state == 'rewritten-history':
        abandoned = _git(repository, 'commit-tree', tree, '-m', 'abandoned history').stdout.strip()
        for ref in ('refs/tofu/candidate', checkpoint.STABLE_REF, checkpoint.CHECKPOINT_BASELINE_REF):
            _git(repository, 'update-ref', ref, abandoned)
    elif state == 'detached':
        _git(repository, 'update-ref', '--no-deref', 'HEAD', head)
    elif state == 'unborn':
        _git(repository, 'symbolic-ref', 'HEAD', 'refs/heads/unborn')
    (repository / 'app.py').write_text('staged WIP\n')
    _git(repository, 'add', 'app.py')
    (repository / 'app.py').write_text('unstaged WIP\n')
    (repository / 'untracked.txt').write_text('preserve user bytes\n')
    before = (_git(repository, 'for-each-ref').stdout,
              (repository / '.git/config').read_bytes(),
              (repository / '.git/HEAD').read_bytes(),
              (repository / '.git/index').read_bytes())
    assert checkpoint.is_enabled(repository) is True

    resolved = checkpoint.resolve_publication_mode(repository)

    assert resolved['requested'] is True and resolved['eligible'] is eligible
    assert resolved['mode'] == ('linear-checkpoint' if eligible else 'isolated-task')
    assert reason in resolved['reason'] and len(resolved['reason']) <= 500
    assert (_git(repository, 'for-each-ref').stdout,
            (repository / '.git/config').read_bytes(),
            (repository / '.git/HEAD').read_bytes(),
            (repository / '.git/index').read_bytes()) == before
    assert (repository / 'app.py').read_text() == 'unstaged WIP\n'
    assert (repository / 'untracked.txt').read_text() == 'preserve user bytes\n'


@pytest.mark.parametrize('failure', [
    'read-error', 'incomplete-config', 'incomplete-refs', 'incomplete-head', 'unknown-mode',
])
def test_publication_mode_resolution_refuses_unknown_probes(repository, monkeypatch, failure):
    monkeypatch.delenv('TOFU_LINEAR_GIT_CHECKPOINT')
    _git(repository, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    original_git = checkpoint._git
    def failing_probe(root, args, **kwargs):
        if failure == 'read-error' and args[0] == 'for-each-ref':
            return subprocess.CompletedProcess(args, 128, '', 'cannot read refs')
        if failure == 'incomplete-config' and args[0] == 'config':
            return subprocess.CompletedProcess(args, 0, '', '')
        if failure == 'incomplete-refs' and args[0] == 'for-each-ref':
            return subprocess.CompletedProcess(args, 0, 'incomplete refs', '')
        if failure == 'incomplete-head' and args[:2] == ['rev-parse', '--verify']:
            return subprocess.CompletedProcess(args, 0, '', '')
        return original_git(root, args, **kwargs)
    monkeypatch.setattr(checkpoint, '_git', failing_probe)
    if failure == 'unknown-mode':
        monkeypatch.setenv('TOFU_LINEAR_GIT_CHECKPOINT', 'unknown')
    with pytest.raises(checkpoint.LinearCheckpointError, match='Cannot resolve publication mode'):
        checkpoint.resolve_publication_mode(repository)


def test_preexisting_dirty_workspace_is_checkpointed_without_admission(
        repository: Path):
    task = _task('task-dirty-start')
    base = _head(repository)
    (repository / 'README.md').write_text('pre-existing\n', encoding='utf-8')

    result = _settle(repository, task)

    assert result and result['status'] == 'committed'
    row = result['repositories'][0]
    assert row['baseSha'] == base
    assert row['checkpointSha'] == _head(repository)
    assert row['verification'] == 'passed'
    assert row['stableUpdated'] is True
    assert _stable(repository) == _head(repository)
    assert checkpoint._revision(
        repository, checkpoint.CHECKPOINT_BASELINE_REF) == base
    assert _git(repository, 'status', '--porcelain').stdout == ''


def test_rewritten_history_refs_are_archived_and_checkpoints_resume(repository: Path):
    tree = _git(repository, 'rev-parse', 'HEAD^{tree}').stdout.strip()
    abandoned = _git(repository, 'commit-tree', tree, '-m', 'abandoned history').stdout.strip()
    stranded = ('refs/tofu/candidate', checkpoint.STABLE_REF, checkpoint.CHECKPOINT_BASELINE_REF)
    for ref in stranded:
        _git(repository, 'update-ref', ref, abandoned)
    base = _head(repository)
    (repository / 'README.md').write_text('after the rewrite\n', encoding='utf-8')

    result = _settle(repository, _task('task-after-rewrite'))

    row = result['repositories'][0]
    assert result['status'] == 'committed' and row['stableUpdated'] is True
    assert row['baseSha'] == base and _stable(repository) == _head(repository) == row['checkpointSha']
    assert checkpoint._revision(repository, checkpoint.CHECKPOINT_BASELINE_REF) == base
    assert checkpoint._revision(repository, 'refs/tofu/candidate') == ''
    archived = _git(repository, 'for-each-ref', '--format=%(refname) %(objectname)',
                    'refs/tofu/retired').stdout.split('\n')
    assert sorted(line.split('/', 4)[-1] for line in archived if line) == sorted(
        f"{ref.removeprefix('refs/tofu/')} {abandoned}" for ref in stranded)


def test_stable_trailing_ordinary_commits_still_publishes(repository: Path):
    first = _settle(repository, _task('task-activate'))
    assert first['status'] == 'no_changes'
    activated = _stable(repository)
    (repository / 'README.md').write_text('landed by a merge\n', encoding='utf-8')
    _git(repository, 'commit', '-qam', 'ordinary commit outside checkpoints')
    advanced = _head(repository)
    (repository / 'NOTES.md').write_text('uncommitted work\n', encoding='utf-8')

    result = _settle(repository, _task('task-after-commit'))

    row = result['repositories'][0]
    assert row['baseSha'] == advanced != activated
    assert row['verification'] == 'passed' and row['stableUpdated'] is True
    assert _stable(repository) == _head(repository) == row['checkpointSha']
    assert _git(repository, 'merge-base', '--is-ancestor', advanced, row['checkpointSha']).returncode == 0


def test_stable_outside_head_history_still_fails_closed(repository: Path):
    head = _head(repository)
    tree = _git(repository, 'rev-parse', 'HEAD^{tree}').stdout.strip()
    side = _git(repository, 'commit-tree', tree, '-p', head, '-m', 'side stable').stdout.strip()
    _git(repository, 'update-ref', checkpoint.CHECKPOINT_BASELINE_REF, head)
    _git(repository, 'update-ref', checkpoint.STABLE_REF, side)
    (repository / 'README.md').write_text('pending\n', encoding='utf-8')

    result = _settle(repository, _task('task-diverged'))

    row = result['repositories'][0]
    assert row['status'] == 'error' and 'outside HEAD history' in row['verificationDetail']
    assert _head(repository) == head and _stable(repository) == side
    assert (repository / 'README.md').read_text() == 'pending\n'


def test_semantic_change_is_committed_but_stable_waits_for_gate(
        repository: Path):
    task = _task('task-code-no-gate')
    base = _head(repository)
    (repository / 'app.py').write_text('VALUE = 2\n', encoding='utf-8')

    result = _settle(repository, task)

    row = result['repositories'][0]
    assert row['status'] == 'committed'
    assert row['verification'] == 'required'
    assert row['stableUpdated'] is False
    assert _head(repository) == base
    assert _stable(repository) == base
    assert 'app.py' in _git(repository, 'status', '--porcelain').stdout
    assert checkpoint._revision(repository, row['checkpointRef']) == \
        row['checkpointSha']


def test_configured_gate_promotes_exact_semantic_checkpoint(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        'TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD',
        f'{sys.executable} -c pass',
    )
    (repository / 'app.py').write_text('VALUE = 3\n', encoding='utf-8')

    result = _settle(repository, _task('task-code-green'))

    row = result['repositories'][0]
    assert row['verification'] == 'passed'
    assert row['stableUpdated'] is True
    assert _stable(repository) == _head(repository)


def test_docs_task_cannot_publish_earlier_unverified_code(repository: Path):
    baseline = _head(repository)
    (repository / 'app.py').write_text('VALUE = 9\n', encoding='utf-8')
    code_result = _settle(repository, _task('task-unverified-code'))
    assert code_result['repositories'][0]['verification'] == 'required'
    assert _stable(repository) == baseline

    (repository / 'README.md').write_text('later docs\n', encoding='utf-8')
    docs_result = _settle(repository, _task('task-docs-after-code'))

    assert docs_result['repositories'][0]['verification'] == 'required'
    assert docs_result['repositories'][0]['stableUpdated'] is False
    assert _stable(repository) == baseline


def test_gate_rewrite_is_refused_and_never_reaches_the_checkout(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    """The gate runs on a private checkout of the captured bytes: a command that
    rewrites tracked files there invalidates its own verification, and the
    shared checkout never sees the rewrite."""
    command = (
        f'{sys.executable} -c "from pathlib import Path; '
        "Path('README.md').write_text('gate rewrite\\n')\""
    )
    monkeypatch.setenv('TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD', command)
    baseline = _head(repository)
    (repository / 'app.py').write_text('VALUE = 4\n', encoding='utf-8')

    result = _settle(repository, _task('task-mutating-gate'))

    row = result['repositories'][0]
    assert row['verification'] == 'workspace_changed'
    assert 'rewrote tracked files' in row['verificationDetail']
    assert _stable(repository) == baseline == _head(repository)
    assert (repository / 'README.md').read_text(encoding='utf-8') == 'base\n'
    assert _git(repository, 'status', '--porcelain').stdout == ' M app.py\n'


def test_unrelated_head_commit_is_carried_forward_during_capture(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    real_stage = checkpoint._stage_working_tree
    moved = False

    def _stage_then_move_head(root: Path, base_sha: str):
        nonlocal moved
        captured = real_stage(root, base_sha)
        if not moved:
            moved = True
            (root / 'landed.txt').write_text('LANDED = True\n')
            _git(root, 'add', 'landed.txt')
            _git(root, 'commit', '-qm', 'unrelated landing')
        return captured

    monkeypatch.setattr(checkpoint, '_stage_working_tree', _stage_then_move_head)
    (repository / 'README.md').write_text('ordinary commit\n')
    _git(repository, 'add', 'README.md')
    _git(repository, 'commit', '-m', 'ordinary index against original HEAD')
    (repository / 'README.md').write_text('checkpoint bytes\n')

    result = _settle(repository, _task('task-unrelated-head-move'))

    row = result['repositories'][0]
    assert row['status'] == 'committed'
    assert row['verification'] == 'passed', repr(row)
    assert row['stableUpdated'] is True
    assert _head(repository) == _stable(repository) == row['checkpointSha']
    assert _git(repository, 'log', '-1', '--format=%s', 'HEAD^').stdout == 'unrelated landing\n'
    assert _git(repository, 'show', 'HEAD:README.md').stdout == 'checkpoint bytes\n'
    assert _git(repository, 'show', 'HEAD:landed.txt').stdout == 'LANDED = True\n'
    assert _git(repository, 'status', '--porcelain').stdout == ''


def test_conflicting_head_commit_refuses_capture_and_preserves_workspace(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    real_stage = checkpoint._stage_working_tree
    moved = False

    def _stage_then_move_head(root: Path, base_sha: str):
        nonlocal moved
        captured = real_stage(root, base_sha)
        if not moved:
            moved = True
            (root / 'README.md').write_text('concurrent landing\n')
            _git(root, 'add', 'README.md')
            _git(root, 'commit', '-qm', 'conflicting landing')
            (root / 'README.md').write_text('checkpoint bytes\n')
        return captured

    monkeypatch.setattr(checkpoint, '_stage_working_tree', _stage_then_move_head)
    (repository / 'README.md').write_text('checkpoint bytes\n')

    result = _settle(repository, _task('task-conflicting-head-move'))

    row = result['repositories'][0]
    assert row['status'] == 'error'
    assert 'README.md' in row['verificationDetail']
    assert _git(repository, 'log', '-1', '--format=%s').stdout == 'conflicting landing\n'
    assert _git(repository, 'show', 'HEAD:README.md').stdout == 'concurrent landing\n'
    assert (repository / 'README.md').read_text() == 'checkpoint bytes\n'
    assert checkpoint._revision(repository, checkpoint.STABLE_REF) != _head(repository)


def test_bytes_written_during_capture_stay_dirty_for_the_next_checkpoint(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    """A concurrent write never blocks publication: the captured bytes publish,
    and bytes written after capture stay dirty until the next checkpoint."""
    monkeypatch.setenv(
        'TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD',
        f'{sys.executable} -c pass',
    )
    real_stage = checkpoint._stage_working_tree
    mutated = False

    def _stage_then_write(root: Path, base_sha: str):
        nonlocal mutated
        captured = real_stage(root, base_sha)
        if not mutated:
            mutated = True
            (root / 'README.md').write_text(
                'written after capture\n', encoding='utf-8')
        return captured

    monkeypatch.setattr(checkpoint, '_stage_working_tree', _stage_then_write)
    (repository / 'app.py').write_text('VALUE = 5\n', encoding='utf-8')

    first = _settle(repository, _task('task-concurrent-capture'))

    row = first['repositories'][0]
    assert row['status'] == 'committed'
    assert row['verification'] == 'passed' and row['stableUpdated'] is True
    assert _head(repository) == _stable(repository) == row['checkpointSha']
    assert _git(repository, 'show', 'HEAD:app.py').stdout == 'VALUE = 5\n'
    assert _git(repository, 'show', 'HEAD:README.md').stdout == 'base\n'
    assert (repository / 'README.md').read_text(encoding='utf-8') == \
        'written after capture\n'
    assert _git(repository, 'status', '--porcelain').stdout == ' M README.md\n'

    monkeypatch.setattr(checkpoint, '_stage_working_tree', real_stage)
    second = _settle(repository, _task('task-capture-residue'))
    assert second['repositories'][0]['verification'] == 'passed'
    assert _git(repository, 'show', 'HEAD:README.md').stdout == \
        'written after capture\n'
    assert _git(repository, 'status', '--porcelain').stdout == ''


def test_writes_during_verification_never_block_publication(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    """Other writers keep editing the shared checkout while the gate runs."""
    monkeypatch.setenv(
        'TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD', f'{sys.executable} -c pass')
    real_gate = checkpoint._run_verification_gate

    def _gate_while_a_writer_edits(root, base, probe, paths, task):
        (root / 'NOTES.md').write_text('a writer keeps working\n', encoding='utf-8')
        (root / 'app.py').write_text('VALUE = 7\n', encoding='utf-8')
        return real_gate(root, base, probe, paths, task)

    monkeypatch.setattr(checkpoint, '_run_verification_gate', _gate_while_a_writer_edits)
    (repository / 'app.py').write_text('VALUE = 6\n', encoding='utf-8')

    row = _settle(repository, _task('task-busy-checkout'))['repositories'][0]

    assert row['verification'] == 'passed' and row['stableUpdated'] is True
    assert _git(repository, 'show', 'HEAD:app.py').stdout == 'VALUE = 6\n'
    assert (repository / 'app.py').read_text(encoding='utf-8') == 'VALUE = 7\n'
    assert _git(repository, 'status', '--porcelain').stdout == ' M app.py\n?? NOTES.md\n'


def test_gate_covers_the_captured_delta_not_the_whole_stable_lag(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    """Commits landed since stable arrived with their own verification; the gate
    sees only what this checkpoint adds, so a lagging stable never grows it."""
    assert _settle(repository, _task('task-activate'))['status'] == 'no_changes'
    (repository / 'legacy.py').write_text('LEGACY = True\n', encoding='utf-8')
    _git(repository, 'add', 'legacy.py')
    _git(repository, 'commit', '-qm', 'landed after stable')
    probe = ("import subprocess, sys; changed = subprocess.run(['git', 'diff', "
             "'--name-only', sys.argv[1], sys.argv[2]], capture_output=True, text=True, "
             "check=True).stdout.split(); sys.exit(0 if changed == ['app.py'] else 1)")
    monkeypatch.setenv('TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD',
                       f'{sys.executable} -c "{probe}" {{base}} {{target}}')
    (repository / 'app.py').write_text('VALUE = 10\n', encoding='utf-8')

    row = _settle(repository, _task('task-after-landing'))['repositories'][0]

    assert row['verification'] == 'passed', row['verificationDetail']
    assert row['stableUpdated'] is True
    assert _stable(repository) == _head(repository) == row['checkpointSha']


def test_branch_moved_over_other_files_during_verification_is_carried(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    real_gate = checkpoint._run_verification_gate

    def _gate_while_a_landing_arrives(root, base, probe, paths, task):
        verdict = real_gate(root, base, probe, paths, task)
        (root / 'landed.txt').write_text('LANDED = True\n', encoding='utf-8')
        _git(root, 'add', 'landed.txt')
        _git(root, 'commit', '-qm', 'landing during verification')
        return verdict

    monkeypatch.setattr(checkpoint, '_run_verification_gate', _gate_while_a_landing_arrives)
    (repository / 'README.md').write_text('checkpoint bytes\n', encoding='utf-8')

    row = _settle(repository, _task('task-carried'))['repositories'][0]

    assert row['verification'] == 'passed' and row['stableUpdated'] is True
    assert row['publishedSha'] == _head(repository) == _stable(repository)
    assert row['publishedSha'] != row['checkpointSha']
    assert _git(repository, 'log', '-1', '--format=%s', 'HEAD^').stdout == \
        'landing during verification\n'
    assert _git(repository, 'show', 'HEAD:README.md').stdout == 'checkpoint bytes\n'
    assert _git(repository, 'status', '--porcelain').stdout == ''


def test_branch_moved_over_the_same_file_defers_to_the_next_checkpoint(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    real_gate = checkpoint._run_verification_gate
    landed = False

    def _gate_while_a_landing_rewrites_the_file(root, base, probe, paths, task):
        nonlocal landed
        verdict = real_gate(root, base, probe, paths, task)
        if not landed:
            landed = True
            (root / 'README.md').write_text('landed version\n', encoding='utf-8')
            _git(root, 'add', 'README.md')
            _git(root, 'commit', '-qm', 'landing over the same file')
            (root / 'README.md').write_text('checkpoint bytes\n', encoding='utf-8')
        return verdict

    monkeypatch.setattr(checkpoint, '_run_verification_gate',
                        _gate_while_a_landing_rewrites_the_file)
    (repository / 'README.md').write_text('checkpoint bytes\n', encoding='utf-8')

    first = _settle(repository, _task('task-overlap'))['repositories'][0]

    assert first['verification'] == 'workspace_changed'
    assert 'README.md' in first['verificationDetail']
    assert first['stableUpdated'] is False
    assert _git(repository, 'show', 'HEAD:README.md').stdout == 'landed version\n'
    assert (repository / 'README.md').read_text(encoding='utf-8') == 'checkpoint bytes\n'

    second = _settle(repository, _task('task-after-overlap'))['repositories'][0]
    assert second['verification'] == 'passed' and second['stableUpdated'] is True
    assert _git(repository, 'show', 'HEAD:README.md').stdout == 'checkpoint bytes\n'
    assert _git(repository, 'status', '--porcelain').stdout == ''


def test_external_head_move_skips_real_index_sync_without_losing_commit(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    real_git = checkpoint._git
    external_commit_created = False

    def _git_then_external_commit(root, args, **kwargs):
        nonlocal external_commit_created
        result = real_git(root, args, **kwargs)
        if (
            not external_commit_created
            and args[:2] == ['update-ref', '-m']
            and '--stdin' in args
            and 'tofu: verified linear checkpoint' in args
            and result.returncode == 0
        ):
            external_commit_created = True
            parent = _head(Path(root))
            tree = _git(Path(root), 'rev-parse', 'HEAD^{tree}').stdout.strip()
            external = _git(Path(root), 'commit-tree', tree, '-p', parent,
                            '-m', 'external concurrent commit').stdout.strip()
            _git(Path(root), 'update-ref', 'HEAD', external, parent)
        return result

    monkeypatch.setattr(checkpoint, '_git', _git_then_external_commit)
    (repository / 'README.md').write_text('checkpoint bytes\n', encoding='utf-8')

    result = _settle(repository, _task('task-head-race'))

    row = result['repositories'][0]
    assert row['status'] == 'committed'
    assert row['indexSynchronized'] is False
    assert row['verification'] == 'workspace_changed'
    assert _git(repository, 'log', '-1', '--format=%s').stdout == \
        'external concurrent commit\n'
    assert _git(repository, 'show', 'HEAD^:README.md').stdout == \
        'checkpoint bytes\n'
    assert _git(repository, 'show', ':README.md').stdout == 'base\n'
    assert (repository / 'README.md').read_text() == 'checkpoint bytes\n'


def test_failed_task_is_preserved_as_wip_without_promoting_stable(
        repository: Path):
    task = _task('task-failed', status='error', error='model failed')
    base = _head(repository)
    (repository / 'README.md').write_text('unfinished\n', encoding='utf-8')

    result = _settle(repository, task)

    row = result['repositories'][0]
    assert row['status'] == 'committed'
    assert row['verification'] == 'task_failed'
    assert _stable(repository) == base
    assert _head(repository) == base
    assert 'README.md' in _git(repository, 'status', '--porcelain').stdout
    assert 'Tofu WIP checkpoint: task-failed' in _git(
        repository, 'show', '-s', '--format=%B', row['checkpointSha']).stdout


def test_concurrent_settlements_serialize_git_not_project_writers(
        repository: Path):
    (repository / 'README.md').write_text('shared snapshot\n', encoding='utf-8')
    barrier = threading.Barrier(3)
    results: list[dict] = []

    def _run(task_id: str) -> None:
        barrier.wait()
        result = _settle(repository, _task(task_id))
        assert result is not None
        results.append(result)

    threads = [
        threading.Thread(target=_run, args=('task-one',)),
        threading.Thread(target=_run, args=('task-two',)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        # FUSE checkouts make each serialized git settlement take seconds;
        # 10 s of join budget only fits an idle tmpfs host.
        thread.join(timeout=60)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    rows = [result['repositories'][0] for result in results]
    assert any(row['verification'] == 'passed' for row in rows)
    assert all(row['status'] in ('committed', 'deferred', 'no_changes') for row in rows)
    assert _git(repository, 'rev-list', '--count', 'HEAD').stdout.strip() == '2'
    assert _git(repository, 'status', '--porcelain').stdout == ''


def test_busy_checkpoint_lock_defers_without_touching_workspace(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        'TOFU_LINEAR_GIT_CHECKPOINT_LOCK_WAIT_SECONDS', '0')
    lock_path = checkpoint._lock_path(repository)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, 'a+b')
    assert checkpoint._try_os_lock(handle)
    base = _head(repository)
    (repository / 'README.md').write_text('still available\n', encoding='utf-8')
    try:
        result = _settle(repository, _task('task-busy-lock'))
    finally:
        checkpoint._unlock_os(handle)

    row = result['repositories'][0]
    assert result['status'] == 'deferred'
    assert row['status'] == 'deferred'
    assert _head(repository) == base
    assert (repository / 'README.md').read_text(encoding='utf-8') == \
        'still available\n'

    followup = _settle(repository, _task('task-after-busy-lock'))
    assert followup['repositories'][0]['status'] == 'committed'
    assert _git(repository, 'status', '--porcelain').stdout == ''


def test_forbidden_path_failure_is_receipt_only_and_preserves_bytes(
        repository: Path):
    runtime_file = repository / 'data' / 'runtime.json'
    runtime_file.parent.mkdir()
    runtime_file.write_text('{}\n', encoding='utf-8')
    task = _task('task-forbidden')

    result = _settle(repository, task)

    assert result['status'] == 'error'
    assert result['repositories'][0]['status'] == 'error'
    assert task['status'] == 'done'
    assert runtime_file.read_text(encoding='utf-8') == '{}\n'
    assert 'data/' in _git(repository, 'status', '--porcelain').stdout


def test_settlement_exception_never_rewrites_task_outcome(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    task = _task('task-checkpoint-error')
    (repository / 'README.md').write_text('keep me\n', encoding='utf-8')
    monkeypatch.setattr(
        checkpoint, '_capture_repository',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError('checkpoint exploded')),
    )

    result = _settle(repository, task)

    assert result['status'] == 'error'
    assert task['status'] == 'done'
    assert task['error'] == ''
    assert (repository / 'README.md').read_text(encoding='utf-8') == 'keep me\n'


def test_tool_pipeline_executes_write_even_if_checkpoint_module_is_broken(
        monkeypatch: pytest.MonkeyPatch):
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    from tests._registered_chat_task import registered_chat_task

    broken_checkpoint_module = types.ModuleType('lib.linear_git_checkpoint')

    def _forbid_checkpoint_access(name: str):
        raise AssertionError(
            f'project tool dispatch accessed checkpoint member {name!r}')

    broken_checkpoint_module.__getattr__ = _forbid_checkpoint_access
    monkeypatch.setitem(
        sys.modules, 'lib.linear_git_checkpoint', broken_checkpoint_module)
    executions: list[str] = []

    def _execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                 cfg, project_path, project_enabled, all_tools=None):
        executions.append(fn_name)
        _finalize_tool_round(
            task, rn, round_entry,
            [{'toolName': fn_name, 'title': fn_name,
              'snippet': 'written', 'source': 'Test'}])
        return tc_id, 'written', False

    monkeypatch.setattr(pipeline, '_execute_tool_one', _execute)
    round_entry = {
        'roundNum': 1, 'llmRound': 1, 'toolName': 'write_file',
        'toolCallId': 'call-write', 'query': 'write_file',
        'status': 'searching',
    }
    parsed = [(
        {'id': 'call-write'}, 'write_file', 'call-write',
        {'path': 'app.py', 'content': 'VALUE = 2\n'},
        1, round_entry, None,
    )]
    task = {
        'id': 'task-pipeline-no-git-gate',
        'convId': 'conv-pipeline-no-git-gate', '_userId': 7,
        'status': 'running', 'aborted': False,
        'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [round_entry], 'model': 'test-model',
        '_attended': False,
    }
    messages: list[dict] = []
    with registered_chat_task(task, user_id=7):
        pipeline.execute_tool_pipeline(
            task, parsed, cfg={'autoApply': True}, project_path='/project',
            project_enabled=True, tool_list=None, messages=messages,
            all_search_results_text=[], round_num=0, model='test-model')

    assert executions == ['write_file']
    assert round_entry['status'] == 'done'
    assert '_rejected' not in round_entry
    tool_contents = [message['content'] for message in messages
                     if message.get('role') == 'tool']
    assert len(tool_contents) == 1
    assert tool_contents[0] == 'written'


def test_first_settlement_does_not_advance_preexisting_stable(
        repository: Path):
    older_stable = _head(repository)
    _git(repository, 'update-ref', checkpoint.STABLE_REF, older_stable)
    (repository / 'README.md').write_text(
        'reviewed baseline\n', encoding='utf-8')
    _git(repository, 'add', 'README.md')
    _git(repository, 'commit', '-qm', 'reviewed baseline')
    clean_head = _head(repository)

    result = _settle(repository, _task('task-activate-linear'))

    assert result and result['status'] == 'error'
    assert _stable(repository) == older_stable
    assert _head(repository) == clean_head
    assert not checkpoint._revision(repository, checkpoint.CHECKPOINT_BASELINE_REF)


def test_syntax_gate_accepts_esm_javascript(repository: Path):
    if not shutil.which('node'):
        pytest.skip('node is not installed')
    (repository / 'module.js').write_text(
        "import value from './value.js';\nexport default value;\n",
        encoding='utf-8',
    )
    (repository / 'value.js').write_text(
        'export default 1;\n', encoding='utf-8')

    assert checkpoint._syntax_check(
        repository, ['module.js', 'value.js']) == (True, '')


def test_unresolved_conflict_marker_stays_only_on_checkpoint_ref(
        repository: Path):
    base = _head(repository)
    (repository / 'README.md').write_text(
        '<<<<<<< Updated upstream\nleft\n=======\nright\n>>>>>>> Stash\n',
        encoding='utf-8')

    result = _settle(repository, _task('task-conflict-marker'))

    row = result['repositories'][0]
    assert row['verification'] == 'failed'
    assert 'conflict marker' in row['verificationDetail']
    assert _head(repository) == base
    assert checkpoint._revision(repository, row['checkpointRef']) == \
        row['checkpointSha']


def test_ruff_redefinition_stays_only_on_checkpoint_ref(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        'TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD',
        f'{sys.executable} -c pass',
    )
    base = _head(repository)
    (repository / 'ruff.toml').write_text(
        'target-version = "py310"\n', encoding='utf-8')
    (repository / 'app.py').write_text(
        'def duplicate():\n    return 1\n\n'
        'def duplicate():\n    return 2\n',
        encoding='utf-8')

    result = _settle(repository, _task('task-ruff-redefinition'))

    row = result['repositories'][0]
    assert row['verification'] == 'failed'
    assert 'F811' in row['verificationDetail']
    assert _head(repository) == base


def test_checkpoint_preserves_preexisting_staged_index(repository):
    (repository / 'README.md').write_text('staged bytes\n')
    _git(repository, 'add', 'README.md')
    before = (repository / '.git' / 'index').read_bytes()
    (repository / 'README.md').write_text('working bytes\n')
    result = _settle(repository, _task('preserve-staged'))
    row = result['repositories'][0]
    assert row['verification'] == 'passed'
    assert row['indexSynchronized'] is False
    assert (repository / '.git' / 'index').read_bytes() == before
    assert _git(repository, 'show', ':README.md').stdout == 'staged bytes\n'
    assert _git(repository, 'show', 'HEAD:README.md').stdout == 'working bytes\n'


def test_failed_publication_never_rolls_back_index(repository, monkeypatch):
    base = _head(repository)
    before = (repository / '.git' / 'index').read_bytes()
    (repository / 'README.md').write_text('checkpoint bytes\n')
    def fail(*args, **kwargs):
        raise checkpoint.LinearCheckpointError('CAS injected failure')
    monkeypatch.setattr(checkpoint, '_publish_verified_refs', fail)
    result = _settle(repository, _task('cas-failure'))
    assert result['repositories'][0]['verification'] == 'failed'
    assert _head(repository) == base
    assert (repository / '.git' / 'index').read_bytes() == before
    assert not (repository / '.git' / 'index.lock').exists()
    assert (repository / 'README.md').read_text() == 'checkpoint bytes\n'


def test_candidate_history_is_not_adopted(repository):
    base = _head(repository)
    _git(repository, 'update-ref', 'refs/tofu/candidate', base)
    (repository / 'README.md').write_text('unpublished\n')
    result = _settle(repository, _task('foreign-candidate'))
    assert result['status'] == 'error'
    assert _head(repository) == base
    assert not checkpoint._revision(repository, checkpoint.STABLE_REF)
    assert not checkpoint._revision(repository, checkpoint.CHECKPOINT_BASELINE_REF)


def test_capture_defers_to_cooperative_tool_writer(repository):
    from lib.project_mod.workspace_lease import workspace_write_lease
    rows = []
    with workspace_write_lease(repository):
        thread = threading.Thread(target=lambda: rows.append(
            checkpoint._capture_repository(_task('busy-writer'), 7, repository)))
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
    assert rows[0]['status'] == 'deferred'
    assert not checkpoint._revision(repository, checkpoint.STABLE_REF)


def test_checkpoint_does_not_follow_ambient_git_index(repository, tmp_path, monkeypatch):
    foreign_index = tmp_path / 'foreign-index'
    foreign_index.write_bytes(b'not an index; must stay untouched')
    monkeypatch.setenv('GIT_INDEX_FILE', str(foreign_index))
    (repository / 'README.md').write_text('explicit checkout\n')
    result = _settle(repository, _task('ambient-index'))
    assert result['repositories'][0]['verification'] == 'passed'
    assert foreign_index.read_bytes() == b'not an index; must stay untouched'


def test_post_publication_index_failure_reports_published_refs(repository, monkeypatch):
    before = (repository / '.git' / 'index').read_bytes()
    (repository / 'README.md').write_text('published\n')
    real_replace = checkpoint.os.replace
    def fail_index(source, destination):
        if Path(destination) == repository / '.git' / 'index':
            raise OSError('injected index install failure')
        return real_replace(source, destination)
    monkeypatch.setattr(checkpoint.os, 'replace', fail_index)
    result = _settle(repository, _task('index-install-failure'))
    row = result['repositories'][0]
    assert row['verification'] == 'failed'
    assert row['stableUpdated'] is True
    assert _head(repository) == _stable(repository) == row['checkpointSha']
    assert (repository / '.git' / 'index').read_bytes() == before
    assert not (repository / '.git' / 'index.lock').exists()


def test_publication_cas_conflict_preserves_both_refs(repository):
    """The stdin batch must not publish HEAD if the stable CAS is rejected."""
    base = _head(repository)
    tree = _git(repository, 'rev-parse', 'HEAD^{tree}').stdout.strip()
    target = _git(repository, 'commit-tree', tree, '-p', base,
                  '-m', 'Unpublished candidate').stdout.strip()
    _git(repository, 'update-ref', checkpoint.STABLE_REF, base)
    branch = _git(repository, 'symbolic-ref', 'HEAD').stdout.strip()
    with pytest.raises(checkpoint.LinearCheckpointError):
        checkpoint._publish_verified_refs(
            repository, branch_ref=branch, base_sha=base,
            stable_sha=target, target_sha=target)
    assert _head(repository) == base
    assert _stable(repository) == base


@pytest.mark.parametrize('key', ['tofu.integrationTestCommand', 'tofu.linearCheckpointTestCommand'])
def test_local_command_promotes_foreign_linear_checkpoint(repository, monkeypatch, key):
    import shlex
    monkeypatch.delenv('TOFU_INTEGRATION_TEST_ROOT', raising=False)
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'missing-global-command')
    _git(repository, 'config', '--local', key, f'{shlex.quote(sys.executable)} -c pass')
    (repository / 'app.py').write_text('VALUE = 7\n')
    row = _settle(repository, _task('local-gate'))['repositories'][0]
    assert row['verification'] == 'passed' and row['stableUpdated']


def test_linear_gate_policy_mutation_refuses_promotion(repository):
    baseline = _head(repository)
    command = 'git config --local tofu.linearCheckpointTestCommand changed-command'
    _git(repository, 'config', '--local', 'tofu.linearCheckpointTestCommand', command)
    (repository / 'app.py').write_text('VALUE = 8\n')
    row = _settle(repository, _task('policy-mutator'))['repositories'][0]
    assert row['verification'] == 'failed'
    assert 'policy changed' in row['verificationDetail']
    assert _stable(repository) == baseline


def test_staged_entry_of_another_writer_never_blocks_other_paths(repository: Path):
    (repository / 'README.md').write_text('staged bytes\n')
    _git(repository, 'add', 'README.md')
    (repository / 'README.md').write_text('working bytes\n')
    (repository / 'NOTES.md').write_text('settled notes\n')

    row = _settle(repository, _task('staged-and-settled'))['repositories'][0]

    assert row['verification'] == 'passed' and row['stableUpdated'] is True
    assert row['indexSynchronized'] is False
    assert _git(repository, 'show', ':README.md').stdout == 'staged bytes\n'
    assert _git(repository, 'show', ':NOTES.md').stdout == 'settled notes\n'
    assert _git(repository, 'show', 'HEAD:NOTES.md').stdout == 'settled notes\n'
    assert _git(repository, 'status', '--porcelain').stdout == 'MM README.md\n'


# The selector landing verification loads from the verified tree
# (lib/project_mod/landing_verification.py): it attributes a failing gate.
SELECTOR = '''\
import ast, os, sys

def build_index(tests_dir, cache_path=None):
    index = {}
    for name in sorted(os.listdir(tests_dir)):
        if name.startswith('test_') and name.endswith('.py'):
            with open(os.path.join(tests_dir, name)) as handle:
                tree = ast.parse(handle.read())
            refs = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    refs.add(node.module.replace('.', '/') + '.py')
            index['tests/' + name] = {'refs': sorted(refs)}
    return index

def select_tests(index, changed):
    chosen = {test: 'direct' for test, record in index.items()
              if test in changed or set(record['refs']) & set(changed)}
    return set(chosen), chosen

def build_pytest_command(existing, jobs):
    return [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', *existing, '-q']
'''


def _commit_python_project(repo: Path, monkeypatch: pytest.MonkeyPatch, tests: str) -> None:
    for relative, text in (('scripts/test_select.py', SELECTOR),
                           ('pkg/calc.py', 'def scale(x):\n    return x * 2\n'),
                           ('tests/test_calc.py', tests)):
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(text, encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'python project')
    monkeypatch.setenv('TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD',
                       f'{sys.executable} -m pytest -q -p no:cacheprovider tests')


def test_gate_failures_already_on_the_base_do_not_block_publication(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    _commit_python_project(repository, monkeypatch,
                           'from pkg.calc import scale\n\ndef test_scale():\n'
                           '    assert scale(2) == 4\n\ndef test_known_broken():\n'
                           '    assert scale(0) == 1\n')
    (repository / 'pkg' / 'calc.py').write_text(
        'def scale(x):\n    return x + x\n', encoding='utf-8')

    row = _settle(repository, _task('task-preexisting-failure'))['repositories'][0]

    assert row['verification'] == 'passed', row['verificationDetail']
    assert 'tests/test_calc.py::test_known_broken' in row['verificationDetail']
    assert row['stableUpdated'] is True
    assert _git(repository, 'show', 'HEAD:pkg/calc.py').stdout == \
        'def scale(x):\n    return x + x\n'


def test_gate_failure_this_checkpoint_causes_blocks_publication(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    _commit_python_project(repository, monkeypatch,
                           'from pkg.calc import scale\n\ndef test_scale():\n'
                           '    assert scale(2) == 4\n')
    base = _head(repository)
    (repository / 'pkg' / 'calc.py').write_text(
        'def scale(x):\n    return x * 3\n', encoding='utf-8')

    row = _settle(repository, _task('task-regression'))['repositories'][0]

    assert row['verification'] == 'failed'
    assert 'tests/test_calc.py::test_scale' in row['verificationDetail']
    assert _head(repository) == base
    assert (repository / 'pkg' / 'calc.py').read_text(encoding='utf-8') == \
        'def scale(x):\n    return x * 3\n'


def test_unfinished_work_of_another_writer_never_blocks_this_tasks_commit(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    from lib.project_mod import integration_radar
    monkeypatch.setenv('TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD', f'{sys.executable} -c pass')
    task = _task('task-own-work')
    (repository / 'app.py').write_text('VALUE = 11\n', encoding='utf-8')
    integration_radar.note_writes(task, str(repository), ['app.py'])
    (repository / 'broken.py').write_text('def unfinished(:\n', encoding='utf-8')

    row = _settle(repository, task)['repositories'][0]

    assert row['verification'] == 'passed', row['verificationDetail']
    assert row['wholeWorkspace']['verification'] == 'failed'
    assert 'broken.py' in row['wholeWorkspace']['verificationDetail']
    assert row['stableUpdated'] is True
    assert _git(repository, 'show', 'HEAD:app.py').stdout == 'VALUE = 11\n'
    assert _git(repository, 'status', '--porcelain').stdout == '?? broken.py\n'


def test_declared_own_paths_narrow_a_failed_capture(repository: Path):
    (repository / 'README.md').write_text('delivered docs\n', encoding='utf-8')
    (repository / 'broken.py').write_text('def unfinished(:\n', encoding='utf-8')

    result = checkpoint.settle_task_checkpoint(
        _task('task-delivered'), user_id=7, project_path=str(repository),
        own_paths=['README.md'])

    row = result['repositories'][0]
    assert row['verification'] == 'passed', row['verificationDetail']
    assert _git(repository, 'show', 'HEAD:README.md').stdout == 'delivered docs\n'
    assert _git(repository, 'status', '--porcelain').stdout == '?? broken.py\n'


def test_own_paths_retry_still_refuses_the_tasks_own_breakage(repository: Path):
    from lib.project_mod import integration_radar
    task = _task('task-own-breakage')
    base = _head(repository)
    (repository / 'app.py').write_text('def broken(:\n', encoding='utf-8')
    integration_radar.note_writes(task, str(repository), ['app.py'])
    (repository / 'NOTES.md').write_text('another writer\n', encoding='utf-8')

    row = _settle(repository, task)['repositories'][0]

    assert row['verification'] == 'failed'
    assert row['wholeWorkspace']['verification'] == 'failed'
    assert _head(repository) == base
    assert (repository / 'app.py').read_text(encoding='utf-8') == 'def broken(:\n'


def test_own_paths_retry_rebuilds_generated_outputs_from_its_own_sources(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    from lib.project_mod import generated_integration, integration_radar
    sections = repository / 'frontend' / 'src' / 'runtime' / 'sections'
    output = repository / 'frontend' / 'src' / 'runtime' / 'app-runtime.js'
    sections.mkdir(parents=True)
    (sections / 'panel.js').write_text('export const panel = 1;\n', encoding='utf-8')
    output.write_text('// composed 1\n', encoding='utf-8')
    _git(repository, 'add', '-A')
    _git(repository, 'commit', '-qm', 'runtime')
    monkeypatch.setenv('TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD', f'{sys.executable} -c pass')
    inputs = []

    def build_from_own_sources(source_root, families, files, *, committed):
        inputs.append((sorted(families), committed, files.get('frontend/src/runtime/sections/panel.js'),
                       'frontend/src/runtime/sections/other.js' in files))
        return {'frontend/src/runtime/app-runtime.js': b'// composed from own sources\n'}

    monkeypatch.setattr(generated_integration, 'build_outputs', build_from_own_sources)
    task = _task('task-runtime')
    (sections / 'panel.js').write_text('export const panel = 2;\n', encoding='utf-8')
    integration_radar.note_writes(task, str(repository), ['frontend/src/runtime/sections/panel.js'])
    (sections / 'other.js').write_text('export const other = 1;\n', encoding='utf-8')
    output.write_text('// composed from every writer\n', encoding='utf-8')
    (repository / 'broken.py').write_text('def unfinished(:\n', encoding='utf-8')

    row = _settle(repository, task)['repositories'][0]

    assert row['verification'] == 'passed', row['verificationDetail']
    assert inputs == [(['runtime'], True, b'export const panel = 2;\n', False)]
    assert _git(repository, 'show', 'HEAD:frontend/src/runtime/app-runtime.js').stdout == \
        '// composed from own sources\n'
    assert _git(repository, 'show', 'HEAD:frontend/src/runtime/sections/panel.js').stdout == \
        'export const panel = 2;\n'
    assert output.read_text(encoding='utf-8') == '// composed from every writer\n'
    assert sorted(_git(repository, 'status', '--porcelain').stdout.splitlines()) == [
        ' M frontend/src/runtime/app-runtime.js', '?? broken.py',
        '?? frontend/src/runtime/sections/other.js']


def test_gate_rewrite_check_outlasts_a_wall_clock_deadline(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
    """`git status` over a large checkout on a loaded volume is slow, not stuck.

    On the 13,000-file shared checkout the gate's rewrite check exceeded a 30 s
    deadline under load, so no checkpoint could publish (2026-11-07 dry run).
    """
    monkeypatch.setenv('TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD', f'{sys.executable} -c pass')
    original = subprocess.run

    def status_deadline_expires(argv, *args, **kwargs):
        if (kwargs.get('timeout') is not None and argv
                and Path(str(argv[0])).name.startswith('git') and 'status' in argv):
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', status_deadline_expires)
    (repository / 'app.py').write_text('VALUE = 13\n', encoding='utf-8')

    row = _settle(repository, _task('task-slow-status'))['repositories'][0]

    assert row['verification'] == 'passed', row['verificationDetail']
    assert row['stableUpdated'] is True
    assert _git(repository, 'show', 'HEAD:app.py').stdout == 'VALUE = 13\n'
