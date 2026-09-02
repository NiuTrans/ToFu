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

pytestmark = pytest.mark.unit


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
    assert _head(repository) != base
    assert _stable(repository) == base
    assert _git(repository, 'status', '--porcelain').stdout == ''


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


def test_gate_rewrite_remains_dirty_and_never_moves_stable(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
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
    assert _stable(repository) == baseline
    assert (repository / 'README.md').read_text(encoding='utf-8') == \
        'gate rewrite\n'
    assert 'README.md' in _git(
        repository, 'status', '--porcelain').stdout
    assert _git(repository, 'show', 'HEAD:README.md').stdout == 'base\n'


def test_bytes_written_during_capture_survive_for_next_checkpoint(
        repository: Path, monkeypatch: pytest.MonkeyPatch):
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
    assert row['verification'] == 'workspace_changed'
    assert _git(repository, 'show', 'HEAD:app.py').stdout == 'VALUE = 5\n'
    assert _git(repository, 'show', 'HEAD:README.md').stdout == 'base\n'
    assert (repository / 'README.md').read_text(encoding='utf-8') == \
        'written after capture\n'
    assert 'README.md' in _git(repository, 'status', '--porcelain').stdout

    monkeypatch.setattr(checkpoint, '_stage_working_tree', real_stage)
    second = _settle(repository, _task('task-capture-residue'))
    assert second['repositories'][0]['status'] == 'committed'
    assert _git(repository, 'show', 'HEAD:README.md').stdout == \
        'written after capture\n'
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
            and len(args) > 3
            and str(args[3]).startswith('refs/heads/')
            and result.returncode == 0
        ):
            external_commit_created = True
            (Path(root) / 'external.txt').write_text(
                'external commit\n', encoding='utf-8')
            _git(Path(root), 'add', '-A')
            _git(Path(root), 'commit', '-qm', 'external concurrent commit')
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
    assert _git(repository, 'show', 'HEAD:external.txt').stdout == \
        'external commit\n'
    assert _git(repository, 'status', '--porcelain').stdout == ''


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
    assert _head(repository) != base
    assert _git(repository, 'status', '--porcelain').stdout == ''
    assert 'Tofu WIP checkpoint: task-failed' in _git(
        repository, 'log', '-1', '--format=%B').stdout


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
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    rows = [result['repositories'][0] for result in results]
    assert sorted(row['status'] for row in rows) == ['committed', 'no_changes']
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
    tool_payload = json.loads(tool_contents[0])
    assert tool_payload['status'] == 'ok'
    assert tool_payload['summary'] == 'written'


def test_first_settlement_anchors_current_head_not_older_stable(
        repository: Path):
    older_stable = _head(repository)
    _git(repository, 'update-ref', checkpoint.STABLE_REF, older_stable)
    (repository / 'README.md').write_text(
        'reviewed baseline\n', encoding='utf-8')
    _git(repository, 'add', 'README.md')
    _git(repository, 'commit', '-qm', 'reviewed baseline')
    clean_head = _head(repository)

    result = _settle(repository, _task('task-activate-linear'))

    assert result and result['status'] == 'no_changes'
    assert _stable(repository) == clean_head
    assert checkpoint._revision(
        repository, checkpoint.CHECKPOINT_BASELINE_REF) == clean_head


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
