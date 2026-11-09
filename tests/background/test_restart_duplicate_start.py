"""A continuation never duplicates a task identity that is still scheduled.

2026-11-06 14:10 restart, lane 27ce29e9 (conversation
worker-d25f21672400a5a2f0666927, Turn ed1a211d): the Worker waited on a
handed-off ``sleep 1500``. The shutdown quiesce aborted the Turn
(``_abort_reason=server_shutdown``) and killed the command. The command's own
completion thread then woke the conversation inside the stopping execution host
(14:10:52). The aborted carrier was still finalizing (a 21.9 s workspace
delivery), so the agent executor still held its job. The wake resumed the same
Turn as attempt 454660cc under the same deterministic identity
``project-worker:<assignment>``. The executor refused it ("agent task already
scheduled"), and that refusal settled the Turn failed, so restart recovery never
resumed the lane.

These specs replay that sequence against the in-memory queue authority and an
isolated chat registry.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from lib import agent_inbox, message_queue
from lib.agent_core.task_runtime import TaskRuntime
from lib.identity import PrincipalContext
from lib.tasks_pkg import background_commands
from lib.turn_lifecycle import LifecycleConflict
from tests._seed import seed_conversation

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

LANE_TASK_ID = 'project-worker:27ce29e9-2a88-5d1d-8bdb-cfa055fb7ea5'


@pytest.fixture(autouse=True)
def _isolated_runtime(monkeypatch):
    agent_inbox.reset_for_test()
    with background_commands._lock:
        background_commands._registry.clear()
        background_commands._recovery_in_progress = False
        background_commands._recovery_scan_failed = False
        background_commands._recovery_incomplete_conversations.clear()
    runtime = TaskRuntime('chat', ttl=3600, push_channel=None)
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime', runtime)
    monkeypatch.setattr('lib.tasks_pkg.manager._registry.chat_task_runtime', runtime)
    monkeypatch.setattr('lib.swarm.integration._state.suspend_swarms_for_restart',
                        lambda: None)
    yield runtime
    from lib.agent_core.execution_session import ExecutionPhase
    for task in runtime.snapshot():
        task['_executionSession'].settle(ExecutionPhase.CANCELLED, cause='test teardown')
    agent_inbox.reset_for_test()
    with background_commands._lock:
        background_commands._registry.clear()


class _Executor:
    """The agent executor's job table: the identity it still schedules."""

    def __init__(self, *scheduled):
        self.scheduled = set(scheduled)

    def scheduling_snapshot(self, task_id=None):
        return {'taskState': 'running' if task_id in self.scheduled else 'unknown'}


def _carrier(runtime, conv_id, *, task_id=LANE_TASK_ID, status='running'):
    task = runtime.create(
        principal=PrincipalContext.user(subject_id='user:1', owner_user_id=1),
        task_id=task_id, meta={'convId': conv_id})
    task.update(convId=conv_id, status=status, aborted=False)
    return task


def _published_receipt(conv_id, command_id):
    message_queue.enqueue_message(
        conv_id, {'text': f'<background-command id="{command_id}">killed'
                          '</background-command>',
                  '_backgroundCommand': command_id},
        {'model': 'test'}, kind=message_queue.KIND_BACKGROUND_RECEIPT, user_id=1)
    done = threading.Event()
    done.set()
    with background_commands._lock:
        background_commands._registry[command_id] = {
            'conv_id': conv_id, 'user_id': 1, 'done': done,
            'state': {'delivery_status': 'queued', 'command': command_id,
                      'result': 'killed'},
        }


def _workflow_wakes(conv_id):
    rows = message_queue._queue_client().query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})
    return [row for row in rows if row['kind'] == message_queue.KIND_WORKFLOW]


