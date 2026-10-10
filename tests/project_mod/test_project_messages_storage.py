"""Disposable real authority journey; never opens or writes application stores."""
from types import SimpleNamespace
import uuid
import pytest
from lib.conversations import project_messages as pm

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit


def test_real_storage_round_trip_and_consideration_survive_reconstruction(chat_sidecar, monkeypatch):
    project = '/project/' + uuid.uuid4().hex
    def snapshot(self, conv, *, user_id, include_messages):
        assert not include_messages
        if conv in {'coordinator', 'worker'} and user_id == 1:
            return SimpleNamespace(metadata={'settings': {'projectPath': project}})
    monkeypatch.setattr(pm.ConversationRepository, 'get', snapshot)
    coordinator = {'id': 'root-turn', 'convId': 'coordinator', '_userId': 1, 'config': {'projectPath': project}}
    worker = {**coordinator, 'id': 'worker-turn', 'convId': 'worker'}
    sent = pm.execute('send_project_message', {'conversation_id': 'worker', 'kind': 'consult', 'content': 'Review evidence', 'idempotency_key': 'request'}, coordinator)
    assert not sent['execution_started']
    incoming = pm.execute('get_project_messages', {}, worker)['messages'][0]
    assert incoming['decision'] is None and incoming['state'] == 'submitted'
    considered = pm.execute('consider_project_message', {'message_id': incoming['id'], 'expected_revision': 1, 'decision': 'accepted', 'note': 'Will review within existing authority'}, worker)
    assert not considered['execution_started']
    reply = pm.execute('send_project_message', {'conversation_id': 'coordinator', 'kind': 'report', 'content': 'Evidence checked', 'reply_to': incoming['id'], 'idempotency_key': 'reply'}, worker)
    reloaded = pm.ProjectMailbox(1, project, 'coordinator').load()[0]['messages'][0]
    assert reloaded['id'] == reply['message']['id']
    assert reloaded['replyTo'] == incoming['id']
    outgoing = pm.execute('get_project_messages', {'direction': 'outgoing', 'conversation_id': 'worker'}, coordinator)['messages'][0]
    assert outgoing['decision'] == 'accepted' and outgoing['state'] == 'submitted'
    assert len(pm.ProjectMailbox(1, project, 'worker').load()[0]['messages']) == 1
