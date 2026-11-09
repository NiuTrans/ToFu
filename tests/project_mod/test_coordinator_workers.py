"""One human request creates independent workers under conserved source policy."""
from copy import deepcopy
import sys
from types import SimpleNamespace
import threading
import time

import pytest

from tests.project_mod.test_coordinator_dispatch_tools import runtime as base_runtime
from tests.project_mod.test_project_dispatch_repository import context as context
from lib.conversations import coordinator_workers
from lib.project_dispatch.delegation import capture_worker_policy
from lib.project_dispatch.service import ProjectDispatchService
from lib.turn_lifecycle import get_turn as production_get_turn

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
        'actor': 'assistant', 'parentTurnId': 'human-turn',
        'currentAttemptId': 'coordinator-attempt', 'projectionRevision': 3}
    r.turns['human-turn'] = r.c.turn
    # The wait's cross-process steer observation reads its own live Turn.
    def turn_get(conv, turn_id, **kw):
        row = deepcopy(r.turns.get(turn_id))
        if isinstance(row, dict) and kw.get('include_projection') is False:
            row.pop('projection', None)
        return row
    monkeypatch.setattr('lib.turn_lifecycle.get_turn', turn_get)
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
    monkeypatch.setattr(workers, 'require_isolated_worker_root', lambda project, **_: None)
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


def test_late_command_consumer_does_not_retain_fixture_turn_reader(runtime):
    from lib.conversation_sync import command_service

    assert command_service.get_turn is production_get_turn


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
    assert {'spawn_agents', 'execute_program'} <= set(row['request']['profile']['tools'])
    assert 'timer_create' not in row['request']['profile']['tools']
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


def test_coordinator_source_reads_carry_turn_metadata_and_keep_the_human_body_fence(runtime):
    r = runtime
    reads, read_turn = [], r.c.repo.turn_get
    def recorded(conversation, turn, **kwargs):
        reads.append((turn, kwargs.get('include_projection', True)))
        return read_turn(conversation, turn, **kwargs)
    r.c.repo.turn_get = recorded
    result = r.call('delegate_project_work', **r.args)
    assert result.get('dispatch') == 'accepted', result
    delegated = set(reads)
    # Its parent supplies only identity/revision before validate_human_source reads the body.
    assert ('human-turn', False) in delegated
    reads.clear()
    r.c.repo.validate(r.c.repo.admission(result['assignment_id']))
    for observed in (delegated, set(reads)):
        # Coordinator Turn identity/parent are metadata; the human source keeps its body fence.
        assert ('coordinator-turn', False) in observed and ('human-turn', True) in observed
        assert ('coordinator-turn', True) not in observed


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


@pytest.mark.parametrize('attempt_lookup', ['injected', 'default'])
def test_background_completion_continues_same_assignment_and_moves_result_to_new_turn(
        runtime, monkeypatch, attempt_lookup):
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
    if attempt_lookup == 'default':
        monkeypatch.setattr('lib.turn_lifecycle.get_attempt', r.c.repo.attempt_get)
        monkeypatch.setattr(r.c.repo, 'attempt_get', None)
    worker_turn_reads, read_turn = [], r.c.repo.turn_get
    def recorded_turn_get(conversation, turn, **kwargs):
        if turn == old['turnId']:
            worker_turn_reads.append((sys._getframe(1).f_globals['__name__'],
                                      kwargs.get('include_projection', True)))
        return read_turn(conversation, turn, **kwargs)
    monkeypatch.setattr(r.c.repo, 'turn_get', recorded_turn_get)
    assert dispatch_completed_background_batch(assignment['request']['recipientId'], user_id=7) == 'continuation-attempt'
    # The resume fence needs only the Worker Turn's revision, never its body.
    assert ('lib.message_queue', False) in worker_turn_reads
    assert ('lib.message_queue', True) not in worker_turn_reads
    monkeypatch.setattr(r.c.repo, 'turn_get', read_turn)
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


@pytest.mark.parametrize('reviewed', [False, True])
def test_completed_worker_background_completion_attaches_once_without_resume(
        runtime, monkeypatch, reviewed):
    from lib import message_queue, agent_inbox
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.project_dispatch.admission import TrustedDispatch
    from lib.conversation_sync.runtime import conversation_turn_commands

    r = runtime
    worker = r.call('delegate_project_work', **r.args)
    assignment = r.c.repo.read(worker['assignment_id'])
    execution = assignment['execution']
    trusted = TrustedDispatch(r.c.repo.admission(assignment['id']))
    config = trusted.capture({}, 7, trusted.principal, worker['conversation_id'])
    r.attempts[execution['attemptId']]['status'] = 'completed'
    turn = r.turns[execution['turnId']]
    turn.update(status='completed', settlement={'cause': 'provider_finished',
                'resumeOptions': [{'operation': 'regenerate', 'anchor': {}}]})
    if reviewed:
        r.c.repo.record_review(assignment['id'], 'accept', 'Verified', 1, 1)
    before = deepcopy(r.c.repo.read(assignment['id']))
    receipts = [{'queueId': 'late-result', 'timestamp': 1, 'config': config,
                 'payload': {'text': 'late command output', '_backgroundCommand': 'bg_late'}}]
    monkeypatch.setattr(message_queue, '_background_receipts', lambda *a, **kw: receipts)
    monkeypatch.setattr(message_queue, 'next_dispatchable_turn', lambda *a, **kw: None)
    monkeypatch.setattr(background_commands, 'conversation_results_complete', lambda **kw: True)
    monkeypatch.setattr(chat_task_runtime, 'snapshot_owned', lambda **kw: [])
    monkeypatch.setattr(conversation_turn_commands, 'resume_turn',
                        lambda *a, **kw: pytest.fail('completed execution cannot Continue'))
    attached, consumed = [], []

    def attach(conv, turn_id, *, projection, expected_projection_revision, user_id, **kw):
        assert (conv, turn_id, user_id) == (worker['conversation_id'], execution['turnId'], 7)
        assert expected_projection_revision == turn['projectionRevision']
        attached.append(projection)
        turn.update(projection=projection, projectionRevision=expected_projection_revision + 1)
    monkeypatch.setattr('lib.turn_lifecycle.update_turn_projection', attach)
    monkeypatch.setattr(agent_inbox, 'consume_peer', lambda *args: consumed.append(args))
    for _ in range(2):
        assert message_queue.dispatch_completed_background_batch(
            worker['conversation_id'], user_id=7) is None
    assert len(attached) == 1
    assert attached[0]['_bgCommandInjects'][0]['previews'][0]['text'] == 'late command output'
    assert consumed == [(worker['conversation_id'], ['late-result'])]
    assert r.c.repo.read(assignment['id']) == before


