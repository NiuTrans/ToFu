"""Proven readers never escalate to a whole-checkout writer; waits stay visible.

Measured 2026-10-20 (conv `mvgpqeeaxhm3un`): ``ls -d evals 2>/dev/null`` inside a
compound command claimed the entire repository exclusively, waited 21 minutes
behind another writer, spawned no process, logged nothing, and therefore could
not be handed off to the background (handoff requires a live PID).
"""
import threading
import time

import pytest

from lib.project_mod.run_command import command_write_lease_claims
from tests.project_mod.test_command_outer_writer_lease import _other_writer, repo as _repo

pytestmark = pytest.mark.unit

repo = _repo


def _claims(root, command):
    return command_write_lease_claims(str(root), command)


@pytest.mark.parametrize('command', [
    'ls -d evals 2>/dev/null',
    'ls -d evals >/dev/null',
    'ls -d evals 2>&1 | head -2',
    'grep -rn "needle" lib 2>/dev/null',
    'find . -name "*.py" 2>/dev/null | head -5',
    'python3 -m pytest -q tests/x.py 2>/dev/null | tail -5',
    'git grep -n "needle" 2>/dev/null | head -5',
    'git status --porcelain 2>/dev/null | head',
    'git log --oneline -5 2>/dev/null',
    'sed -n 1258,1340p lib/tools/ga.py',
    'sed -n "1,20p" JOURNAL.md 2>/dev/null',
])
def test_proven_reader_claims_no_writer_lease(repo, command):
    assert _claims(repo, command) == ()


def test_incident_compound_command_claims_nothing(repo):
    command = (
        f'cd {repo} && ls -d lib/research 2>&1 | head -2; '
        'ls docs | grep -i "research\\|agent_capab" ; echo "--- evals dir:"; '
        'ls -d evals benchmarks 2>/dev/null; echo "--- JOURNAL research mentions:"; '
        'grep -ci "research cycle\\|direction" JOURNAL.md 2>/dev/null'
    )
    assert _claims(repo, command) == ()


@pytest.mark.parametrize('command', [
    'ls -d evals > out.txt',
    'ls -d evals 2> err.txt',
    'grep -rn needle lib | tee out.txt',
    'git grep -n needle > hits.txt',
    'sed -i "s/a/b/" source',
    'rm -rf build',
    'git branch -D topic',
    'echo hi >> source',
])
def test_named_writes_keep_their_lease(repo, command):
    assert _claims(repo, command) != ()


def test_named_output_file_claims_only_that_file(repo):
    claims = _claims(repo, 'ls -d evals > out.txt')
    assert claims == (('file', str(repo / 'out.txt')),)


def test_existing_directory_deletion_claims_the_tree(repo):
    (repo / 'build').mkdir()
    claims = _claims(repo, 'rm -rf build')
    assert ('tree', str(repo / 'build')) in claims


@pytest.mark.parametrize('command', [
    'sed -n "1,20p;w leak.txt" source',
    'sed -n "1,20p;e id" source',
    'git grep -O less needle',
    'git grep --open-files-in-pager=cat needle',
    'git grep -rO needle',
    'git grep --unknown-option needle',
])
def test_unproven_shapes_keep_writer_admission(repo, command):
    assert _claims(repo, command) != ()


def test_admission_wait_is_logged_with_holder_evidence(repo, monkeypatch, caplog):
    from lib.project_mod import workspace_lease

    monkeypatch.setattr(workspace_lease, '_ADMISSION_NOTICE_SECONDS', 0.05)
    monkeypatch.setattr(workspace_lease, '_ADMISSION_NOTICE_INTERVAL_SECONDS', 60.0)
    assert _ADMISSION_BUDGET > 0.05 * 2
    with _other_writer(repo):
        with pytest.raises(workspace_lease.WorkspaceBusyError):
            with workspace_lease.workspace_write_lease(repo, timeout=_ADMISSION_BUDGET):
                pytest.fail('a held checkout lease must not be admitted twice')
    messages = [record.getMessage() for record in caplog.records
                if 'WorkspaceLease' in record.getMessage()]
    assert messages, 'a multi-second admission wait must be observable'
    assert 'waiter(' in messages[0] and 'pid=' in messages[0]
    assert 'holder: ' in messages[0] and 'held_seconds=' in messages[0]


_ADMISSION_BUDGET = 2.5


def test_handoff_adopts_a_command_waiting_for_admission(monkeypatch):
    """No PID exists yet, so only the admission clock can trigger a handoff."""
    from lib import agent_inbox
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.handlers import _background_command as background

    agent_inbox.reset_for_test()
    with background_commands._lock:
        background_commands._registry.clear()
    monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 0.2)
    monkeypatch.setattr(background, '_queue_completion', lambda **_kwargs: True)

    task = {'id': 'task-admission', 'convId': 'conv-admission',
            '_userId': 7, 'aborted': False, 'status': 'running', 'toolRounds': []}
    release = threading.Event()
    admitted = threading.Event()

    def execute(command_task):
        command_task['_workspaceAdmission'] = {'since': time.monotonic(), 'depth': 1,
                                              'claimCount': 1}
        admitted.set()
        release.wait(5)
        command_task.pop('_workspaceAdmission', None)
        return '$ probe\n[exit code: 0]'

    try:
        result = background.run_with_steer_handoff(
            task=task, config={}, command='probe', execute=execute)
        assert admitted.is_set()
        assert background.is_background_command_result(result)
        command_id = (result.split(background.BACKGROUND_COMMAND_MARKER, 1)[1]
                      .split('.', 1)[0].strip())
        assert background_commands.check_background_command(
            command_id, user_id=7)['status'] == 'running'
        release.set()
        assert background_commands.check_background_command(
            command_id, user_id=7, wait_s=5)['status'] == 'completed'
    finally:
        release.set()
        agent_inbox.reset_for_test()
        with background_commands._lock:
            background_commands._registry.clear()


def test_manual_handoff_refuses_a_foreground_phase_without_process_or_wait():
    from lib.tasks_pkg.handlers import _background_command as background

    task = {'id': 'task-phase', 'convId': 'conv-phase', '_userId': 7,
            'aborted': False, 'status': 'running', 'toolRounds': []}
    handoff = background.CommandHandoffControl(task, 3, 'call_1')
    assert handoff.request(round_num=3, tool_call_id='call_1') == {
        'backgrounded': False, 'reason': 'no_active_command'}
    task['_workspaceAdmission'] = {'since': time.monotonic(), 'depth': 1}
    assert handoff.request(round_num=3, tool_call_id='call_1')['reason'] == 'handoff_pending'
