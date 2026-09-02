"""Contracts for generic authenticated SPA network evidence.

The Friday skills-market fixture is intentionally only a payload shape, not a
hostname-specific adapter.  These tests pin the reusable contract: capture
must begin before navigation, useful API bodies survive, telemetry and denied
origins do not, credential-shaped fields are redacted, and all output is
bounded before it reaches model context.
"""

import json

import pytest

from tests._browser_extension_probe import run_extension_probe


pytestmark = pytest.mark.unit

FRIDAY_PAGE = (
    'https://friday.internal.example.com/skills/skills-market?deepSearch=false&page=1'
    '&pageSize=30&viewMode=card'
)


def _skills_body():
    return {
        'data': {
            'list': [
                {
                    'id': 'friday-skill-a',
                    'name': '网页信息提取',
                    'description': '读取动态网站中的真实业务数据',
                    'accessToken': 'raw-body-secret',
                },
                {
                    'id': 'friday-skill-b',
                    'name': '深度检索',
                    'description': '聚合并排序多个来源',
                    'note': 'Bearer bearer-secret-123456',
                },
            ],
            'total': 2,
            'page': 1,
        }
    }


def _friday_payload(*, page_text='技能市场'):
    business_body = json.dumps(_skills_body(), ensure_ascii=False)
    return {
        'title': '技能市场',
        'url': FRIDAY_PAGE,
        'html': '<html><body><div id="app"></div></body></html>',
        'text': page_text,
        'network': {
            'responses': [
                {
                    'url': (
                        'https://friday.internal.example.com/api/skills/market'
                        '?page=1&pageSize=30&access_token=raw-query-secret'
                    ),
                    'method': 'GET',
                    'status': 200,
                    'contentType': 'application/json; charset=utf-8',
                    'responsePreview': business_body,
                },
                # Same body on a second endpoint must not waste context.
                {
                    'url': 'https://friday.internal.example.com/api/skills/repeated',
                    'method': 'GET',
                    'status': 200,
                    'contentType': 'application/json',
                    'responsePreview': business_body,
                },
                {
                    'url': 'https://friday.internal.example.com/api/telemetry/collect',
                    'method': 'POST',
                    'status': 200,
                    'contentType': 'application/json',
                    'responsePreview': json.dumps({
                        'events': [{'event': 'telemetry-sentinel'}],
                        'traceId': 'not-business-data',
                    }),
                },
                {
                    'url': 'https://denied.example/api/catalog',
                    'method': 'GET',
                    'status': 200,
                    'contentType': 'application/json',
                    'responsePreview': json.dumps({
                        'data': {'list': [{'name': 'denied-sentinel'}]},
                    }),
                },
            ],
        },
    }


def _allow_only_friday(monkeypatch):
    monkeypatch.setattr(
        'lib.browser.access.is_read_allowed',
        lambda owner_user_id, url: (
            str(owner_user_id) == '41'
            and str(url).startswith('https://friday.internal.example.com/')
        ),
    )


def test_friday_shaped_api_data_is_ranked_redacted_deduplicated_and_scoped(
        monkeypatch):
    from lib.browser.network_evidence import render_network_evidence

    _allow_only_friday(monkeypatch)
    out = render_network_evidence(
        _friday_payload(), owner_user_id='41', max_chars=20_000)

    assert '网页信息提取' in out and '深度检索' in out
    assert out.count('friday-skill-a') == 1
    assert 'raw-body-secret' not in out
    assert 'bearer-secret-123456' not in out
    assert 'raw-query-secret' not in out
    assert '[redacted]' in out
    assert 'telemetry-sentinel' not in out
    assert 'denied-sentinel' not in out


def test_network_evidence_fails_closed_and_honors_one_context_budget(
        monkeypatch):
    from lib.browser.network_evidence import (
        merge_page_and_network,
        render_network_evidence,
    )

    _allow_only_friday(monkeypatch)
    assert render_network_evidence(
        _friday_payload(), owner_user_id='not-an-owner') == ''
    assert render_network_evidence(
        _friday_payload(), owner_user_id='41', max_chars='bad-budget') == ''
    bounded = render_network_evidence(
        _friday_payload(), owner_user_id='41', max_chars=700)
    assert bounded and len(bounded) <= 700
    assert len(merge_page_and_network(
        'page shell', bounded, max_chars=17)) <= 17
    assert merge_page_and_network(
        'page shell', bounded, max_chars='bad-budget') == ''