def _idle_background_batch(r, monkeypatch, receipts):
    from lib import message_queue
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    monkeypatch.setattr('lib.background_command_delivery._ATTACHED_BACKGROUND_BLOCKS', {})
    monkeypatch.setattr(message_queue, '_background_receipts', lambda *a, **kw: receipts)
    monkeypatch.setattr(message_queue, 'next_dispatchable_turn', lambda *a, **kw: None)
    monkeypatch.setattr(background_commands, 'conversation_results_complete', lambda **kw: True)
    monkeypatch.setattr(chat_task_runtime, 'snapshot_owned', lambda **kw: [])


def test_closed_worker_batch_reads_the_turn_body_only_to_attach(runtime, monkeypatch):
    """Every maintenance tick re-evaluates late results of a closed Worker.

    Its decisions read Turn metadata; the projection of a long Worker Turn is
    read to attach the block and once more to observe it at its new revision,
    never again while that revision stands.
    """
    from lib import message_queue, agent_inbox
    from lib.project_dispatch.admission import TrustedDispatch
    from lib.conversation_sync.runtime import conversation_turn_commands

    r = runtime
    worker = r.call('delegate_project_work', **r.args)
    conversation = worker['conversation_id']
    assignment = r.c.repo.read(worker['assignment_id'])
    execution = assignment['execution']
    trusted = TrustedDispatch(r.c.repo.admission(assignment['id']))
    config = trusted.capture({}, 7, trusted.principal, conversation)
    r.attempts[execution['attemptId']]['status'] = 'completed'
    turn = r.turns[execution['turnId']]
    turn.update(status='completed', projection={'content': 'done'})
    receipts = [{'queueId': 'late-result', 'timestamp': 1, 'config': config,
                 'payload': {'text': 'late command output', '_backgroundCommand': 'bg_late'}}]
    _idle_background_batch(r, monkeypatch, receipts)
    monkeypatch.setattr(conversation_turn_commands, 'resume_turn',
                        lambda *a, **kw: pytest.fail('completed execution cannot Continue'))
    body_reads, read_turn = [], r.c.repo.turn_get

    def recorded(conv, turn_id, **kwargs):
        row = read_turn(conv, turn_id, **kwargs)
        if kwargs.get('include_projection', True):
            body_reads.append(turn_id)
        elif isinstance(row, dict):
            row.pop('projection', None)
        return row
    monkeypatch.setattr(r.c.repo, 'turn_get', recorded)
    attached = []

    def attach(conv, turn_id, *, projection, expected_projection_revision, user_id, **kw):
        assert expected_projection_revision == turn['projectionRevision']
        attached.append(projection)
        turn.update(projection=projection, projectionRevision=expected_projection_revision + 1)
    monkeypatch.setattr('lib.turn_lifecycle.update_turn_projection', attach)
    monkeypatch.setattr(agent_inbox, 'consume_peer', lambda *args: None)
    for _ in range(5):
        assert message_queue.dispatch_completed_background_batch(conversation, user_id=7) is None
    assert len(attached) == 1
    assert body_reads.count(execution['turnId']) == 2
    # A projection written elsewhere is a new revision: it is read again.
    turn['projectionRevision'] += 1
    assert message_queue.dispatch_completed_background_batch(conversation, user_id=7) is None
    assert body_reads.count(execution['turnId']) == 3 and len(attached) == 1


