"""One human request creates independent workers under conserved source policy."""
from copy import deepcopy
from types import SimpleNamespace
import threading
import time

import pytest

from tests.project_mod.test_coordinator_dispatch_tools import runtime as base_runtime
from tests.project_mod.test_project_dispatch_repository import context as context
from lib.conversations import coordinator_workers
from lib.project_dispatch.delegation import capture_worker_policy
from lib.project_dispatch.service import ProjectDispatchService

# Keep the imported fixture's registered alias visible to static tooling.
__all__ = ('base_runtime',)

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(base_runtime, monkeypatch):
    r = base_runtime
    from lib.project_dispatch import repository
    from lib.conversations import coordinator_workers as workers
    r.task.update(_turnId='coordinator-turn', _attemptId='coordinator-attempt', _attended=True)
    r.task['config'].update(codeExecEnabled=True, autoApply=True)
    # Execution identity has no reusable API scopes. The accepted canonical
    # request is the authority; the adapter must not forge a human API principal.
    r.task['_principalContext']['scopes'] = []
    r.turns['coordinator-turn'] = {'conversationId': 'coordinator', 'turnId': 'coordinator-turn',
        'actor': 'assistant', 'parentTurnId': 'human-turn'}
    r.turns['human-turn'] = r.c.turn
    r.attempts['coordinator-attempt'] = {'conversationId': 'coordinator', 'turnId': 'coordinator-turn',
        'attemptId': 'coordinator-attempt', 'taskId': r.task['id'], 'status': 'running', 'createdAt': 1_000_000,
        'config': deepcopy(r.task['config'])}
    r.c.repo.turn_get = lambda conv, turn, **kw: deepcopy(r.turns.get(turn))

    def attempt_get(attempt, *, user_id=None, include_execution_config=False):
        # Like turn.attempt.get: the accepted execution config only on request.
        row = deepcopy(r.attempts.get(attempt))
        if isinstance(row, dict) and not include_execution_config:
            row.pop('config', None)
        return row
    r.c.repo.attempt_get = attempt_get
    monkeypatch.setattr(repository, 'ProjectDispatchRepository', lambda owner, path: r.c.repo)
    monkeypatch.setattr(workers, 'ProjectDispatchRepository', lambda owner, path: r.c.repo)
    class Commands:
        def create_turn(self, conv, owner, request, **kwargs):
            trusted = kwargs['trusted_dispatch']; trusted.checked()
            assert kwargs['principal'].kind == 'system' and request['inputActor'] == 'virtual_user'
            assert r.c.turn['projection']['content'] in request['inputTurn']['content']
            assert 'Assigned objective:' in request['inputTurn']['content']
            turn, attempt, task = (prefix + conv for prefix in ('turn-', 'attempt-', 'task-'))
            if attempt in r.attempts:
                return SimpleNamespace(value={})
            trusted.bind_execution(turn, attempt)
            r.turns[turn] = {'conversationId': conv, 'turnId': turn, 'actor': 'assistant',
                            'projectionRevision': 1, 'projection': {'content': ''}}
            r.attempts[attempt] = {'conversationId': conv, 'turnId': turn, 'attemptId': attempt,
                                  'taskId': task, 'status': 'running'}
            r.calls.append(conv)
            return SimpleNamespace(value={})
    r.service = ProjectDispatchService(r.c.repo, commands=Commands(),
        turn_get=r.c.repo.turn_get, attempt_get=r.c.repo.attempt_get)
    monkeypatch.setattr(workers, 'ProjectDispatchService', lambda repo: r.service)
    # '/project' is a fake port; the real isolation preflight is specified on real directories.
    monkeypatch.setattr(workers, 'require_isolated_worker_root', lambda project: None)
    # Waits read the Coordinator's real project mailbox through the record store.
    from lib.storage import service as storage_service
    from tests.swarm.test_retained_agents import RecordClient
    records, conversation_storage = RecordClient(), storage_service._runtime.client()
    class Storage:
        def query(self, operation, payload):
            return records.query(operation, payload)
        def command(self, operation, payload, command_id):
            owner = records if operation.startswith('record.') else conversation_storage
            return owner.command(operation, payload, command_id)
    monkeypatch.setattr(storage_service, '_runtime', SimpleNamespace(client=lambda **kwargs: Storage()))
    r.c.conversations.snapshot = True
    capture_worker_policy(r.task, r.task['config'], [
        {'function': {'name': name}} for name in ('run_command', 'read_files', 'write_file', 'spawn_agents', 'timer_create')], 'model-a',
        worker_model='model-a')
    r.args = {'title': 'Implement one part', 'brief': 'Implement and verify the requested feature.', 'idempotency_key': 'part-one'}
    return r


def test_one_human_request_creates_and_starts_independent_worker_without_grant_click(runtime):
    r = runtime
    result = r.call('delegate_project_work', **r.args)
    assert result.get('dispatch') == 'accepted', result
    assert result['conversation_id'] != r.task['convId'] and r.calls == [result['conversation_id']]
    row = r.c.repo.read(result['assignment_id'])
    assert row['request']['authorizationSource'] == r.c.request['authorizationSource']
    assert row['grant']['subject'] == 'user:7' and row['grant']['tenant'] == 'tenant-a'
    assert row['request']['profile']['mode'] == 'project'
    assert 'run_command' in row['request']['profile']['tools']
    assert not {'spawn_agents', 'timer_create'} & set(row['request']['profile']['tools'])
    assert r.task['_principalContext']['scopes'] == []
    replay = r.call('delegate_project_work', **r.args)
    assert replay == result and len(r.calls) == len(r.created) == 1
    assert 'error' in r.call('delegate_project_work', **{**r.args, 'brief': 'Different request'})
    assert len(r.calls) == len(r.created) == 1


def test_project_history_lists_worker_without_delegation_and_exact_lookup_keeps_it(runtime, monkeypatch):
    r = runtime
    monkeypatch.setattr(r.c.repo, 'workspace_get', lambda *args, **kwargs: None)
    result = r.call('delegate_project_work', **r.args)
    stored = r.c.repo.read(result['assignment_id'])['request']
    assert stored['delegation']['attemptId'] == 'coordinator-attempt'
    listed = r.service.history_page(principal=r.c.principal)['assignments']
    row = next(view for view in listed if view['assignmentId'] == result['assignment_id'])
    assert row['request'] == {name: stored[name] for name in
                              ('projectPath', 'coordinatorId', 'recipientId', 'brief', 'profile')}
    exact = r.service.history_page(principal=r.c.principal, assignment_id=result['assignment_id'])
    assert exact['assignments'][0]['request'] == stored
    assert r.c.repo.read(result['assignment_id'])['request'] == stored


