"""Real storage preserves completion identity while refusing legacy queue cores."""
import json
import threading
import uuid

import pytest

from lib import message_queue
from lib.tasks_pkg import background_commands as commands
from lib.tasks_pkg.handlers._background_command import _queue_completion

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.unit


def test_completion_config_publication_and_recovery(semantic_authority, monkeypatch, tmp_path):
    get_client, owner_id, native_engine = semantic_authority
    client = get_client(write=True)
    conv_id = f'completion-{uuid.uuid4().hex}'
    client.command('conversation.create', {'conv_id': conv_id, 'user_id': owner_id,
        'title': 'Completion contract', 'settings': {}}, f'create-{conv_id}')
    monkeypatch.setattr(message_queue, '_queue_client', get_client)
    monkeypatch.setattr(commands, '_records_root', lambda: str(tmp_path / 'records'))
    monkeypatch.setattr(commands, '_registry', {})
    command_id = 'bg_config_authority'
    folder = tmp_path / 'records' / command_id
    folder.mkdir(parents=True)
    original_message = {'text': 'original completed result', '_backgroundCommand': command_id,
        '_backgroundCommandCompletion': {'commandId': command_id, 'status': 'completed'},
        'timestamp': 123}
    original_config = {'model': 'original-model', 'thinkingEnabled': True,
                       'checkpointToolRounds': ['x' * 1100000], 'contentPrefix': 'old'}
    frozen = {'command_id': command_id, 'conv_id': conv_id, 'user_id': owner_id,
              'message': original_message, 'config': original_config}
    path = folder / 'delivery.json'
    path.write_text(json.dumps(frozen))
    for _ in range(2):
        assert _queue_completion(task={'convId': conv_id, '_userId': owner_id},
            config={'model': 'changed'}, command_id=command_id,
            command='echo done', result='reconstructed')
    rows = client.query('queue.list', {'conv_id': conv_id, 'user_id': owner_id,
        'include_full': True})
    assert len(rows) == 1
    assert rows[0]['payload'] == original_message
    if native_engine is not None:
        assert rows[0]['config'] == {'model': 'original-model', 'thinkingEnabled': True}
    else:
        # SQL can accept the old request. Its committed envelope is replayed
        # exactly; it must never be rewritten after a lost ACK.
        assert rows[0]['config'] == original_config
    assert json.loads(path.read_text()) == frozen
