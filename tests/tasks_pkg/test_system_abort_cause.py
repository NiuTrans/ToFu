"""A system-initiated abort is never recorded or shown as a user Stop (D2).

2026-11-06 restart. The recovery backstop settled two resumed Worker attempts
(73000186, beac53a8). Their executors then hit the stale-attempt fence, and the
fatal path persisted ``error kind='aborted'`` "Stopped by user". Both storage
authorities wrote ``evidence='user_abort'`` on Turns that died with the server.
The shutdown finalizer logged "The user likely clicked Stop". Its quiesce killed
six paused lanes' commands with ``[Command aborted by user]``, so each lane
reported a Stop nobody gave. Project Brain recovery closed restart-paused work
items as "cancelled" while project dispatch resumed the same tasks.

The abort site stamps the cause (``_abort_reason``), and the canonical verdict
reads it (the representation delivery uses, 4a16ec29e1). A person's Stop keeps
the ``user_abort`` reading and stays sticky.
"""
import logging
import threading

import pytest

from lib.tasks_pkg.event_delivery import EventDeliveryRevoked
from lib.turn_verdict import (
    TurnStatus,
    TurnTerminationEvidence,
    derive_task_verdict,
)

pytestmark = pytest.mark.unit

# The stamps are the abort-cause representation (lib.turn_verdict).
RESTART_RECOVERY = 'restart_recovery'
STOPPED_ELSEWHERE = 'attempt_stopped_by_user'


def _task(**overrides):
    return {
        'id': 'project-worker:73000186', 'convId': 'worker-lane', '_turnId': 'turn-1',
        '_attemptId': 'attempt-1', '_userId': 1,
        'status': 'running', 'aborted': False, 'abort_event': threading.Event(),
        'content': '', 'thinking': '', 'toolRounds': [],
        **overrides,
    }


def _settle_durably(task, event):
    from lib.turn_lifecycle import _settlement
    return _settlement(task, event, {
        'content': task.get('content') or '',
        'thinking': task.get('thinking') or '',
        'toolRounds': task.get('toolRounds') or [],
    })


@pytest.mark.parametrize('reason', ['server_shutdown', RESTART_RECOVERY])
def test_restart_pauses_settle_system_recovery_never_a_user_stop(reason):
    task = _task(status='aborted', finishReason='aborted', aborted=True,
                 _abort_reason=reason, thinking='in-flight reasoning',
                 toolRounds=[{'toolCallId': 'saved-read', 'toolName': 'read_files',
                              'toolArgs': {'path': 'a'}, 'toolContent': 'saved',
                              'status': 'done', 'roundNum': 1, 'llmRound': 0}])
    event = {'type': 'aborted', 'finishReason': 'aborted'}

    verdict = derive_task_verdict(task, event)
    assert verdict.status is TurnStatus.INTERRUPTED
    assert verdict.evidence is TurnTerminationEvidence.SYSTEM_RECOVERY
    status, settlement = _settle_durably(task, event)
    assert status == 'interrupted'
    assert (settlement['cause'], settlement['evidence']) == ('server_restart', 'system_recovery')
    assert any(option['operation'] == 'continue' for option in settlement['resumeOptions'])


