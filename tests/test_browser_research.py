"""Contracts for bounded generic deep website research and Friday adaptation."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from tests._browser_extension_probe import run_extension_probe


pytestmark = pytest.mark.unit

FRIDAY_URL = (
    'https://friday.internal.example.com/skills/skills-market?keyword=&page=1&pageSize=30'
)


def _payload():
    body = {
        'data': {
            'list': [
                {'id': 'skill-a', 'name': '网页信息提取',
                 'description': '读取动态网页真实数据',
                 'accessToken': 'body-secret'},
                {'id': 'skill-b', 'name': '深度检索',
                 'description': '聚合多个来源'},
            ],
            'total': 2,
        },
    }
    serialized = json.dumps(body, ensure_ascii=False)
    return {
        'requestedUrl': FRIDAY_URL,
        'url': FRIDAY_URL,
        'title': '技能市场',
        'collectedText': '技能市场\n网页信息提取\n深度检索',
        'cookieNames': ['cf_clearance'],
        'initialState': {'__NEXT_DATA__': True},
        'initialStatePayloads': {
            '__NEXT_DATA__': json.dumps({
                'page': 'skills', 'authorization': 'state-secret'}),
        },
        'research': {
            'pagesVisited': 2, 'scrollsCompleted': 4,
            'stopReason': 'page_limit',
        },
        'network': {
            'bodyCapture': True,
            'droppedEntries': 1,
            'droppedBodies': 0,
            'webSocketFrameCount': 1,
            'responses': [
                {
                    'url': 'https://friday.internal.example.com/api/skills?page=1',
                    'method': 'GET', 'status': 200,
                    'contentType': 'application/json',
                    'responsePreview': serialized,
                },
                {
                    'url': 'https://friday.internal.example.com/api/skills?page=2',
                    'method': 'GET', 'status': 200,
                    'contentType': 'application/json',
                    'responsePreview': serialized,
                },
                {
                    'url': 'wss://friday.internal.example.com/stream',
                    'method': 'WS', 'status': 101,
                    'contentType': 'application/websocket+json',
                    'responsePreview': json.dumps({
                        'data': {'items': [{'name': '实时技能'}]}}),
                },
                {
                    'url': 'https://denied.example/private',
                    'method': 'GET', 'status': 200,
                    'contentType': 'application/json',
                    'responsePreview': json.dumps({
                        'data': {'items': [{'name': 'denied-sentinel'}]}}),
                },
            ],
        },
    }


@pytest.fixture
def allow_friday(monkeypatch):
    monkeypatch.setattr(
        'lib.browser.access.is_read_allowed',
        lambda owner, url: str(owner) == '41' and (
            str(url).startswith('https://friday.internal.example.com/')
            or str(url).startswith('wss://friday.internal.example.com/')),
    )


def test_network_analysis_collapses_pagination_and_exposes_shapes(allow_friday):
    from lib.browser.network_evidence import analyze_network_evidence

    report = analyze_network_evidence(_payload(), owner_user_id='41')
    skills = next(row for row in report['candidates']
                  if row['key'] == 'GET friday.internal.example.com/api/skills')

    assert skills['verdict'] == 'likely_data'
    assert skills['observations'] == 2
    assert skills['shape']['$.data.list'] == 'array(2)'
    assert report['denied_response_count'] == 1
    assert report['capture']['websocket_frames'] == 1
    assert all('denied.example' not in row['url']
               for row in report['candidates'])


def test_business_record_extraction_is_generic_bounded_and_redacted(
        allow_friday):
    from lib.browser.network_evidence import extract_business_records

    records = extract_business_records(
        _payload(), owner_user_id='41', source_url=FRIDAY_URL,
        query='网页', limit=20)

    assert [row['title'] for row in records] == ['网页信息提取']
    assert records[0]['id'] == 'skill-a'
    assert records[0]['url'] == FRIDAY_URL
    assert 'body-secret' not in json.dumps(records, ensure_ascii=False)
    assert records[0]['metadata']['accessToken'] == '[redacted]'


def test_research_report_combines_strategy_content_and_redacted_data(
        allow_friday):
    from lib.browser.research import render_research_payload

    out = render_research_payload(
        _payload(), owner_user_id='41', mode='both', max_chars=20_000)

    assert 'Strategy: hydrated_state' in out
    assert 'GET friday.internal.example.com/api/skills' in out
    assert 'WebSocket frame(s)' in out
    assert '网页信息提取' in out
    assert 'state-secret' not in out and 'body-secret' not in out
    assert '[redacted]' in out
    assert len(out) <= 20_000


def test_research_policy_failure_logs_once_and_skips_captured_bodies(
        monkeypatch, caplog):
    from lib.browser.research import analyze_research_payload
    import lib.browser.network_evidence as network_evidence

    payload = _payload()
    payload['cookieNames'] = []
    monkeypatch.setattr(network_evidence, '_last_policy_failure_log_at', 0.0)
    monkeypatch.setattr(
        'lib.browser.access.is_read_allowed',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError('policy-store-down')),
    )
    with caplog.at_level(logging.DEBUG, logger='lib.browser.research'):
        report = analyze_research_payload(payload, owner_user_id='41')

    records = [
        record for record in caplog.records
        if record.name == 'lib.browser.research'
        if 'response policy check failed closed' in record.getMessage()
    ]
    assert len(records) == 1
    assert 'policy-store-down' in records[0].getMessage()
    durable_records = [
        record for record in caplog.records
        if 'response policy check failed closed; captured bodies were withheld'
        in record.getMessage()
    ]
    assert len(durable_records) == 1, (
        'one failing policy backend may reject many captured rows, but must '
        'produce one bounded durable diagnostic checkpoint')
    assert report['network']['candidates'] == []


def test_research_handler_routes_one_owner_device_and_clamps_limits(
        monkeypatch, allow_friday):
    from lib.browser.research import research_page
    from lib.browser.tool_runtime import BrowserToolRuntime

    monkeypatch.setattr(
        'lib.browser.protocol.require_capabilities', lambda *_a, **_k: {})
    calls = []

    def sender(command, params=None, timeout=30, **route):
        calls.append((command, params, timeout, route))
        return _payload(), None

    runtime = BrowserToolRuntime(
        owner_user_id='41', client_id='friday-browser', sender=sender)
    out = research_page({
        'url': FRIDAY_URL, 'mode': 'analysis',
        'maxScrolls': 999, 'maxPages': 999,
    }, runtime)

    assert out.startswith('Browser research report')
    assert calls == [(
        'research_url', {
            'url': FRIDAY_URL, 'maxChars': 60_000,
            'maxScrolls': 8, 'maxPages': 5,
            'pagination': 'auto', 'timeoutMs': 65_000,
        }, 80, {'client_id': 'friday-browser', 'owner_user_id': '41'},
    )]


def test_friday_adapter_uses_generic_research_and_normalized_records(
        allow_friday):
    from lib.browser.adapters import _friday_search, get_adapter

    class FakePage:
        lease = SimpleNamespace(owner_user_id='41')

        def __init__(self):
            self.calls = []

        def research(self, url, **kwargs):
            self.calls.append((url, kwargs))
            payload = _payload()
            payload['requestedUrl'] = url
            payload['url'] = url
            return {'result': payload}

    page = FakePage()
    rows = _friday_search(
        page, {'query': '网页', 'limit': 30, 'pages': 2})

    assert get_adapter('friday').domains == ('friday.internal.example.com',)
    assert rows[0]['title'] == '网页信息提取'
    assert rows[0]['metadata']['adapter'] == 'friday'
    assert page.calls[0][1]['max_pages'] == 2
    assert 'keyword=%E7%BD%91%E9%A1%B5' in page.calls[0][0]


def test_extension_research_capture_is_pre_navigation_bounded_and_dynamic():
    probe = run_extension_probe('research')
    operations = probe['operations']
    result = probe['result']

    assert (
        operations.index('tabs.create:about:blank')
        < operations.index('debugger.Network.enable')
        < operations.index('tabs.update:https://example.test/list')
    )
    assert result['research'] == {
        **result['research'],
        'maxScrolls': 8,
        'maxPages': 5,
        'stopReason': 'no-safe-next-control',
    }
    assert result['network']['webSocketFrameCount'] == 1
    assert probe['limits']['websocketFrames'] == 40
    assert probe['limits']['active'] == 4
    assert 'deep_collect' in probe['limits']['capabilities']


def test_extension_pagination_simulation_only_uses_semantic_safe_controls():
    probe = run_extension_probe('pagination')

    assert probe['wizard'] == {
        'advanced': False,
        'reason': 'no-safe-next-control',
    }
    assert probe['semantic']['advanced'] is True
    assert probe['semantic']['kind'] == 'click'
    assert probe['clicked'] is True
    assert probe['crossOrigin'] == {
        'advanced': False,
        'reason': 'cross-origin-next-blocked',
    }
    assert probe['sameOrigin']['advanced'] is True
    assert probe['sameOrigin']['kind'] == 'link'
    assert probe['sameOrigin']['href'] == 'https://example.com/list?page=2'


def test_research_tool_is_in_schema_dispatch_and_read_policy():
    from lib.browser.access import browser_tool_domain
    from lib.browser.dispatch import BROWSER_HANDLERS
    from lib.tools.browser import BROWSER_TOOL_NAMES

    assert 'browser_research_page' in BROWSER_TOOL_NAMES
    assert 'browser_research_page' in BROWSER_HANDLERS
    assert browser_tool_domain(
        'browser_research_page', {'url': FRIDAY_URL},
        client_id='unused', owner_user_id='41') == 'friday.internal.example.com'