@pytest.mark.parametrize('violation', ['synthetic', 'unattended', 'source', 'attempt', 'parent', 'plan', 'readonly', 'binding', 'no-policy', 'model-argument'])
def test_invalid_source_never_creates_conversation_or_grant(runtime, violation):
    r = runtime
    if violation == 'synthetic': r.task['_principalContext']['kind'] = 'system'
    if violation == 'unattended': r.task['_attended'] = False
    if violation == 'source': r.c.turn['actor'] = 'virtual_user'
    if violation == 'attempt': r.attempts['coordinator-attempt']['taskId'] = 'another-task'
    if violation == 'parent': r.turns['coordinator-turn']['parentTurnId'] = 'missing'
    if violation == 'plan': r.task['config']['planMode'] = True
    if violation == 'readonly': r.task['config']['readOnlyPaths'] = ['/project']
    if violation == 'binding': r.c.binding.value['revision'] = 2
    if violation == 'no-policy': r.task.pop('_coordinatorWorkerPolicy')
    if violation == 'model-argument': r.args['model'] = 'unapproved-model'
    # Pin the original live role before changing its epoch.
    r.task['_coordinatorDispatchScope'] = {'user_id': 7, 'project_path': '/project',
                                          'conversation_id': 'coordinator', 'binding_revision': 1}
    assert 'error' in r.call('delegate_project_work', **r.args)
    assert not r.created and not r.calls and not r.c.repo.list()


def test_workers_have_no_spend_or_concurrency_ceiling(runtime, monkeypatch):
    r = runtime
    monkeypatch.setattr('runtime_guards.task_concurrency_hard_ceiling', lambda: 2)
    results = [r.call('delegate_project_work', **{**r.args, 'idempotency_key': str(i)}) for i in range(12)]
    assert all(item.get('dispatch') == 'accepted' for item in results), results
    assert all(r.c.repo.read(item['assignment_id'])['request']['budget'] == {} for item in results)
    assert all('maxConcurrent' not in r.c.repo.read(item['assignment_id'])['request']['delegation']['policy']
               for item in results)
    assert len(r.created) == len(r.calls) == 12


def test_background_completion_continues_same_assignment_and_moves_result_to_new_turn(runtime, monkeypatch):
    from lib.message_queue import dispatch_completed_background_batch
    from lib.tasks_pkg import background_commands
    r = runtime
    worker = r.call('delegate_project_work', **r.args)
    assignment = r.c.repo.read(worker['assignment_id'])
    from lib.agent_core.execution_budget import model_budget_admission
    from lib.project_dispatch import admission
    binding = admission.TrustedDispatch(r.c.repo.admission(assignment['id']))
    cfg = binding.capture({}, 7, binding.principal, worker['conversation_id'])
    cfg['_turnOwnerUserId'] = 7
    initial = {'id': assignment['execution']['taskId'], 'convId': worker['conversation_id'],
               'status': 'running', '_userId': 7,
               '_principalContext': binding.principal.to_payload(),
               '_turnId': assignment['execution']['turnId'],
               '_attemptId': assignment['execution']['attemptId'],
               '_attended': False, 'usage': {}, 'config': cfg,
               'abort_event': threading.Event()}
    assert cfg['model'] == assignment['request']['model']
    assert cfg.get('_turnPrincipalTenantId', '') == (binding.binding['tenant'] or '')
    r.attempts[assignment['execution']['attemptId']]['taskId'] = assignment['execution']['taskId']
    admission.bind_account(initial)
    lease = model_budget_admission(initial)(input_tokens=4, max_output_tokens=6)
    lease.mark_dispatched()
    lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    old = assignment['execution']
    old['taskId'] = 'project-worker:' + assignment['id']
    r.attempts[old['attemptId']]['taskId'] = old['taskId']
    r.attempts[old['attemptId']]['status'] = 'interrupted'
    r.turns[old['turnId']].update(status='interrupted', projectionRevision=1,
        settlement={'cause': 'server_restart', 'resumeOptions': [{'operation': 'continue', 'anchor': {}}]})
    monkeypatch.setattr(background_commands, 'conversation_results_complete', lambda **kwargs: True)
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    monkeypatch.setattr(chat_task_runtime, 'snapshot_owned', lambda **kwargs: [])
    import lib.message_queue as queue
    monkeypatch.setattr(queue, 'next_dispatchable_turn', lambda *args, **kwargs: None)
    queued = []
    class QueueClient:
        def command(self, operation, payload, command_id):
            assert operation == 'queue.enqueue'
            queued.append(payload)
            return {'queueId': payload['queue_id']}
        def query(self, operation, payload):
            assert operation == 'queue.list'
            return []
    monkeypatch.setattr(queue, '_queue_client', lambda **kwargs: QueueClient())
    monkeypatch.setattr('lib.message_queue._background_receipts', lambda *args, **kwargs: [{
        'queueId': 'receipt-1', 'kind': 'background_receipt', 'timestamp': 1,
        'config': deepcopy(r.attempts[old['attemptId']].get('config') or {
            'model': 'model-a', '_projectDispatchAdmission': r.c.repo.admission(assignment['id']),
            'canonicalProjectPath': '/project', 'projectPath': '/project'}),
    }])
    dispatched = []
    def create(conv, turn_id, owner, body, **kwargs):
        new_turn, new_attempt = turn_id, 'continuation-attempt'
        trusted = kwargs['trusted_dispatch']
        r.turns[new_turn] = {'conversationId': conv, 'turnId': new_turn,
                             'actor': 'assistant', 'projectionRevision': 1,
                             'projection': {'content': ''}, 'status': 'running'}
        r.attempts[new_attempt] = {'conversationId': conv, 'turnId': new_turn,
                                  'attemptId': new_attempt, 'taskId': old['taskId'],
                                  'status': 'running'}
        continued = trusted.continue_execution(new_turn, new_attempt)
        assert continued['execution']['attemptId'] == new_attempt
        dispatched.append(body)
        return SimpleNamespace(value={'attempt': r.attempts[new_attempt]})
    from lib.conversation_sync.runtime import conversation_turn_commands
    monkeypatch.setattr(conversation_turn_commands, 'resume_turn', create)
    assert dispatch_completed_background_batch(assignment['request']['recipientId'], user_id=7) == 'continuation-attempt'
    continued = r.c.repo.read(assignment['id'])['execution']
    assert continued['turnId'] == old['turnId'] and continued['attemptId'] == 'continuation-attempt'
    assert continued['taskId'] == old['taskId']
    assert dispatched[0]['config']['_projectDispatchAdmission']['assignmentId'] == assignment['id']
    r.turns[old['turnId']]['projection'] = {'content': 'Consolidated result'}
    r.attempts['continuation-attempt']['status'] = 'completed'
    r.attempts['coordinator-attempt']['status'] = 'completed'
    stored = r.c.repo.read(assignment['id'])
    stored['request']['delegation']['policy']['continuation'] = {
        'version': 1, 'model': 'model-a', 'workerModel': 'model-a',
        'workerTools': ['read_files', 'write_file', 'run_command'],
        'tools': ['get_project_workers', 'get_project_assignment']}
    def add_policy(value):
        row = next(row for row in value['assignments'] if row['id'] == assignment['id'])
        row['request']['delegation']['policy']['continuation'] = deepcopy(
            stored['request']['delegation']['policy']['continuation'])
        return row
    r.c.repo._change(add_policy)
    outcome = r.call('await_project_workers', assignment_ids=[assignment['id']], timeout_seconds=0)
    assert outcome['wait'] == 'settled' and outcome['workers'][0]['result_excerpt'] == 'Consolidated result'
    assert outcome['workers'][0]['turn_id'] == old['turnId']
    from lib.project_dispatch import continuation
    original = continuation.recover_coordinator
    wakes = []
    def wake(repository, **kwargs):
        result = original(repository, **kwargs)
        wakes.append(result)
        return result
    monkeypatch.setattr(continuation, 'recover_coordinator', wake)
    from lib.project_dispatch import recovery
    recovery.recover_project_dispatch(7, '/project', repository=r.c.repo,
                             service=r.service, was_aborted_after=lambda *args: False,
                             clock=lambda: r.c.now[0])
    assert wakes and all(result == ['accepted'] for result in wakes), wakes
    from lib.agent_core.execution_budget import model_budget_admission
    from lib.project_dispatch import admission
    binding = r.c.repo.admission(assignment['id'])
    trusted = admission.TrustedDispatch(binding)
    cfg = trusted.capture({}, 7, trusted.principal, assignment['request']['recipientId'])
    cfg.update(_turnOwnerUserId=7, _turnId=old['turnId'], _attemptId='continuation-attempt',
               _turnOperation='continue',
               _resumeSource={'attemptId': old['attemptId'], 'taskId': old['taskId'],
                              'status': 'interrupted', 'cause': 'server_restart'})
    r.attempts['continuation-attempt'].update(status='running', taskId=old['taskId'])
    restored = {'id': old['taskId'], 'convId': assignment['request']['recipientId'],
                'status': 'running', '_userId': 7,
                '_principalContext': trusted.principal.to_payload(),
                '_turnId': old['turnId'], '_attemptId': 'continuation-attempt',
                '_attended': False, 'usage': {}, 'config': cfg,
                'abort_event': threading.Event()}
    admission.bind_account(restored)
    lease = model_budget_admission(restored)(input_tokens=4, max_output_tokens=6)
    assert lease.ordinal == 2
    lease.mark_dispatched()
    lease.settle({'prompt_tokens': 5, 'completion_tokens': 7})
    attempts = r.c.repo.read(assignment['id'])['modelAttempts']
    assert [row['attempt_ordinal'] for row in attempts] == [1, 2]
    assert sum(row['reported_tokens'] for row in attempts) == 22


