"""Independent conversations reuse existing durable grants and Turn admission."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.conversations.coordinator_tools import execute_coordinator_tool
from lib.project_dispatch import admission
from lib.project_dispatch.service import ProjectDispatchService
from tests.project_mod.test_project_dispatch_repository import context as context
from tests.project_mod.test_coordinator_reports import reports as reports

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(context, monkeypatch):
    c = context
    from lib.conversations import project_coordination, project_messages, repository as conversations
    from lib.project_dispatch import repository, service
    import lib.storage
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', lambda owner: c.binding)
    monkeypatch.setattr(repository, 'ProjectDispatchRepository', lambda owner, path: c.repository(owner, path))
    monkeypatch.setattr(conversations, 'ConversationRepository', lambda: c.conversations)
    monkeypatch.setattr(project_messages, 'ConversationRepository', lambda: c.conversations)
    monkeypatch.setattr(admission, 'repository_for', lambda binding: c.repo)
    state = SimpleNamespace(c=c, created=[], calls=[], turns={}, attempts={})

    class Storage:
        def command(self, operation, payload, command_id):
            assert operation == 'conversation.create'
            assert payload['user_id'] == 7 and payload['conv_id'] not in c.conversations.rows
            assert 'messages' not in payload and 'config' not in payload
            c.conversations.rows[payload['conv_id']] = {
                'user_id': 7, 'title': payload['title'], 'settings': deepcopy(payload['settings'])}
            state.created.append(deepcopy(payload))
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda **kw: Storage())

    class Commands:
        def create_turn(self, conv, owner, request, **kwargs):
            trusted = kwargs['trusted_dispatch']
            trusted.checked()
            assert kwargs['principal'].kind == 'system' and owner == 7
            assert request['inputActor'] == 'virtual_user'
            if state.calls:
                return SimpleNamespace(value={})
            state.calls.append((conv, deepcopy(request), kwargs['principal']))
            trusted.bind_execution('worker-turn', 'worker-attempt')
            state.turns['worker-turn'] = {'conversationId': conv, 'turnId': 'worker-turn', 'actor': 'assistant',
                'projectionRevision': 1, 'projection': {'content': ''}}
            state.attempts['worker-attempt'] = {'conversationId': conv, 'turnId': 'worker-turn',
                'attemptId': 'worker-attempt', 'taskId': 'worker-task', 'status': 'running'}
            return SimpleNamespace(value={})
    state.service = ProjectDispatchService(c.repo, commands=Commands(),
        turn_get=lambda conv, turn, **kw: deepcopy(state.turns.get(turn)),
        attempt_get=lambda attempt, **kw: deepcopy(state.attempts.get(attempt)))
    monkeypatch.setattr(service, 'ProjectDispatchService', lambda repo: state.service)
    state.task = {'id': 'coordinator-task', 'convId': 'coordinator', '_userId': 7,
                  '_principalContext': c.principal.to_payload(), 'config': {'projectPath': '/project'}}
    state.call = lambda name, **args: json.loads(execute_coordinator_tool(state.task, name, args))
    return state


def create_worker(r):
    result = r.call('create_conversation', title='Independent worker', idempotency_key='work-one')
    assert result['created_only'] and result['execution_started'] is False
    return result['conversation_id']


@pytest.mark.parametrize('files', [False, True])
def test_create_human_authorize_dispatch_follow_ordinary_worker(runtime, files):
    r = runtime
    worker = create_worker(r)
    assert worker != r.task['convId'] and len(r.created) == 1
    assert not r.calls and not r.c.repo.list()
    assert create_worker(r) == worker and len(r.created) == 1
    r.c.request['recipientId'] = worker
    if files:
        r.c.request['profile'] = {'mode': 'files', 'tools': ['read_files', 'write_file'],
                                  'readPaths': ['src'],
                                  'writePaths': ['src'], 'maxToolCalls': 4}
    # This is the existing explicit human grant boundary, not the tool handler.
    assignment = r.c.repo.authorize(r.c.request, r.c.principal, 'approved-work')
    before = deepcopy(assignment['grant'])
    result = r.call('dispatch_project_assignment', assignment_id=assignment['id'], expected_revision=1)
    assert result.get('dispatch') == 'accepted', result
    assert r.calls[0][0] == worker
    assert r.calls[0][1]['config'] == {}  # Config authority stays at ordinary Turn admission.
    assert len(r.calls) == 1
    assert r.c.repo.read(assignment['id'])['grant'] == before
    if files:
        assert r.c.repo.read(assignment['id'])['request']['profile'] == r.c.request['profile']
    # Exact retry reuses the ordinary execution association.
    assert r.call('dispatch_project_assignment', assignment_id=assignment['id'],
                  expected_revision=1)['dispatch'] == 'accepted'
    assert len(r.calls) == 1
    writes = r.c.client.puts
    r.attempts['worker-attempt']['status'] = 'completed'
    r.turns['worker-turn'].update(projectionRevision=2, projection={'content': 'Verified worker result'})
    followed = r.call('get_project_assignment', assignment_id=assignment['id'])
    assert followed['execution']['taskId'] == 'worker-task'
    assert followed['execution']['status'] == 'completed'
    assert followed['execution']['resultRevision'] == 2
    assert not followed['canonicalDelivery']
    assert len(r.calls) == 1 and r.c.client.puts == writes


@pytest.mark.parametrize('violation', ['missing', 'stale', 'revoked', 'tenant', 'source', 'binding', 'recipient'])
def test_invalid_authority_never_dispatches_or_mints_grants(runtime, violation):
    r = runtime
    assignment = r.c.repo.authorize(r.c.request, r.c.principal, 'approved-work')
    args = {'assignment_id': assignment['id'], 'expected_revision': 1}
    if violation == 'missing': args['assignment_id'] = 'missing'
    if violation == 'stale': args['expected_revision'] = 2
    if violation == 'revoked': r.c.repo.revoke(assignment['id'], 1)
    if violation == 'tenant': r.task['_principalContext']['tenant_id'] = 'other'
    if violation == 'source': r.c.turn['actor'] = 'virtual_user'
    if violation == 'binding': r.c.binding.value['revision'] = 2
    if violation == 'recipient': r.c.conversations.rows['recipient']['settings']['projectPath'] = '/other'
    writes = r.c.client.puts
    result = r.call('dispatch_project_assignment', **args)
    assert 'error' in result, result
    assert not r.calls and r.c.client.puts == writes
    assert len(r.c.repo.list()) == 1


def test_creation_conflict_and_foreign_coordinator_are_rejected(runtime):
    r = runtime
    create_worker(r)
    assert 'error' in r.call('create_conversation', title='different', idempotency_key='work-one')
    r.c.binding.value['conversationId'] = 'recipient'
    assert 'error' in r.call('create_conversation', title='new', idempotency_key='two')
    assert len(r.created) == 1 and not r.calls


def test_registered_tools_create_and_dispatch_through_real_pipeline(runtime):
    from tests.tools.test_tool_settle_all_lanes import _mk_task, _mk_tc
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tools.coordinator import COORDINATOR_TOOLS
    r = runtime
    r.task.update({key: value for key, value in _mk_task().items() if key not in r.task})
    r.task['config'].update(autoApply=True)
    messages = []
    def run(name, args):
        parsed = _mk_tc(name, name, len(messages), args=args)
        r.task['toolRounds'] = [parsed[5]]
        execute_tool_pipeline(r.task, [parsed], r.task['config'], '/project', True,
                              COORDINATOR_TOOLS, messages, [], 0, 'test-model')
        assert parsed[5]['status'] == 'done', messages[-1]
    run('create_conversation', {'title': 'Independent worker', 'idempotency_key': 'work-one'})
    assert len(r.created) == 1 and not r.calls
    r.c.request['recipientId'] = r.created[0]['conv_id']
    assignment = r.c.repo.authorize(r.c.request, r.c.principal, 'approved-work')
    run('dispatch_project_assignment', {'assignment_id': assignment['id'], 'expected_revision': 1})
    assert len(r.calls) == 1 and r.calls[0][0] == r.created[0]['conv_id']
    run('get_project_assignment', {'assignment_id': assignment['id']})
    assert len(r.calls) == 1


def test_report_tool_reaches_canonical_publication_through_real_pipeline(runtime, reports, monkeypatch):
    from tests.tools.test_tool_settle_all_lanes import _mk_task, _mk_tc
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tools.coordinator import COORDINATOR_TOOLS
    from lib.conversations import coordinator_reports
    r = runtime
    monkeypatch.setattr(coordinator_reports, 'CoordinatorReports', reports.factory)
    r.task.update({key: value for key, value in _mk_task().items() if key not in r.task})
    r.task['config'].update(autoApply=True)
    for index in range(2):
        parsed = _mk_tc('report-' + str(index), 'report_to_user', index, args=reports.request)
        r.task['toolRounds'] = [parsed[5]]
        messages = []
        execute_tool_pipeline(r.task, [parsed], r.task['config'], '/project', True,
                              COORDINATOR_TOOLS, messages, [], 0, 'test-model')
        assert parsed[5]['status'] == 'done', messages
        assert json.loads(messages[-1]['content'])['published']
    assert len(reports.turns) == 1 and reports.repo.read()['unread']
    assert not r.created and not r.calls  # Reporting starts no worker/model.


def test_dispatch_error_does_not_claim_execution_never_started(runtime, monkeypatch):
    from lib.storage.errors import StorageError
    r = runtime
    assignment = r.c.repo.authorize(r.c.request, r.c.principal, 'approved-work')
    monkeypatch.setattr(r.service, 'dispatch_authorized',
                        lambda binding: (_ for _ in ()).throw(StorageError('storage_unavailable', 'lost response')))
    result = r.call('dispatch_project_assignment', assignment_id=assignment['id'], expected_revision=1)
    assert result['execution_started'] is None
    assert 'get_project_assignment' in result['recovery']


def test_creation_command_is_stable_under_uncertain_readback(runtime, monkeypatch):
    import lib.storage
    r = runtime
    attempts = []
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda **kw: SimpleNamespace(
        command=lambda operation, payload, command_id: attempts.append((operation, deepcopy(payload), command_id))))
    first = r.call('create_conversation', title='Independent worker', idempotency_key='work-one')
    second = r.call('create_conversation', title='Independent worker', idempotency_key='work-one')
    assert 'could not be confirmed' in first['error'] and second == first
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    payload = attempts[0][1]
    r.c.conversations.rows[payload['conv_id']] = {
        'user_id': 7, 'title': payload['title'], 'settings': deepcopy(payload['settings'])}
    assert create_worker(r) == payload['conv_id']
    assert len(attempts) == 2 and not r.calls


def test_creation_replay_uses_original_request_after_ordinary_metadata_edits(runtime):
    r = runtime
    worker = create_worker(r)
    row = r.c.conversations.rows[worker]
    row['title'] = 'Human renamed this worker'
    row['settings'].update(model='different-model', temperature=0.5)
    assert create_worker(r) == worker
    assert 'error' in r.call('create_conversation', title=row['title'], idempotency_key='work-one')
    assert len(r.created) == 1 and not r.calls


@pytest.mark.parametrize('collision', ['request', 'project', 'binding', 'missing_marker'])
def test_creation_race_validates_committed_identity(runtime, monkeypatch, collision):
    import lib.storage
    r = runtime
    def command(operation, payload, command_id):
        settings = deepcopy(payload['settings'])
        if collision == 'request': settings['_coordinatorCreationRequest'] = 'another-request'
        if collision == 'project': settings['projectPath'] = '/other'
        if collision == 'binding': settings['_coordinatorCreationKey'] = 'another-binding'
        if collision == 'missing_marker': settings.pop('_coordinatorCreationRequest')
        r.c.conversations.rows[payload['conv_id']] = {
            'user_id': 7, 'title': payload['title'], 'settings': settings}
        return {'applied': False}
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda **kw: SimpleNamespace(command=command))
    result = r.call('create_conversation', title='Independent worker', idempotency_key='work-one')
    assert 'conflicts' in result['error'] and result['execution_started'] is False
    assert not r.calls


def test_pinned_coordinator_turn_cannot_create_after_binding_epoch_changes(runtime):
    r = runtime
    create_worker(r)
    r.c.binding.value['revision'] += 1
    assert 'error' in r.call('create_conversation', title='another', idempotency_key='new')
    assert len(r.created) == 1