def _fence_and_fail(monkeypatch, settled_turn):
    """The executor's next write is rejected: the D1 backstop kill sequence."""
    import lib.tasks_pkg.orchestrator._post_loop as post
    from lib.turn_lifecycle import _signal_stale_attempt_abort

    monkeypatch.setattr('lib.turn_lifecycle.get_turn', lambda *a, **kw: settled_turn)
    task = _task()
    _signal_stale_attempt_abort(task, 'attempt-1', 'event-record-rejected')
    monkeypatch.setattr(post, '_settle_fatal_tool_rounds', lambda _task: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.stamp_chat_task_terminal', lambda *a, **kw: None)
    monkeypatch.setattr('lib.tasks_pkg.compaction._settlement.settle_compaction_usage',
                        lambda _task: None)
    monkeypatch.setattr(post, '_maybe_auto_retry_turn',
                        lambda *a, **kw: pytest.fail('a fenced attempt must not auto-retry'))
    monkeypatch.setattr(post, 'append_event', lambda _task, event: None)
    monkeypatch.setattr(post, 'persist_task_result', lambda _task: None)
    assert post.handle_task_fatal(task, EventDeliveryRevoked(
        'Task event delivery admission previously failed')) is False
    return task


def test_fence_after_restart_recovery_settles_interrupted_not_stopped_by_user(monkeypatch):
    from lib.tasks_pkg.workspace_delivery import RESTART_INTERRUPTED, workspace_outcome

    task = _fence_and_fail(monkeypatch, {
        'currentAttemptId': 'attempt-1', 'status': 'interrupted',
        'settlement': {'cause': 'server_restart', 'evidence': 'system_recovery'}})

    assert task['_abort_reason'] == RESTART_RECOVERY
    assert task['status'] == 'aborted' and task['error'] is None
    assert derive_task_verdict(task).evidence is TurnTerminationEvidence.SYSTEM_RECOVERY
    # Delivery reads the same cause: resumable, never a publication denial.
    assert workspace_outcome(task) == RESTART_INTERRUPTED


def test_fence_after_a_stop_elsewhere_stays_a_user_stop(monkeypatch):
    task = _fence_and_fail(monkeypatch, {
        'currentAttemptId': 'attempt-1', 'status': 'interrupted',
        'settlement': {'cause': 'user_abort', 'evidence': 'user_abort'}})

    assert task['_abort_reason'] == STOPPED_ELSEWHERE
    assert task['status'] == 'aborted' and task['error'] is None
    verdict = derive_task_verdict(task)
    assert (verdict.cause, verdict.evidence) == ('user_abort', TurnTerminationEvidence.USER_ABORT)


def test_fence_of_a_replaced_attempt_is_a_fence_failure_never_stopped_by_user(monkeypatch):
    task = _fence_and_fail(monkeypatch, {
        'currentAttemptId': 'attempt-2', 'status': 'running', 'settlement': None})

    assert task['_abort_reason'] == 'turn_attempt_stale_fence'
    assert task['status'] == 'error'
    assert task['error']['kind'] == 'storage_fence'
    assert 'Stopped by user' not in str(task['error'])


def test_genuine_user_stop_keeps_user_abort_with_no_error():
    task = _task(status='aborted', finishReason='aborted', aborted=True)
    event = {'type': 'aborted', 'finishReason': 'aborted'}

    verdict = derive_task_verdict(task, event)
    assert (verdict.status, verdict.cause) == (TurnStatus.INTERRUPTED, 'user_abort')
    status, settlement = _settle_durably(task, event)
    assert status == 'interrupted'
    assert (settlement['cause'], settlement['evidence'], settlement['error']) == (
        'user_abort', 'user_abort', None)


def test_restart_killed_command_names_the_restart_not_the_user():
    from lib.project_mod import run_command
    from lib.project_mod.foreground_command_slots import command_task_view

    parent = _task()
    view = command_task_view(parent)
    # The quiesce stamps the parent after the command's view was taken.
    parent.update(aborted=True, _abort_reason='server_shutdown')
    assert view.get('_abort_reason') == 'server_shutdown'
    restarted = run_command._format_aborted_output('sleep 1500', '', '', view, [])
    assert restarted.endswith('[Command interrupted by server restart]\n[exit code: -1]')

    stopped = run_command._format_aborted_output('sleep 1500', '', '', _task(aborted=True), [])
    assert stopped.endswith('[Command aborted by user]\n[exit code: -1]')


def test_shutdown_finalize_never_claims_a_user_stop(caplog):
    from lib.tasks_pkg.orchestrator._finalize import _settle_post_loop_finish_reason

    task = _task(aborted=True, _abort_reason='server_shutdown')
    with caplog.at_level(logging.INFO):
        finish = _settle_post_loop_finish_reason(
            task, 'tool_calls', loop_exit_reason='done', abort_detected_phase='',
            model='claude-opus-5.5', tid='27ce29e9')
    assert finish == 'aborted'
    assert 'clicked Stop' not in caplog.text
    assert 'server_shutdown' in caplog.text


def test_project_brain_recovery_keeps_restart_paused_work_active(monkeypatch):
    """Audit defect 3: 18 resumed lanes' work items were closed 'cancelled'."""
    from lib.conversations import project_brain_startup as startup
    from lib.tasks_pkg import workspace_delivery as delivery

    finishes, intents = [], []
    rows = {
        'paused-by-shutdown': {'status': 'aborted', 'metadata': {
            'finishReason': 'aborted', 'aborted': True, '_abort_reason': 'server_shutdown'}},
        'interrupted-by-recovery': {'status': 'interrupted', 'metadata': {}},
        'stopped-by-user': {'status': 'aborted', 'metadata': {
            'finishReason': 'aborted', 'aborted': True}},
    }

    class Client:
        def maintenance(self, operation, payload, **kwargs):
            return {'capped': False, 'projects': [
                {'ownerUserId': 7, 'projectKey': '/project', 'workItems': [
                    {'id': f'work-{key}', 'taskId': key, 'status': 'active'} for key in rows]}]}

        def query(self, operation, payload, **kwargs):
            if operation == 'record.list':
                return []
            return dict(rows[payload['key']])

        def command(self, operation, payload, command_id):
            finishes.append((payload['work_id'], payload.get('status')))

    monkeypatch.setattr(startup, 'get_storage_client', lambda **kw: Client())
    monkeypatch.setattr(delivery, 'record_task_workspace_intents',
                        lambda task, root: intents.append(task['id']))

    assert startup.recover_active_work_items() == 1
    assert [work for work, _status in finishes] == ['work-stopped-by-user']
    # Every task still records its workspace receipt (non-denied for a pause).
    assert intents == list(rows)