def test_cancelled_worker_background_completion_preserves_evidence_without_model_turn(
        runtime, monkeypatch, tmp_path):
    from lib import message_queue
    from lib.tasks_pkg import background_commands

    r = runtime
    monkeypatch.setattr('lib.project_dispatch.service.ProjectDispatchService', lambda repo: r.service)
    monkeypatch.setattr('lib.project_dispatch.repository.ProjectDispatchRepository', lambda owner, path: r.c.repo)
    worker = r.call('delegate_project_work', **r.args)
    assignment = r.c.repo.read(worker['assignment_id'])
    execution = assignment['execution']
    from lib.project_dispatch.admission import TrustedDispatch
    trusted = TrustedDispatch(r.c.repo.admission(assignment['id']))
    config = trusted.capture({}, 7, trusted.principal, worker['conversation_id'])
    config.update(_turnOwnerUserId=7, _turnId=execution['turnId'], _attemptId=execution['attemptId'])
    worker_task = {
        'id': execution['taskId'], 'convId': worker['conversation_id'], '_userId': 7,
        '_attemptId': execution['attemptId'], '_turnId': execution['turnId'],
        '_principalContext': trusted.principal.to_payload(), 'config': config,
    }
    r.attempts[execution['attemptId']]['config'] = config
    r.turns[execution['turnId']]['status'] = 'interrupted'
    queued = []
    model_starts = []

    class QueueClient:
        def command(self, operation, payload, command_id):
            queued.append((operation, payload, command_id))
            return {'queueId': payload['queue_id']}

        def query(self, operation, payload):
            return []

    monkeypatch.setattr(message_queue, '_queue_client', lambda **kwargs: QueueClient())
    monkeypatch.setattr(message_queue, 'next_dispatchable_turn', lambda *args, **kwargs: None)
    monkeypatch.setattr(background_commands, 'wake_completed_background_batch', lambda *args: None)
    r.service.turn_get = r.c.repo.turn_get
    r.service.attempt_get = r.c.repo.attempt_get
    from lib.conversation_sync.runtime import conversation_turn_commands
    monkeypatch.setattr(conversation_turn_commands, 'create_turn',
                        lambda *args, **kwargs: model_starts.append((args, kwargs)))
    monkeypatch.setattr(conversation_turn_commands, 'resume_turn',
                        lambda *args, **kwargs: model_starts.append((args, kwargs)))
    receipts = [{
        'queueId': 'cancelled-result-receipt', 'kind': message_queue.KIND_BACKGROUND_RECEIPT,
        'timestamp': 1, 'config': config,
        'payload': {'text': 'cancelled-attempt-result', '_backgroundCommand': 'bg_cancelled'},
        'backgroundCommand': {'commandId': 'bg_cancelled',
                              'resultPreview': 'cancelled-attempt-result\n[exit code: 0]'},
    }]
    monkeypatch.setattr(message_queue, '_background_receipts', lambda *args, **kwargs: receipts)

    def attach(conversation_id, turn_id, *, projection, expected_projection_revision, user_id, **kwargs):
        current = r.turns[turn_id]
        assert (conversation_id, expected_projection_revision, user_id) == (
            worker['conversation_id'], current['projectionRevision'], 7)
        current.update(projection=projection,
                       projectionRevision=expected_projection_revision + 1)
        return {'turn': deepcopy(current)}
    monkeypatch.setattr('lib.turn_lifecycle.update_turn_projection', attach)
    command_started = threading.Event()
    finish_command = threading.Event()

    def execute(_task, _context):
        command_started.set()
        assert finish_command.wait(10)
        return 'cancelled-attempt-result\n[exit code: 0]'

    monkeypatch.setattr(background_commands, '_records_root', lambda: str(tmp_path))
    command_id = background_commands.start_background_command(
        task=worker_task, command='produce cancelled result', execute=execute, config=config)
    try:
        assert command_started.wait(10)

        def stop(attempt_id, owner, provenance):
            assert (attempt_id, owner) == (execution['attemptId'], 7)
            r.attempts[attempt_id]['status'] = 'interrupted'
            r.turns[execution['turnId']].update(
                status='interrupted', settlement={'cause': 'user_abort', 'detail': provenance})
            return {'stopped': True}
        monkeypatch.setattr(r.service, '_cancel_execution', stop)
        cancelled = r.call('cancel_project_work', assignment_id=assignment['id'])
        assert cancelled['revoked'] is True
        finish_command.set()
        with background_commands._lock:
            command_done = background_commands._registry[command_id]['done']
        assert command_done.wait(10)
        with background_commands._lock:
            background_commands._registry[command_id]['state']['delivery_status'] = 'queued'
        assert message_queue.dispatch_completed_background_batch(
            worker['conversation_id'], user_id=7) is None
        assert model_starts == []
        assert not [call for call in queued
                    if call[1].get('kind') == message_queue.KIND_WORKFLOW]
        evidence = r.call('get_project_assignment', assignment_id=assignment['id'])
        assert evidence['revoked'] is True
        assert evidence['execution']['attemptId'] == execution['attemptId']
        assert evidence['execution']['status'] == 'interrupted'
        injections = evidence['execution']['result']['_bgCommandInjects']
        assert len(injections) == 1
        preview = injections[0]['previews'][0]
        assert preview['commandId'] == 'bg_cancelled'
        assert 'cancelled-attempt-result' in preview['text']
        assert preview['backgroundCommand']['resultPreview'] == (
            'cancelled-attempt-result\n[exit code: 0]')

        assert message_queue.dispatch_completed_background_batch(
            worker['conversation_id'], user_id=7) is None
        replayed = r.call('get_project_assignment', assignment_id=assignment['id'])
        assert replayed['execution']['result']['_bgCommandInjects'] == injections

        retired = []
        monkeypatch.setattr(message_queue, '_finalize_queue_dispatch',
                            lambda *args, **kwargs: retired.append((args, kwargs)))
        wake = {'queueId': 'already-queued-wake', 'kind': message_queue.KIND_WORKFLOW}
        payload = {'_backgroundBatchWake': ['cancelled-result-receipt']}
        assert message_queue._dispatch_queued_turn(
            worker['conversation_id'], wake, payload, config, None, user_id=7) is None
        assert model_starts == []
        assert len(retired) == 1
        assert retired[0][0][:2] == (worker['conversation_id'], 'already-queued-wake')
    finally:
        finish_command.set()
        with background_commands._lock:
            background_commands._registry.pop(command_id, None)