def test_restart_quiesced_conversation_starts_no_background_successor(
        _isolated_runtime, monkeypatch):
    """The stopping process leaves the paused lane to the next generation."""
    from lib.tasks_pkg.manager import quiesce_running_tasks

    conv_id = 'worker-d25f21672400a5a2f0666927'
    seed_conversation(conv_id, user_id=1, title='Worker lane')
    carrier = _carrier(_isolated_runtime, conv_id)
    assert quiesce_running_tasks(reason='server_shutdown') == 1
    # The quiesced Turn settles while its worker still finalizes the workspace.
    carrier['status'] = 'aborted'
    _published_receipt(conv_id, 'bg_70e731470099')
    started = []
    monkeypatch.setattr(message_queue, 'dispatch_next_queued',
                        lambda *a, **kw: started.append(a) or 'attempt')

    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) is None
    assert started == []
    assert _workflow_wakes(conv_id) == []
    # The receipt stays durable for the resumed Turn of the next process.
    assert message_queue._background_receipts(conv_id, user_id=1, max_items=None)


def test_continuation_reusing_a_scheduled_identity_creates_no_attempt(
        _isolated_runtime, monkeypatch):
    """Refusal precedes the durable attempt: nothing is settled or aborted."""
    from lib.conversation_sync import command_service
    from lib.project_dispatch.admission import WORKSPACE_TASK_KEY, TrustedDispatch
    from lib.tasks_pkg import spawn

    conv_id = 'worker-identity-busy'
    live = _carrier(_isolated_runtime, conv_id, status='interrupted')
    live['_activeWorkerCount'] = 1  # Its worker still runs workspace delivery.
    executor = _Executor(LANE_TASK_ID)
    monkeypatch.setattr(spawn, '_agent_executor', executor)
    monkeypatch.setattr(TrustedDispatch, 'capture',
                        lambda self, config, *a: {**config, WORKSPACE_TASK_KEY: LANE_TASK_ID})
    durable = []

    class _Created(Exception):
        pass

    def create_attempt(*args, **kwargs):
        durable.append(kwargs.get('command_id'))
        raise _Created
    monkeypatch.setattr(command_service, 'create_attempt', create_attempt)
    monkeypatch.setattr(command_service, 'get_turn', lambda *a, **kw: {
        'turnId': 'ed1a211d', 'conversationId': conv_id, 'projection': {}})
    monkeypatch.setattr('lib.conversation_sync.workspace.resolve_workspace_config',
                        lambda config, *a, **kw: dict(config))
    monkeypatch.setattr('lib.acceptance.continuity.review_source', lambda *a, **kw: None)
    service = command_service.ConversationTurnCommandService(
        build_user_message=lambda *a: None, was_aborted_after=lambda *a: False,
        start_task=lambda *a, **kw: pytest.fail('no executor may start'))
    body = {'commandId': 'project-dispatch-background:receipts', 'operation': 'continue',
            'expectedProjectionRevision': 3, 'config': {}}
    trusted = TrustedDispatch({'assignmentId': '27ce29e9'})

    with pytest.raises(LifecycleConflict) as refused:
        service.resume_turn(conv_id, 'ed1a211d', 1, body, request_started_at=0.0,
                            trusted_dispatch=trusted)
    assert refused.value.code == 'lane_busy'
    assert durable == []
    assert _isolated_runtime.get(LANE_TASK_ID) is live
    assert live['status'] == 'interrupted' and not live.get('error')

    # Once the previous carrier has exited, the same continuation proceeds.
    live['_activeWorkerCount'] = 0
    executor.scheduled.clear()
    with pytest.raises(_Created):
        service.resume_turn(conv_id, 'ed1a211d', 1, body, request_started_at=0.0,
                            trusted_dispatch=trusted)
    assert durable == ['project-dispatch-background:receipts']


