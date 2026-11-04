"""HTTP surface (/api/v4/channels/…): auth, scopes, owner scoping and wire contract."""
import asyncio
from types import SimpleNamespace

import jsonschema
import pytest

from lib.channels.model import CONTRACT
from lib.identity import PrincipalContext
from tests.channels.conftest import OWNER, OWNER_UID, ROUTER

pytestmark = pytest.mark.unit


def _validate(value, name):
    jsonschema.Draft202012Validator({'$ref': f'#/$defs/{name}', '$defs': CONTRACT['$defs']}).validate(value)


@pytest.fixture
def app(records, router, monkeypatch):
    monkeypatch.setattr('lib.channels.dx.supervisor.notify_account_changed', lambda owner: None)
    created = []
    monkeypatch.setattr('lib.channels.dx.adapter.create_command_group',
                        lambda owner, *, name, member_uid, mis: created.append((name, member_uid, mis)) or '4242')
    from quart import Quart, g, request
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from lib.http_transport.error_handlers import register_http_error_handlers
    from routes.api_v1.channels import api_v1_channels_bp
    application = Quart(__name__)
    register_http_error_handlers(application)
    application.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(api_v1_channels_bp,
                                                                          name='channels_v4')))

    @application.before_request
    def identity():
        owner = request.headers.get('X-Test-Owner')
        if owner:
            scopes = request.headers.get('X-Test-Scopes', 'conversations,admin').split(',')
            g.auth_ctx = SimpleNamespace(is_authenticated=True, owner_user_id=int(owner), key_id='test',
                                         name='test owner', tenant_id=None, scopes=scopes,
                                         has_scope=lambda scope: scope in scopes)
            g.principal_context = PrincipalContext.user(subject_id='owner', owner_user_id=int(owner))
    application.created_groups = created
    return application


def test_account_threads_and_outbox_round_trip(app, store):
    headers = {'X-Test-Owner': str(OWNER)}

    async def exercise():
        client = app.test_client()
        assert (await client.get('/api/v4/channels/dx')).status_code == 401
        assert (await client.get('/api/v4/channels/dx', headers={**headers, 'X-Test-Scopes': 'tasks'})).status_code == 403
        view = (await (await client.get('/api/v4/channels/dx', headers=headers)).get_json())['data']
        _validate(view, 'ChannelAccountView')
        assert view['enabled'] is False and view['status']['state'] == 'disabled'
        # Enabling needs the admin scope and answers at once; the supervisor
        # confirms the identity (whoami) of the login MIS afterwards.
        denied = await client.put('/api/v4/channels/dx', headers={**headers, 'X-Test-Scopes': 'conversations'},
                                  json={'enabled': True})
        assert denied.status_code == 403
        enabled = await client.put('/api/v4/channels/dx', headers=headers, json={'enabled': True, 'mis': 'owner'})
        view = (await enabled.get_json())['data']
        _validate(view, 'ChannelAccountView')
        assert view['identity'] is None and view['loginMis'] == 'owner' and view['routerConversationId'] == ROUTER
        assert view['status']['state'] == 'starting'
        assert (await client.put('/api/v4/channels/dx', headers=headers, json={'mis': 'not a mis'})).status_code == 400
        store.update_account('dx', lambda account: {
            **account, 'identity': {'uid': OWNER_UID, 'mis': 'owner', 'name': 'Owner'}})
        assert (await client.put('/api/v4/channels/dx', headers=headers, json={'bogus': 1})).status_code == 400
        assert (await client.put('/api/v4/channels/dx', headers=headers,
                                 json={'commandChat': 'dx:gid:x'})).status_code == 400
        created = await client.post('/api/v4/channels/dx/command-chat', headers=headers, json={})
        view = (await created.get_json())['data']
        assert view['commandChat'] == 'dx:gid:4242' and app.created_groups == [('Tofu Router', OWNER_UID, 'owner')]
        page = (await (await client.get('/api/v4/channels/dx/threads', headers=headers)).get_json())['data']
        _validate(page, 'ChannelThreadPage')
        assert [item['key'] for item in page['items']] == ['dx:gid:4242']
        assert page['items'][0]['isCommandChat'] and page['items'][0]['mode'] == 'command'
        updated = await client.put('/api/v4/channels/dx/threads/dx:gid:500', headers=headers,
                                   json={'mode': 'watch', 'replyPolicy': 'auto'})
        thread = (await updated.get_json())['data']
        _validate(thread, 'ChannelThreadView')
        assert (thread['mode'], thread['replyPolicy']) == ('watch', 'auto')
        assert (await client.put('/api/v4/channels/dx/threads/dx:gid:500', headers=headers,
                                 json={'mode': 'command'})).status_code == 400
        from lib.channels import outbox
        draft = outbox.enqueue(OWNER, 'dx:uid:2002', 'please confirm', idempotency_key='k',
                               origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 't'})
        listing = (await (await client.get('/api/v4/channels/outbox', headers=headers)).get_json())['data']
        _validate(listing, 'OutboxPage')
        assert [item['state'] for item in listing['items']] == ['draft']
        # Each channel's Settings tab lists only its own items, like its thread page.
        dx_only = await client.get('/api/v4/channels/outbox?channel=dx', headers=headers)
        assert [item['id'] for item in (await dx_only.get_json())['data']['items']] == [draft.id]
        github_only = await client.get('/api/v4/channels/outbox?channel=github', headers=headers)
        assert (await github_only.get_json())['data']['items'] == []
        assert (await client.get('/api/v4/channels/outbox?channel=mail', headers=headers)).status_code == 400
        # Another owner cannot see or decide this owner's items.
        other = {'X-Test-Owner': str(OWNER + 1)}
        assert (await (await client.get('/api/v4/channels/outbox', headers=other)).get_json())['data']['items'] == []
        assert (await client.post(f'/api/v4/channels/outbox/{draft.id}/approve', headers=other)).status_code == 404
        approved = await client.post(f'/api/v4/channels/outbox/{draft.id}/approve', headers=headers)
        item = (await approved.get_json())['data']
        _validate(item, 'OutboxItemView')
        assert item['state'] == 'queued'
        assert (await client.post(f'/api/v4/channels/outbox/{draft.id}/discard', headers=headers)).status_code == 200
        assert (await client.post('/api/v4/channels/outbox/missing/approve', headers=headers)).status_code == 404
    asyncio.run(exercise())
