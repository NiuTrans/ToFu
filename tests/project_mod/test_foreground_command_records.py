"""Foreground command groups never outlive a dead server unobserved."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from lib.project_mod import foreground_command_records as records
from lib.project_mod.process_group import group_has_live_members

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    not sys.platform.startswith('linux'), reason='Linux process identity')]


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / 'records'
    monkeypatch.setattr(records, '_records_root', lambda: str(path))
    return path


@pytest.fixture
def spawned():
    processes = []

    def spawn(argv, **kwargs):
        process = subprocess.Popen(argv, start_new_session=True, **kwargs)
        processes.append(process)
        return process

    yield spawn
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _write(root, process, owner, *, call_id='call-1', conv_id='conv', token=None):
    root.mkdir(exist_ok=True)
    record = {'schema': 1, 'owner': owner, 'pid': process.pid, 'pgid': process.pid,
              'pid_starttime': token or records._start_time_token(process.pid),
              'conv_id': conv_id, 'tool_call_id': call_id, 'command': 'sleep 60',
              'started_at': time.time()}
    path = root / f"{owner['instance']}-{process.pid}.json"
    path.write_text(json.dumps(record))
    return path


def _owner_of(process, **overrides):
    current = records._current_owner()
    owner = {**current, 'pid': process.pid,
             'started': records._start_time_token(process.pid), 'instance': 'other-process'}
    owner.update(overrides)
    return owner


def _dead(process, timeout=5):
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def test_restarted_process_retires_group_left_by_its_previous_exec(root, spawned):
    orphan = spawned(['sleep', '60'])
    path = _write(root, orphan, {**records._current_owner(), 'instance': 'before-execv'})
    assert records.retire_orphaned_foreground_commands() == {'retired': 1, 'gone': 0, 'kept': 0}
    assert _dead(orphan)
    assert not path.exists()


def test_crashed_owner_group_is_retired(root, spawned):
    owner = spawned(['true'])
    identity = _owner_of(owner)
    owner.wait(timeout=5)
    orphan = spawned(['sleep', '60'])
    _write(root, orphan, identity)
    assert records.retire_orphaned_foreground_commands()['retired'] == 1
    assert _dead(orphan)


def _members(pgid):
    found = []
    for name in os.listdir('/proc'):
        if not name.isdecimal() or int(name) == pgid:
            continue
        try:
            if os.getpgid(int(name)) == pgid:
                found.append(int(name))
        except ProcessLookupError:
            continue
    return found


def test_survivors_are_retired_after_their_leader_exited(root, spawned):
    leader = spawned(['sh', '-c', 'sleep 60 & read line'], stdin=subprocess.PIPE)
    deadline = time.monotonic() + 5
    while not _members(leader.pid):
        assert time.monotonic() < deadline, 'background member never started'
        time.sleep(0.02)
    _write(root, leader, {**records._current_owner(), 'instance': 'before-execv'})
    leader.stdin.close()
    leader.wait(timeout=5)
    assert group_has_live_members(leader.pid)
    assert records.retire_orphaned_foreground_commands()['retired'] == 1
    assert not group_has_live_members(leader.pid)


@pytest.mark.parametrize('owner_kind', ['live_process', 'other_host', 'other_namespace'])
def test_live_or_foreign_owners_are_never_signalled(root, spawned, owner_kind):
    orphan = spawned(['sleep', '60'])
    if owner_kind == 'live_process':
        owner = _owner_of(spawned(['sleep', '60']))
    else:
        field = 'host' if owner_kind == 'other_host' else 'pid_namespace'
        owner = {**records._current_owner(), 'instance': 'elsewhere', field: 'elsewhere'}
    path = _write(root, orphan, owner)
    assert records.retire_orphaned_foreground_commands() == {'retired': 0, 'gone': 0, 'kept': 1}
    assert orphan.poll() is None
    assert path.exists()


def test_previous_boot_records_are_dropped_without_signalling(root, spawned):
    orphan = spawned(['sleep', '60'])
    path = _write(root, orphan, {**records._current_owner(), 'instance': 'old', 'boot': 'old-boot'})
    assert records.retire_orphaned_foreground_commands()['gone'] == 1
    assert orphan.poll() is None
    assert not path.exists()


def test_reused_leader_pid_is_not_signalled(root, spawned):
    unrelated = spawned(['sleep', '60'])
    path = _write(root, unrelated, {**records._current_owner(), 'instance': 'before-execv'},
                  token='1')
    assert records.retire_orphaned_foreground_commands()['gone'] == 1
    assert unrelated.poll() is None
    assert not path.exists()


def test_unreadable_foreign_record_is_dropped_but_own_publication_is_kept(root):
    root.mkdir()
    (root / 'dead-instance-12.json').write_text('{not json')
    own = root / f"{records._current_owner()['instance']}-13.json.tmp"
    own.write_text('{partial')
    records.retire_orphaned_foreground_commands()
    assert sorted(path.name for path in root.iterdir()) == [own.name]


def test_surviving_records_match_open_calls_of_the_same_conversation(root, spawned):
    process = spawned(['sleep', '60'])
    owner = records._current_owner()
    _write(root, process, owner, call_id='open-call', conv_id='conv')
    assert [row['tool_call_id'] for row in records.surviving_foreground_commands(
        conv_id='conv', tool_call_ids=['open-call', 'other'])] == ['open-call']
    assert records.surviving_foreground_commands(conv_id='other-conv', tool_call_ids=['open-call']) == []
    assert records.surviving_foreground_commands(conv_id='conv', tool_call_ids=[]) == []


def test_background_commands_are_never_recorded(root):
    process = SimpleNamespace(pid=os.getpid(), _tofu_owned_pgid=os.getpid())
    assert records.record_foreground_command(
        process, task={'_background_detached': True}, command='sleep 60') is None
    assert not root.exists()


def _context(call_id):
    return SimpleNamespace(tool_call_id=call_id, cancellation_requested=False,
                           register_cancel_callback=lambda callback: (lambda: None))


def test_runner_records_while_running_and_releases_after_exit(root, tmp_path):
    from lib.project_mod import run_command as commands
    seen = []
    output = commands._run_command_simple(
        'sleep 0.2; echo finished', 'sleep 0.2; echo finished', 10, str(tmp_path),
        task={'id': 'task', 'convId': 'conv-7'}, runtime_context=_context('call-7'),
        on_spawn=lambda *args: seen.extend(json.loads(path.read_text()) for path in root.iterdir()))
    assert 'finished' in output and '[exit code: 0]' in output
    assert [(row['tool_call_id'], row['conv_id'], row['pid'] == row['pgid']) for row in seen] == [
        ('call-7', 'conv-7', True)]
    assert seen[0]['owner']['instance'] == records._current_owner()['instance']
    assert list(root.iterdir()) == []


def test_unproven_retirement_keeps_the_record_for_recovery(root, tmp_path, monkeypatch):
    from lib.project_mod import run_command as commands
    from lib.project_mod.process_group import ProcessGroupRetirementError
    task = {'id': 'task', 'convId': 'conv', 'aborted': False}

    def refuse(*args, **kwargs):
        raise ProcessGroupRetirementError('synthetic retirement failure')

    monkeypatch.setattr(commands, '_kill_process_tree', refuse)
    output = commands._run_command_simple(
        'sleep 60', 'sleep 60', 30, str(tmp_path), task=task,
        runtime_context=_context('call-9'), on_spawn=lambda *args: task.update(aborted=True))
    left = [json.loads(path.read_text()) for path in root.iterdir()]
    try:
        # The command's own Stop verdict survives; retirement adds a warning.
        assert 'Error executing command' not in output
        assert '[warning process-group-retirement-incomplete:' in output
        assert output.rstrip().endswith('[Command aborted by user]\n[exit code: -1]')
        assert [row['tool_call_id'] for row in left] == ['call-9']
        assert records.surviving_foreground_commands(
            conv_id='conv', tool_call_ids=['call-9'])
    finally:
        for row in left:
            try:
                os.killpg(row['pgid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
