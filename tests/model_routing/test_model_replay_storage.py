"""Completed model replay witnesses against the real owner-scoped Sidecar."""
import copy

import pytest

from tests.storage.test_storage_authority_sql_contract import storage, _import_conversation  # noqa: F401

pytestmark = pytest.mark.unit


def test_checkpoint_get_and_list_use_the_same_authority_witness(storage, monkeypatch, tmp_path):  # noqa: F811
    from lib import storage as storage_api
    from lib.storage.model_continuation import ModelContinuationStore
    from lib.tasks_pkg import model_replay
    client = storage.client
    monkeypatch.setattr(storage_api, 'get_storage_client', lambda **kwargs: client)
    cache = ModelContinuationStore(client=client)
    monkeypatch.setattr(model_replay, 'ModelContinuationStore', lambda: cache)
    _import_conversation(client, 'model-replay', messages=[
        {'role': 'user', 'content': 'work'}, {'role': 'assistant', 'content': 'done'}])
    rows = client.query('turn.list', {'conversation_id': 'model-replay', 'user_id': 1})
    config = {'model': 'kimi-k3', '_modelReplayBase': [model_replay.turn_witness(rows[0])]}
    model_view = [{'role': 'user', 'content': 'hook-filtered work'},
                  {'role': 'assistant', 'content': 'done'}]
    model_replay.publish_completed_task({
        'id': 'test-task', '_userId': 1, 'convId': 'model-replay',
        '_turnId': rows[-1]['turnId'], 'model': 'kimi-k3', 'content': 'done',
        'messages': model_view, 'config': config,
    })
    pending = {**rows[-1], 'turnId': 'new-attempt', 'projection': {'content': ''}}
    next_config = {'model': 'kimi-k3'}
    restored, covered = model_replay.restore_prefix(
        rows + [pending], next_config, conversation_id='model-replay', user_id=1)
    assert covered == len(rows) and restored == model_view
    assert next_config['_modelReplayStatus'] == 'restored'
    assert client.query('turn.list', {'conversation_id': 'model-replay', 'user_id': 1}) == rows
    changed = copy.deepcopy(rows)
    changed[0]['projection']['content'] = 'edited'
    assert model_replay.restore_prefix(changed + [pending], {},
        conversation_id='model-replay', user_id=1) == ([], 0)
