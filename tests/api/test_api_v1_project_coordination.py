"""Real owner-scoped binding HTTP/record CAS journey; no LLM is dispatched."""
import uuid
import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


@pytest.fixture
def flask_app(chat_sidecar):
    # Opt into the repository's disposable authority BEFORE importing the
    # application. Do not use the shared native binary from another revision.
    import server
    server.app.config.update(TESTING=True)
    return server.app


def test_bind_replay_conflict_owner_and_delete(flask_client, chat_sidecar):
    from tests._seed import seed_conversation, delete_conversation
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