def test_unbound_worker_completion_after_cancel_preserves_evidence_without_model_turn(
        runtime, monkeypatch):
    """A turn that ran without the Worker's binding cannot keep waking it after cancel."""
    from lib import message_queue
    from lib.background_command_delivery import settle_revoked_dispatch_completion
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    r = runtime
    monkeypatch.setattr('lib.project_dispatch.service.ProjectDispatchService', lambda repo: r.service)
    worker = r.call('delegate_project_work', **r.args)
    conversation = worker['conversation_id']
    assignment = r.c.repo.read(worker['assignment_id'])
    execution = assignment['execution']
    r.attempts[execution['attemptId']]['status'] = 'interrupted'
    r.turns[execution['turnId']]['status'] = 'interrupted'
    r.service.turn_get = r.c.repo.turn_get
    r.service.attempt_get = r.c.repo.attempt_get
    # An unbound turn's command receipts carry ordinary config, no dispatch binding.
    unbound = {'model': 'model-a', 'projectPath': '/project'}
    receipts = [{
        'queueId': 'unbound-result-receipt', 'kind': message_queue.KIND_BACKGROUND_RECEIPT,
        'timestamp': 1, 'config': unbound,
        'payload': {'text': 'unbound-turn-result', '_backgroundCommand': 'bg_unbound'},
        'backgroundCommand': {'commandId': 'bg_unbound', 'resultPreview': 'unbound-turn-result'},
    }]
    # A live grant and an ordinary conversation keep the ordinary wake.
    assert settle_revoked_dispatch_completion(
        unbound, receipts, conv_id=conversation, user_id=7) is False
    assert settle_revoked_dispatch_completion(
        unbound, receipts, conv_id='coordinator', user_id=7) is False

    queued, model_starts, retired = [], [], []

    class QueueClient:
        def command(self, operation, payload, command_id):
            queued.append((operation, payload, command_id))
            return {'queueId': payload['queue_id']}

        def query(self, operation, payload):
            return []

    monkeypatch.setattr(message_queue, '_queue_client', lambda **kwargs: QueueClient())
    monkeypatch.setattr(message_queue, 'next_dispatchable_turn', lambda *args, **kwargs: None)
    monkeypatch.setattr(message_queue, 'dispatch_next_queued', lambda *args, **kwargs: None)
    monkeypatch.setattr(message_queue, '_background_receipts', lambda *args, **kwargs: receipts)
    monkeypatch.setattr(message_queue, '_finalize_queue_dispatch',
                        lambda *args, **kwargs: retired.append(args))
    monkeypatch.setattr(background_commands, 'conversation_results_complete', lambda **kwargs: True)
    monkeypatch.setattr(chat_task_runtime, 'snapshot_owned', lambda **kwargs: [])
    from lib.conversation_sync.runtime import conversation_turn_commands
    monkeypatch.setattr(conversation_turn_commands, 'create_turn',
                        lambda *args, **kwargs: model_starts.append((args, kwargs)))
    monkeypatch.setattr(conversation_turn_commands, 'resume_turn',
                        lambda *args, **kwargs: model_starts.append((args, kwargs)))

    def attach(conversation_id, turn_id, *, projection, expected_projection_revision, user_id, **kwargs):
        current = r.turns[turn_id]
        assert (conversation_id, expected_projection_revision, user_id) == (
            conversation, current['projectionRevision'], 7)
        current.update(projection=projection,
                       projectionRevision=expected_projection_revision + 1)
        return {'turn': deepcopy(current)}
    monkeypatch.setattr('lib.turn_lifecycle.update_turn_projection', attach)
    monkeypatch.setattr(r.service, '_cancel_execution', lambda *args: {'stopped': True})
    assert r.call('cancel_project_work', assignment_id=assignment['id'])['revoked'] is True

    assert message_queue.dispatch_completed_background_batch(conversation, user_id=7) is None
    assert model_starts == []
    assert not [call for call in queued if call[1].get('kind') == message_queue.KIND_WORKFLOW]
    evidence = r.call('get_project_assignment', assignment_id=assignment['id'])
    assert evidence['execution']['attemptId'] == execution['attemptId']
    injections = evidence['execution']['result']['_bgCommandInjects']
    assert [preview['commandId'] for preview in injections[0]['previews']] == ['bg_unbound']
    assert 'unbound-turn-result' in injections[0]['previews'][0]['text']

    wake = {'queueId': 'unbound-queued-wake', 'kind': message_queue.KIND_WORKFLOW}
    assert message_queue._dispatch_queued_turn(
        conversation, wake, {'_backgroundBatchWake': ['unbound-result-receipt']},
        unbound, None, user_id=7) is None
    assert model_starts == []
    assert [args[:2] for args in retired] == [(conversation, 'unbound-queued-wake')]


