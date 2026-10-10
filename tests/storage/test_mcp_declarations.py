"""Durable declaration authority on the configured disposable storage process."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import uuid

import pytest

from lib.storage.mcp_declarations import (
    MAX_DECLARATION_BYTES, MCPDeclarationError, MCPDeclarationStore, SETTING_KEY,
)
from tests.storage.test_storage_application_contract import application_storage as application_storage

pytestmark = pytest.mark.api


def definition(name='read'):
    # Deliberately non-alphabetic nested keys: JSONB/map ordering must not
    # change the provider wire, including schema properties/required arrays.
    return {'type': 'function', 'function': {'name': f'mcp__docs__{name}',
        'parameters': {'type': 'object', 'properties': {'z': {'type': 'string'},
                       'a': {'type': 'integer'}}, 'required': ['z', 'a']},
        'description': 'ordered wire'}}


def create(client, conv='declarations', settings=None):
    client.command('conversation.create', {'conv_id': conv, 'user_id': 1,
        'title': 'Declaration test', 'created_at': 1, 'updated_at': 1,
        'settings': settings or {'unrelated': 'preserve'}}, uuid.uuid4().hex)
    return conv


def test_durable_empty_reset_owner_and_delete(application_storage):
    client = application_storage
    conv = create(client)
    store = MCPDeclarationStore(client)
    assert store.get(user_id=1, conversation_id=conv) is None
    assert store.freeze([], user_id=1, conversation_id=conv) == []
    assert MCPDeclarationStore(client).freeze([definition()], user_id=1, conversation_id=conv) == []
    with pytest.raises(MCPDeclarationError):
        store.get(user_id=2, conversation_id=conv)
    store.reset(user_id=1, conversation_id=conv)
    assert store.get(user_id=1, conversation_id=conv) is None
    wire = [definition('second'), definition('first')]
    store.freeze(wire, user_id=1, conversation_id=conv)
    assert json.dumps(store.get(user_id=1, conversation_id=conv)) == json.dumps(wire)
    # Reset/re-freeze the same candidate must not replay an old command receipt.
    store.reset(user_id=1, conversation_id=conv)
    store.freeze(wire, user_id=1, conversation_id=conv)
    assert store.get(user_id=1, conversation_id=conv) == wire
    settings = client.query('conversation.get', {'conv_id': conv, 'user_id': 1,
        'derive_messages': False})['metadata']['settings']
    assert settings['unrelated'] == 'preserve'
    clone = create(client, 'clone', settings)
    assert store.get(user_id=1, conversation_id=clone) is None
    client.command('conversation.delete', {'conv_id': conv, 'user_id': 1}, uuid.uuid4().hex)
    with pytest.raises(MCPDeclarationError):
        store.get(user_id=1, conversation_id=conv)


def test_atomic_first_writer_preserves_concurrent_settings(application_storage):
    client = application_storage
    conv = create(client)
    barrier = threading.Barrier(2)
    local = threading.local()

    class RacingClient:
        def query(self, operation, payload):
            result = client.query(operation, payload)
            if not getattr(local, 'read', False):
                local.read = True
                barrier.wait(timeout=10)
            return result

        def command(self, *args):
            return client.command(*args)

    store = MCPDeclarationStore(RacingClient())
    def freeze(index):
        return store.freeze([definition(str(index))], user_id=1, conversation_id=conv)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(freeze, range(2)))
    assert results[0] == results[1]
    assert MCPDeclarationStore(client).get(user_id=1, conversation_id=conv) == results[0]
    assert client.query('conversation.get', {'conv_id': conv, 'user_id': 1,
        'derive_messages': False})['metadata']['settings']['unrelated'] == 'preserve'


def test_corruption_bounds_and_missing_fail_closed(application_storage):
    client = application_storage
    conv = create(client)
    store = MCPDeclarationStore(client)
    oversized = definition()
    oversized['function']['description'] = 'x' * MAX_DECLARATION_BYTES
    with pytest.raises(MCPDeclarationError, match='bytes'):
        store.freeze([oversized], user_id=1, conversation_id=conv)
    assert store.get(user_id=1, conversation_id=conv) is None
    with pytest.raises(MCPDeclarationError, match='missing'):
        store.freeze([], user_id=1, conversation_id='absent')
    client.command('conversation.settings.update', {'conv_id': conv, 'user_id': 1,
        'updates': {SETTING_KEY: {'version': 999}}}, uuid.uuid4().hex)
    with pytest.raises(MCPDeclarationError, match='Unsupported'):
        store.get(user_id=1, conversation_id=conv)


def test_contention_exhaustion_and_storage_failure_are_visible():
    class Client:
        def query(self, *args):
            return {'metadata': {'settings': {}}}
        def command(self, *args):
            return {'applied': False, 'conflict': True}
    with pytest.raises(MCPDeclarationError, match='contention'):
        MCPDeclarationStore(Client()).freeze([], user_id=1, conversation_id='c')
    class Broken(Client):
        def command(self, *args):
            raise OSError('storage offline')
    with pytest.raises(MCPDeclarationError, match='write failed') as caught:
        MCPDeclarationStore(Broken()).freeze([], user_id=1, conversation_id='c')
    assert isinstance(caught.value.__cause__, OSError)


def test_unrelated_settings_race_retries_without_clobber(application_storage):
    client = application_storage
    conv = create(client)

    class RacingSettings:
        raced = False
        def query(self, *args):
            return client.query(*args)
        def command(self, *args):
            if not self.raced:
                self.raced = True
                client.command('conversation.settings.update', {
                    'conv_id': conv, 'user_id': 1,
                    'updates': {'parallel_worker': 'retained'},
                }, uuid.uuid4().hex)
            return client.command(*args)

    store = MCPDeclarationStore(RacingSettings())
    store.freeze([definition()], user_id=1, conversation_id=conv)
    settings = client.query('conversation.get', {'conv_id': conv, 'user_id': 1,
        'derive_messages': False})['metadata']['settings']
    assert settings['parallel_worker'] == 'retained'
    assert settings['unrelated'] == 'preserve'
    # No write/history growth on ordinary repeated freezes or detached reads.
    first = store.get(user_id=1, conversation_id=conv)
    first[0]['function']['description'] = 'mutated caller copy'
    assert store.freeze([], user_id=1, conversation_id=conv) == [definition()]


def test_corrupt_slot_requires_explicit_reset(application_storage):
    client = application_storage
    conv = create(client, settings={SETTING_KEY: {'version': 999}})
    store = MCPDeclarationStore(client)
    with pytest.raises(MCPDeclarationError):
        store.freeze([], user_id=1, conversation_id=conv)
    store.reset(user_id=1, conversation_id=conv)
    assert store.get(user_id=1, conversation_id=conv) is None
    assert store.freeze([], user_id=1, conversation_id=conv) == []
