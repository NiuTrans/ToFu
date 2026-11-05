"""GitHub account (C8): token custody, verification, supervisor policy, HTTP surface."""
import asyncio
import logging
from types import SimpleNamespace

import jsonschema
import pytest

from lib.channels.github import account as github_account
from lib.channels.model import CONTRACT, ChannelError, ThreadKey
from tests.channels.conftest import GH_NOW, GH_TOKEN, GH_USER, OWNER, ROUTER, github_adapter

pytestmark = pytest.mark.unit

ENTRY = f'channel.u{OWNER}.github.token'


def _validate(value, name):
    jsonschema.Draft202012Validator({'$ref': f'#/$defs/{name}', '$defs': CONTRACT['$defs']}).validate(value)


@pytest.fixture(autouse=True)
def quiet_supervisor(monkeypatch):
    calls = []
    monkeypatch.setattr('lib.channels.github.supervisor.notify_account_changed', calls.append)
    return calls


@pytest.fixture
def prober(github):
    """``probe`` against the fake server (the real client code, no network)."""
    def run(api_base, token):
        return github_account.probe(api_base, token, transport=github)
    return run


def test_pasted_token_is_verified_then_kept_only_in_the_reserved_vault_entry(
        store, router, vault, github, prober, quiet_supervisor, caplog):
    caplog.set_level(logging.DEBUG)
    view = github_account.update_account(OWNER, {'token': GH_TOKEN, 'enabled': True}, store=store,
                                         prober=prober)
    _validate(view, 'GitHubAccountView')
    assert view['enabled'] and view['status']['state'] == 'starting'
    assert view['identity'] == {'uid': str(GH_USER['id']), 'mis': GH_USER['login'], 'name': GH_USER['name']}
    assert view['credential'] == {'name': ENTRY, 'hint': 'ghp_…0001', 'managed': True}
    assert view['poll']['intervalS'] == 60 and view['routerConversationId'] == ROUTER
    assert [call['path'] for call in github.requests] == ['/user', '/notifications']
    assert vault.get_entry(ENTRY) == GH_TOKEN and quiet_supervisor == [OWNER]
    # The value exists nowhere but the vault: not in views, channel storage or logs.
    assert GH_TOKEN not in str(view) and GH_TOKEN not in str(store.account('github'))
    assert GH_TOKEN not in caplog.text
    # Agents cannot discover or request channel tokens.
    assert ENTRY not in vault.build_vault_index()
    with pytest.raises(ValueError):
        vault.resolve_run_command_env([ENTRY])
    assert ENTRY not in vault.exec_env_overlay() and GH_TOKEN not in vault.exec_env_overlay().values()


def test_rejected_or_unsuitable_tokens_are_never_stored(store, router, vault, github, prober):
    with pytest.raises(ChannelError) as rejected:
        github_account.update_account(OWNER, {'token': 'ghp_wrong0000000000000000000000000000'},
                                      store=store, prober=prober)
    assert rejected.value.code == 'auth_failed' and vault.get_entry(ENTRY) is None
    github.responders.append(lambda record: github.reply(
        403, {'message': 'Resource not accessible by personal access token'}, record['url'])
        if record['path'] == '/notifications' else None)
    with pytest.raises(ChannelError) as unsupported:
        github_account.update_account(OWNER, {'token': GH_TOKEN, 'enabled': True}, store=store, prober=prober)
    assert unsupported.value.code == 'token_unsupported' and vault.get_entry(ENTRY) is None
    assert not (store.account('github') or {}).get('enabled')