def test_worker_execution_without_continue_settles_its_background_batch_once(
        runtime, monkeypatch, caplog):
    """A failed Continue start leaves only Regenerate; the authority refuses every later Continue.

    Re-issuing the batch's Continue on each maintenance tick looped forever on
    ``database_conflict``. The batch settles once as evidence on that exact Turn.
    """
    import logging
    from lib import message_queue, agent_inbox
    from lib.background_command_delivery import settle_closed_dispatch_completion
    from lib.project_dispatch.admission import TrustedDispatch
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.turn_lifecycle import LifecycleConflict

    r = runtime
    worker = r.call('delegate_project_work', **r.args)
    conversation = worker['conversation_id']
    assignment = r.c.repo.read(worker['assignment_id'])
    execution = assignment['execution']
    trusted = TrustedDispatch(r.c.repo.admission(assignment['id']))
    config = trusted.capture({}, 7, trusted.principal, conversation)
    r.attempts[execution['attemptId']]['status'] = 'failed'
    turn = r.turns[execution['turnId']]
    settlement = {'outcome': 'failed', 'cause': 'generation_error',
                  'error': {'kind': 'task_start_failed',
                            'raw': 'Worker tool reservations cannot be replenished'},
                  'resumeOptions': [{'operation': 'regenerate', 'anchor': {'type': 'turn_start'}}]}
    turn.update(status='failed', settlement=deepcopy(settlement))
    before = deepcopy(r.c.repo.read(assignment['id']))
    receipts = [{'queueId': 'late-result', 'timestamp': 1, 'config': config,
                 'payload': {'text': 'late command output', '_backgroundCommand': 'bg_late'}}]
    _idle_background_batch(r, monkeypatch, receipts)
    refused = []

    def resume(conv, turn_id, owner, body, **kwargs):
        # turn.attempt.create admits only an operation the current settlement offers.
        refused.append(body['commandId'])
        raise LifecycleConflict('database_conflict', 'database_conflict')
    monkeypatch.setattr(conversation_turn_commands, 'resume_turn', resume)
    attached, consumed = [], []

    def attach(conv, turn_id, *, projection, expected_projection_revision, user_id, **kwargs):
        assert (conv, turn_id, user_id) == (conversation, execution['turnId'], 7)
        assert expected_projection_revision == turn['projectionRevision']
        attached.append(projection)
        turn.update(projection=projection, projectionRevision=expected_projection_revision + 1)
    monkeypatch.setattr('lib.turn_lifecycle.update_turn_projection', attach)
    monkeypatch.setattr(agent_inbox, 'consume_peer', lambda *args: consumed.append(args))
    with caplog.at_level(logging.INFO, logger='lib.background_command_delivery'):
        for _ in range(3):  # successive maintenance ticks
            assert message_queue.dispatch_completed_background_batch(conversation, user_id=7) is None
    assert refused == []
    assert len(attached) == 1
    assert attached[0]['_bgCommandInjects'][0]['previews'][0]['text'] == 'late command output'
    assert consumed == [(conversation, ['late-result'])]
    # The failed Turn keeps its own settlement; evidence is not a completion.
    assert turn['status'] == 'failed' and turn['settlement'] == settlement
    assert r.c.repo.read(assignment['id']) == before
    causes = [record.getMessage() for record in caplog.records
              if 'execution_not_continuable' in record.getMessage()]
    assert len(causes) == 1 and execution['turnId'] in causes[0] and conversation in causes[0]
    # Receipts without the binding (human takeover of the Worker) keep the ordinary wake.
    unbound = {'model': 'model-a', 'projectPath': '/project'}
    assert settle_closed_dispatch_completion(
        unbound, [{**receipts[0], 'config': unbound}], conv_id=conversation, user_id=7) is False