def test_background_continuation_of_a_scheduled_identity_is_a_no_op(
        _isolated_runtime, monkeypatch):
    """The wake leaves its receipts for the tick after the carrier exits."""
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.project_dispatch import admission

    conv_id = 'worker-background-identity-busy'
    seed_conversation(conv_id, user_id=1, title='Worker lane')
    _published_receipt(conv_id, 'bg_receipt')
    binding = {'assignmentId': '27ce29e9', 'owner': 1}
    monkeypatch.setattr(message_queue, '_background_receipts', lambda *a, **kw: [{
        'queueId': 'receipt-1', 'timestamp': 1,
        'config': {'model': 'test', admission.KEY: binding}}])
    monkeypatch.setattr('lib.background_command_delivery.settle_closed_dispatch_completion',
                        lambda *a, **kw: False)
    execution = {'turnId': 'ed1a211d', 'attemptId': 'bcfcebc9', 'taskId': LANE_TASK_ID}
    repository = SimpleNamespace(
        get_attempt=lambda attempt_id: {'attemptId': attempt_id, 'status': 'interrupted'},
        turn_get=lambda *a, **kw: {'projectionRevision': 3})
    monkeypatch.setattr(admission.TrustedDispatch, 'checked',
                        lambda self: {'execution': execution})
    monkeypatch.setattr(admission.TrustedDispatch, 'repository', property(lambda self: repository))
    monkeypatch.setattr(admission.TrustedDispatch, 'principal', property(lambda self: None))

    def busy(*args, **kwargs):
        raise LifecycleConflict('lane_busy', f'{LANE_TASK_ID} is still scheduled here')
    monkeypatch.setattr(conversation_turn_commands, 'resume_turn', busy)
    monkeypatch.setattr(message_queue, 'dispatch_next_queued',
                        lambda *a, **kw: pytest.fail('no ordinary wake for a bound lane'))

    assert message_queue.dispatch_completed_background_batch(conv_id, user_id=1) is None
    assert _workflow_wakes(conv_id) == []


def test_registry_never_replaces_a_live_carrier_of_its_identity(
        _isolated_runtime, monkeypatch):
    """A refused duplicate leaves the live carrier, its row and trace intact."""
    from lib.tasks_pkg import spawn
    from lib.tasks_pkg.manager import _registry

    from lib.agent_core.execution_session import ExecutionPhase

    conv_id = 'worker-registry-identity'
    live = _carrier(_isolated_runtime, conv_id, status='aborted')
    # As at 14:10:52: terminal persistence settled its execution session while
    # its worker still delivered the workspace.
    live['_executionSession'].settle(ExecutionPhase.CANCELLED, cause='server_shutdown')
    live['_activeWorkerCount'] = 1
    monkeypatch.setattr(spawn, '_agent_executor', _Executor(LANE_TASK_ID))
    monkeypatch.setattr(_registry, '_upsert_task_row',
                        lambda *a, **kw: pytest.fail('the live row must not be rewritten'))

    with pytest.raises(RuntimeError, match='already scheduled'):
        _registry._create_chat_task(
            conv_id, [{'role': 'user', 'content': 'continue'}],
            {'model': 'test', '_projectDispatchWorkspaceTaskId': LANE_TASK_ID},
            user_id=1)
    assert _isolated_runtime.get(LANE_TASK_ID) is live


@pytest.mark.parametrize(('role', 'drains'), [('api', False), ('execution', True), ('all', True)])
def test_maintenance_tick_wakes_batches_only_where_command_recovery_runs(
        monkeypatch, role, drains):
    """The API host cannot see the execution host's commands or carriers."""
    from lib.tasks_pkg.manager import _maintenance

    monkeypatch.setattr('runtime_guards.load_deployment_configuration',
                        lambda: SimpleNamespace(process_role=role))
    for name in ('retry_pending_terminal_persistence', 'reap_stuck_running_tasks',
                 'report_orphan_running_results', 'trim_released_task_heap'):
        monkeypatch.setattr(_maintenance, name, lambda *a, **kw: None)
    monkeypatch.setattr(_maintenance, '_claim_orphan_result_report', lambda: False)
    monkeypatch.setattr('lib.agent_core.execution_session.reconcile_overdue_execution_sessions',
                        lambda: None)
    monkeypatch.setattr(background_commands, 'retry_failed_background_deliveries', lambda: 0)
    monkeypatch.setattr(message_queue, 'reap_expired_queue_leases', lambda: None)
    drained = []
    monkeypatch.setattr(message_queue, 'drain_idle_background_batches',
                        lambda: drained.append(role) or [])

    _maintenance.cleanup_old_tasks()
    assert drained == ([role] if drains else [])