def test_an_existing_vault_entry_can_be_chosen_by_name(store, router, vault, github, prober):
    vault.set_entry('github_token', GH_TOKEN)
    view = github_account.update_account(OWNER, {'credentialName': 'github_token', 'enabled': True},
                                         store=store, prober=prober)
    assert view['credential'] == {'name': 'github_token', 'hint': 'ghp_…0001', 'managed': False}
    for reserved in ('skill.u7.demo.token', f'channel.u{OWNER + 1}.github.token'):
        vault.set_entry(reserved, 'x' * 40)
        with pytest.raises(ChannelError):
            github_account.update_account(OWNER, {'credentialName': reserved}, store=store, prober=prober)
    with pytest.raises(ChannelError) as missing:
        github_account.update_account(OWNER, {'credentialName': 'nope'}, store=store, prober=prober)
    assert missing.value.code == 'credential_missing'
    # Switching away from a pasted token deletes the managed entry; removing disables.
    github_account.update_account(OWNER, {'token': GH_TOKEN}, store=store, prober=prober)
    assert vault.get_entry(ENTRY) == GH_TOKEN
    view = github_account.update_account(OWNER, {'credentialName': None}, store=store, prober=prober)
    assert view['credential'] is None and not view['enabled'] and vault.get_entry(ENTRY) is None
    with pytest.raises(ChannelError):
        github_account.update_account(OWNER, {'enabled': True}, store=store, prober=prober)


def test_enterprise_api_base_is_validated_and_changes_only_while_disabled(store, router, vault, github, prober):
    view = github_account.update_account(OWNER, {'apiBaseUrl': 'https://ghe.example.com/api/v3'},
                                         store=store, prober=prober)
    assert (view['apiBaseUrl'], view['webBaseUrl']) == ('https://ghe.example.com/api/v3', 'https://ghe.example.com')
    for bad in ('http://ghe.example.com/api/v3', 'https://user:pw@ghe.example.com', 'https://ghe.example.com/?x=1'):
        with pytest.raises(ChannelError):
            github_account.update_account(OWNER, {'apiBaseUrl': bad}, store=store, prober=prober)
    view = github_account.update_account(OWNER, {'apiBaseUrl': None}, store=store, prober=prober)
    assert view['apiBaseUrl'] == 'https://api.github.com' and view['webBaseUrl'] == 'https://github.com'
    github_account.update_account(OWNER, {'token': GH_TOKEN, 'enabled': True}, store=store, prober=prober)
    with pytest.raises(ChannelError):
        github_account.update_account(OWNER, {'apiBaseUrl': 'https://ghe.example.com/api/v3'},
                                      store=store, prober=prober)


def test_the_token_never_reaches_other_origins_or_error_text(github):
    from lib.channels.github.client import GitHubClient, GitHubHTTPError
    client = GitHubClient('https://api.github.com', lambda: GH_TOKEN, transport=github)
    with pytest.raises(GitHubHTTPError):
        client.get('https://evil.example.com/repos/x')
    github.responders.append(lambda record: github.reply(
        301, None, record['url'], {'location': 'https://evil.example.com/steal'})
        if record['path'] == '/user' else None)
    with pytest.raises(GitHubHTTPError) as error:
        client.get('/user')
    assert GH_TOKEN not in str(error.value)
    assert all('evil.example.com' not in record['url'] for record in github.requests)


def test_post_redirects_repeat_the_post_on_the_same_origin(store, router, github_enabled, github):
    from lib.channels import outbox, service
    github_enabled()
    github.issue('octo/renamed', 12, title='t', created=GH_NOW - 10)
    service.configure_thread(OWNER, 'github:octo/repo#12', reply_policy='auto', store=store)
    github.responders.append(lambda record: github.reply(
        307, None, record['url'], {'location': 'https://api.github.com/repos/octo/renamed/issues/12/comments'})
        if record['method'] == 'POST' and '/octo/repo/' in record['path'] else None)
    item = outbox.enqueue(OWNER, 'github:octo/repo#12', 'hi', idempotency_key='r',
                          origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 't'}, store=store)
    github_adapter(store, github)._outbox_pass()
    assert store.outbox_item(item.id)[0].state == 'sent'
    assert [call['method'] for call in github.requests if 'comments' in call['path']] == ['POST', 'POST']


# ── supervisor ────────────────────────────────────────────────────────