def test_read_page_auto_uses_api_evidence_instead_of_sparse_spa_diagnosis(
        monkeypatch):
    from lib.browser.handlers import _handle_read_page
    from lib.browser.tool_runtime import BrowserToolRuntime

    _allow_only_friday(monkeypatch)
    payload = _friday_payload()
    runtime = BrowserToolRuntime(
        owner_user_id='41', client_id='network-auto-test',
        sender=lambda *args, **kwargs: (payload, None),
    )
    out = _handle_read_page({'tabId': 7, 'mode': 'auto'}, runtime)

    assert 'Captured API data from the authenticated browser session' in out
    assert 'friday-skill-a' in out
    assert 'Text extraction is sparse' not in out


def test_explicit_data_mode_names_the_extension_upgrade_boundary(monkeypatch):
    from lib.browser.handlers import _handle_read_page
    from lib.browser.tool_runtime import BrowserToolRuntime

    runtime = BrowserToolRuntime(
        owner_user_id='41', client_id='network-body-missing-test',
        sender=lambda *args, **kwargs: (_friday_payload(), None),
    )
    out = _handle_read_page({'tabId': 7, 'mode': 'data'}, runtime)
    assert 'upgrade required' in out
    assert 'network_body' in out


def test_explicit_data_mode_returns_only_ranked_api_evidence(monkeypatch):
    from lib.browser.handlers import _handle_read_page
    from lib.browser.tool_runtime import BrowserToolRuntime

    _allow_only_friday(monkeypatch)
    monkeypatch.setattr(
        'lib.browser.protocol.require_capabilities',
        lambda client_id, required: {'client_id': client_id})
    runtime = BrowserToolRuntime(
        owner_user_id='41', client_id='network-body-current-test',
        sender=lambda *args, **kwargs: (_friday_payload(), None),
    )
    out = _handle_read_page({'tabId': 7, 'mode': 'data'}, runtime)

    assert 'Captured API data' in out and 'friday-skill-a' in out
    assert 'Rendered page text' not in out


def test_fetch_url_can_succeed_from_api_data_when_dom_is_only_a_shell(
        monkeypatch):
    import lib.browser.fetch as browser_fetch

    _allow_only_friday(monkeypatch)
    monkeypatch.setattr(
        'lib.browser.access.require_access', lambda *args, **kwargs: 'friday.internal.example.com')
    monkeypatch.setattr(
        browser_fetch, 'is_extension_connected',
        lambda client_id, *, owner_user_id: (
            client_id == 'test-browser' and owner_user_id == '41'))
    monkeypatch.setattr(
        'lib.browser.protocol.require_capabilities',
        lambda client_id, required: {'client_id': client_id})
    seen = {}

    def fake_send(command, params, **kwargs):
        seen.update(command=command, params=params, kwargs=kwargs)
        return _friday_payload(page_text=''), None

    monkeypatch.setattr(browser_fetch, 'send_browser_command', fake_send)
    out = browser_fetch.fetch_url_via_browser(
        FRIDAY_PAGE, client_id='test-browser', owner_user_id='41')

    assert out and 'friday-skill-a' in out
    assert seen['command'] == 'fetch_url'
    assert seen['params']['timeoutMs'] == 20_000
    assert seen['kwargs']['timeout'] == 35


def test_extension_capture_is_bounded_and_starts_before_navigation():
    probe = run_extension_probe('fetch')
    operations = probe['operations']
    network = probe['result']['network']

    assert probe['publicBlankRejected'] is True
    assert probe['limits'] == {
        'entries': 80,
        'trackedRequests': 160,
        'totalBodyChars': 1024 * 1024,
        'active': 4,
    }
    assert 'Network.getResponseBody' in probe['debuggerCommands']
    assert network['pageUrl'] == 'https://example.test/app'
    assert network['webSocketFrameCount'] == 1
    assert network['responses'][0]['responsePreview'] == (
        '{"data":[{"id":"captured"}]}')
    assert (
        operations.index('tabs.create:about:blank')
        < operations.index('debugger.Network.enable')
        < operations.index('tabs.update:https://example.test/app')
    )


def test_read_page_schema_exposes_captured_data_mode():
    from lib.tools.browser import BROWSER_TOOL_READ_PAGE

    modes = BROWSER_TOOL_READ_PAGE['function']['parameters']['properties'][
        'mode']['enum']
    assert 'data' in modes
