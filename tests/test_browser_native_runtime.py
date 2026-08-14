"""Contracts for the native BrowserPage, policy, leases, and adapters."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_browser_runtime(tmp_path, monkeypatch):
    from lib.browser import access, adapters, sessions
    from lib.browser.queue import _state

    monkeypatch.setattr(access, '_STORE_PATH', str(tmp_path / 'browser_access.json'))
    monkeypatch.setattr(access, 'audit_log', mock.Mock())
    monkeypatch.setattr(adapters, 'audit_log', mock.Mock())
    with _state._clients_lock:
        _state._clients.clear()
    with sessions._leases_lock:
        for timer in sessions._lease_timers.values():
            timer.cancel()
        sessions._lease_timers.clear()
        sessions._leases.clear()
    yield
    with _state._clients_lock:
        _state._clients.clear()
    with sessions._leases_lock:
        for timer in sessions._lease_timers.values():
            timer.cancel()
        sessions._lease_timers.clear()
        sessions._leases.clear()


def test_capability_negotiation_preserves_legacy_but_gates_new_adapters():
    from lib.browser import (
        ALL_CAPABILITIES, BrowserCapability, BrowserUpgradeRequired, mark_poll,
    )
    from lib.browser.adapters import adapter_health, get_adapter
    from lib.browser.protocol import client_protocol, require_capabilities

    mark_poll('legacy', user_id='u1')
    assert BrowserCapability.READ.value in client_protocol('legacy')['capabilities']
    with pytest.raises(BrowserUpgradeRequired) as exc:
        require_capabilities('legacy', [BrowserCapability.SNAPSHOT])
    assert exc.value.missing == ('snapshot',)
    assert adapter_health(get_adapter('xiaohongshu'), client_id='legacy')['status'] \
        == 'upgrade_required'

    mark_poll('modern', user_id='u1', protocol_version=2,
              capabilities=sorted(ALL_CAPABILITIES), profile='Work')
    # The queue records one long poll twice; an omitted handshake must not
    # silently downgrade the already-negotiated client.
    mark_poll('modern', user_id='u1')
    info = require_capabilities('modern', ['snapshot'])
    assert info['protocol_version'] == 2
    assert info['profile'] == 'Work'
    assert adapter_health(get_adapter('modelplaza'), client_id='modern')['healthy'] is True


def test_browser_access_defaults_readable_and_isolates_denials_and_grants():
    from lib.browser.access import (
        BrowserAccessDenied,
        BrowserWriteAuthorizationRequired,
        grant_write,
        has_write_grant,
        require_access,
        replace_read_denials,
        revoke_write,
    )

    assert require_access('alice', 'https://docs.example.com/page') == 'docs.example.com'
    replace_read_denials('alice', ['example.com'])
    with pytest.raises(BrowserAccessDenied):
        require_access('alice', 'https://docs.example.com/page')
    # Policies are per-user; Alice's denial cannot leak into Bob's browser.
    assert require_access('bob', 'https://docs.example.com/page') == 'docs.example.com'

    with pytest.raises(BrowserWriteAuthorizationRequired):
        require_access('bob', 'https://shop.example.net/cart', access='write',
                       client_id='c1', profile='Work')
    grant_write('bob', 'shop.example.net', client_id='c1', profile='Work')
    assert has_write_grant('bob', 'shop.example.net', client_id='c1', profile='Work')
    # Grants are exact-domain and exact browser identity: redirects and a
    # second profile cannot inherit the authorization.
    assert not has_write_grant('bob', 'pay.example.net', client_id='c1', profile='Work')
    assert not has_write_grant('bob', 'shop.example.net', client_id='c1', profile='Personal')
    revoke_write('bob', 'shop.example.net', client_id='c1', profile='Work')
    assert not has_write_grant('bob', 'shop.example.net', client_id='c1', profile='Work')


def test_page_write_requires_one_domain_grant_and_read_adapter_can_click():
    from lib.browser import ALL_CAPABILITIES, BrowserPage, mark_poll
    from lib.browser.access import BrowserWriteAuthorizationRequired, grant_write
    from lib.browser.sessions import acquire_browser_lease

    mark_poll('c1', user_id='alice', protocol_version=2,
              capabilities=sorted(ALL_CAPABILITIES), profile='Work')
    calls = []

    def sender(command, params, *, timeout, client_id):
        calls.append((command, params, client_id))
        if command == 'page_state':
            return {'tabId': 7, 'url': 'https://app.example.com/form'}, None
        return {'done': True}, None

    lease = acquire_browser_lease(user_id='alice', client_id='c1',
                                  session='persistent', tab_id=7)
    page = BrowserPage(lease, sender=sender)
    page._url = 'https://app.example.com/form'
    with pytest.raises(BrowserWriteAuthorizationRequired):
        page.click(selector='#submit')
    assert [row[0] for row in calls] == ['page_state']

    grant_write('alice', 'app.example.com', client_id='c1', profile='Work')
    assert page.click(selector='#submit')['ok'] is True
    click = next(row for row in calls if row[0] == 'page_click')
    assert click[1]['expectedDomain'] == 'app.example.com'
    # A trusted read adapter may paginate without turning every internal
    # click into a separate write authorization.
    assert page.click(selector='.next', trusted_read=True)['ok'] is True


def test_page_redirect_cannot_inherit_previous_domains_write_grant():
    from lib.browser import ALL_CAPABILITIES, BrowserPage, mark_poll
    from lib.browser.access import BrowserWriteAuthorizationRequired, grant_write
    from lib.browser.sessions import acquire_browser_lease

    mark_poll('c1', user_id='alice', protocol_version=2,
              capabilities=sorted(ALL_CAPABILITIES), profile='Work')
    grant_write('alice', 'shop.example.com', client_id='c1', profile='Work')
    calls = []

    def sender(command, params, *, timeout, client_id):
        calls.append(command)
        if command == 'page_state':
            return {'tabId': 7, 'url': 'https://pay.example.net/confirm'}, None
        return {'done': True}, None

    lease = acquire_browser_lease(user_id='alice', client_id='c1',
                                  session='persistent', tab_id=7)
    page = BrowserPage(lease, sender=sender)
    page._url = 'https://shop.example.com/cart'
    with pytest.raises(BrowserWriteAuthorizationRequired):
        page.click(selector='#confirm')
    assert calls == ['page_state']


@pytest.mark.parametrize('session,should_close', [
    ('ephemeral', True),
    ('persistent', False),
])
def test_lease_release_always_stops_capture_and_only_closes_ephemeral_tab(
        session, should_close):
    from lib.browser import ALL_CAPABILITIES, mark_poll
    from lib.browser.sessions import acquire_browser_lease, release_browser_lease

    mark_poll('c1', user_id='alice', protocol_version=2,
              capabilities=sorted(ALL_CAPABILITIES))
    lease = acquire_browser_lease(user_id='alice', client_id='c1',
                                  session=session, tab_id=9)
    lease.network_captures.add('capture-1')
    commands = []

    def sender(command, params, *, timeout, client_id):
        commands.append((command, params, client_id))
        return {'ok': True}, None

    release_browser_lease(lease, reason='cancelled', sender=sender)
    assert commands[0][0] == 'network_capture_stop'
    assert ('close_tab' in [row[0] for row in commands]) is should_close
    assert lease.active is False
    release_browser_lease(lease, sender=sender)
    assert len(commands) == (2 if should_close else 1), 'release must be idempotent'


def test_adapter_schema_validation_and_audit_redaction():
    from lib.browser.access import summarize_parameters
    from lib.browser.adapters import AdapterCommand, AdapterValidationError

    with pytest.raises(AdapterValidationError):
        AdapterCommand('publish', 'bad manifest', access='mutate')
    summary = summarize_parameters({
        'query': 'q' * 200,
        'password': 'never log this',
        'cookies': ['secret'],
        'body': {'full': 'page'},
    })
    assert summary['password'] == '[redacted]'
    assert summary['cookies'] == '[redacted]'
    assert summary['query'].endswith('…') and len(summary['query']) == 161
    assert summary['body'] == '[redacted]'


def test_adapter_invalid_output_fails_with_structured_error():
    from lib.browser import ALL_CAPABILITIES, mark_poll
    from lib.browser.adapters import (
        AdapterCommand,
        AdapterExecutionError,
        SiteAdapter,
        invoke_adapter,
        register_adapter,
        unregister_adapter,
    )

    mark_poll('c1', user_id='alice', protocol_version=2,
              capabilities=sorted(ALL_CAPABILITIES))
    adapter = SiteAdapter(
        id='bad-output', name='Bad output', domains=('example.com',),
        commands=(AdapterCommand(
            'search', 'test', output_schema={'type': 'array'},
            handler=lambda page, params: {'not': 'an array'}),))
    register_adapter(adapter)
    try:
        with pytest.raises(AdapterExecutionError) as exc:
            invoke_adapter('bad-output', 'search', {}, user_id='alice',
                           client_id='c1')
        assert exc.value.code == 'invalid_output'
        assert exc.value.retryable is False
    finally:
        unregister_adapter('bad-output')


def test_adapter_detail_url_cannot_escape_manifest_domains():
    from lib.browser.adapters import AdapterValidationError, invoke_adapter

    with pytest.raises(AdapterValidationError, match='outside adapter domains'):
        invoke_adapter(
            'xiaohongshu', 'detail',
            {'url': 'https://attacker.example/steal'}, user_id='alice')


def test_adapter_upgrade_failure_releases_lease_before_any_page_command():
    from lib.browser import BrowserUpgradeRequired, mark_poll
    from lib.browser.adapters import invoke_adapter
    from lib.browser.sessions import lease_status

    mark_poll('legacy', user_id='alice')
    with pytest.raises(BrowserUpgradeRequired):
        invoke_adapter(
            'xiaohongshu', 'search', {'query': 'tofu'},
            user_id='alice', client_id='legacy')
    assert lease_status(user_id='alice') == []


def test_read_search_health_does_not_depend_on_future_write_capabilities():
    from lib.browser import mark_poll
    from lib.browser.adapters import (
        AdapterCommand, SiteAdapter, adapter_health,
    )

    adapter = SiteAdapter(
        id='mixed-capabilities', name='Mixed', domains=('example.com',),
        commands=(
            AdapterCommand('search', 'read search',
                           required_capabilities=('tabs',)),
            AdapterCommand('publish', 'future write', access='write',
                           required_capabilities=('upload',)),
        ))
    mark_poll('read-only-v2', user_id='alice', protocol_version=2,
              capabilities=['tabs'])

    assert adapter_health(
        adapter, client_id='read-only-v2', command_name='search')['healthy']
    whole = adapter_health(adapter, client_id='read-only-v2')
    assert whole['healthy'] is False
    assert whole['missing_capabilities'] == ['upload']


def test_chatui_site_provider_binds_the_request_users_browser(monkeypatch):
    from lib.browser import ALL_CAPABILITIES, mark_poll
    import lib.browser as browser
    from lib.search_bridge import _ChatuiSiteSearchProvider
    from routes.api_v1 import auth as auth_routes

    mark_poll('alice-browser', user_id='alice', protocol_version=2,
              capabilities=sorted(ALL_CAPABILITIES), profile='Alice Work')
    mark_poll('bob-browser', user_id='bob', protocol_version=2,
              capabilities=sorted(ALL_CAPABILITIES), profile='Bob Work')
    monkeypatch.setattr(
        auth_routes, 'current_auth',
        lambda: SimpleNamespace(user_id='alice'))
    captured = {}

    def invoke(adapter_id, command, params, **kwargs):
        captured.update(kwargs)
        return {'ok': True, 'result': []}

    monkeypatch.setattr(browser, 'invoke_adapter', invoke)
    provider = _ChatuiSiteSearchProvider().bind()
    assert provider.client_id == 'alice-browser'
    assert provider.profile == 'Alice Work'
    assert provider.list_sources()
    assert provider.search('modelplaza', 'embedding') == []
    assert captured['user_id'] == 'alice'
    assert captured['client_id'] == 'alice-browser'


def test_chatui_site_provider_never_uses_a_global_unbound_browser(monkeypatch):
    from lib.browser import ALL_CAPABILITIES, mark_poll
    import lib.browser as browser
    from lib.search_bridge import _ChatuiSiteSearchProvider

    mark_poll('somebody-elses-browser', user_id='other-user',
              protocol_version=2, capabilities=sorted(ALL_CAPABILITIES))
    monkeypatch.setattr(
        browser, 'invoke_adapter',
        lambda *args, **kwargs: pytest.fail('unbound provider executed'))

    provider = _ChatuiSiteSearchProvider()
    assert provider.list_sources() == []
    assert provider.search('modelplaza', 'embedding') is None


def test_chatui_browser_provider_never_uses_a_global_unbound_browser(
        monkeypatch):
    from lib.browser import ALL_CAPABILITIES, mark_poll
    import lib.browser as browser
    from lib.search_bridge import _ChatuiBrowserProvider

    mark_poll('somebody-elses-browser', user_id='other-user',
              protocol_version=2, capabilities=sorted(ALL_CAPABILITIES))
    monkeypatch.setattr(
        browser, 'fetch_url_via_browser',
        lambda *args, **kwargs: pytest.fail('unbound provider fetched'))

    provider = _ChatuiBrowserProvider()
    assert provider.is_connected() is False
    assert provider.fetch_url('https://example.com') is None


def test_approved_browser_write_is_promoted_to_a_durable_domain_grant(
        monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _approval
    import lib.browser.access as access

    calls = []
    monkeypatch.setattr(_approval, 'request_write_approval',
                        lambda approval_id, timeout: True)
    monkeypatch.setattr(_approval, 'append_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        access, 'browser_tool_access',
        lambda fn_name, fn_args, **kwargs: calls.append(
            (fn_name, fn_args, kwargs)))

    approved, rejected = _approval._handle_approval(
        {'id': 'task-browser-grant', '_userId': 'alice'},
        'browser_click', {'tab_id': 7, 'selector': '#save'},
        1, {'toolCallId': 'call-1'}, None, 1, 'test-model',
        cfg={'browserClientId': 'alice-browser'})

    assert approved is True and rejected is None
    assert calls == [(
        'browser_click', {'tab_id': 7, 'selector': '#save'},
        {'user_id': 'alice', 'client_id': 'alice-browser',
         'grant_on_success': True},
    )]


def test_extension_v2_advertises_and_implements_native_page_contract():
    root = Path(__file__).resolve().parents[1]
    source = (root / 'browser_extension' / 'background.js').read_text()
    manifest = json.loads(
        (root / 'browser_extension' / 'manifest.json').read_text())
    from lib.browser.protocol import ALL_CAPABILITIES

    assert re.search(r'const PROTOCOL_VERSION\s*=\s*2\s*;', source)
    advertised_match = re.search(
        r'const BROWSER_CAPABILITIES\s*=\s*\[(.*?)\]\s*;',
        source, re.DOTALL)
    assert advertised_match
    advertised = set(re.findall(r"'([^']+)'", advertised_match.group(1)))
    assert advertised == set(ALL_CAPABILITIES)
    for command in (
        'page_state', 'page_snapshot', 'page_click', 'page_fill',
        'page_press', 'page_select', 'page_execute', 'page_upload',
        'network_capture_start', 'network_capture_stop', 'wait_download',
    ):
        assert f"case '{command}'" in source
    assert 'frameIds = [Number(params.frameId)]' in source
    assert {'debugger', 'downloads', 'webRequest'} <= set(
        manifest.get('permissions') or [])


def test_generic_browser_dispatch_rejects_cross_tenant_client_before_enqueue():
    from lib.browser import execute_browser_tool, mark_poll
    from lib.browser.queue import _state

    mark_poll('alice-browser', user_id='alice')
    mark_poll('bob-browser', user_id='bob')
    result = execute_browser_tool(
        'browser_list_tabs', {}, client_id='bob-browser', user_id='alice')
    assert result == 'Error: Browser client is not connected for this user'
    with _state._commands_lock:
        assert _state._commands == {}


def test_browser_access_implicitly_selects_only_the_request_users_client(
        monkeypatch):
    from lib.browser import mark_poll
    from lib.browser import access

    mark_poll('alice-browser', user_id='alice', profile='Alice Work')
    # Bob polls last, so a global "freshest client" fallback would pick him.
    mark_poll('bob-browser', user_id='bob', profile='Bob Work')
    selected = {}

    def domain(_name, _args, *, client_id=''):
        selected['client_id'] = client_id
        return ''

    monkeypatch.setattr(access, 'browser_tool_domain', domain)
    access.browser_tool_access('browser_list_tabs', {}, user_id='alice')
    # list_tabs is authorized without reading any cross-page domain.
    assert selected == {}

    access.browser_tool_access('browser_navigate', {
        'url': 'https://example.com/'}, user_id='alice')
    assert selected['client_id'] == 'alice-browser'


def test_user_scoped_lease_ignores_another_users_global_active_client():
    from lib.browser import mark_poll
    from lib.browser.sessions import acquire_browser_lease

    mark_poll('alice-browser', user_id='alice', profile='Alice Work')
    mark_poll('bob-browser', user_id='bob', profile='Bob Work')
    lease = acquire_browser_lease(user_id='alice', session='persistent')
    assert lease.client_id == 'alice-browser'
    assert lease.profile == 'Alice Work'


def test_expired_lease_fails_before_page_action_and_cleans_ephemeral_tab():
    from lib.browser import (
        ALL_CAPABILITIES, BrowserCommandError, BrowserPage, mark_poll,
    )
    from lib.browser.sessions import acquire_browser_lease

    mark_poll('alice-browser', user_id='alice', protocol_version=2,
              capabilities=sorted(ALL_CAPABILITIES))
    calls = []

    def sender(command, params, *, timeout, client_id):
        calls.append((command, params, client_id))
        return {'ok': True}, None

    lease = acquire_browser_lease(
        user_id='alice', client_id='alice-browser', tab_id=12,
        session='ephemeral')
    lease.expires_at = 1
    with pytest.raises(BrowserCommandError, match='expired'):
        BrowserPage(lease, sender=sender).snapshot()
    assert calls == [('close_tab', {'tabId': 12}, 'alice-browser')]