class FakeSession:
    instances = []

    def __init__(self, owner, *, store=None):
        from lib.channels.github.adapter import account_binding
        self.owner, self.running, self.ticks = owner, False, 0
        self.binding = account_binding(store.account('github'))
        FakeSession.instances.append(self)

    def start(self):
        self.running = True

    def alive(self):
        return self.running

    def tick(self):
        self.ticks += 1

    def stop(self, timeout):
        self.running = False
        return True


@pytest.fixture
def supervisor(monkeypatch):
    from lib.channels.github import supervisor as module
    FakeSession.instances = []
    monkeypatch.setattr(module, '_ADAPTERS', {})
    return module


def test_supervisor_starts_restarts_rebinds_and_stops(store, router, github_enabled, supervisor):
    run = lambda: supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeSession)  # noqa: E731
    assert run() == 'idle'
    github_enabled()
    assert run() == 'started' and run() == 'running' and FakeSession.instances[0].ticks == 1
    FakeSession.instances[0].running = False  # session ended (network error)
    assert run() == 'started' and len(FakeSession.instances) == 2
    store.update_account('github', lambda account: {**account, 'identity': {'uid': '42', 'mis': 'x', 'name': ''}})
    assert run() == 'started' and not FakeSession.instances[1].running  # new identity → new session
    store.update_account('github', lambda account: {**account, 'enabled': False})
    assert run() == 'stopped' and not FakeSession.instances[2].running


def test_needs_login_stops_restarts_until_the_owner_fixes_the_token(
        store, router, vault, github, github_enabled, supervisor, prober):
    github_enabled()
    supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeSession)
    FakeSession.instances[0].running = False
    store.set_status('github', 'needs_login', 'GitHub rejected the token (401)')
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeSession) == 'needs_login'
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeSession) == 'needs_login'
    github_account.update_account(OWNER, {'token': GH_TOKEN}, store=store, prober=prober)
    assert store.account('github')['status']['state'] == 'starting'
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeSession) == 'started'
    vault.delete_entry(ENTRY)
    FakeSession.instances[-1].running = False
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeSession) == 'needs_login'


def test_enabling_again_or_replacing_the_token_keeps_the_live_session_status(
        store, router, vault, github, supervisor, prober):
    github_account.update_account(OWNER, {'token': GH_TOKEN, 'enabled': True}, store=store, prober=prober)
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeSession) == 'started'
    store.set_status('github', 'connected', '', connected_at_ms=5)
    for update in ({'enabled': True}, {'token': GH_TOKEN}):
        view = github_account.update_account(OWNER, update, store=store, prober=prober)
        assert view['status']['state'] == 'connected', update
        assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeSession) == 'running'
    assert len(FakeSession.instances) == 1


# ── HTTP ──────────────────────────────────────────────────────────────

@pytest.fixture
def app(records, router, vault, github, monkeypatch):
    monkeypatch.setattr('lib.channels.dx.supervisor.notify_account_changed', lambda owner: None)
    # The real probe and client, answered by the fake server.
    monkeypatch.setattr('lib.channels.github.account.probe',
                        lambda api_base, token: _ORIGINAL_PROBE(api_base, token, transport=github))
    from quart import Quart, g, request
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from lib.http_transport.error_handlers import register_http_error_handlers
    from lib.identity import PrincipalContext
    from routes.api_v1.channels import api_v1_channels_bp
    application = Quart(__name__)
    register_http_error_handlers(application)
    application.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(api_v1_channels_bp,
                                                                          name='channels_v4_github')))

    @application.before_request
    def identity():
        owner = request.headers.get('X-Test-Owner')
        if owner:
            scopes = request.headers.get('X-Test-Scopes', 'conversations,admin').split(',')
            g.auth_ctx = SimpleNamespace(is_authenticated=True, owner_user_id=int(owner), key_id='test',
                                         name='test owner', tenant_id=None, scopes=scopes,
                                         has_scope=lambda scope: scope in scopes)
            g.principal_context = PrincipalContext.user(subject_id='owner', owner_user_id=int(owner))
    return application


_ORIGINAL_PROBE = github_account.probe


