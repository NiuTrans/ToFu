"""Lease diagnostics describe active holders, never requester payloads or ownership."""
import os
import subprocess

import sys
import threading
import weakref

import pytest

from lib.project_mod import workspace_lease as leases
from lib.project_mod.tool_admission import tool_admission_scope

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'a.txt').write_text('a\n')
    return tmp_path


def conflict(call):
    errors = []

    def run():
        try:
            call()
        except leases.WorkspaceBusyError as error:
            errors.append(str(error))

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1
    return errors[0]


@pytest.mark.parametrize('api', [leases.workspace_write_lease, leases.workspace_read_lease])
def test_reports_holder_not_waiter_and_reclaims_diagnostic_state(repo, api):
    holder = {'id': 'holder-task', 'convId': 'holder-conv', 'command': 'SECRET body'}
    with tool_admission_scope(holder, tool_name='run_command'):
        with api(repo):
            def wait():
                with tool_admission_scope({'id': 'waiter-task', 'convId': 'waiter-conv'}, tool_name='edit_file'):
                    with leases.workspace_file_write_lease(repo / 'a.txt'):
                        pytest.fail('conflicting holder was ignored')

            text = conflict(wait)
            assert 'tool=run_command' in text
            assert 'task=holder-task' in text
            assert 'conversation=holder-conv' in text
            assert f'pid={os.getpid()}' in text
            assert 'held_seconds=' in text
            assert 'waiter-task' not in text
            assert 'SECRET' not in text
    assert not leases._registry
    assert not leases._active_descriptors


def test_file_target_contention_reports_target_and_original_reentrant_holder(repo):
    with tool_admission_scope({'id': 'original-task'}, tool_name='edit_file'):
        with leases.workspace_file_write_lease(repo / 'a.txt'):
            with tool_admission_scope({'id': 'nested-task'}, tool_name='write_file'):
                with leases.workspace_file_write_lease(repo / 'a.txt'):
                    text = conflict(lambda: leases.workspace_file_write_lease(repo / 'a.txt').__enter__())
                    assert str(repo / 'a.txt') in text
                    assert 'tool=edit_file' in text
                    assert 'task=original-task' in text
                    assert 'nested-task' not in text
    assert not leases._registry


def test_invalid_identifiers_are_not_echoed_or_retained(repo):
    class Task(dict):
        pass

    task = Task(id='private token\n' * 100, convId='bad\nvalue', body='SECRET')
    ref = weakref.ref(task)
    with tool_admission_scope(task, tool_name='bad\nlabel'):
        with leases.workspace_write_lease(repo):
            del task
            text = conflict(lambda: leases.workspace_write_lease(repo).__enter__())
            assert 'pid=' in text
            assert 'private' not in text
            assert 'bad' not in text
            assert 'SECRET' not in text
            holders = next(iter(leases._registry.values()))['holders']
            assert all(set(item) == {'pid', 'thread', 'thread_name', 'tool', 'task',
                                     'conversation', 'owner', 'request', 'mode', 'since'}
                       for item in holders.values())
            # The request names claims and an explicit caller summary only;
            # nothing is read from the task's own payload.
            assert all(item['request'].purpose == '' for item in holders.values())
            # The registry keeps no reference to the task; the scope's own
            # strong reference is what keeps it alive here.
            assert ref() is not None
    assert ref() is None
    assert not leases._registry


def test_holder_outside_any_tool_scope_is_named_by_its_thread(repo):
    """Settlement/delivery leases carry no tool or task; the thread names them."""
    def acquire_on(thread_name):
        acquired, release = threading.Event(), threading.Event()

        def hold():
            with leases.workspace_write_lease(repo):
                acquired.set()
                release.wait(5)

        worker = threading.Thread(target=hold, name=thread_name)
        worker.start()
        assert acquired.wait(5)
        try:
            return conflict(lambda: leases.workspace_write_lease(repo).__enter__())
        finally:
            release.set()
            worker.join(5)

    text = acquire_on('commit-round-0123abcd')
    assert 'thread_name=commit-round-0123abcd' in text
    assert 'tool=' not in text and 'task=' not in text
    # Names outside the bounded identifier alphabet are omitted, never echoed.
    text = acquire_on('Thread-9 (secret payload)')
    assert 'thread_name=' not in text and 'secret' not in text
    assert not leases._registry