def test_background_batch_issues_its_continue_once(runtime, monkeypatch):
    """The deterministic command must not be replayed as a new wake on every tick."""
    from lib import message_queue
    from lib.project_dispatch.admission import TrustedDispatch
    from lib.conversation_sync.runtime import conversation_turn_commands

    r = runtime
    worker = r.call('delegate_project_work', **r.args)
    conversation = worker['conversation_id']
    assignment = r.c.repo.read(worker['assignment_id'])
    old = assignment['execution']
    trusted = TrustedDispatch(r.c.repo.admission(assignment['id']))
    config = trusted.capture({}, 7, trusted.principal, conversation)
    paused = {'cause': 'server_restart', 'resumeOptions': [{'operation': 'continue', 'anchor': {}}]}
    r.attempts[old['attemptId']]['status'] = 'interrupted'
    r.turns[old['turnId']].update(status='interrupted', settlement=deepcopy(paused))
    receipts = [{'queueId': 'pending-result', 'timestamp': 1, 'config': config,
                 'payload': {'text': 'command output', '_backgroundCommand': 'bg_pending'}}]
    _idle_background_batch(r, monkeypatch, receipts)
    issued = []

    def resume(conv, turn_id, owner, body, **kwargs):
        issued.append(body['commandId'])
        replay = next((row for row in r.attempts.values()
                       if row.get('commandId') == body['commandId']), None)
        if replay is not None:  # turn.attempt.create replays the command's attempt
            return SimpleNamespace(value={'attempt': deepcopy(replay), 'idempotentReplay': True})
        r.attempts['continuation-attempt'] = {
            'conversationId': conv, 'turnId': turn_id, 'attemptId': 'continuation-attempt',
            'commandId': body['commandId'], 'taskId': old['taskId'], 'status': 'running'}
        kwargs['trusted_dispatch'].continue_execution(turn_id, 'continuation-attempt')
        return SimpleNamespace(value={'attempt': deepcopy(r.attempts['continuation-attempt'])})
    monkeypatch.setattr(conversation_turn_commands, 'resume_turn', resume)
    assert message_queue.dispatch_completed_background_batch(
        conversation, user_id=7) == 'continuation-attempt'
    # Stopped before its first model round consumed the receipt; Continue stays offered.
    r.attempts['continuation-attempt']['status'] = 'interrupted'
    r.turns[old['turnId']].update(settlement={'cause': 'user_abort', 'resumeOptions': paused['resumeOptions']})
    for _ in range(2):
        assert message_queue.dispatch_completed_background_batch(conversation, user_id=7) is None
    assert len(issued) == 1
    assert r.c.repo.read(assignment['id'])['execution']['attemptId'] == 'continuation-attempt'

    # A later human Continue advances the assignment, while the original
    # unconsumed receipt still names the first background batch command.
    r.attempts['manual-attempt'] = {
        **r.attempts['continuation-attempt'], 'attemptId': 'manual-attempt',
        'commandId': 'manual-continue', 'taskId': '', 'status': 'pending'}
    trusted.continue_execution(old['turnId'], 'manual-attempt')
    r.attempts['manual-attempt'].update(taskId='manual-task', status='interrupted')
    before = r.c.repo.read(assignment['id'])
    assert message_queue.dispatch_completed_background_batch(conversation, user_id=7) is None
    assert len(set(issued)) == 1
    assert r.c.repo.read(assignment['id']) == before


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
    from lib.background_command_delivery import settle_closed_dispatch_completion
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
    assert settle_closed_dispatch_completion(
        unbound, receipts, conv_id=conversation, user_id=7) is False
    assert settle_closed_dispatch_completion(
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


@pytest.mark.parametrize('wait_for', ['execution', 'delivery'])
def test_full_worker_wait_remains_live_without_worker_output(runtime, monkeypatch, wait_for):
    """Successful status reads prove the waiter is live throughout its declared wait."""
    from lib.agent_core import admission
    from lib.conversations.coordinator_tools import _scope
    from lib.tasks_pkg.liveness import judge

    r = runtime
    worker = r.call('delegate_project_work', **r.args)
    scope = _scope(r.task)
    clock = SimpleNamespace(elapsed=0.0, started=time.time())
    r.task.update(status='running', aborted=False, created_at=clock.started - 18,
                  _t_last_event=clock.started - 18, _dispatch_heartbeat=clock.started - 18)
    monkeypatch.setattr('lib.fs_keepalive.filesystem_unresponsive', lambda: False)
    monkeypatch.setattr('lib.fs_keepalive.filesystem_resumed_at', lambda: (0.0, 0.0))
    monkeypatch.setattr(coordinator_workers, 'time', SimpleNamespace(
        time=lambda: clock.started + clock.elapsed, monotonic=lambda: clock.elapsed))
    monkeypatch.setattr(coordinator_workers, '_input_waiting', lambda task: False)
    monkeypatch.setattr(coordinator_workers, '_incoming_project_messages', lambda task: None)

    class AdvancingEvent(threading.Event):
        def wait(self, timeout=None):
            clock.elapsed += timeout
            verdict = judge(r.task, clock.started + clock.elapsed, 1800, commands=[])
            assert verdict.state == 'alive', 'a responsive worker wait must not become worker_lost'
            return self.is_set()

    monkeypatch.setattr(coordinator_workers, 'threading', SimpleNamespace(Event=AdvancingEvent))
    result = coordinator_workers.await_workers(r.task, scope, {
        'assignment_ids': [worker['assignment_id']], 'timeout_seconds': 1800,
        'wait_for': wait_for,
    })
    assert result['wait'] == 'timeout' and result['workers'][0]['status'] == 'running'
    assert clock.elapsed == 1800 and not r.task['aborted']
    assert r.task['_t_last_event'] == clock.started - 18, 'observation is not worker output'
    assert worker['task_id'] not in admission._term_callbacks and len(r.calls) == 1
    # A returned call grants no permanent exemption if its owner then wedges.
    assert judge(r.task, clock.started + clock.elapsed + 1801, 1800, commands=[]).state == 'wedged'


@pytest.mark.parametrize('blocked_read_number', [1, 2])
def test_blocked_worker_status_read_remains_reapable(runtime, monkeypatch, blocked_read_number):
    """Entering a read or emitting transport ticks cannot attest that it returned."""
    from lib.agent_core import admission
    from lib.conversations.coordinator_tools import _scope
    from lib.tasks_pkg.liveness import judge
    from lib.tasks_pkg.tool_dispatch._heartbeat import _emit_tool_heartbeat
    from tests.tools.test_tool_settle_all_lanes import _mk_tc

    r = runtime
    worker = r.call('delegate_project_work', **r.args)
    scope = _scope(r.task)
    clock = SimpleNamespace(now=time.time(), reads=0)
    stale = clock.now - 1801
    r.task.update(status='running', aborted=False, created_at=stale,
                  _t_last_event=stale, _dispatch_heartbeat=stale)
    monkeypatch.setattr('lib.fs_keepalive.filesystem_unresponsive', lambda: False)
    monkeypatch.setattr('lib.fs_keepalive.filesystem_resumed_at', lambda: (0.0, 0.0))
    monkeypatch.setattr(coordinator_workers, 'time', SimpleNamespace(
        time=lambda: clock.now, monotonic=lambda: clock.now))
    ticks = []
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._heartbeat.append_event',
                        lambda task, event: ticks.append(event))

    read = r.service.view_for_coordinator
    def blocked_read(*args, **kwargs):
        clock.reads += 1
        if clock.reads < blocked_read_number:
            return read(*args, **kwargs)
        clock.now += 1801
        item = _mk_tc('wait', 'await_project_workers', 1)
        _emit_tool_heartbeat(r.task, [item], stale)
        assert ticks and all(event.get('_selfTick') for event in ticks)
        assert judge(r.task, clock.now, 1800, commands=[]).state == 'wedged'
        raise RuntimeError('status read did not complete')

    monkeypatch.setattr(r.service, 'view_for_coordinator', blocked_read)
    with pytest.raises(RuntimeError, match='status read did not complete'):
        coordinator_workers.await_workers(r.task, scope, {'assignment_ids': [worker['assignment_id']]})
    assert r.task['_dispatch_heartbeat'] == (stale if blocked_read_number == 1 else clock.now - 1801)
    assert worker['task_id'] not in admission._term_callbacks


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