def test_cancel_stops_every_live_attempt_of_the_worker_conversation(runtime, monkeypatch):
    """The association can name a long-settled attempt while an unbound turn runs."""
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    r = runtime
    monkeypatch.setattr('lib.project_dispatch.service.ProjectDispatchService', lambda repo: r.service)
    first = r.call('delegate_project_work', **r.args)
    second = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'part-two'})
    live = [{'id': 'coordinator-task', 'convId': 'coordinator', 'status': 'running',
             '_attemptId': 'coordinator-attempt'}]
    for index, worker in enumerate((first, second)):
        execution = r.c.repo.read(worker['assignment_id'])['execution']
        r.attempts[execution['attemptId']]['status'] = 'interrupted'
        live += [{'id': f'unbound-task-{index}', 'convId': worker['conversation_id'],
                  'status': 'running', '_attemptId': f'unbound-attempt-{index}'},
                 {'id': f'stopped-task-{index}', 'convId': worker['conversation_id'],
                  'status': 'running', 'aborted': True, '_attemptId': f'stopped-attempt-{index}'}]
    monkeypatch.setattr(chat_task_runtime, 'snapshot_owned',
                        lambda *, user_id: deepcopy(live) if user_id == 7 else [])
    cancelled = []
    monkeypatch.setattr(r.service, '_cancel_execution',
                        lambda attempt_id, owner, provenance: cancelled.append(
                            (attempt_id, owner, provenance)))

    assert r.call('cancel_project_work', assignment_id=first['assignment_id'])['revoked'] is True
    stale = r.c.repo.read(first['assignment_id'])['execution']['attemptId']
    assert [row[:2] for row in cancelled] == [(stale, 7), ('unbound-attempt-0', 7)]
    assert all('cancel_project_work' in row[2] and first['assignment_id'] in row[2]
               for row in cancelled)

    cancelled.clear()
    row = r.c.repo.read(second['assignment_id'])
    r.service.revoke(second['assignment_id'], principal=r.c.principal,
                     expected_revision=row['grant']['revision'])
    assert [item[:2] for item in cancelled] == [
        (row['execution']['attemptId'], 7), ('unbound-attempt-1', 7)]


def test_wait_uses_terminal_notification_and_removes_subscription(runtime):
    from lib.agent_core import admission
    r = runtime
    worker = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(worker['assignment_id']); attempt = row['execution']['attemptId']
    ready = threading.Event(); real = admission.on_terminal
    # Worker settlement is real durable evidence in the injected repository;
    # notification alone is deliberately insufficient to declare completion.
    def subscribe(task, fn):
        unsubscribe = real(task, fn); ready.set(); return unsubscribe
    result = []
    from unittest.mock import patch
    with patch.object(admission, 'on_terminal', subscribe):
        thread = threading.Thread(target=lambda: result.append(r.call('await_project_workers',
            assignment_ids=[row['id']], timeout_seconds=2)))
        thread.start(); assert ready.wait(1)
        r.turns[row['execution']['turnId']]['projection'] = {'content': 'Verified result'}
        r.attempts[attempt]['status'] = 'completed'
        admission.fire_terminal_callbacks(worker['task_id'])
        thread.join(2)
    assert not thread.is_alive() and result[0]['wait'] == 'settled', result
    assert result[0]['workers'][0]['result_excerpt'] == 'Verified result'
    assert worker['task_id'] not in admission._term_callbacks
    assert len(r.calls) == 1


def test_cancelled_wait_wakes_and_unsubscribes_without_stopping_the_worker(runtime, monkeypatch):
    from lib.agent_core import admission
    from lib.tasks_pkg.tool_runtime import ToolExecutionContext
    r = runtime
    subscribed = _subscriptions(monkeypatch)
    worker = r.call('delegate_project_work', **r.args)
    context = ToolExecutionContext(
        task=r.task, round_num=1, tool_call_id='wait', tool_name='await_project_workers',
        owner_user_id=7, round_entry={})

    def cancel():
        from lib.conversations.coordinator_workers import await_workers
        from lib.conversations.coordinator_tools import _scope
        scope = _scope(r.task)
        return await_workers(
            r.task, scope, {'assignment_ids': [worker['assignment_id']], 'timeout_seconds': 1800},
            runtime_context=context)

    results = []
    thread = threading.Thread(target=lambda: results.append(cancel()))
    thread.start()
    assert subscribed(worker['task_id'])
    context.request_resource_cancellation()
    thread.join(5)
    assert not thread.is_alive()
    assert results[0]['wait'] == 'timeout'
    assert not r.task.get('aborted')
    assert worker['task_id'] not in admission._term_callbacks
    assert len(r.calls) == 1


def test_timeout_and_stop_leave_no_callbacks_and_never_restart_workers(runtime):
    from lib.agent_core import admission
    r = runtime; worker = r.call('delegate_project_work', **r.args)
    args = {'assignment_ids': [worker['assignment_id']], 'timeout_seconds': 0.01}
    assert r.call('await_project_workers', **args)['wait'] == 'timeout'
    assert worker['task_id'] not in admission._term_callbacks
    r.task['aborted'] = True
    assert r.call('await_project_workers', **args)['wait'] == 'cancelled'
    assert len(r.calls) == 1


def test_delegate_and_await_reach_handlers_through_ordinary_pipeline(runtime, monkeypatch):
    from tests.tools.test_tool_settle_all_lanes import _mk_task, _mk_tc
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tools.coordinator import COORDINATOR_TOOLS
    r = runtime
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *args, **kwargs: True)
    r.task.update({key: value for key, value in _mk_task().items() if key not in r.task})
    messages = []
    def run(name, arguments):
        parsed = _mk_tc(name, name, len(messages), args=arguments)
        r.task['toolRounds'] = [parsed[5]]
        execute_tool_pipeline(r.task, [parsed], r.task['config'], '/project', True,
                              COORDINATOR_TOOLS, messages, [], 0, 'model-a')
        assert parsed[5]['status'] == 'done', messages[-1]
    run('delegate_project_work', r.args)
    assert len(r.created) == len(r.calls) == 1
    row = r.c.repo.list()[0]
    r.attempts[row['execution']['attemptId']]['status'] = 'completed'
    run('await_project_workers', {'assignment_ids': [row['id']]})
    assert len(r.calls) == 1


def test_stop_of_original_request_prevents_new_grants_even_before_task_flag_catches_up(runtime):
    from lib.project_dispatch.repository import record_stop
    r = runtime
    r.c.now[0] = 1010
    record_stop(7, 'coordinator', 1005, client=r.c.client)
    assert 'stopped' in r.call('delegate_project_work', **r.args)['error']
    assert not r.created and not r.c.repo.list()


def test_long_running_request_keeps_delegating_with_no_deadline(runtime):
    r = runtime
    first = r.call('delegate_project_work', **r.args)
    assert first['dispatch'] == 'accepted' and 'budget' not in first
    r.c.now[0] += 30 * 24 * 3600
    late = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'late'})
    assert late['dispatch'] == 'accepted', late
    assert len(r.created) == len(r.calls) == 2


def _worker_task(r):
    from lib.project_dispatch import admission
    import time
    r.c.now[0] = time.time()
    r.attempts['coordinator-attempt']['createdAt'] = int(r.c.now[0] * 1000)
    result = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(result['assignment_id'])
    trusted = admission.TrustedDispatch(r.c.repo.admission(row['id']))
    config = trusted.capture({}, 7, trusted.principal, result['conversation_id'])
    config['_turnOwnerUserId'] = 7
    task = {'id': result['task_id'], 'convId': result['conversation_id'], 'config': config,
            'status': 'running', '_userId': 7, '_principalContext': trusted.principal.to_payload(),
            '_attended': False, '_turnId': row['execution']['turnId'], '_attemptId': row['execution']['attemptId'],
            'abort_event': threading.Event(), 'usage': {}}
    admission.bind_account(task)
    return task, row


