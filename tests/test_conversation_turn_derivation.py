"""Conversation reads project transcripts from the canonical turn store.

The conversation row owns only identity, metadata, and an immutable imported
archive. Runtime transcript creation and mutation are turn commands. Retired
whole-document writers are absent from the public storage operation registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.storage import StorageError, StorageSupervisor

pytestmark = pytest.mark.unit


@pytest.fixture()
def storage(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '2')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        yield supervisor
    finally:
        supervisor.stop()


def _make_turn_native_conv(storage, monkeypatch, conv_id='turn-native-conv'):
    """One human input + one settled assistant reply on the main lane."""
    from lib.turn_lifecycle import (
        claim_attempt_start, create_turn_pair, record_task_event,
    )
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)
    created = create_turn_pair(
        conv_id, command_id=f'{conv_id}-cmd-1',
        input_projection={'content': 'hello server', 'timestamp': 111},
        config={}, user_id=1,
        conversation_defaults={
            'allowCreate': True,
            'title': 'Turn native',
            'createdAt': 1,
            'settings': {},
        })
    attempt_id = created['attempt']['attemptId']
    assert claim_attempt_start(attempt_id, user_id=1) is True
    assert record_task_event(
        {'_attemptId': attempt_id, '_userId': 1,
         'content': 'assistant body',
         'thinking': '', 'toolRounds': [], 'segments': [], 'status': 'done'},
        {'type': 'done', 'finishReason': 'stop'}) is True
    return created


def test_get_without_flag_derives_read_only_compatibility_view(
        storage, monkeypatch):
    _make_turn_native_conv(storage, monkeypatch)
    doc = storage.client.query('conversation.get', {
        'conv_id': 'turn-native-conv', 'user_id': 1})
    assert [message['role'] for message in doc['messages']] == [
        'user', 'assistant',
    ]
    assert doc['metadata']['msg_count'] == 2  # count backfill (pre-existing)


def test_get_with_flag_derives_body_from_turns(storage, monkeypatch):
    _make_turn_native_conv(storage, monkeypatch)
    doc = storage.client.query('conversation.get', {
        'conv_id': 'turn-native-conv', 'user_id': 1, 'derive_messages': True})
    messages = doc['messages']
    assert [m['role'] for m in messages] == ['user', 'assistant']
    user_msg, assistant_msg = messages
    assert user_msg['content'] == 'hello server'
    assert user_msg['timestamp'] == 111   # projection timestamp wins
    assert assistant_msg['content'] == 'assistant body'
    # Turn identity rides along exactly like the client's own projection.
    assert assistant_msg['_turnId']
    assert assistant_msg['_turnActor'] == 'assistant'
    assert assistant_msg['_turnKind'] == 'reply'
    assert assistant_msg['_turnLaneId'] == 'main'
    assert assistant_msg['_turnStatus'] == 'completed'
    assert assistant_msg['_turnSettlement']['outcome'] == 'completed'
    # No projection timestamp on the assistant turn → turn createdAt.
    assert assistant_msg['timestamp'] > 0


def test_list_full_projection_derives_bodies(storage, monkeypatch):
    _make_turn_native_conv(storage, monkeypatch)
    docs = storage.client.query('conversation.list', {
        'user_id': 1, 'limit': 10, 'order_by': 'id_asc',
        'include_messages': True, 'derive_messages': True})
    doc = next(d for d in docs
               if d['metadata']['id'] == 'turn-native-conv')
    assert [m['role'] for m in doc['messages']] == ['user', 'assistant']


@pytest.mark.parametrize('operation', [
    'conversation.upsert',
    'conversation.replace',
])
def test_whole_document_writers_are_not_public_operations(
        storage, monkeypatch, operation):
    _make_turn_native_conv(storage, monkeypatch)
    with pytest.raises(StorageError) as excinfo:
        storage.client.command(operation, {
            'conv_id': 'turn-native-conv', 'user_id': 1,
            'messages': [{'role': 'user', 'content': 'hijack'}],
            'metadata': {},
        }, f'retired-writer:{operation}')
    assert excinfo.value.code == 'database_protocol_error'
    assert 'Unknown storage operation' in str(excinfo.value)


def test_metadata_update_renames_without_touching_transcript(
        storage, monkeypatch):
    _make_turn_native_conv(storage, monkeypatch)
    result = storage.client.command('conversation.metadata.update', {
        'conv_id': 'turn-native-conv', 'user_id': 1,
        'updates': {'title': 'Renamed'},
    }, 'turn-native-rename')
    assert result['applied'] is True
    after = storage.client.query('conversation.get', {
        'conv_id': 'turn-native-conv', 'user_id': 1,
        'derive_messages': True,
    })
    assert after['metadata']['title'] == 'Renamed'
    assert [message['content'] for message in after['messages']] == [
        'hello server', 'assistant body',
    ]