def test_reader_upgrade_diagnostics_restore_original_reader(repo):
    with tool_admission_scope({'id': 'reader'}, tool_name='read_files'):
        with leases.workspace_read_lease(repo):
            with tool_admission_scope({'id': 'writer'}, tool_name='edit_file'):
                with leases.workspace_write_lease(repo):
                    text = conflict(lambda: leases.workspace_write_lease(repo).__enter__())
                    assert 'task=writer' in text
                    assert 'task=reader' not in text
            text = conflict(lambda: leases.workspace_write_lease(repo).__enter__())
            assert 'Workspace read is active' in text
            assert 'task=reader' in text
            assert 'task=writer' not in text
    assert not leases._registry


def test_diagnostic_failure_cannot_orphan_descriptor(repo, monkeypatch):
    def fail(cancel):
        raise RuntimeError('diagnostic fault')

    with monkeypatch.context() as patch:
        patch.setattr(leases, '_holder_identity', fail)
        with pytest.raises(RuntimeError, match='diagnostic fault'):
            with leases.workspace_write_lease(repo):
                pytest.fail('diagnostic fault was ignored')
    assert not leases._registry
    assert not leases._active_descriptors
    with leases.workspace_write_lease(repo):
        pass
    child = subprocess.run(
        [sys.executable, '-c',
         'import sys; from lib.project_mod.workspace_lease import workspace_write_lease; '
         'cm=workspace_write_lease(sys.argv[1]); cm.__enter__(); cm.__exit__(None,None,None)', str(repo)],
        capture_output=True, text=True, timeout=5)
    assert child.returncode == 0, child.stderr


@pytest.mark.parametrize('length, expected', [(96, True), (97, False)])
def test_identifier_length_boundary(repo, length, expected):
    identity = 'x' * length
    with tool_admission_scope({'id': identity}, tool_name='edit_file'):
        with leases.workspace_write_lease(repo):
            text = conflict(lambda: leases.workspace_write_lease(repo).__enter__())
            assert ('task=' + identity in text) is expected
    assert not leases._registry


@pytest.mark.parametrize('api', [leases.workspace_write_lease, leases.workspace_runtime_setup_lease])
def test_initial_publication_fault_releases_physical_and_logical_claim(repo, monkeypatch, api):
    original = leases._publish_hold

    def fault(*args):
        original(*args)
        raise RuntimeError('publication fault')

    with monkeypatch.context() as patch:
        patch.setattr(leases, '_publish_hold', fault)
        with pytest.raises(RuntimeError, match='publication fault'):
            with api(repo):
                pytest.fail('publication fault ignored')
    assert not leases._registry
    assert not leases._active_descriptors
    with leases.workspace_write_lease(repo):
        pass


@pytest.mark.parametrize('contended', [False, True])
def test_admission_phases_distinguish_io_from_local_wait(repo, monkeypatch, contended):
    from lib.tools import execution_timing as timing

    evidence = []
    monkeypatch.setattr(timing, 'TOOL_EXECUTION_BUDGETS_MS', {'write_file': -1})
    monkeypatch.setattr(timing, 'log_event', lambda *args, **kwargs: evidence.append(kwargs))
    target = repo / 'a.txt'

    def admit():
        with timing.tool_execution_timing('write_file'):
            if contended:
                with pytest.raises(leases.WorkspaceBusyError):
                    with leases.workspace_file_write_lease(target, timeout=0.03):
                        pytest.fail('same-file holder was ignored')
            else:
                with leases.workspace_file_write_lease(target):
                    pass

    if contended:
        with leases.workspace_file_write_lease(target):
            worker = threading.Thread(target=admit)
            worker.start()
            worker.join(5)
            assert not worker.is_alive()
    else:
        admit()
    assert len(evidence) == 1
    phases = evidence[0]['phases']
    assert phases['workspace_registry_wait']['calls'] > 0
    if contended:
        assert phases['workspace_local_wait']['calls'] > 0
        assert 'workspace_flock_wait' not in phases
    else:
        assert phases['workspace_lock_io']['calls'] > 0
        assert 'workspace_local_wait' not in phases
        assert 'workspace_flock_wait' not in phases
    assert not leases._registry
    assert not leases._active_descriptors


def test_registry_timer_finalizer_fault_cannot_strand_admission(repo, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def broken_timer(*args, **kwargs):
        yield
        raise RuntimeError('timer finalizer fault')

    with monkeypatch.context() as patch:
        patch.setattr(leases, 'timing_phase', broken_timer)
        with pytest.raises(RuntimeError, match='timer finalizer fault'):
            with leases.workspace_write_lease(repo):
                pytest.fail('timer fault ignored')
    assert leases._registry_lock.acquire(blocking=False)
    leases._registry_lock.release()
    assert not leases._registry
    assert not leases._active_descriptors
    with leases.workspace_write_lease(repo):
        pass