def test_parallel_live_tools_share_one_account_but_uncertain_effect_blocks_more(runtime, monkeypatch):
    from lib.llm.llm_errors import AbortedError
    r = runtime; task, row = _worker_task(r); budget = task['_executionBudget']
    first = budget.begin_tool_attempt(task, 'first', 'read_files', 1)
    second = budget.begin_tool_attempt(task, 'second', 'read_files', 1)
    budget.finish_tool_attempt(first, phase='executor_returned')
    mutate = r.c.repo.mutate
    monkeypatch.setattr(r.c.repo, 'mutate', lambda *a: (_ for _ in ()).throw(OSError('lost settlement')))
    budget.finish_tool_attempt(second, phase='executor_returned')
    monkeypatch.setattr(r.c.repo, 'mutate', mutate)
    with pytest.raises(AbortedError, match='unconfirmed'):
        budget.begin_tool_attempt(task, 'third', 'write_file', 2)
    saved = r.c.repo.read(row['id'])['toolAttempts']
    assert saved[0]['phase'] == 'executor_returned' and saved[1]['phase'] == 'intent'


def test_worker_cannot_enlarge_its_frozen_capability_config(runtime):
    from lib.project_dispatch import admission
    from lib.llm.llm_errors import AbortedError
    worker, _ = _worker_task(runtime)
    worker['config']['schedulerEnabled'] = True
    with pytest.raises(AbortedError):
        admission.validate_task(worker)


def test_changed_delegation_source_invalidates_dependency_without_losing_read_evidence(runtime):
    from lib.project_dispatch.dependencies import _evidence
    r = runtime
    result = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(result['assignment_id'])
    r.turns['coordinator-turn']['parentTurnId'] = 'different-source'
    assert _evidence(r.c.repo, row)[:2] == ('invalid', 'scope_changed')
    viewed = r.call('get_project_assignment', assignment_id=row['id'])
    assert viewed['assignmentId'] == row['id'] and len(r.calls) == 1


def test_coordinator_review_is_exact_and_not_human_acceptance(runtime):
    r = runtime
    result = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(result['assignment_id']); execution = row['execution']
    scope = {'user_id': 7, 'project_path': '/project', 'conversation_id': 'coordinator', 'binding_revision': 1}
    arguments = {'assignment_id': row['id'], 'result_revision': 2, 'decision': 'accept', 'note': 'Evidence checked'}
    with pytest.raises(ValueError, match='exact current completed'):
        r.service.review_for_coordinator(r.task, scope, arguments)
    r.attempts[execution['attemptId']]['status'] = 'completed'
    r.turns[execution['turnId']]['projectionRevision'] = 2
    viewed = r.service.review_for_coordinator(r.task, scope, arguments)
    assert viewed['review']['actor'] == 'coordinator' and not viewed['canonicalDelivery']
    assert r.service.review_for_coordinator(r.task, scope, arguments) == viewed
    with pytest.raises(ValueError, match='immutable'):
        r.service.review_for_coordinator(r.task, scope, {**arguments, 'note': 'different review'})
    r.c.binding.value['revision'] = 2
    with pytest.raises(ValueError):
        r.service.review_for_coordinator(r.task, scope, arguments)


@pytest.mark.parametrize('violation', ['readonly', 'synthetic', 'unattended', 'source'])
def test_role_visible_delegation_keeps_request_authority_gates(runtime, violation):
    from tests.tasks_pkg.test_coordinator_tool_surface import assemble, names, wire

    r = runtime
    if violation == 'readonly':
        r.task['config']['readOnlyPaths'] = ['/project']
    elif violation == 'synthetic':
        r.task['_principalContext']['kind'] = 'system'
    elif violation == 'unattended':
        r.task['_attended'] = False
    else:
        r.c.turn['actor'] = 'virtual_user'
    tools = assemble(r.task)
    assert 'delegate_project_work' in names(wire(r.task, tools)['tools'])
    result = r.call('delegate_project_work', **r.args)
    assert 'error' in result
    assert not r.created and not r.calls and not r.c.repo.list()


@pytest.mark.parametrize('aliased', [False, True])
def test_plain_folder_is_prepared_and_delegation_proceeds(runtime, monkeypatch, tmp_path, aliased):
    from lib.conversations import coordinator_workers as workers
    from lib.project_dispatch import admission
    r = runtime
    plain = (tmp_path / 'rsi-exam').resolve()
    plain.mkdir()
    (plain / 'plan.md').write_text('# plan\n')
    project = plain
    if aliased:
        project = tmp_path / 'project-alias'
        project.symlink_to(plain, target_is_directory=True)
    monkeypatch.setattr(workers, 'require_isolated_worker_root',
                        lambda _: admission.require_isolated_worker_root(str(project)))
    accepted = r.call('delegate_project_work', **r.args)
    assert accepted['dispatch'] == 'accepted', accepted
    assert (plain / '.git').is_dir() and (plain / 'AGENTS.md').is_file()
    assert (plain / 'plan.md').read_text() == '# plan\n'
    assert r.task['convId'] == 'coordinator' and len(r.created) == len(r.calls) == 1
    assert r.call('delegate_project_work', **r.args) == accepted
    assert len(r.created) == len(r.calls) == 1


def test_unisolatable_root_refuses_before_any_worker_conversation_or_grant(runtime, monkeypatch, tmp_path):
    import subprocess
    from lib.conversations import coordinator_workers as workers
    from lib.project_dispatch import admission
    r = runtime
    repository = (tmp_path / 'repo').resolve()
    (repository / 'nested').mkdir(parents=True)
    for args in (['init', '-q'], ['-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                                  'commit', '-q', '--allow-empty', '-m', 'mine']):
        subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True, timeout=20)
    checked = []
    def preflight(project):
        checked.append(project)
        admission.require_isolated_worker_root(str(repository / 'nested'))
    monkeypatch.setattr(workers, 'require_isolated_worker_root', preflight)
    refused = r.call('delegate_project_work', **r.args)
    assert checked == ['/project']
    assert 'subdirectory of the Git repository' in refused['error'] and 'git ' not in refused['error']
    assert refused['execution_started'] is False and 'instead of retrying' in refused['recovery']
    assert not r.created and not r.calls and not r.c.repo.list()
    assert not (repository / 'nested' / '.git').exists()


def test_accepted_delegation_replays_without_rechecking_the_root(runtime, monkeypatch):
    from lib.conversations import coordinator_workers as workers
    r = runtime
    accepted = r.call('delegate_project_work', **r.args)
    assert accepted['dispatch'] == 'accepted', accepted
    monkeypatch.setattr(workers, 'require_isolated_worker_root',
                        lambda project: pytest.fail('an accepted replay rechecked the root'))
    assert r.call('delegate_project_work', **r.args) == accepted
    assert len(r.created) == len(r.calls) == 1


def test_await_reports_the_settled_cause_of_a_failed_worker(runtime):
    r = runtime
    failed = r.c.repo.read(r.call('delegate_project_work', **r.args)['assignment_id'])
    done = r.c.repo.read(r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'two'})['assignment_id'])
    r.attempts[failed['execution']['attemptId']]['status'] = 'failed'
    r.turns[failed['execution']['turnId']]['settlement'] = {'outcome': 'failed', 'error': {
        'kind': 'task_start_failed', 'detail': 'The agent worker could not begin task execution: no Git',
        'hint': 'Initialize Git in the project root', 'raw': 'x' * 300, 'message': 'title'}}
    r.attempts[done['execution']['attemptId']]['status'] = 'completed'
    result = r.call('await_project_workers', assignment_ids=[failed['id'], done['id']], timeout_seconds=2)
    assert result['wait'] == 'settled', result
    first, second = result['workers']
    assert first['status'] == 'failed' and first['error'] == {
        'kind': 'task_start_failed', 'detail': 'The agent worker could not begin task execution: no Git',
        'hint': 'Initialize Git in the project root'}
    assert second['status'] == 'completed' and 'error' not in second


