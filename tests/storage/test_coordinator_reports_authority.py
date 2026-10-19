"""Coordinator report publication/attention on the actual selected authority."""
import uuid
import pytest

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_report_replay_read_and_restart_keep_one_canonical_turn(semantic_authority, monkeypatch):
    factory, owner, engine = semantic_authority
    if engine is not None:
        assert engine._build_identity['source_sha256'] == engine._checkout_source_sha256
    from lib import storage
    monkeypatch.setattr(storage, 'get_storage_client', factory)
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.coordinator_reports import CoordinatorReports, POLICY
    from lib.turn_lifecycle import get_turn, create_turn_pair, bind_task, mark_task_started, list_turns
    project = '/report-authority/' + uuid.uuid4().hex
    binding = ProjectCoordinationRepository(owner).create(project, folder_id='test', expected_revision=0, command_id='create')
    scope = {'owner': owner, 'projectPath': project, 'coordinatorId': binding['conversationId'], 'bindingRevision': binding['revision']}
    reports = CoordinatorReports(owner, project)
    active = create_turn_pair(binding['conversationId'], user_id=owner,
        command_id='active-coordinator-turn', input_projection={'content': 'Coordinate this project'}, config={})
    attempt = active['attempt']['attemptId']
    task_id = 'active-report-tool-task'
    bind_task(attempt, task_id, user_id=owner)
    mark_task_started(attempt, task_id, user_id=owner)
    request = {'title': 'Verified result', 'message': 'The acceptance checks passed.', 'kind': 'delivery', 'idempotency_key': 'delivery'}
    published = reports.publish(scope, request)
    # The report can arrive during its own Coordinator tool loop without
    # superseding the ordinary attempt or preventing its final settlement.
    factory(write=True).command('turn.event.record', dict(user_id=owner,
        attempt_id=attempt, task_id=task_id, terminal=True, status='completed',
        projection={'content': 'Coordinator finished planning'},
        settlement={'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []}), 'finish-coordinator')
    rows = list_turns(binding['conversationId'], user_id=owner)['turns']
    assert [t['projection']['content'] for t in rows] == [
        'Coordinate this project', 'Coordinator finished planning', 'Verified result\n\nThe acceptance checks passed.']
    reports.mark_read({'path': project, 'conversationId': binding['conversationId'], 'bindingRevision': binding['revision'], 'turnId': published['turnId']})
    if engine is not None:
        engine.crash_cycle()
    restored = CoordinatorReports(owner, project)
    assert restored.publish(scope, request) == published
    assert restored.read()['unread'] is False
    with pytest.raises(ValueError, match='conflicts'):
        restored.publish(scope, {**request, 'message': 'Changed content'})
    assert restored.publish(scope, request) == published
    turn = get_turn(binding['conversationId'], published['turnId'], user_id=owner)
    assert turn['actor'] == 'assistant' and turn['projection']['origin']['initiator'] == 'brain'
    assert factory().query('record.list', {'namespace': POLICY['pendingNamespace'], 'prefix': reports.key, 'limit': 32}) == []


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_deletion_during_publication_never_recreates_coordinator(semantic_authority, monkeypatch):
    factory, owner, _ = semantic_authority
    from lib import storage
    from lib.turn_lifecycle import append_settled_turn, LifecycleNotFound
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.coordinator_reports import CoordinatorReports, recover_pending_reports, POLICY
    from lib.conversations.repository import ConversationRepository
    from tests._seed import delete_conversation
    monkeypatch.setattr(storage, 'get_storage_client', factory)
    project = '/report-delete/' + uuid.uuid4().hex
    binding = ProjectCoordinationRepository(owner).create(project, folder_id='test', expected_revision=0, command_id='create')
    scope = {'owner': owner, 'projectPath': project, 'coordinatorId': binding['conversationId'], 'bindingRevision': binding['revision']}

    def race_delete(conversation_id, **kwargs):
        delete_conversation(conversation_id, user_id=owner)
        return append_settled_turn(conversation_id, **kwargs)

    reports = CoordinatorReports(owner, project, append_turn=race_delete)
    with pytest.raises(LifecycleNotFound):
        reports.publish(scope, {'title': 'Result', 'message': 'Ready', 'kind': 'delivery', 'idempotency_key': 'deleted'})
    assert ConversationRepository().get(binding['conversationId'], user_id=owner) is None
    assert recover_pending_reports(client=factory()) == 0
    assert factory().query('record.list', {'namespace': POLICY['pendingNamespace'], 'prefix': reports.key, 'limit': 32}) == []
