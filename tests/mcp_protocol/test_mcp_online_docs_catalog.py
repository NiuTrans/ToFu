"""Tencent Docs and Kingsoft Docs catalog entries and the reviewed-read override.

Both vendors run an official remote MCP endpoint with a self-service personal
token, so the cards must hand the user a real route to that token. Kingsoft
annotates every tool ``readOnlyHint=false, destructiveHint=true``; its entry
carries a host-reviewed list of pure reads that may only ADD read-only
classification, and only for a server configured at that exact endpoint.
"""

import json
import re

import pytest

from lib.mcp import registry as reg
from lib.mcp.client._bridge import MCPBridge, _MCPServerHandle
from lib.mcp.health_probe import classify_probe_result, validate_health_probe

pytestmark = pytest.mark.unit

SECRET = 'not-a-real-token-0000'
_WRITE_TOKENS = frozenset({
    'accept', 'add', 'apply', 'cancel', 'clear', 'close', 'convert', 'copy',
    'create', 'delete', 'disable', 'enable', 'execute', 'export', 'import',
    'insert', 'merge', 'move', 'open', 'reject', 'remove', 'rename', 'replace',
    'restore', 'run', 'save', 'send', 'set', 'split', 'submit', 'trigger',
    'update', 'upload',
})
_TOKEN_KEYS = {'tencent-docs': 'TENCENT_DOCS_TOKEN', 'kdocs': 'KDOCS_TOKEN'}


class _Tool:
    def __init__(self, name, *, read_only=False):
        self.name = name
        self.description = name
        self.inputSchema = {'type': 'object', 'properties': {}}
        self.annotations = {'readOnlyHint': read_only, 'destructiveHint': True}


@pytest.mark.parametrize('server_id', sorted(_TOKEN_KEYS))
def test_card_is_self_serve_remote_with_a_token_route(server_id):
    entry = reg.get_catalog_entry(server_id)
    assert entry['transport'] == 'streamable-http'
    assert entry['endpoint'].startswith('https://')
    key = _TOKEN_KEYS[server_id]
    assert f'${{{key}}}' in entry['headers']['Authorization']
    [spec] = [s for s in entry['env_specs'] if s.get('required')]
    assert spec['key'] == key and spec['secret'] is True
    assert spec['obtain_url'].startswith('https://')
    assert spec['obtain_steps']

    config = reg.build_server_config(server_id, {key: SECRET})
    assert config['url'] == entry['endpoint']
    assert config['env'] == {key: SECRET}
    assert SECRET not in str(config['headers']), 'headers stay a template'


def test_kingsoft_probe_detects_the_vendors_in_band_auth_failures():
    spec = validate_health_probe(
        reg.get_catalog_entry('kdocs')['health_probe'], server='kdocs')
    assert spec['tool'] == 'list_latest_items'
    observed = (
        '{"code":400006,"message":"未提供认证凭证，请在 Authorization 请求头中携带 Bearer Token"}',
        '{"code":400006,"message":"Token 无效，请检查 Authorization 请求头中的 Bearer Token"}',
    )
    for text in observed:
        assert classify_probe_result(text, spec)[0] == 'expired'
    ok = '{"code":0,"data":{"items":[{"name":"季度预算.xlsx"}]}}'
    assert classify_probe_result(ok, spec) == ('ok', '')


def test_tencent_has_no_periodic_probe_spending_its_daily_quota():
    # A bad token already fails initialize with HTTP 401 at install time.
    assert 'health_probe' not in reg.get_catalog_entry('tencent-docs')


def test_reviewed_kingsoft_reads_contain_no_write_operation():
    reviewed = reg.get_catalog_entry('kdocs')['read_only_tools']
    assert len(reviewed) == len(set(reviewed))
    for name in reviewed:
        tokens = set(re.split(r'[._]', name))
        assert not tokens & _WRITE_TOKENS, name


def _kdocs_config(**overrides):
    config = reg.build_server_config('kdocs', {'KDOCS_TOKEN': SECRET})
    config.update(overrides)
    return config