def _restart_continuation(r, task, row, *, resume_source=True, attempt_id='resumed-attempt'):
    """The Worker's attempt is restart-paused; Continue accepts and binds its successor."""
    from lib.project_dispatch import admission
    old = row['execution']
    r.attempts[old['attemptId']]['status'] = 'interrupted'
    r.turns[old['turnId']].update(status='interrupted', settlement={
        'cause': 'server_restart', 'resumeOptions': [{'operation': 'continue', 'anchor': {}}]})
    trusted = admission.TrustedDispatch(r.c.repo.admission(row['id']))
    r.attempts[attempt_id] = {'conversationId': task['convId'], 'turnId': old['turnId'],
        'attemptId': attempt_id, 'taskId': task['id'], 'status': 'running'}
    trusted.continue_execution(old['turnId'], attempt_id)
    config = trusted.capture({}, 7, trusted.principal, task['convId'])
    config['_turnOwnerUserId'] = 7
    if resume_source:
        config.update(_turnOperation='continue', _resumeSource={
            'attemptId': old['attemptId'], 'taskId': task['id'],
            'status': 'interrupted', 'cause': 'server_restart'})
    return {'id': task['id'], 'convId': task['convId'], 'config': config, 'status': 'running',
            '_userId': 7, '_principalContext': task['_principalContext'], '_attended': False,
            '_turnId': old['turnId'], '_attemptId': attempt_id,
            'abort_event': threading.Event(), 'usage': {}}


def test_restart_continue_keeps_counting_the_worker_tool_reservations(runtime):
    from lib.project_dispatch import admission
    from tests.project_mod.test_coordinator_request_budget import bind
    r = runtime
    bind(r)
    task, row = _worker_task(r)
    assert row['request']['delegation']['policy']['requestBudgetId']
    for _ in range(2):
        assert task['_executionBudget'].reserve_tool(task) is None
    spent = r.c.repo.read(row['id'])['toolReservations']
    # The ledger's root is the assignment's work id, shared by every physical attempt.
    assert spent['count'] == 2 and spent['rootTaskId'] == row['execution']['taskId']
    resumed = _restart_continuation(r, task, row)
    admission.bind_account(resumed)
    assert r.c.repo.read(row['id'])['toolReservations'] == spent
    assert resumed['_executionBudget'].reserve_tool(resumed) is None
    assert r.c.repo.read(row['id'])['toolReservations'] == {**spent, 'count': 3}
    assert not resumed.get('aborted')


def test_a_new_physical_account_still_cannot_replenish_worker_tool_reservations(runtime):
    from lib.llm.llm_errors import AbortedError
    from lib.project_dispatch import admission
    from tests.project_mod.test_coordinator_request_budget import bind
    r = runtime
    bind(r)
    task, row = _worker_task(r)
    assert task['_executionBudget'].reserve_tool(task) is None
    spent = r.c.repo.read(row['id'])['toolReservations']
    fresh = _restart_continuation(r, task, row, resume_source=False)
    with pytest.raises(AbortedError, match='cannot be replenished'):
        admission.bind_account(fresh)
    assert r.c.repo.read(row['id'])['toolReservations'] == spent


def _follow_up_turn(r, task, row, *, witness=None):
    """The Worker's Turn completed; a Coordinator follow-up accepts the next Turn's attempt."""
    from lib.project_dispatch import admission
    old = row['execution']
    r.attempts[old['attemptId']]['status'] = 'completed'
    r.turns[old['turnId']].update(status='completed')
    source = {'attemptId': old['attemptId'], 'turnId': old['turnId'], 'taskId': task['id'],
              'status': 'completed'}
    trusted = admission.TrustedDispatch(r.c.repo.admission(row['id']),
                                        follows={'execution': deepcopy(old), 'source': source})
    r.turns['follow-up-turn'] = {'conversationId': task['convId'], 'turnId': 'follow-up-turn',
                                 'actor': 'assistant', 'projectionRevision': 1}
    r.attempts['follow-up-attempt'] = {'conversationId': task['convId'], 'turnId': 'follow-up-turn',
        'attemptId': 'follow-up-attempt', 'taskId': '', 'status': 'pending'}
    trusted.continue_execution('follow-up-turn', 'follow-up-attempt')
    config = trusted.capture({}, 7, trusted.principal, task['convId'])
    assert config[admission.FOLLOW_UP_KEY] == source
    if witness is not None:
        config[admission.FOLLOW_UP_KEY] = {**source, **witness}
    config['_turnOwnerUserId'] = 7
    r.attempts['follow-up-attempt'].update(taskId=task['id'], status='running')
    return {'id': task['id'], 'convId': task['convId'], 'config': config, 'status': 'running',
            '_userId': 7, '_principalContext': task['_principalContext'], '_attended': False,
            '_turnId': 'follow-up-turn', '_attemptId': 'follow-up-attempt',
            'abort_event': threading.Event(), 'usage': {}}


