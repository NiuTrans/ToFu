"""A new user is told how to connect Tencent Docs / Kingsoft Docs.

Two triggers share one projection of catalog data (obtain_url/steps) and the
owner's MCP configuration: a Tool Search that names a curated connector the
task cannot call, and a fetch of a document link only that connector can read
(an anonymous fetch returns a JS shell or a login redirect). Evidence never
grants authority, never starts a bridge and never fires on generic words.
"""
import json

import pytest

from lib.mcp import connector_guidance as guidance
from lib.tasks_pkg.handlers import tool_gateway
from lib.tasks_pkg.tool_search_projection import tool_search_display_results

pytestmark = pytest.mark.unit

TENCENT_TOKEN_PAGE = 'https://docs.qq.com/open/auth/mcp.html'
SHEET_URL = 'https://docs.qq.com/sheet/DUmFpbnZtYnZsQ0xW'


@pytest.fixture
def owner_config(monkeypatch):
    configs = {}
    reads = []

    def load(*, user_id):
        reads.append(user_id)
        return configs.get(user_id, {})

    monkeypatch.setattr(guidance, 'load_mcp_config', load)
    return configs, reads


def _availability(query='', namespace='', usable=(), owner=7):
    return guidance.connector_search_availability(owner, query, namespace, usable)


def test_alias_search_for_an_uninstalled_connector_returns_the_token_route(owner_config):
    [row] = _availability('腾讯文档 表格')
    assert row['reason_code'] == 'mcp_connector_not_installed'
    assert row['namespace'] == row['server_id'] == 'tencent-docs'
    assert row['available'] is False
    assert TENCENT_TOKEN_PAGE in row['recovery_hint']
    assert 'Settings → MCP' in row['recovery_hint']


def test_namespace_filter_names_a_connector_exactly(owner_config):
    configs, _ = owner_config
    configs[7] = {'kdocs': {'enabled': False}}
    [row] = _availability('read sheet', namespace='KDocs')
    assert row['reason_code'] == 'mcp_connector_disabled'


def test_installed_but_absent_from_task_points_at_settings_and_token(owner_config):
    configs, _ = owner_config
    configs[7] = {'kdocs': {'enabled': True}}
    [row] = _availability('金山文档')
    assert row['reason_code'] == 'mcp_connector_unavailable'
    assert 'https://www.kdocs.cn/latest' in row['recovery_hint']


def test_no_evidence_when_usable_unowned_or_generic(owner_config):
    _configs, reads = owner_config
    assert _availability('腾讯文档', usable=['tencent-docs']) == []
    assert _availability('腾讯文档', owner=0) == []
    assert _availability('online spreadsheet read range') == []
    assert reads == [], 'no configuration read without a named connector'


def test_unreadable_configuration_makes_no_claim(monkeypatch):
    def broken(*, user_id):
        raise OSError('disk')
    monkeypatch.setattr(guidance, 'load_mcp_config', broken)
    assert _availability('腾讯文档') == []
    assert guidance.document_link_guidance(7, SHEET_URL) is None


@pytest.mark.parametrize(('url', 'server_id'), [
    (SHEET_URL, 'tencent-docs'),
    ('https://DOCS.QQ.COM/doc/DSGxxxx?tab=1', 'tencent-docs'),
    ('https://www.kdocs.cn/l/cmAqaKBmDXAN', 'kdocs'),
    ('https://kdocs.cn/l/cmAqaKBmDXAN', 'kdocs'),
    ('https://docs.qq.com/open/document/mcp/', None),
    ('https://365.kdocs.cn/l/abc', None),
    ('ftp://docs.qq.com/sheet/x', None),
    ('not a url', None),
])
def test_document_links_map_to_their_connector(url, server_id):
    entry = guidance.connector_for_document_url(url)
    assert (entry['id'] if entry else None) == server_id


def test_document_link_guidance_follows_install_state(owner_config):
    configs, _ = owner_config
    text = guidance.document_link_guidance('7', SHEET_URL)
    assert SHEET_URL in text and TENCENT_TOKEN_PAGE in text
    configs[7] = {'tencent-docs': {'enabled': True}}
    text = guidance.document_link_guidance('7', SHEET_URL)
    assert "namespace 'tencent-docs'" in text
    assert TENCENT_TOKEN_PAGE not in text
    assert guidance.document_link_guidance('', SHEET_URL) is None
    assert guidance.document_link_guidance('7', 'https://example.com/') is None


def test_fetch_reader_answers_links_but_yields_to_auth_sources(monkeypatch, owner_config):
    from lib import auth_sources
    from lib.search import search_bridge
    reader = search_bridge._ChatuiConnectorDocReader()
    monkeypatch.setattr(search_bridge, '_bind_browser_identity',
                        lambda: ('7', '', ''))
    monkeypatch.setattr(auth_sources, 'match_source', lambda url: None)
    assert reader.matches(SHEET_URL)
    assert not reader.matches('https://docs.qq.com/open/document/mcp/')
    assert TENCENT_TOKEN_PAGE in reader.read(SHEET_URL)
    monkeypatch.setattr(auth_sources, 'match_source',
                        lambda url: {'domain': 'docs.qq.com'})
    assert reader.read(SHEET_URL) is None


def _search(monkeypatch, task, query, namespace=''):
    monkeypatch.setattr(tool_gateway, '_finalize', lambda *a, **kw: None)
    _, content, _ = tool_gateway.handle_search_tools(
        task, {}, 'search_tools', 'connector-search',
        {'query': query, 'namespace': namespace}, 1, {}, {}, None, False)
    return json.loads(content)


def test_tool_search_relays_setup_guidance_until_installed(monkeypatch, owner_config):
    task = {'id': 'connector-task', '_userId': 7,
            '_executable_tool_catalog': [], '_executableToolNamespaceByName': {}}
    result = _search(monkeypatch, task, 'tencent docs spreadsheet')
    [evidence] = result['capability_availability']
    assert evidence['reason_code'] == 'mcp_connector_not_installed'
    assert TENCENT_TOKEN_PAGE in result['notice']
    ui = tool_search_display_results(result)
    assert ui[-1]['type'] == 'tool_catalog_availability'
    assert ui[-1]['reasonCode'] == 'mcp_connector_not_installed'

    repeated = _search(monkeypatch, task, 'tencent docs spreadsheet')
    assert repeated['repeated_query'] == 2
    assert TENCENT_TOKEN_PAGE in repeated['notice']

    task['_executableToolNamespaceByName'] = {
        'mcp__tencent-docs__get_sheet': 'tencent-docs'}
    assert 'capability_availability' not in _search(
        monkeypatch, task, 'tencent docs sheet')