def _subscriptions(monkeypatch):
    """Observe real terminal subscriptions; a notification alone never settles a worker."""
    from lib.agent_core import admission
    real, seen, changed = admission.on_terminal, [], threading.Condition()
    def subscribe(task_id, fn):
        unsubscribe = real(task_id, fn)
        with changed:
            seen.append(task_id)
            changed.notify_all()
        return unsubscribe
    monkeypatch.setattr(admission, 'on_terminal', subscribe)
    def subscribed(*task_ids):
        with changed:
            return changed.wait_for(lambda: set(task_ids) <= set(seen), timeout=2)
    return subscribed


def _await_in_thread(r, **arguments):
    result = []
    def run():
        started = time.monotonic()
        result.append((r.call('await_project_workers', **arguments), time.monotonic() - started))
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def _delivery_receipts(r, monkeypatch):
    """Serve each worker's exact registered workspace receipt to delivery_view."""
    from lib.project_dispatch import admission
    receipts = {}
    def workspace_get(project, task_id, *, user_id):
        for identity, receipt in list(receipts.items()):
            row = r.c.repo.read(identity)
            if r.attempts[row['execution']['attemptId']]['taskId'] == task_id:
                return {'task_id': task_id, 'user_id': user_id, 'project_root': project,
                        'origin': {admission.KEY: r.c.repo._admission(row), 'delivery': dict(receipt)}}
        return None
    monkeypatch.setattr(r.c.repo, 'workspace_get', workspace_get)
    return receipts


def test_wait_returns_when_any_running_worker_settles_and_names_the_rest(runtime, monkeypatch):
    from lib.agent_core import admission
    r = runtime; subscribed = _subscriptions(monkeypatch)
    first = r.call('delegate_project_work', **r.args)
    second = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'two'})
    done, running = (r.c.repo.read(item['assignment_id']) for item in (first, second))
    thread, result = _await_in_thread(r, assignment_ids=[done['id'], running['id']], timeout_seconds=30)
    assert subscribed(first['task_id'], second['task_id'])
    r.turns[done['execution']['turnId']]['projection'] = {'content': 'First verified result'}
    r.attempts[done['execution']['attemptId']]['status'] = 'completed'
    admission.fire_terminal_callbacks(first['task_id'])
    thread.join(5)
    assert not thread.is_alive() and result, 'the wait held a settled result until every worker settled'
    value, elapsed = result[0]
    assert value['wait'] == 'progress' and elapsed < 5, value
    assert [row['status'] for row in value['workers']] == ['completed', 'running']
    assert value['workers'][0]['result_excerpt'] == 'First verified result'
    assert running['id'] in value['instruction'] and done['id'] not in value['instruction']
    assert not {first['task_id'], second['task_id']} & set(admission._term_callbacks)
    assert len(r.calls) == 2


def test_worker_settled_before_the_call_never_ends_the_wait_early(runtime, monkeypatch):
    from lib.agent_core import admission
    r = runtime; subscribed = _subscriptions(monkeypatch)
    done = r.c.repo.read(r.call('delegate_project_work', **r.args)['assignment_id'])
    running = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'two'})
    r.attempts[done['execution']['attemptId']]['status'] = 'completed'
    ids = [done['id'], running['assignment_id']]
    started = time.monotonic()
    timed_out = r.call('await_project_workers', assignment_ids=ids, timeout_seconds=0.6)
    assert timed_out['wait'] == 'timeout' and time.monotonic() - started >= 0.6, timed_out
    assert running['task_id'] not in admission._term_callbacks
    thread, result = _await_in_thread(r, assignment_ids=ids, timeout_seconds=30)
    assert subscribed(running['task_id'])
    row = r.c.repo.read(running['assignment_id'])
    r.attempts[row['execution']['attemptId']]['status'] = 'failed'
    admission.fire_terminal_callbacks(running['task_id'])
    thread.join(5)
    assert not thread.is_alive() and result[0][0]['wait'] == 'settled', result
    assert running['task_id'] not in admission._term_callbacks


@pytest.mark.parametrize('state', ['blocked', 'failed', 'preserved'])
def test_held_delivery_settles_a_delivery_wait_with_its_reason(runtime, monkeypatch, state):
    from lib.agent_core import admission
    r = runtime; subscribed = _subscriptions(monkeypatch)
    worker = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(worker['assignment_id'])
    receipts = _delivery_receipts(r, monkeypatch)
    receipts[row['id']] = {'state': 'working'}
    arguments = {'assignment_ids': [row['id']], 'wait_for': 'delivery', 'timeout_seconds': 30}
    thread, result = _await_in_thread(r, **arguments)
    assert subscribed(worker['task_id'])
    r.attempts[row['execution']['attemptId']]['status'] = 'completed'
    receipts[row['id']] = {'state': state, 'reason': 'canonical_dirty', 'checkpointSha': 'abc123'}
    admission.fire_terminal_callbacks(worker['task_id'])
    thread.join(5)
    assert not thread.is_alive() and result, 'a held delivery receipt kept the wait open'
    value, elapsed = result[0]
    assert value['wait'] == 'settled' and elapsed < 5, value
    assert value['workers'][0]['delivery']['status'] == state
    assert value['workers'][0]['delivery']['reason'] == 'canonical_dirty'
    assert value['workers'][0]['canonical_delivery'] is False
    assert 'delivery.reason' in value['instruction'] and row['id'] in value['instruction']
    started = time.monotonic()
    assert r.call('await_project_workers', **arguments)['wait'] == 'settled'
    assert time.monotonic() - started < 2
    assert worker['task_id'] not in admission._term_callbacks


@pytest.fixture
def coordinator_inbox():
    from lib import agent_inbox
    agent_inbox.reset_for_test('coordinator')
    yield agent_inbox
    agent_inbox.reset_for_test('coordinator')