def test_a_follow_up_turn_continues_the_worker_tool_account(runtime):
    """A follow-up is a new Turn, so storage issues no _resumeSource; its witness is the
    settled association it continues from, and the one account keeps counting."""
    from lib.project_dispatch import admission
    from tests.project_mod.test_coordinator_request_budget import bind
    r = runtime
    bind(r)
    task, row = _worker_task(r)
    for _ in range(2):
        assert task['_executionBudget'].reserve_tool(task) is None
    spent = r.c.repo.read(row['id'])['toolReservations']
    follow_up = _follow_up_turn(r, task, row)
    admission.bind_account(follow_up)
    assert r.c.repo.read(row['id'])['execution']['turnId'] == 'follow-up-turn'
    assert follow_up['_executionBudget'].reserve_tool(follow_up) is None
    assert r.c.repo.read(row['id'])['toolReservations'] == {**spent, 'count': 3}
    assert not follow_up.get('aborted')


@pytest.mark.parametrize('forged', [{'status': 'failed'}, {'turnId': 'another-turn'}, {'taskId': 'another-task'}])
def test_a_follow_up_witness_must_name_its_settled_predecessor(runtime, forged):
    from lib.llm.llm_errors import AbortedError
    from lib.project_dispatch import admission
    from tests.project_mod.test_coordinator_request_budget import bind
    r = runtime
    bind(r)
    task, row = _worker_task(r)
    assert task['_executionBudget'].reserve_tool(task) is None
    spent = r.c.repo.read(row['id'])['toolReservations']
    with pytest.raises(AbortedError, match='follow-up source differs'):
        admission.bind_account(_follow_up_turn(r, task, row, witness=forged))
    assert r.c.repo.read(row['id'])['toolReservations'] == spent


def test_a_follow_up_pin_refuses_a_session_that_moved_on(runtime):
    from lib.project_dispatch import admission
    from lib.project_dispatch.repository import ProjectDispatchError
    r = runtime
    task, row = _worker_task(r)
    stale = deepcopy(row['execution'])
    _follow_up_turn(r, task, row)
    later = admission.TrustedDispatch(r.c.repo.admission(row['id']), follows={
        'execution': stale, 'source': {'attemptId': stale['attemptId'], 'turnId': stale['turnId'],
                                       'taskId': task['id'], 'status': 'completed'}})
    r.attempts['second-attempt'] = {'conversationId': task['convId'], 'turnId': 'second-turn',
        'attemptId': 'second-attempt', 'taskId': '', 'status': 'pending'}
    with pytest.raises(ProjectDispatchError, match='source changed'):
        later.continue_execution('second-turn', 'second-attempt')
    assert r.c.repo.read(row['id'])['execution']['attemptId'] == 'follow-up-attempt'


def test_each_restart_generation_continues_the_one_worker_tool_account_once(runtime):
    """The stored counter is the only offset: the restored budget ordinals continue it.

    Counting a stored reservation both as an adopted offset and as a restored
    ordinal would skip ordinals, and restarting the occurrence ordinal would
    collide with the previous generation's tool rows.
    """
    from lib.project_dispatch import admission
    from tests.project_mod.test_coordinator_request_budget import _run_tool, bind
    r = runtime
    bind(r)
    task, row = _worker_task(r)
    worker = task
    for generation in range(3):
        if generation:
            worker = _restart_continuation(r, task, r.c.repo.read(row['id']),
                                           attempt_id=f'restart-generation-{generation}')
            admission.bind_account(worker)
        _run_tool(worker, f'call-{generation}')
        assert not worker.get('aborted')
    stored = r.c.repo.read(row['id'])
    counter = stored['toolReservations']
    assert counter == {'rootTaskId': stored['execution']['taskId'],
                       'accountNonce': counter['accountNonce'], 'count': 3}
    assert [(item['tool_ordinal'], item['tool_call_id'], item['account_nonce'], item['phase'])
            for item in stored['toolAttempts']] == [
        (number + 1, f'call-{number}', counter['accountNonce'], 'executor_returned') for number in range(3)]


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
                        lambda _, **kwargs: admission.require_isolated_worker_root(str(project), **kwargs))
    accepted = r.call('delegate_project_work', **r.args)
    assert accepted['dispatch'] == 'accepted', accepted
    assert (plain / '.git').is_dir() and (plain / 'AGENTS.md').is_file()
    assert (plain / 'plan.md').read_text() == '# plan\n'
    assert r.task['convId'] == 'coordinator' and len(r.created) == len(r.calls) == 1
    assert r.call('delegate_project_work', **r.args) == accepted
    assert len(r.created) == len(r.calls) == 1


# The reaper's silence threshold, dilated so a fake volume can outlast it in seconds.
_REAPER_THRESHOLD = 3.0


