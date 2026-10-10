"""Ignored dependency preparation is not exclusive source-checkout ownership."""
from contextlib import contextmanager
import subprocess
import sys
import threading

import pytest

from lib.project_mod import workspace_lease as leases
from lib.tasks_pkg import workspace_delivery

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'source.py').write_text('value = 1\n')
    return tmp_path


@contextmanager
def runtime_holder(repo):
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with leases.workspace_runtime_setup_lease(repo):
                ready.set()
                assert release.wait(10)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=hold)
    worker.start()
    assert ready.wait(5)
    try:
        yield
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors


def clean():
    assert not leases._registry
    assert not leases._active_descriptors


def test_runtime_setup_allows_source_edit_but_fences_other_setup_and_whole_tree(repo):
    with runtime_holder(repo):
        with leases.workspace_file_write_lease(repo / 'source.py'):
            (repo / 'source.py').write_text('value = 2\n')
        with pytest.raises(leases.WorkspaceBusyError):
            with leases.workspace_runtime_setup_lease(repo):
                pytest.fail('two dependency preparers overlapped')
        with pytest.raises(leases.WorkspaceBusyError):
            with leases.workspace_write_lease(repo):
                pytest.fail('worktree removal/publication ignored setup')
    clean()


def test_runtime_setup_fds_fence_children_without_blocking_file_edits(repo):
    child = None
    try:
        with leases.workspace_runtime_setup_lease(repo):
            fds = leases.workspace_lease_fds()
            assert len(fds) == 2
            with leases.workspace_runtime_setup_lease(repo):
                assert leases.workspace_lease_fds() == fds
            child = subprocess.Popen(
                [sys.executable, '-c', 'import sys; print("ready",flush=True); sys.stdin.read()'],
                pass_fds=fds, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == 'ready'
        with leases.workspace_file_write_lease(repo / 'source.py'):
            pass
        for api in (leases.workspace_write_lease, leases.workspace_runtime_setup_lease):
            with pytest.raises(leases.WorkspaceBusyError, match='holder type unknown'):
                with api(repo):
                    pytest.fail('inherited descriptor lost exclusion')
    finally:
        if child is not None:
            child.stdin.close()
            child.wait(timeout=5)
            child.stdout.close()
    with leases.workspace_runtime_setup_lease(repo):
        pass
    clean()


def test_prepare_worker_runtime_does_not_wrap_slow_setup_in_whole_writer(repo, monkeypatch):
    from lib.project_mod import workspace_runtime
    from lib.project_mod.write_tools import _ops

    entered, release = threading.Event(), threading.Event()
    errors = []
    row = {'canonicalRoot': str(repo), 'workspacePath': str(repo)}
    task = {'id': 'runtime-task', '_workspaceDelivery': {'repositories': [row]}}
    monkeypatch.setattr(workspace_delivery, 'execution_repositories', lambda task: [row])
    monkeypatch.setattr(workspace_delivery, '_persist', lambda task, row: None)
    monkeypatch.setattr(_ops, '_salvage_root', lambda: str(repo.parent / 'salvage'))
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')

    def prepare(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return ['.venv']

    monkeypatch.setattr(workspace_runtime, 'prepare_workspace_runtime', prepare)

    def run():
        try:
            workspace_delivery.prepare_worker_command_runtime(task)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(5)
    try:
        result = _ops.tool_apply_diff(str(repo), 'source.py', 'value = 1', 'value = 3')
        assert result['ok'], result
        assert (repo / 'source.py').read_text() == 'value = 3\n'
        assert not row.get('runtimePrepared')
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert row['runtimePrepared']
    clean()


def test_runtime_inside_whole_writer_preserves_strong_exclusion(repo):
    with leases.workspace_write_lease(repo):
        original = leases.workspace_lease_fds()
        with leases.workspace_runtime_setup_lease(repo):
            assert set(original).issubset(leases.workspace_lease_fds())
        assert leases.workspace_lease_fds() == original
    clean()


def test_source_file_holder_allows_runtime_setup_in_reverse_order(repo):
    result, errors = [], []

    def prepare():
        try:
            with leases.workspace_runtime_setup_lease(repo):
                result.append('entered')
        except BaseException as error:
            errors.append(error)

    with leases.workspace_file_write_lease(repo / 'source.py'):
        worker = threading.Thread(target=prepare)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors
        assert result == ['entered']
    clean()


def test_runtime_setup_serializes_across_processes(repo):
    child = subprocess.Popen(
        [sys.executable, '-c',
         'import sys; from lib.project_mod.workspace_lease import workspace_runtime_setup_lease; '
         'cm=workspace_runtime_setup_lease(sys.argv[1]); cm.__enter__(); '
         'print("ready",flush=True); sys.stdin.read(); cm.__exit__(None,None,None)', str(repo)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        with leases.workspace_file_write_lease(repo / 'source.py'):
            pass
        with pytest.raises(leases.WorkspaceBusyError, match='holder type unknown'):
            with leases.workspace_runtime_setup_lease(repo):
                pytest.fail('separate process setup overlapped')
    finally:
        child.stdin.close()
        child.wait(timeout=5)
        child.stdout.close()
    clean()


def test_cancelled_setup_wait_releases_its_shared_barrier(repo, monkeypatch):
    observed, cancel = threading.Event(), threading.Event()
    errors = []
    original = leases._try_acquire

    def probe(entry, thread_id, mode):
        outcome = original(entry, thread_id, mode)
        if outcome == 'write':
            observed.set()
        return outcome

    def wait():
        try:
            with leases.workspace_runtime_setup_lease(repo, timeout=5, cancel_event=cancel):
                pytest.fail('cancelled waiter entered')
        except leases.WorkspaceLeaseCancelled:
            pass
        except BaseException as error:
            errors.append(error)

    with leases.workspace_runtime_setup_lease(repo):
        monkeypatch.setattr(leases, '_try_acquire', probe)
        worker = threading.Thread(target=wait)
        worker.start()
        assert observed.wait(5)
        cancel.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors
    with leases.workspace_write_lease(repo):
        pass
    clean()


def test_aborted_task_does_not_prepare_dependencies(repo, monkeypatch):
    from lib.project_mod import workspace_runtime
    row = {'canonicalRoot': str(repo), 'workspacePath': str(repo)}
    task = {'id': 'aborted-setup', 'aborted': True}
    monkeypatch.setattr(workspace_delivery, 'execution_repositories', lambda task: [row])
    monkeypatch.setattr(workspace_runtime, 'prepare_workspace_runtime',
                        lambda *a, **k: pytest.fail('aborted task prepared dependencies'))
    with pytest.raises(leases.WorkspaceLeaseCancelled):
        workspace_delivery.prepare_worker_command_runtime(task)
    clean()
