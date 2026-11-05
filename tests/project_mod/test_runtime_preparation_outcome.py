"""Dependency admission cannot block inspections or invent command side effects."""
from contextlib import contextmanager
import subprocess
import threading
from types import SimpleNamespace

import pytest

from lib.project_mod import workspace_lease, workspace_runtime
from lib.tasks_pkg import workspace_delivery
from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_tool_outcome_finality import dispatch
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task
from tests.tasks_pkg.test_workspace_execution_lifetime import execution_workspace as execution_workspace

pytestmark = pytest.mark.unit

INSPECTION = "git log -1 --format='%H %s'; git branch --show-current; git status --short | head"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    row = {'canonicalRoot': str(tmp_path / 'canonical'), 'workspacePath': str(root)}
    monkeypatch.setattr(workspace_delivery, 'execution_repositories', lambda task: [row])
    monkeypatch.setattr(workspace_delivery, '_persist', lambda *args: None)
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    return root, row


@contextmanager
def writer(root):
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with workspace_lease.workspace_tree_write_lease(root):
                ready.set()
                assert release.wait(15)
        except BaseException as error:
            errors.append(error)
            ready.set()

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5) and not errors
        yield release
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors


def test_git_inspection_skips_runtime_setup_under_active_tree_writer(workspace, monkeypatch):
    root, _ = workspace
    task = {'id': 'inspection', 'aborted': False}
    monkeypatch.setattr(workspace_runtime, 'prepare_workspace_runtime',
                        lambda *a, **k: pytest.fail('Git inspection prepared dependencies'))
    with writer(root):
        workspace_delivery.prepare_worker_command_runtime(task, INSPECTION)
    assert not task.get('_workspaceRuntimePrepared'), 'a skipped setup cannot prepare later builds'


@pytest.mark.parametrize('command', [
    'python3 -m pytest', 'git status --short; python3 -m pytest',
    'git status --short; npm test', 'git status > report.txt', 'git frobnicate',
])
def test_runtime_commands_keep_preparation(workspace, monkeypatch, command):
    root, _ = workspace
    prepared = []
    monkeypatch.setattr(workspace_runtime, 'prepare_workspace_runtime',
                        lambda *a, **k: prepared.append(a) or [])
    task = {'id': 'runtime', 'aborted': False}
    workspace_delivery.prepare_worker_command_runtime(task, command)
    assert prepared and task['_workspaceRuntimePrepared'] == [str(root)]


@pytest.mark.parametrize('failure', ['busy', 'cancelled', 'body'])
def test_runtime_admission_settles_effect_at_the_actual_entry_boundary(workspace, monkeypatch, failure):
    root, _ = workspace
    executed = []

    def prepare(*args, **kwargs):
        if failure == 'body':
            (root / 'partial-runtime').write_text('created before failure')
            raise workspace_lease.WorkspaceBusyError('failure after preparation began')
        pytest.fail('refused admission entered preparation')

    monkeypatch.setattr(workspace_runtime, 'prepare_workspace_runtime', prepare)

    def handler(task, tc, name, call_id, *args):
        if failure == 'cancelled':
            task['aborted'] = True
        workspace_delivery.prepare_worker_command_runtime(task)
        executed.append(call_id)
        return call_id, 'unexpected command execution', False

    task = recovery_task(handler, 'reconcile')
    if failure == 'body':
        row, messages, settled = dispatch(task)
    else:
        with writer(root):
            row, messages, settled = dispatch(task)
    error = tool_result_error(messages[0]['content'])
    assert not executed
    if failure == 'body':
        assert error.code == 'tool_effect_unknown'
        assert row['executionEffect']['state'] == 'unknown'
        assert (root / 'partial-runtime').exists()
    else:
        assert row['executionEffect']['state'] == 'not_started'
        assert error.code == ('tool_execution_cancelled' if failure == 'cancelled' else 'tool_execution_failed')
        assert row['status'] == ('aborted' if failure == 'cancelled' else 'error')
        assert 'may have taken effect' not in error.message
        if failure == 'busy':
            assert 'was not started' in error.message
        assert error.retryable is False


def test_default_setup_wait_outlives_old_five_second_limit(workspace, monkeypatch):
    root, _ = workspace
    monkeypatch.delenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS')
    prepared = []
    monkeypatch.setattr(workspace_runtime, 'prepare_workspace_runtime',
                        lambda *a, **k: prepared.append(a) or [])
    clock, acquire = workspace_lease.time, workspace_lease._try_acquire
    elapsed, probes = 0, 0
    with writer(root) as release:
        def probe(*args):
            nonlocal elapsed, probes
            result = acquire(*args)
            if result not in ('acquire', 'enter'):
                probes += 1
                elapsed = 10  # Expire the former private deadline without sleeping.
                if probes == 2:
                    release.set()
            return result

        monkeypatch.setattr(workspace_lease, 'time', SimpleNamespace(
            monotonic=lambda: clock.monotonic() + elapsed, sleep=clock.sleep))
        monkeypatch.setattr(workspace_lease, '_try_acquire', probe)
        workspace_delivery.prepare_worker_command_runtime({'id': 'waiter', 'aborted': False})
    assert probes >= 2 and prepared


def test_real_project_handler_runs_inspection_while_runtime_setup_is_blocked(
        execution_workspace, monkeypatch):
    from lib.tasks_pkg.handlers import project
    env = execution_workspace
    root = env.selected
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'initial', '--allow-empty'], check=True)
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    monkeypatch.setattr(workspace_runtime, 'prepare_workspace_runtime',
                        lambda *a, **k: pytest.fail('inspection entered dependency preparation'))
    monkeypatch.setattr(project, '_finalize_tool_round', lambda *a, **k: None)
    with writer(root):
        _, output, _ = project._handle_project_tool(
            env.task, {}, 'run_command', 'inspection-call', {'command': INSPECTION},
            1, {}, {}, str(root), True)
    assert 'initial' in output and '[exit code: 0]' in output


@pytest.mark.parametrize('cancelled', [False, True])
def test_later_repository_refusal_cannot_erase_earlier_preparation(workspace, monkeypatch, cancelled):
    root, row = workspace
    earlier = root.parent / 'earlier'
    subprocess.run(['git', 'init', '-q', str(earlier)], check=True)
    rows = [{'canonicalRoot': row['canonicalRoot'], 'workspacePath': str(earlier)}, row]
    monkeypatch.setattr(workspace_delivery, 'execution_repositories', lambda task: rows)

    def prepare(source, destination, **kwargs):
        assert destination == str(earlier)
        (earlier / 'prepared').write_text('dependency bridge')
        if cancelled:
            task['aborted'] = True
        return ['prepared']

    monkeypatch.setattr(workspace_runtime, 'prepare_workspace_runtime', prepare)

    def handler(task, tc, name, call_id, *args):
        workspace_delivery.prepare_worker_command_runtime(task)
        pytest.fail('command ran despite conflicting second repository')

    task = recovery_task(handler, 'reconcile')
    with writer(root):
        result, messages, _ = dispatch(task)
    assert (earlier / 'prepared').exists()
    assert result['executionEffect']['state'] == 'unknown'
    assert result['status'] == ('aborted' if cancelled else 'error')
    assert tool_result_error(messages[0]['content']).code == 'tool_effect_unknown'