def _plain_folder_delegation(r, monkeypatch, tmp_path):
    from lib.conversations import coordinator_workers as workers
    from lib.project_dispatch import admission
    plain = (tmp_path / 'autoharness').resolve()
    plain.mkdir()
    (plain / 'plan.md').write_text('# plan\n')
    monkeypatch.setattr(workers, 'require_isolated_worker_root',
                        lambda _, **kwargs: admission.require_isolated_worker_root(str(plain), **kwargs))
    monkeypatch.setattr('lib.fs_keepalive.filesystem_unresponsive', lambda: False)
    monkeypatch.setattr('lib.fs_keepalive.filesystem_resumed_at', lambda: (0.0, 0.0))
    return plain


def test_a_long_root_preparation_keeps_its_delegating_coordinator_live(runtime, monkeypatch, tmp_path):
    """2026-11-09: preparing a 1.4M-file plain folder took 6,931 s inside
    ``delegate_project_work``; at 1,836 s the stuck-task reaper force-failed the
    Coordinator as ``worker_lost`` and the delegation's result was lost. While
    the preparation's Git executes, that is the waiting Coordinator's liveness."""
    from lib.project_mod import managed_repository
    from lib.tasks_pkg.liveness import judge
    from tests.support.volume_git import volume_git
    r = runtime
    plain = _plain_folder_delegation(r, monkeypatch, tmp_path)
    script, marker = volume_git(tmp_path, 'slow', 'add', seconds=2.2 * _REAPER_THRESHOLD)
    monkeypatch.setattr(managed_repository, 'git_executable', lambda: str(script))
    started = time.time()
    r.task.update(status='running', aborted=False, created_at=started,
                  _t_last_event=started, _dispatch_heartbeat=started)
    verdicts, done = [], threading.Event()

    def reaper():
        while not done.wait(0.1):
            verdicts.append(judge(r.task, time.time(), _REAPER_THRESHOLD, commands=[]).state)

    sweeps = threading.Thread(target=reaper)
    sweeps.start()
    try:
        accepted = r.call('delegate_project_work', **r.args)
    finally:
        done.set()
        sweeps.join(5)
    assert marker.exists() and time.time() - started > 2 * _REAPER_THRESHOLD
    assert accepted['dispatch'] == 'accepted', accepted
    assert (plain / '.git').is_dir() and len(r.created) == len(r.calls) == 1
    assert verdicts and set(verdicts) == {'alive'}, verdicts
    assert r.task['_t_last_event'] == started, 'preparation progress is not Worker output'


def test_a_root_preparation_that_stops_advancing_leaves_its_coordinator_reapable(
        runtime, monkeypatch, tmp_path):
    """Only an observed advance is evidence: a step that stops advancing supplies
    none, so a genuinely stuck delegation is reaped one threshold after its last
    advance, and a returned delegation keeps no exemption."""
    from lib.project_mod import managed_repository
    from lib.tasks_pkg.liveness import judge
    r = runtime
    _plain_folder_delegation(r, monkeypatch, tmp_path)
    stale = time.time() - 1801
    r.task.update(status='running', aborted=False, created_at=stale,
                  _t_last_event=stale, _dispatch_heartbeat=stale)
    blocked = []
    walk = managed_repository.plan_content

    def stalled_walk(*args, **kwargs):
        # The content walk stops here (one stat that never returns): no further advance.
        now = time.time()
        blocked.append((judge(r.task, now + 1799, 1800, commands=[]).state,
                        judge(r.task, now + 1801, 1800, commands=[]).state))
        return walk(*args, **kwargs)

    monkeypatch.setattr(managed_repository, 'plan_content', stalled_walk)
    accepted = r.call('delegate_project_work', **r.args)
    assert accepted['dispatch'] == 'accepted', accepted
    assert blocked == [('alive', 'wedged')], blocked
    assert judge(r.task, time.time() + 1801, 1800, commands=[]).state == 'wedged'


def test_a_folder_of_other_projects_refuses_delegation_with_the_owners_question(
        runtime, monkeypatch, tmp_path):
    """A folder without a project manifest that holds other projects needs the
    owner's answer. The typed question reaches the Coordinator as its tool result
    (code, candidates, recommended answer, no-retry recovery); no Worker exists
    and nothing was written into the folder."""
    r = runtime
    plain = _plain_folder_delegation(r, monkeypatch, tmp_path)
    (plain / 'work' / 'copy').mkdir(parents=True)
    (plain / 'work' / 'copy' / 'pyproject.toml').write_text('[project]\nname = "copy"\n')
    refused = r.call('delegate_project_work', **r.args)
    assert refused.get('code') == 'ambiguous_project_root', refused
    assert refused['details']['candidates'] == {'work': 1}
    assert refused['details']['recommended'] == 'shared'
    assert 'work/ (1 inside)' in refused['error'] and 'git ' not in refused['error']
    assert refused['execution_started'] is False and 'owner' in refused['recovery']
    assert not r.created and not r.calls and not r.c.repo.list()
    assert sorted(path.name for path in plain.iterdir()) == ['plan.md', 'work']


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
    def preflight(project, **kwargs):
        checked.append(project)
        admission.require_isolated_worker_root(str(repository / 'nested'), **kwargs)
    monkeypatch.setattr(workers, 'require_isolated_worker_root', preflight)
    refused = r.call('delegate_project_work', **r.args)
    assert checked == ['/project']
    assert 'subdirectory of the Git repository' in refused['error'] and 'git ' not in refused['error']
    assert refused['execution_started'] is False and 'instead of retrying' in refused['recovery']
    assert not r.created and not r.calls and not r.c.repo.list()
    assert not (repository / 'nested' / '.git').exists()


