"""Real owner-scoped binding HTTP/record CAS journey; no LLM is dispatched."""
import uuid
import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


def test_budget_defaults_cas_owner_and_binding_never_start_work(flask_client, chat_sidecar):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.project_dispatch.repository import ProjectDispatchRepository
    path = '/test/budget-' + uuid.uuid4().hex
    binding = ProjectCoordinationRepository(1).create(path, folder_id='test', expected_revision=0, command_id='create')
    endpoint = '/api/v4/project/coordination/budget'
    initial = flask_client.get(endpoint, query_string={'path': path})
    assert initial.status_code == 200, initial.get_json()
    state = initial.get_json()
    assert state['revision'] == 0 and state['request'] is None
    request = {'path': path, 'conversationId': binding['conversationId'], 'bindingRevision': binding['revision'],
        'expectedRevision': 0, 'settings': {**state['settings'], 'maxTokens': 9000, 'maxModelRequests': 12}}
    updated = flask_client.post(endpoint, json=request)
    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()['revision'] == 1 and updated.get_json()['settings']['maxTokens'] == 9000
    assert flask_client.post(endpoint, json=request).get_json() == updated.get_json()
    assert flask_client.post(endpoint, json={**request, 'settings': {**request['settings'], 'maxTokens': 8000}}).status_code == 400
    assert flask_client.post(endpoint, json={**request, 'bindingRevision': 999}).status_code == 403
    assert flask_client.post(endpoint, json={**request, 'path': '/foreign'}).status_code == 403
    assert flask_client.post(endpoint, json={**request, 'owner': 2}).status_code == 400
    assert flask_client.post(endpoint, json={**request, 'settings': {**request['settings'], 'maxTokens': -1}}).status_code == 400
    assert ProjectCoordinationRepository(1).read(path) == binding
    value, _ = ProjectDispatchRepository(1, path)._load()
    assert not value['assignments'] and not value.get('requestBudgets')


def test_coordinator_reports_use_real_turns_and_ack_only_displayed_message(flask_client, chat_sidecar):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.coordinator_reports import CoordinatorReports
    from lib.turn_lifecycle import get_turn
    path = '/test/reports-' + uuid.uuid4().hex
    binding = ProjectCoordinationRepository(1).create(path, folder_id='test', expected_revision=0, command_id='create')
    scope = {'owner': 1, 'projectPath': path, 'coordinatorId': binding['conversationId'], 'bindingRevision': binding['revision']}
    reports = CoordinatorReports(1, path)
    request = {'title': 'Migration decision', 'message': 'Choose the maintenance window.', 'kind': 'decision', 'idempotency_key': 'one'}
    first = reports.publish(scope, request)
    endpoint = '/api/v4/project/coordination/reports'
    response = flask_client.get(endpoint, query_string={'path': path})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['unread'] is True
    assert 'maintenance window' in response.get_json()['latest']['content']
    turn = get_turn(binding['conversationId'], first['turnId'], user_id=1)
    assert turn['actor'] == 'assistant' and turn['kind'] == 'coordinator_report.decision'
    assert turn['projection']['origin']['initiator'] == 'brain'
    ack = {'path': path, 'conversationId': binding['conversationId'], 'bindingRevision': binding['revision'], 'turnId': first['turnId']}
    read = flask_client.post(endpoint + '/read', json=ack)
    assert read.status_code == 200, read.get_json()
    assert read.get_json()['unread'] is False
    assert reports.publish(scope, request) == first
    assert reports.read()['unread'] is False
    second = reports.publish(scope, {**request, 'message': 'Tests passed.', 'kind': 'delivery', 'idempotency_key': 'two'})
    stale = flask_client.post(endpoint + '/read', json=ack)
    assert stale.get_json()['unread'] is True
    assert stale.get_json()['latest']['turnId'] == second['turnId']
    assert flask_client.post(endpoint + '/read', json={**ack, 'bindingRevision': 999}).status_code == 403
    assert flask_client.post(endpoint + '/read', json={**ack, 'path': '/foreign'}).status_code == 403
    assert flask_client.post(endpoint + '/read', json={**ack, 'owner': 2}).status_code == 400
    assert flask_client.get(endpoint, query_string={'path': '/foreign'}).get_json()['latest'] is None


