"""Public settings cannot expose or roll back the durable MCP declaration slot."""
from __future__ import annotations

import asyncio
import copy
import inspect
import time
import uuid

import pytest
from quart import Quart

from lib.storage.mcp_declarations import MCPDeclarationStore, SETTING_KEY
from tests.storage.test_storage_application_contract import application_storage as application_storage
from tests.storage.test_storage_application_contract import application_storage_owner_id as application_storage_owner_id

pytestmark = pytest.mark.api


def _settings(client, conv_id):
    return client.query('conversation.get', {
        'conv_id': conv_id, 'user_id': 1, 'derive_messages': False,
    })['metadata']['settings']


def _seed(client):
    conv_id = f'private-settings-{uuid.uuid4().hex}'
    client.command('conversation.create', {
        'conv_id': conv_id, 'user_id': 1, 'title': 'Private settings',
        'settings': {'serverOwned': 'preserve', 'pinned': False},
        'created_at': 10, 'updated_at': 10,
    }, f'create:{conv_id}')
    return conv_id


def _freeze(store, conv_id, schema):
    declarations = [{'type': 'function', 'function': {
        'name': 'mcp__fixture__lookup', 'description': schema,
        'parameters': {'type': 'object', 'properties': {}},
    }}]
    return store.freeze(declarations, user_id=1, conversation_id=conv_id)


def _app(monkeypatch):
    """Exercise real HTTP parsing/handlers with an isolated authenticated owner."""
    import routes.conversations as routes
    import routes.chat_tool_state as tool_state
    from routes.api_v1.conversations import (
        resolve_config_route, resolve_settings_route,
    )

    monkeypatch.setattr(routes, '_owner_id', lambda: 1)
    monkeypatch.setattr(routes, '_notify_conv_changed', lambda *a, **k: None)
    monkeypatch.setattr(tool_state, 'request_user_id', lambda: 1)
    app = Quart(__name__)
    app.config['TESTING'] = True
    for path, view, method in (
        ('/conversations/<conv_id>/settings', routes.patch_conv_settings, 'PATCH'),
        ('/conversations/<conv_id>/settings/resolve', routes.patch_resolved_conv_settings, 'PATCH'),
        ('/tool-state/<conv_id>', tool_state.chat_tool_state, 'PATCH'),
        ('/resolve', resolve_settings_route, 'POST'),
        ('/config', resolve_config_route, 'POST'),
    ):
        app.add_url_rule(path, view_func=inspect.unwrap(view), methods=[method])
    return app


def _request(app, method, path, body):
    async def send():
        response = await app.test_client().open(path, method=method, json=body)
        return response.status_code, await response.get_json()
    return asyncio.run(send())


def _assert_public(client, conv_id, expected):
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from routes.conversations import _metadata

    # The storage application's native adapter and SQL producer both redact.
    stored_snapshot = client.query('turn.sync.snapshot', {
        'conversation_id': conv_id, 'user_id': 1,
    })
    assert SETTING_KEY not in stored_snapshot['settings']
    service = ConversationSyncService(SidecarConversationSyncRepository(
        client_factory=lambda **kwargs: client))
    for representation in ('full', 'refs'):
        snapshot = service.snapshot(conv_id, 1, segment_payload=representation)
        assert SETTING_KEY not in snapshot['settings']
    document = client.query('conversation.get', {
        'conv_id': conv_id, 'user_id': 1, 'derive_messages': False,
    })
    for sidebar in (False, True):
        assert SETTING_KEY not in _metadata(document, sidebar=sidebar)['settings']
    # Trusted reads are deliberately NOT public projections: the CAS owner
    # must still see the current slot, without a second database or cache.
    assert MCPDeclarationStore(client).get(user_id=1, conversation_id=conv_id) == expected
    assert SETTING_KEY in document['metadata']['settings']


@pytest.mark.parametrize('surface', ['patch', 'tool-state', 'resolve'])
def test_stale_a_reset_b_then_public_patch_preserves_b(
        application_storage, monkeypatch, surface):
    client = application_storage
    app = _app(monkeypatch)
    conv_id = _seed(client)
    store = MCPDeclarationStore(client)
    frozen_a = _freeze(store, conv_id, 'schema A')
    stale_a = copy.deepcopy(_settings(client, conv_id)[SETTING_KEY])
    store.reset(user_id=1, conversation_id=conv_id)
    frozen_b = _freeze(store, conv_id, 'schema B')
    assert frozen_b != frozen_a

    # Both a formerly valid slot and values that could poison store decoding
    # must be ignored. Ordinary unknown/public settings continue to merge.
    for slot in (stale_a, None, 'malformed', {'generation': -1}, []):
        updates = {SETTING_KEY: slot, 'pinned': True, 'folderId': 'folder-new'}
        if surface == 'tool-state':
            path, body = f'/tool-state/{conv_id}', updates
        elif surface == 'resolve':
            path = f'/conversations/{conv_id}/settings/resolve'
            body = {'conv_settings': updates, 'overrides': {SETTING_KEY: slot}}
        else:
            path, body = f'/conversations/{conv_id}/settings', updates
        status, result = _request(app, 'PATCH', path, body)
        assert status == 200, result
        assert store.get(user_id=1, conversation_id=conv_id) == frozen_b
        persisted = _settings(client, conv_id)
        assert persisted['serverOwned'] == 'preserve'
        assert persisted['folderId'] == 'folder-new'
        if surface != 'resolve':
            assert persisted['pinned'] is True
    if surface != 'resolve':
        # Even a protected-key-only request is ignored, never a replacement
        # with an empty settings object or an opportunity to restore A.
        status, result = _request(app, 'PATCH', path, {SETTING_KEY: stale_a})
        assert status == 200, result
        assert store.get(user_id=1, conversation_id=conv_id) == frozen_b
        assert _settings(client, conv_id)['pinned'] is True
    _assert_public(client, conv_id, frozen_b)