def test_a_coordinator_without_a_model_is_refused_without_retry_advice(runtime):
    """Tool assembly records the Coordinator's own model for its continuations. A request
    running without one issues no Worker; the refusal names that cause and, being
    deterministic and effect-free, does not advise retrying the same key."""
    r = runtime
    capture_worker_policy(r.task, r.task['config'], [{'function': {'name': 'run_command'}}], '')
    refused = r.call('delegate_project_work', **r.args)
    assert 'has no model' in refused['error'], refused
    assert refused['execution_started'] is False and 'instead of retrying' in refused['recovery']
    assert not r.created and not r.calls and not r.c.repo.list()


def test_accepted_delegation_replays_without_rechecking_the_root(runtime, monkeypatch):
    from lib.conversations import coordinator_workers as workers
    r = runtime
    accepted = r.call('delegate_project_work', **r.args)
    assert accepted['dispatch'] == 'accepted', accepted
    monkeypatch.setattr(workers, 'require_isolated_worker_root',
                        lambda project, **_: pytest.fail('an accepted replay rechecked the root'))
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
            if row['execution']['taskId'] == task_id:
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
            'a project message, or a background completion. The waiting list names its kind, source and '
            'preview. Workers keep running; still pending: '
            + ', '.join(pending) + '. Answer or act on that input first, then call await_project_workers '
            'again with only the pending assignment IDs and the same wait_for. Ending a wait for input is not '
            'failure.')


@pytest.mark.parametrize('source', ['router', 'channel', 'typed'])
def test_steer_committed_by_another_process_during_the_wait_ends_it_and_names_its_source(
        runtime, monkeypatch, coordinator_inbox, source):
    """2026-11-06: an owner forward accepted by the API process while the
    Coordinator executes in the execution process left the wait blind. The
    durable steer record on the live Turn is the cross-process delivery."""
    from lib.agent_core import admission
    r = runtime; subscribed = _subscriptions(monkeypatch)
    monkeypatch.setattr(coordinator_workers, '_DURABLE_RECHECK_SECONDS', 0.2)
    worker = r.call('delegate_project_work', **r.args)
    thread, result = _await_in_thread(r, assignment_ids=[worker['assignment_id']], timeout_seconds=30)
    assert subscribed(worker['task_id'])
    origin = {'initiator': 'human'}
    if source == 'router':
        origin['forwardedFrom'] = {'conversationId': 'router-conv', 'turnId': 'owner-turn'}
    elif source == 'channel':
        origin['channel'] = {'channel': 'dx', 'thread': 'dx:gid:1'}
    author = {'actor': 'human', 'text': 'Answer me first', 'origin': origin}
    # turn.steer.commit in the other process: one pending record, next revision.
    r.turns['coordinator-turn'].update(projectionRevision=4, projection={'_userSteerInjects': [
        {'blockId': 'injection:user-steer:router-forward:x', 'commandId': 'router-forward:x',
         'previews': [{'text': 'Answer me first'}], 'deliveryState': 'pending', 'author': author}]})
    sent = time.monotonic()
    thread.join(5)
    assert not thread.is_alive() and result, 'a cross-process steer never ended the wait'
    value, elapsed = result[0]
    assert value['wait'] == 'pending_input' and elapsed < 5, value
    assert value['workers'][0]['status'] == 'running'
    assert value['instruction'] == _pending_input_instruction(worker['assignment_id'])
    assert value['waiting'] == [{'kind': 'human', 'preview': 'Answer me first', 'source': {
        'router': 'Global Router forward', 'channel': 'dx channel',
        'typed': 'typed in this conversation'}[source]}]
    assert worker['task_id'] not in admission._term_callbacks
    assert time.monotonic() - sent < 5
    # The next round boundary delivers it once, with its committed author.
    from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox
    messages = [{'role': 'user', 'content': 'go'}]
    drain_and_inject_inbox(task=r.task, messages=messages, round_num=5, tid='coordinator')
    assert messages[-1]['content'].count('Answer me first') == 1
    assert [item['steerAuthor'] for item in r.task['_steer_inject_pending']] == [author]
    messages = [{'role': 'user', 'content': 'go'}]
    drain_and_inject_inbox(task=r.task, messages=messages, round_num=6, tid='coordinator')
    assert len(messages) == 1, 'a committed steer was delivered twice'
    assert not coordinator_inbox.pending_items('coordinator', modes=['user-steer'])


def test_project_message_during_the_wait_names_its_id(runtime, monkeypatch, coordinator_inbox):
    from lib.agent_core import admission
    r = runtime
    monkeypatch.setattr(coordinator_workers, '_DURABLE_RECHECK_SECONDS', 0.2)
    worker = r.call('delegate_project_work', **r.args)
    arguments = {'assignment_ids': [worker['assignment_id']], 'timeout_seconds': 30}
    subscribed = _subscriptions(monkeypatch)
    thread, result = _await_in_thread(r, **arguments)
    assert subscribed(worker['task_id'])
    question = _worker_message(worker, 'named-question')
    thread.join(5)
    assert not thread.is_alive() and result
    value, _ = result[0]
    assert value['wait'] == 'pending_input'
    assert value['waiting'] == [{'kind': 'project_message', 'source': worker['conversation_id'],
                                 'id': question['id'], 'preview': 'Which API version should this target?'}]
    assert worker['task_id'] not in admission._term_callbacks


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