@pytest.fixture
def flask_app(chat_sidecar):
    # Opt into the repository's disposable authority BEFORE importing the
    # application. Do not use the shared native binary from another revision.
    import server
    server.app.config.update(TESTING=True)
    return server.app


def test_bind_replay_conflict_owner_and_delete(flask_client, chat_sidecar):
    from tests._seed import seed_conversation, delete_conversation, seed_coordinator_binding
    path = '/test/project-' + uuid.uuid4().hex
    conv_id, foreign = 'coord-' + uuid.uuid4().hex, 'foreign-' + uuid.uuid4().hex
    seed_conversation(conv_id, settings={'projectPath': path})
    seed_conversation(foreign, user_id=2, settings={'projectPath': path})
    endpoint = '/api/v4/project/coordination'
    initial = flask_client.get(endpoint, query_string={'path': path})
    assert initial.status_code == 200
    assert initial.get_json()['availability'] == 'unbound'
    body = {'path': path, 'conversationId': conv_id, 'expectedRevision': 0}
    headers = {'Idempotency-Key': 'bind-one'}

    rejected = flask_client.post(endpoint + '/bind', json=body, headers=headers)
    assert rejected.status_code == 400, rejected.get_json()
    assert flask_client.get(endpoint, query_string={'path': path}).get_json()['availability'] == 'unbound'
    seed_coordinator_binding(path, conv_id)
    bound = flask_client.post(endpoint + '/bind', json=body, headers=headers)
    assert bound.status_code == 200, bound.get_json()
    assert bound.get_json()['revision'] == 1
    assert bound.get_json()['availability'] == 'available'
    assert flask_client.post(endpoint + '/bind', json=body, headers=headers).get_json()['revision'] == 1
    conflict = flask_client.post(endpoint + '/bind', json={**body, 'conversationId': ''},
                                 headers={'Idempotency-Key': 'stale'})
    assert conflict.status_code == 409
    denied = flask_client.post(endpoint + '/bind', json={**body, 'conversationId': foreign, 'expectedRevision': 1},
                               headers={'Idempotency-Key': 'foreign'})
    assert denied.status_code == 400
    missing_id = flask_client.post(endpoint + '/bind', json={**body, 'expectedRevision': 1})
    assert missing_id.status_code == 400
    delete_conversation(conv_id)
    unavailable = flask_client.get(endpoint, query_string={'path': path}).get_json()
    assert unavailable['availability'] == 'unavailable'
    assert unavailable['capabilities'] == {'assign': False, 'sendMessage': False, 'steerSubagent': True}


def test_scoped_message_receipts_are_readonly_and_match_contract(flask_client, chat_sidecar):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from tests._seed import seed_conversation
    from lib.conversations.project_messages import ProjectMailbox
    path = '/test/mailbox-' + uuid.uuid4().hex
    recipient = 'mailbox-' + uuid.uuid4().hex
    seed_conversation(recipient, settings={'projectPath': path})
    mailbox = ProjectMailbox(1, path, recipient)
    mailbox.submit('sender', {'content': 'x' * 1000, 'idempotency_key': 'one'}, 'sender-task')
    before = mailbox.load()
    endpoint = '/api/v4/project/coordination/messages'
    response = flask_client.get(endpoint, query_string={'path': path, 'conversation_id': recipient})
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert len(result['messages'][0]['content']) == 256
    assert result['messages'][0]['state'] == 'submitted'
    assert result['messages'][0]['decision'] is None
    contract = json.loads((Path(__file__).resolve().parents[2] / 'contracts/project_messages_v1.json').read_text())
    assert result['ok'] is True
    payload = {key: value for key, value in result.items() if key != 'ok'}
    Draft202012Validator({**contract, '$ref': '#/$defs/pageView'}).validate(payload)
    assert mailbox.load() == before
    assert flask_client.get(endpoint, query_string={'path': '/different', 'conversation_id': recipient}).status_code == 400