def test_review_applies_only_to_the_catalog_endpoint():
    assert 'sheet.get_range_data' in reg.reviewed_read_only_tools(
        'kdocs', _kdocs_config())
    assert reg.reviewed_read_only_tools(
        'kdocs', _kdocs_config(url='https://example.invalid/mcp')) == frozenset()
    assert reg.reviewed_read_only_tools(
        'kdocs', _kdocs_config(transport='sse')) == frozenset()
    assert reg.reviewed_read_only_tools(
        'tencent-docs', reg.build_server_config(
            'tencent-docs', {'TENCENT_DOCS_TOKEN': SECRET})) == frozenset()
    assert reg.reviewed_read_only_tools('no-such-server', {}) == frozenset()


def _catalog(config, tools):
    bridge = MCPBridge()
    handle = _MCPServerHandle('kdocs', config)
    bridge._servers['kdocs'] = handle
    bridge._replace_server_catalog('kdocs', handle, tools)
    return bridge, handle


def test_bridge_folds_the_review_into_the_read_partition():
    bridge, _handle = _catalog(_kdocs_config(), [
        _Tool('sheet.get_range_data'),
        _Tool('sheet.update_range_data'),
        _Tool('server_declared_read', read_only=True),
    ])
    assert bridge.get_tool_safety() == {
        'mcp__kdocs__sheet_get_range_data': True,
        'mcp__kdocs__sheet_update_range_data': False,
        'mcp__kdocs__server_declared_read': True,
    }
    info = bridge.get_tool_info('mcp__kdocs__sheet_get_range_data')
    assert info['annotations']['readOnlyHint'] is False, (
        'server annotations stay the vendor\'s own evidence')


def test_a_custom_server_reusing_the_id_gets_no_review():
    bridge, _handle = _catalog(
        _kdocs_config(url='https://example.invalid/mcp'),
        [_Tool('sheet.get_range_data')])
    assert bridge.get_tool_safety() == {
        'mcp__kdocs__sheet_get_range_data': False}


def test_failed_install_names_the_token_page(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from quart import g
    import lib.mcp
    from lib.api_keys import AuthContext
    from lib.app.app_factory import create_base_app
    from lib.mcp import config
    from lib.mcp.client import MCPConnectError
    from routes.api_v1 import mcp as mcp_routes

    monkeypatch.setattr(config, '_CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(mcp_routes, '_install_companion_skill', lambda entry: None)

    def rejected(server_id, _cfg):
        # What the SDK leaves of Tencent's HTTP 401 at initialize.
        raise MCPConnectError(server_id, RuntimeError('Server returned an error response'))
    monkeypatch.setattr(lib.mcp, 'get_bridge',
                        lambda owner: SimpleNamespace(connect_server=rejected))
    app = create_base_app(__name__, {'TESTING': True})

    @app.before_request
    async def authenticated_fixture():
        g.auth_ctx = AuthContext(key_id='test', scopes=frozenset({'admin'}),
                                 owner_user_id=7)
        g.rate_decision = None

    app.register_blueprint(mcp_routes.api_v1_mcp_bp)

    async def install():
        response = await app.test_client().post(
            '/api/v1/mcp/catalog/install',
            json={'id': 'tencent-docs', 'env': {'TENCENT_DOCS_TOKEN': SECRET}})
        return response.status_code, await response.get_json()

    status, body = asyncio.run(install())
    assert status == 500 and body['config_saved'] is True
    message = json.dumps(body, ensure_ascii=False)
    assert 'https://docs.qq.com/open/auth/mcp.html' in message
    assert SECRET not in message


def test_a_changed_review_rebuilds_an_identical_catalog(monkeypatch):
    tools = [_Tool('sheet.get_range_data')]
    bridge, handle = _catalog(_kdocs_config(), tools)
    monkeypatch.setattr(reg, 'reviewed_read_only_tools',
                        lambda _server, _config: frozenset())
    assert bridge._replace_server_catalog('kdocs', handle, tools)
    assert bridge.get_tool_safety() == {
        'mcp__kdocs__sheet_get_range_data': False}
