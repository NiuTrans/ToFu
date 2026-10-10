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
            assert all(set(item) == {'pid', 'thread', 'tool', 'task', 'conversation', 'since'}
                       for item in holders.values())
    assert ref() is None
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