def test_binding_schema_and_generated_dto_are_current():
    import json
    import subprocess
    from pathlib import Path
    from jsonschema import Draft202012Validator
    root = Path(__file__).resolve().parents[2]
    schema = json.loads((root / 'contracts/project_coordination_v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate({
        'projectPath': '/project', 'conversationId': '', 'revision': 0,
        'availability': 'unbound', 'capabilities': {'assign': False, 'sendMessage': False, 'steerSubagent': False},
    })
    subprocess.run(['python3', str(root / 'scripts/gen_project_coordination_contract.py'), '--check'], check=True)


def test_create_empty_coordinator_replay_and_scope(flask_client, chat_sidecar, monkeypatch, tmp_path):
    import routes.api_v1.folders as folders
    from lib.conversations.repository import ConversationRepository
    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    root = str(tmp_path / 'project')

    from tests._seed import seed_conversation
    historical = 'historical-' + uuid.uuid4().hex
    seed_conversation(historical, settings={'projectPath': root},
                      messages=[{'role': 'user', 'content': 'Unrelated historical context'}])
    folder = folders._repository().create(1, {'name': 'Project', 'projectPaths': [root]})
    body = {'path': root, 'folderId': folder['id'], 'expectedRevision': 0}
    endpoint = '/api/v4/project/coordination/create'
    headers = {'Idempotency-Key': 'create-one'}
    assert flask_client.post(endpoint, json=body).status_code == 400
    invalid = flask_client.post(endpoint, json={**body, 'path': root + '-other'}, headers=headers)
    assert invalid.status_code == 400, invalid.get_json()
    invalid = flask_client.post(endpoint, json={**body, 'folderId': 'foreign'}, headers=headers)
    assert invalid.status_code == 403, invalid.get_json()
    created = flask_client.post(endpoint, json=body, headers=headers)
    assert created.status_code == 200, created.get_json()
    result = created.get_json()
    assert result['availability'] == 'available'
    cid = result['conversationId']

    assert cid != historical
    snapshot = ConversationRepository().get(cid, user_id=1)
    assert snapshot is not None
    assert snapshot.messages == []
    assert snapshot.metadata['settings']['projectPath'] == root
    assert snapshot.metadata['settings']['folderId'] == folder['id']
    assert result['capabilities']['assign'] is False
    repeated = flask_client.post(endpoint, json=body, headers={'Idempotency-Key': 'another-click'})
    assert repeated.status_code == 200
    assert repeated.get_json()['conversationId'] == cid
    assert repeated.get_json()['revision'] == result['revision']
    assert ConversationRepository().get(cid, user_id=2) is None

    replacement = flask_client.post(endpoint, json={**body, 'expectedRevision': result['revision']},
                                    headers={'Idempotency-Key': 'replace-clean'})
    assert replacement.status_code == 200, replacement.get_json()
    fresh = replacement.get_json()
    assert fresh['conversationId'] not in (historical, cid)
    assert fresh['revision'] == result['revision'] + 1
    assert ConversationRepository().get(fresh['conversationId'], user_id=1).messages == []
    assert ConversationRepository().get(cid, user_id=1) is not None
    assert ConversationRepository().get(historical, user_id=1).messages[0]['content'] == 'Unrelated historical context'
    replay = flask_client.post(endpoint, json={**body, 'expectedRevision': result['revision']},
                              headers={'Idempotency-Key': 'replace-clean'})
    assert replay.status_code == 200
    assert replay.get_json()['conversationId'] == fresh['conversationId']
