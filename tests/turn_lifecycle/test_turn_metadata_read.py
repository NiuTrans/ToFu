"""Owned Turn metadata reads avoid bodies while preserving public identity.

The shared storage fixture exercises the real SQL sidecar and native daemon;
facade fault probes enforce strict result/flag boundaries before context use.
"""
import pytest

from lib import turn_lifecycle as turns
from lib.storage.errors import StorageError
from tests.turn_lifecycle.test_task_event_delivery import storage as storage

pytestmark = pytest.mark.unit


def test_metadata_matches_default_owned_turn_without_growing_body(storage, monkeypatch):
    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: storage.client)
    conversation = 'metadata-read'
    body = {'content': 'growing answer ' * 24000}
    created = turns.append_settled_turn(conversation,
        command_id='metadata-seed', actor='assistant', projection=body, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Metadata'})
    turn_id = created['turn']['turnId']
    full = turns.get_turn(conversation, turn_id, user_id=1)
    explicit = turns.get_turn(conversation, turn_id, user_id=1, include_projection=True)
    metadata = turns.get_turn(conversation, turn_id, user_id=1, include_projection=False)
    assert explicit == full
    assert metadata == {key: value for key, value in full.items() if key != 'projection'}
    assert full['projection']['content'] == body['content']
    assert 'projection' not in metadata and not any(key.startswith('_') for key in metadata)
    # SQL filters the owner before lookup; the native daemon also refuses a
    # payload owner outside its authenticated session before operation entry.
    with pytest.raises((turns.LifecycleNotFound, StorageError)) as foreign:
        turns.get_turn(conversation, turn_id, user_id=2, include_projection=False)
    if isinstance(foreign.value, StorageError):
        assert foreign.value.code == 'database_forbidden'
    with pytest.raises(turns.LifecycleNotFound):
        turns.get_turn('foreign-conversation', turn_id, user_id=1, include_projection=False)
    with pytest.raises(turns.LifecycleNotFound):
        turns.get_turn(conversation, 'missing', user_id=1, include_projection=False)
    for invalid in [None, 0, 1, 'false', [], {}]:
        with pytest.raises(StorageError) as caught:
            storage.client.query('turn.get', {'conversation_id': conversation,
                'turn_id': turn_id, 'user_id': 1, 'include_projection': invalid})
        assert caught.value.code == 'database_protocol_error'


@pytest.mark.parametrize('flag', [None, 0, 1, 'false', [], {}])
def test_invalid_projection_flag_is_rejected_before_storage(monkeypatch, flag):
    monkeypatch.setattr(turns, '_turn_client',
        lambda **kwargs: pytest.fail('Invalid projection flag reached storage'))
    with pytest.raises(TypeError, match='projection flag'):
        turns.get_turn('conversation', 'turn', user_id=7, include_projection=flag)


@pytest.mark.parametrize('private_field', ['projection', '_projectionHead'])
def test_metadata_response_cannot_smuggle_a_body(monkeypatch, private_field):
    class Client:
        def query(self, operation, payload):
            assert operation == 'turn.get' and payload['include_projection'] is False
            return {'turnId': 'turn', private_field: {'content': 'not metadata'}}
    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: Client())
    with pytest.raises(StorageError) as caught:
        turns.get_turn('conversation', 'turn', user_id=7, include_projection=False)
    assert caught.value.code == 'database_protocol_error'