def test_reset_committing_at_public_patch_merge_is_not_restored_from_stale_read(
        application_storage, monkeypatch):
    import routes.conversations as routes

    client = application_storage
    app = _app(monkeypatch)
    conv_id = _seed(client)
    store = MCPDeclarationStore(client)
    frozen_a = _freeze(store, conv_id, 'schema A')
    stale_settings = copy.deepcopy(_settings(client, conv_id))
    original_command = routes._command
    committed = []

    async def reset_before_merge(operation, identity, payload):
        # Deterministic competing writer: reset and freeze commit after the
        # public handler built its payload but before its atomic storage merge.
        assert SETTING_KEY not in payload['updates']
        assert 'replace' not in payload and 'expected_settings' not in payload
        store.reset(user_id=1, conversation_id=conv_id)
        committed.append(_freeze(store, conv_id, 'schema B'))
        return await original_command(operation, identity, payload)

    monkeypatch.setattr(routes, '_command', reset_before_merge)
    status, result = _request(app, 'PATCH', f'/conversations/{conv_id}/settings', {
        **stale_settings, 'pinned': True,
    })
    assert status == 200, result
    assert committed[0] != frozen_a
    assert store.get(user_id=1, conversation_id=conv_id) == committed[0]
    assert _settings(client, conv_id)['pinned'] is True
    _assert_public(client, conv_id, committed[0])


def test_public_create_append_and_busy_queue_ignore_private_settings(application_storage):
    from lib.conversation_sync.command_service import ConversationTurnCommandService

    client = application_storage

    def start_task(conv_id, config, request_data, abort_after, on_registered):
        on_registered('private-settings-task')
        return 'private-settings-task', None

    service = ConversationTurnCommandService(
        build_user_message=lambda message, *args: {'content': message['text']},
        was_aborted_after=lambda *args: False,
        start_task=start_task,
    )
    conv_id = f'private-create-{uuid.uuid4().hex}'
    body = {
        'commandId': 'create', 'message': {'text': 'hello'},
        'config': {}, 'conversation': {
            'allowCreate': True, 'settings': {SETTING_KEY: 'malformed', 'pinned': True},
        },
    }
    original = copy.deepcopy(body)
    service.create_turn(conv_id, 1, body, request_started_at=time.time())
    assert body == original
    assert SETTING_KEY not in _settings(client, conv_id)
    assert _settings(client, conv_id)['pinned'] is True
    store = MCPDeclarationStore(client)
    frozen = _freeze(store, conv_id, 'server schema')
    body['commandId'] = 'queue'
    body['injectMode'] = 'queue'
    queued = service.create_turn(conv_id, 1, body, request_started_at=time.time())
    assert queued.value['queued'] is True
    assert store.get(user_id=1, conversation_id=conv_id) == frozen

    # Settled append is a separate public create/defaults path.
    append_id = f'private-append-{uuid.uuid4().hex}'
    service.append_settled_turn(append_id, 1, {
        'commandId': 'append', 'actor': 'human', 'projection': {'content': 'import'},
        'conversation': body['conversation'],
    })
    assert SETTING_KEY not in _settings(client, append_id)
    assert _settings(client, append_id)['pinned'] is True
    _assert_public(client, conv_id, frozen)


def test_resolver_projections_ignore_private_slot(application_storage, monkeypatch):
    app = _app(monkeypatch)
    private = {SETTING_KEY: {'generation': 'malformed'}, 'model': 'gpt-4o'}
    for path, body in (
        ('/resolve', {'conv_settings': private, 'overrides': private}),
        ('/config', {'conv_settings': private, 'settings_conv_settings': private,
                     'overrides': private, 'include_settings': True}),
    ):
        status, result = _request(app, 'POST', path, body)
        assert status == 200, result
        assert SETTING_KEY not in result
        assert SETTING_KEY not in result.get('settings', {})