@pytest.mark.parametrize('mode', ['user-steer', 'background-command'])
def test_input_for_the_coordinator_ends_the_wait_and_names_the_pending_workers(
        runtime, monkeypatch, coordinator_inbox, mode):
    from lib.agent_core import admission
    r = runtime; subscribed = _subscriptions(monkeypatch)
    worker = r.call('delegate_project_work', **r.args)
    arguments = {'assignment_ids': [worker['assignment_id']], 'timeout_seconds': 30}
    thread, result = _await_in_thread(r, **arguments)
    assert subscribed(worker['task_id'])
    # A human message (or another completion) waits for the Coordinator's next round.
    coordinator_inbox.enqueue('coordinator', 'Status? Also add a changelog entry.', mode=mode)
    thread.join(5)
    assert not thread.is_alive() and result, 'the wait held human input until a worker settled'
    value, elapsed = result[0]
    assert value['wait'] == 'pending_input' and elapsed < 5, value
    assert value['workers'][0]['status'] == 'running'
    assert value['instruction'] == _pending_input_instruction(worker['assignment_id']), value['instruction']
    assert worker['task_id'] not in admission._term_callbacks
    # Input still waiting when a call starts ends that call at once too.
    started = time.monotonic()
    assert r.call('await_project_workers', **arguments)['wait'] == 'pending_input'
    assert time.monotonic() - started < 2
    assert len(r.calls) == 1, 'a wait that yields to input never restarts or recreates work'


def _worker_message(worker, key):
    from lib.conversations import project_messages
    sender = {'id': 'sender-' + key, 'convId': worker['conversation_id'], '_userId': 7,
              'config': {'projectPath': '/project'}}
    return project_messages.execute('send_project_message', {
        'conversation_id': 'coordinator', 'kind': 'question', 'idempotency_key': key,
        'content': 'Which API version should this target?'}, sender)['message']


def test_project_message_submitted_during_the_wait_ends_it_but_an_earlier_one_does_not(
        runtime, monkeypatch, coordinator_inbox):
    from lib.agent_core import admission
    r = runtime
    monkeypatch.setattr(coordinator_workers, '_DURABLE_RECHECK_SECONDS', 0.2)
    worker = r.call('delegate_project_work', **r.args)
    _worker_message(worker, 'earlier')
    arguments = {'assignment_ids': [worker['assignment_id']], 'timeout_seconds': 0.6}
    # Already waiting at call start: the Coordinator's round already acquires it,
    # so it never makes every later wait return at once in a paid model loop.
    assert r.call('await_project_workers', **arguments)['wait'] == 'timeout'
    subscribed = _subscriptions(monkeypatch)
    thread, result = _await_in_thread(r, **{**arguments, 'timeout_seconds': 30})
    assert subscribed(worker['task_id'])
    question = _worker_message(worker, 'question')
    thread.join(5)
    assert not thread.is_alive() and result, 'a Worker question waited for the Coordinator timeout'
    value, elapsed = result[0]
    assert value['wait'] == 'pending_input' and elapsed < 5, value
    assert value['instruction'] == _pending_input_instruction(worker['assignment_id'])
    assert question['state'] == 'submitted' and worker['task_id'] not in admission._term_callbacks


def test_notification_before_durable_settlement_rechecks_promptly_and_cheaply(runtime, monkeypatch):
    from lib.agent_core import admission
    r = runtime; subscribed = _subscriptions(monkeypatch)
    worker = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(worker['assignment_id'])
    reads, turn_get = [], r.service.turn_get
    def recorded(conversation, turn, **kwargs):
        reads.append(kwargs.get('include_projection', True))
        return turn_get(conversation, turn, **kwargs)
    monkeypatch.setattr(r.service, 'turn_get', recorded)
    thread, result = _await_in_thread(r, assignment_ids=[row['id']], timeout_seconds=30)
    assert subscribed(worker['task_id'])
    # The in-memory signal precedes the durable attempt status it announces.
    admission.fire_terminal_callbacks(worker['task_id'])
    time.sleep(0.8)
    assert thread.is_alive(), 'a notification alone declared settlement'
    r.turns[row['execution']['turnId']]['projection'] = {'content': 'Durable result'}
    r.attempts[row['execution']['attemptId']]['status'] = 'completed'
    flipped = time.monotonic()
    thread.join(5)
    assert not thread.is_alive() and result[0][0]['wait'] == 'settled', result
    assert time.monotonic() - flipped < 1.5, 'the wait slept to the slow durable recheck'
    assert result[0][0]['workers'][0]['result_excerpt'] == 'Durable result'
    # Polls read Turn metadata only; the returned row alone reads the projection.
    assert reads.count(False) >= 3 and reads.count(True) == 1, reads
    assert worker['task_id'] not in admission._term_callbacks


def _pending_input_instruction(*pending):
    # One instruction for every input that ends a wait: it names the Workers to wait on next.
    return ('New input is waiting for you and arrives right after this result: a human message or steer, '
            'a project message, or a background completion. Workers keep running; still pending: '
            + ', '.join(pending) + '. Answer or act on that input first, then call await_project_workers '
            'again with only the pending assignment IDs and the same wait_for. Ending a wait for input is not '
            'failure.')


@pytest.fixture
def user_message(runtime):
    """Send a chat message into the Coordinator's live turn (the steer lane)."""
    from lib import agent_inbox
    conversation = runtime.task['convId']
    agent_inbox.reset_for_test(conversation)
    yield lambda: agent_inbox.enqueue(conversation, 'Answer me first', mode='user-steer', priority='next')
    agent_inbox.reset_for_test(conversation)


def test_pending_user_message_ends_the_wait_before_it_starts(runtime, user_message):
    from lib.agent_core import admission
    r = runtime; worker = r.call('delegate_project_work', **r.args)
    user_message()
    result = r.call('await_project_workers', assignment_ids=[worker['assignment_id']], timeout_seconds=30)
    assert result['wait'] == 'pending_input', result
    assert result['instruction'] == _pending_input_instruction(worker['assignment_id'])
    assert [row['status'] for row in result['workers']] == ['running']
    assert worker['task_id'] not in admission._term_callbacks
    assert len(r.calls) == 1


def test_user_message_during_the_wait_ends_it_promptly(runtime, monkeypatch, user_message):
    from lib.agent_core import admission
    r = runtime; subscribed = _subscriptions(monkeypatch)
    worker = r.call('delegate_project_work', **r.args)
    thread, result = _await_in_thread(r, assignment_ids=[worker['assignment_id']], timeout_seconds=30)
    assert subscribed(worker['task_id'])
    sent = time.monotonic()
    user_message()
    thread.join(5)
    assert not thread.is_alive() and result[0][0]['wait'] == 'pending_input', result
    assert time.monotonic() - sent < 1.5, 'the wait slept past the user message'
    assert result[0][0]['workers'][0]['status'] == 'running'
    assert worker['task_id'] not in admission._term_callbacks
    assert len(r.calls) == 1


def test_already_settled_workers_return_at_once_despite_a_pending_message(runtime, user_message):
    r = runtime
    row = r.c.repo.read(r.call('delegate_project_work', **r.args)['assignment_id'])
    r.attempts[row['execution']['attemptId']]['status'] = 'completed'
    user_message()
    assert r.call('await_project_workers', assignment_ids=[row['id']], timeout_seconds=30)['wait'] == 'settled'
