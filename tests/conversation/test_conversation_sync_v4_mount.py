"""Conversation Sync serves natively on the HTTP v4 major.

The axis-C schema stays ``tofu.conversation-sync/v3``; the wire paths are
``/api/v4/...`` — registered directly by the sync blueprint itself (no
projection, no twin). The retired ``/api/v3`` prefix answers 426 from the
shared fence. Success and error bytes are the sync protocol's own, identical
to what legacy clients saw on v3.
"""

from __future__ import annotations

import uuid

import pytest

from lib.conversation_sync.generated_contract import OPENAPI_PATHS


pytestmark = [pytest.mark.api, pytest.mark.auth_mode("open")]
pytest_plugins = ("tests._chat_sidecar",)


def _sync_methods(path_item: dict) -> set[str]:
    return {
        method for method in path_item
        if method in {'get', 'post', 'put', 'patch', 'delete'}
    }


def _rule_shape(rule: str) -> str:
    """Normalize path converters so contract and Quart paths compare equal."""
    import re
    return re.sub(r'<[^>]+>', '<>', re.sub(r'\{[^}]+\}', '<>', rule))


@pytest.fixture()
def conversation_sync_db(chat_sidecar, monkeypatch):
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from lib.storage import get_storage_client
    from routes import conversation_sync_v3 as sync_routes

    client = get_storage_client(write=True)
    fixture_id = uuid.uuid4().hex
    monkeypatch.setattr(
        sync_routes,
        "_service",
        ConversationSyncService(SidecarConversationSyncRepository(
            client_factory=lambda write=False: client,
        )),
    )
    client.command(
        "conversation.create",
        {
            "conv_id": "conv-sync-v4",
            "user_id": 1,
            "title": "V4",
            "created_at": 1,
            "updated_at": 1,
            "settings": {},
        },
        f"create-sync-v4-fixture-{fixture_id}",
    )
    try:
        yield client
    finally:
        client.command(
            "conversation.purge",
            {"conv_id": "conv-sync-v4", "user_id": 1},
            f"cleanup-sync-v4-{fixture_id}",
        )


def test_every_contract_path_registers_natively_on_v4(flask_app):
    """Every declared /api/v4 sync rule is served; nothing mounts /api/v3."""
    registered = {}
    for rule in flask_app.url_map.iter_rules():
        registered.setdefault(_rule_shape(rule.rule), set()).update(
            rule.methods - {'HEAD', 'OPTIONS'})
    assert OPENAPI_PATHS, 'contract must declare sync paths'
    for path, path_item in OPENAPI_PATHS.items():
        assert path.startswith('/api/v4/')
        contract_methods = {method.upper() for method in _sync_methods(path_item)}
        assert contract_methods, f'{path} declares no HTTP method'
        shape = _rule_shape(path)
        assert shape in registered, f'{path} is not registered'
        assert contract_methods <= registered[shape], (
            f'{path} missing methods {contract_methods - registered[shape]}')
    v3_leftovers = [
        rule for rule in flask_app.url_map.iter_rules()
        if rule.rule.startswith('/api/v3')]
    assert v3_leftovers == []


def test_v4_snapshot_serves_raw_sync_document(flask_client, conversation_sync_db):
    """The sync protocol keeps its own document shape on the v4 surface."""
    response = flask_client.get("/api/v4/conversations/conv-sync-v4/sync")
    assert response.status_code == 200
    snapshot = response.get_json()
    assert snapshot["contract"] == "tofu.conversation-sync.snapshot/v1"
    assert snapshot["conversationId"] == "conv-sync-v4"
    assert isinstance(snapshot["cursor"], str) and snapshot["cursor"]
    assert "turns" in snapshot and "attempts" in snapshot


def test_v4_sync_error_keeps_protocol_shape(flask_client, conversation_sync_db):
    """A sync 4xx on /api/v4 uses the uniform error envelope, never a
    translated platform shape (``violations`` and friends stay top-level)."""
    invalid = flask_client.post(
        "/api/v4/conversations/conv-sync-v4/turns",
        json={"commandId": "missing-config"},
    )
    assert invalid.status_code == 400
    body = invalid.get_json()
    assert body["ok"] is False
    assert "CreateTurnRequest contract violation" in str(body["error"])
    assert body["violations"]


def test_retired_v3_sync_prefix_answers_426():
    # This retirement contract must not inherit the legacy API major used by
    # unrelated fixtures in the shared test application.
    import asyncio
    from quart import Quart
    from lib.api_v4 import configure_api_version_policy, legacy_api_upgrade_before_request
    app = Quart(__name__)
    app.config['ACTIVE_API_MAJOR'] = 4
    configure_api_version_policy(app)
    app.before_request(legacy_api_upgrade_before_request)

    async def check():
        response = await app.test_client().get('/api/v3/conversations/retired/sync')
        assert response.status_code == 426
        assert response.content_type == 'application/problem+json'
        body = await response.get_json()
        assert body['status'] == 426
        assert body['code'] == 'api_version_upgrade_required'
        assert body['upgradeUrl'] == '/api/v4/meta'
    asyncio.run(check())


def test_native_error_contract_uses_matched_blueprint_and_preserves_receipts():
    import asyncio
    from quart import Blueprint, Quart
    from lib.api_response import api_error
    from lib.api_v4 import NativeContractBlueprint
    app = Quart(__name__)
    native = NativeContractBlueprint('native_sync', __name__, contract_id='tofu.conversation-sync/v3')
    ordinary = Blueprint('ordinary', __name__)
    receipt = {'turnId': 'committed-turn', 'projectionRevision': 3}

    @native.get('/api/v4/conversations/native')
    def native_error():
        return api_error({'kind': 'server_busy', 'message': 'Retry later'}, status=503,
                         storageCode='database_timeout', retryAfterMs=125,
                         latestTurn=receipt)

    @ordinary.get('/api/v4/conversations/ordinary')
    def ordinary_error():
        return api_error('Denied', status=403)

    app.register_blueprint(native)
    app.register_blueprint(ordinary)

    async def check():
        response = await app.test_client().get('/api/v4/conversations/native')
        assert response.status_code == 503
        body = await response.get_json()
        assert body['storageCode'] == 'database_timeout'
        assert body['retryAfterMs'] == 125
        assert body['latestTurn'] == receipt
        assert body['error']['kind'] == 'server_busy'
        response = await app.test_client().get('/api/v4/conversations/ordinary')
        assert response.status_code == 403
        assert response.content_type == 'application/problem+json'
        assert (await response.get_json())['code'] == 'permission_denied'
    asyncio.run(check())


def test_native_contract_cannot_be_accidentally_double_wrapped():
    from lib.api_v4 import NativeContractBlueprint, attach_v4_envelope
    blueprint = NativeContractBlueprint('sync', __name__, contract_id='tofu.conversation-sync/v3')
    with pytest.raises(ValueError, match='native response contract'):
        attach_v4_envelope(blueprint)
