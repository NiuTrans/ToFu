"""Owner-scoped effect inspection HTTP and malformed/ambiguous evidence guards."""
import copy
import uuid
from types import SimpleNamespace

import pytest

from lib.conversation_sync.service import ConversationSyncService, ConversationSyncNotFound
from lib.project_mod.effect_reconciliation import reference_for
from tests.project_mod.test_file_effect_reconciliation import prepared as prepared

pytest_plugins = ('tests._chat_sidecar',)


def round_for(record):
    return {'toolCallId': 'call', 'toolName': 'write_file', 'taskId': record['taskId'],
            'status': 'error', 'executionEffect': {
                'operationId': 'a' * 32, 'state': 'unknown', 'scope': 'attempt',
                'recovery': 'reconcile', 'reconciliation': reference_for(record)}}


@pytest.mark.unit
@pytest.mark.parametrize('invalid', ['wrong_task', 'duplicate', 'bad_schema', 'process', 'other_tool', 'no_reference'])
def test_invalid_or_unsupported_occurrence_never_queries_file_authority(prepared, monkeypatch, invalid):
    _, _, _, record = prepared
    row = round_for(record)
    if invalid == 'wrong_task': row['taskId'] = 'foreign'
    elif invalid == 'bad_schema': row['executionEffect']['reconciliation']['postBytes'] = -1
    elif invalid == 'process': row['executionEffect']['scope'] = 'process'
    elif invalid == 'other_tool': row['toolName'] = 'run_command'
    elif invalid == 'no_reference': row['executionEffect'].pop('reconciliation')
    rows = [row, copy.deepcopy(row)] if invalid == 'duplicate' else [row]
    service = ConversationSyncService(SimpleNamespace(turn=lambda *a: {'projection': {'toolRounds': rows}}))
    monkeypatch.setattr('lib.project_mod.effect_reconciliation.inspect_prepared_write',
                        lambda *a, **kw: pytest.fail('invalid occurrence reached file authority'))
    value = service.inspect_tool_effect('conversation', 'turn', 1, operation_id='a' * 32)
    assert value['outcome'] == 'unknown'


@pytest.mark.api
@pytest.mark.auth_mode('open')
def test_http_inspection_fences_owner_and_does_not_cache_or_publish(
        flask_client, chat_sidecar, prepared, monkeypatch):
    from lib.storage import get_storage_client
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.turn_images import turn_image_owner_scope
    from routes import conversation_sync_v3 as routes
    _, target, _, record = prepared
    client = get_storage_client(write=True)
    cid = record['convId']
    service = ConversationSyncService(SidecarConversationSyncRepository(client_factory=lambda write=False: client))
    monkeypatch.setattr(routes, '_service', service)
    unique = uuid.uuid4().hex
    client.command('conversation.create', {'conv_id': cid, 'user_id': 1, 'title': 'File effect',
                   'created_at': 1, 'updated_at': 1, 'settings': {}}, unique + '-create')
    try:
        client.command('turn.append_settled', {'conversation_id': cid, 'user_id': 1,
                       'command_id': unique + '-append', 'actor': 'assistant', 'kind': 'fixture',
                       'projection': {'toolRounds': [round_for(record)]}, 'created_at': 2}, unique + '-append')
        snapshot = service.snapshot(cid, 1)
        tid = snapshot['turns'][0]['turnId']
        url = f'/api/v4/conversations/{cid}/turns/{tid}/tool-effects/' + 'a' * 32
        fence = {'ownerScope': turn_image_owner_scope(1, cid)}
        response = flask_client.get(url, query_string=fence)
        assert response.status_code == 200, response.get_json()
        assert response.get_json()['outcome'] == 'file_not_published'
        assert response.headers['Cache-Control'] == 'private, no-store'
        assert target.read_bytes() == b'old\r\n\xff'
        assert flask_client.get(url).status_code == 400
        assert flask_client.get(url, query_string={'ownerScope': '0' * 24}).status_code == 404
        assert flask_client.get(url[:-32] + 'invalid', query_string=fence).status_code == 400
        assert flask_client.get(url[:-32] + 'b' * 32, query_string=fence).status_code == 404
        with pytest.raises(ConversationSyncNotFound):
            service.inspect_tool_effect(cid, tid, 2, operation_id='a' * 32)
    finally:
        client.command('conversation.purge', {'conv_id': cid, 'user_id': 1}, unique + '-purge')