def test_http_surface_round_trip_and_owner_scoping(app, store, github):
    headers = {'X-Test-Owner': str(OWNER)}

    async def exercise():
        client = app.test_client()
        view = (await (await client.get('/api/v4/channels/github', headers=headers)).get_json())['data']
        _validate(view, 'GitHubAccountView')
        assert view['enabled'] is False and view['credential'] is None
        denied = await client.put('/api/v4/channels/github', headers={**headers, 'X-Test-Scopes': 'conversations'},
                                  json={'token': GH_TOKEN})
        assert denied.status_code == 403
        bad = await client.put('/api/v4/channels/github', headers=headers, json={'token': 'ghp_bad000000000000000000000000000000'})
        assert bad.status_code == 400 and (await bad.get_json())['detail'] == 'GitHub rejected the token'
        enabled = await client.put('/api/v4/channels/github', headers=headers, json={'token': GH_TOKEN, 'enabled': True})
        text = await enabled.get_data(as_text=True)
        assert enabled.status_code == 200 and GH_TOKEN not in text
        _validate((await enabled.get_json())['data'], 'GitHubAccountView')
        assert (await client.put('/api/v4/channels/github', headers=headers, json={'bogus': 1})).status_code == 400
        key = 'github:octo/repo#12'
        from urllib.parse import quote
        updated = await client.put(f'/api/v4/channels/github/threads/{quote(key, safe="")}', headers=headers,
                                   json={'mode': 'watch', 'replyPolicy': 'auto'})
        thread = (await updated.get_json())['data']
        _validate(thread, 'ChannelThreadView')
        assert (thread['key'], thread['kind'], thread['mode'], thread['replyPolicy']) == (key, 'issue', 'watch', 'auto')
        assert (await client.put(f'/api/v4/channels/github/threads/{quote("dx:gid:5", safe="")}',
                                 headers=headers, json={'mode': 'watch'})).status_code == 400
        store.update_thread(ThreadKey.parse('dx:gid:500'), lambda value: {**value, 'title': 'Team'})
        page = (await (await client.get('/api/v4/channels/github/threads', headers=headers)).get_json())['data']
        _validate(page, 'ChannelThreadPage')
        assert [item['key'] for item in page['items']] == [key]
        dx_page = (await (await client.get('/api/v4/channels/dx/threads', headers=headers)).get_json())['data']
        assert [item['key'] for item in dx_page['items']] == ['dx:gid:500']
        other = {'X-Test-Owner': str(OWNER + 1)}
        other_view = (await (await client.get('/api/v4/channels/github', headers=other)).get_json())['data']
        assert other_view['enabled'] is False and other_view['identity'] is None
        assert (await (await client.get('/api/v4/channels/github/threads', headers=other)).get_json())['data']['items'] == []
    asyncio.run(exercise())


def test_credentials_routes_do_not_expose_channel_entries(vault):
    from routes.api_v1.credentials import _managed_name
    assert _managed_name(ENTRY) and _managed_name(' Channel.U7.GitHub.Token ')
    assert not _managed_name('github_token')


def test_github_has_no_command_chat_and_its_threads_reach_router_tools(store, router, github_enabled):
    from lib.channels import service, tools
    with pytest.raises(ChannelError):
        service.update_account(OWNER, {'commandChat': 'github:octo/repo#12'}, store=store)
    assert tools.visible_tools(OWNER, ROUTER) == []  # no channel account yet
    github_enabled()
    assert {tool['function']['name'] for tool in tools.visible_tools(OWNER, ROUTER)} == tools.CHANNEL_TOOL_NAMES
    service.configure_thread(OWNER, 'GitHub:Octo/Repo#12', mode='watch', store=store)
    service.configure_thread(OWNER, 'dx:gid:500', mode='watch', store=store)
    page = service.thread_page(OWNER, channel='github', store=store)
    assert [item['key'] for item in page['items']] == ['github:octo/repo#12']
    assert {item['key'] for item in service.thread_page(OWNER, store=store)['items']} == {
        'github:octo/repo#12', 'dx:gid:500'}
