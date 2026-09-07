"""tests/test_mcp_updates.py — MCP upstream update check + apply.

Covers lib/mcp/updates.py:

  * launch-spec parsing (npx / uvx, pinned vs floating, local paths, remote
    transports) — only registry-resolvable stdio specs are updatable;
  * tolerant version comparison (semver + PEP 440 pre-release ordering);
  * latest-version lookup shaping (PyPI ``info.version``, npm ``version``,
    non-200 → '' and no cache poisoning);
  * the check verdict (pinned older → update, floating + live handshake
    version → comparable, floating + disconnected → undecidable);
  * apply_update: args rewritten in place (extras kept, --exclude-newer
    dropped), env preserved via patch_server, enabled servers reconnect,
    disabled servers are config-only.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_mcp_updates.py -m unit
"""

from __future__ import annotations

import asyncio

import pytest

import lib.mcp.updates as updates
from lib.mcp.updates import (
    apply_update, build_updated_args, check_server_update, compare_versions,
    fetch_latest_version, parse_package_ref,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_latest_cache():
    updates._LATEST_CACHE.clear()
    yield
    updates._LATEST_CACHE.clear()


# ── parse_package_ref ───────────────────────────────────────────────

def test_parse_npx_unpinned_scoped():
    ref, reason = parse_package_ref({
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-github'],
    })
    assert reason == ''
    assert ref['source'] == 'npm'
    assert ref['package'] == '@modelcontextprotocol/server-github'
    assert ref['current'] == '' and ref['pinned'] is False


def test_parse_npx_pinned_and_scoped_with_version():
    ref, _ = parse_package_ref({'command': 'npx', 'args': ['-y', 'pkg@1.2.3']})
    assert ref['package'] == 'pkg'
    assert ref['current'] == '1.2.3' and ref['pinned'] is True

    ref, _ = parse_package_ref({
        'command': 'npx', 'args': ['-y', '@scope/name@2.0.0']})
    assert ref['package'] == '@scope/name'
    assert ref['current'] == '2.0.0' and ref['pinned'] is True


def test_parse_npx_package_is_first_positional_not_trailing_args():
    # The filesystem server appends allowed dirs AFTER the package token —
    # the package is the FIRST positional, not the last token.
    ref, _ = parse_package_ref({
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-filesystem', '/data', '/tmp'],
    })
    assert ref['package'] == '@modelcontextprotocol/server-filesystem'
    assert ref['arg_index'] == 1


def test_parse_npx_dist_tag_is_floating():
    ref, _ = parse_package_ref({'command': 'npx', 'args': ['-y', 'pkg@latest']})
    assert ref['package'] == 'pkg'
    assert ref['current'] == '' and ref['pinned'] is False


def test_parse_uvx_from_pinned_with_extras_and_cutoff():
    ref, reason = parse_package_ref({
        'command': 'uvx',
        'args': ['--exclude-newer', '2026-08-14T00:00:00Z',
                 '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp'],
    })
    assert reason == ''
    assert ref['source'] == 'pypi'
    assert ref['package'] == 'overleaf-mcp-plus'
    assert ref['extras'] == '[compile]'
    assert ref['current'] == '0.3.1' and ref['pinned'] is True
    assert ref['arg_index'] == 3


def test_parse_uvx_from_range_is_floating():
    ref, _ = parse_package_ref({
        'command': 'uvx',
        'args': ['--from', 'overleaf-mcp-plus[compile]>=0.1.3', 'overleaf-mcp'],
    })
    assert ref['package'] == 'overleaf-mcp-plus'
    assert ref['current'] == '' and ref['pinned'] is False


def test_parse_uvx_local_path_not_updatable():
    ref, reason = parse_package_ref({
        'command': 'uvx',
        'args': ['--from', '/opt/tools/github-batch-mcp', 'github-batch-mcp'],
    })
    assert ref is None and reason == 'local-path'


def test_parse_uvx_direct_at_latest():
    ref, reason = parse_package_ref({
        'command': 'uvx', 'args': ['mcp-email-server@latest', 'stdio']})
    assert reason == ''
    assert ref['source'] == 'pypi'
    assert ref['package'] == 'mcp-email-server'
    assert ref['current'] == '' and ref['pinned'] is False


def test_parse_remote_transport_not_updatable():
    ref, reason = parse_package_ref({
        'transport': 'sse', 'url': 'https://example.com/mcp'})
    assert ref is None and reason == 'remote-transport'


def test_parse_unknown_launcher_not_updatable():
    ref, reason = parse_package_ref({'command': 'hope-mcp', 'args': []})
    assert ref is None and reason == 'unsupported-launcher'


# ── compare_versions ────────────────────────────────────────────────

@pytest.mark.parametrize('a,b,expected', [
    ('0.3.1', '0.3.2', -1),
    ('0.3.2', '0.3.1', 1),
    ('1.0', '1.0.0', 0),          # zero-padding: 1.0 == 1.0.0
    ('v1.2.3', '1.2.3', 0),       # v-prefix tolerated
    ('1.0.0rc1', '1.0.0', -1),    # pre-release < final
    ('1.0.0a1', '1.0.0b1', -1),   # alpha < beta
    ('0.10.0', '0.9.9', 1),       # numeric, not lexicographic
])
def test_compare_versions(a, b, expected):
    assert compare_versions(a, b) == expected


def test_compare_versions_unparseable_returns_none():
    assert compare_versions('not-a-version', '1.0.0') is None
    assert compare_versions('1.0.0', '') is None


# ── fetch_latest_version ────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _fake_http(routes):
    calls = []

    async def _get(url, **kw):
        calls.append(url)
        for needle, resp in routes.items():
            if needle in url:
                return resp
        return _FakeResp(404, {})
    _get.calls = calls
    return _get


def test_fetch_latest_pypi_and_npm_shapes(monkeypatch):
    fake = _fake_http({
        'pypi.org': _FakeResp(200, {'info': {'version': '0.3.1'}}),
        'registry.npmjs.org': _FakeResp(200, {'version': '2.5.0'}),
    })
    monkeypatch.setattr(updates, 'async_http_get', fake)
    assert asyncio.run(fetch_latest_version('pypi', 'overleaf-mcp-plus')) == '0.3.1'
    assert asyncio.run(fetch_latest_version('npm', 'pkg')) == '2.5.0'


def test_fetch_latest_scoped_npm_url_encodes_slash(monkeypatch):
    fake = _fake_http({'registry.npmjs.org': _FakeResp(200, {'version': '1.0.0'})})
    monkeypatch.setattr(updates, 'async_http_get', fake)
    assert asyncio.run(
        fetch_latest_version('npm', '@scope/name')) == '1.0.0'
    assert '%2F' in fake.calls[0] or '%2f' in fake.calls[0]


def test_fetch_latest_failure_not_cached(monkeypatch):
    monkeypatch.setattr(
        updates, 'async_http_get',
        _fake_http({'pypi.org': _FakeResp(404, {})}))
    assert asyncio.run(fetch_latest_version('pypi', 'gone')) == ''
    # A failure must NOT be cached — a later healthy lookup succeeds.
    monkeypatch.setattr(
        updates, 'async_http_get',
        _fake_http({'pypi.org': _FakeResp(200, {'info': {'version': '1.0.0'}})}))
    assert asyncio.run(fetch_latest_version('pypi', 'gone')) == '1.0.0'


def test_fetch_latest_success_is_cached(monkeypatch):
    fake = _fake_http({'pypi.org': _FakeResp(200, {'info': {'version': '1.0.0'}})})
    monkeypatch.setattr(updates, 'async_http_get', fake)
    assert asyncio.run(fetch_latest_version('pypi', 'pkg')) == '1.0.0'
    assert asyncio.run(fetch_latest_version('pypi', 'pkg')) == '1.0.0'
    assert len(fake.calls) == 1


# ── check_server_update ─────────────────────────────────────────────

def _pin_latest(monkeypatch, version):
    async def _fetch(source, package, **kw):
        return version
    monkeypatch.setattr(updates, 'fetch_latest_version', _fetch)


def test_check_pinned_older_flags_update(monkeypatch):
    _pin_latest(monkeypatch, '0.4.0')
    cfg = {'command': 'uvx',
           'args': ['--from', 'pkg==0.3.1', 'pkg']}
    out = asyncio.run(check_server_update('x', cfg))
    assert out['updatable'] is True
    assert out['current'] == '0.3.1'
    assert out['latest'] == '0.4.0'
    assert out['update_available'] is True


def test_check_pinned_current_means_no_update(monkeypatch):
    _pin_latest(monkeypatch, '0.3.1')
    cfg = {'command': 'uvx', 'args': ['--from', 'pkg==0.3.1', 'pkg']}
    out = asyncio.run(check_server_update('x', cfg))
    assert out['update_available'] is False


def test_check_floating_uses_live_handshake_version(monkeypatch):
    _pin_latest(monkeypatch, '2.0.0')
    cfg = {'command': 'npx', 'args': ['-y', 'some-server']}
    out = asyncio.run(check_server_update('x', cfg, live_version='1.4.2'))
    assert out['current'] == '1.4.2'
    assert out['update_available'] is True


def test_check_floating_disconnected_is_undecidable(monkeypatch):
    _pin_latest(monkeypatch, '2.0.0')
    cfg = {'command': 'npx', 'args': ['-y', 'some-server']}
    out = asyncio.run(check_server_update('x', cfg))
    assert out['current'] == ''
    assert out['update_available'] is None
    assert out['latest'] == '2.0.0'


def test_check_lookup_failure_marks_error(monkeypatch):
    _pin_latest(monkeypatch, '')
    cfg = {'command': 'npx', 'args': ['-y', 'some-server']}
    out = asyncio.run(check_server_update('x', cfg))
    assert out['updatable'] is True
    assert out['error'] == 'lookup-failed'
    assert out['update_available'] is None


# ── build_updated_args ──────────────────────────────────────────────

def test_build_updated_args_npm_preserves_trailing_args():
    cfg = {'command': 'npx',
           'args': ['-y', '@scope/fs', '/data', '/tmp']}
    ref, _ = parse_package_ref(cfg)
    assert build_updated_args(cfg, ref, '3.1.0') == [
        '-y', '@scope/fs@3.1.0', '/data', '/tmp']


def test_build_updated_args_uvx_keeps_extras_drops_cutoff():
    cfg = {'command': 'uvx',
           'args': ['--exclude-newer', '2026-08-14T00:00:00Z',
                    '--from', 'overleaf-mcp-plus[compile]==0.3.1',
                    'overleaf-mcp']}
    ref, _ = parse_package_ref(cfg)
    # --exclude-newer caps floating resolution at a reviewed date; it would
    # reject the deliberately-newer pin, so the rewrite drops it.
    assert build_updated_args(cfg, ref, '0.4.0') == [
        '--from', 'overleaf-mcp-plus[compile]==0.4.0', 'overleaf-mcp']


# ── apply_update ────────────────────────────────────────────────────

class _FakeTool:
    def __init__(self, name):
        self.name = name


class _FakeBridge:
    def __init__(self, connected=()):
        self._connected = set(connected)
        self.disconnected = []
        self.connect_calls = []

    def list_servers(self):
        return [{'name': n} for n in sorted(self._connected)]

    def _disconnect_one(self, name, forget=False):
        self.disconnected.append((name, forget))
        self._connected.discard(name)

    def connect_server(self, name, cfg):
        self.connect_calls.append((name, cfg))
        self._connected.add(name)
        return [_FakeTool('t1'), _FakeTool('t2')]


def _patch_config(monkeypatch, config, bridge):
    """Swap the config store + bridge for fakes; returns the patch log."""
    patches = []
    monkeypatch.setattr('lib.mcp.config.load_mcp_config',
                        lambda: dict(config))
    monkeypatch.setattr('lib.mcp.config.patch_server',
                        lambda name, changes: patches.append((name, changes))
                        or dict(config))
    monkeypatch.setattr('lib.mcp.get_bridge', lambda: bridge)
    return patches


def test_apply_update_rewrites_args_and_reconnects(monkeypatch):
    _pin_latest(monkeypatch, '0.4.0')
    cfg = {'command': 'uvx',
           'args': ['--from', 'pkg[extra]==0.3.1', 'pkg'],
           'env': {'TOKEN': 'secret'}, 'enabled': True}
    bridge = _FakeBridge(connected={'x'})
    patches = _patch_config(monkeypatch, {'x': cfg}, bridge)

    result = asyncio.run(apply_update('x'))

    assert result['updated'] is True
    assert result['version'] == '0.4.0' and result['previous'] == '0.3.1'
    # Only args are patched — env/credentials never touch the patch.
    assert patches == [('x', {'args': ['--from', 'pkg[extra]==0.4.0', 'pkg']})]
    assert bridge.disconnected == [('x', True)]
    assert bridge.connect_calls and bridge.connect_calls[0][0] == 'x'
    assert result['reconnected'] is True and result['tools_count'] == 2


def test_apply_update_disabled_server_is_config_only(monkeypatch):
    _pin_latest(monkeypatch, '2.0.0')
    cfg = {'command': 'npx', 'args': ['-y', 'pkg@1.0.0'], 'enabled': False}
    bridge = _FakeBridge()
    _patch_config(monkeypatch, {'x': cfg}, bridge)

    result = asyncio.run(apply_update('x'))

    assert result['updated'] is True and result['reconnected'] is False
    assert bridge.connect_calls == []


def test_apply_update_already_latest_is_noop(monkeypatch):
    _pin_latest(monkeypatch, '1.0.0')
    cfg = {'command': 'npx', 'args': ['-y', 'pkg@1.0.0'], 'enabled': True}
    bridge = _FakeBridge()
    patches = _patch_config(monkeypatch, {'x': cfg}, bridge)

    result = asyncio.run(apply_update('x'))

    assert result['updated'] is False
    assert result['already_latest'] is True
    assert patches == [] and bridge.connect_calls == []


def test_apply_update_unknown_server_raises_key_error(monkeypatch):
    _pin_latest(monkeypatch, '1.0.0')
    _patch_config(monkeypatch, {}, _FakeBridge())
    with pytest.raises(KeyError):
        asyncio.run(apply_update('ghost'))


def test_apply_update_non_updatable_raises_value_error(monkeypatch):
    _pin_latest(monkeypatch, '1.0.0')
    cfg = {'transport': 'sse', 'url': 'https://example.com/mcp'}
    _patch_config(monkeypatch, {'x': cfg}, _FakeBridge())
    with pytest.raises(ValueError):
        asyncio.run(apply_update('x'))


# ── Route smoke tests (GET /updates, POST /updates/apply) ──────────

def _route_app():
    from quart import g

    from lib.api_keys import local_admin_context
    from lib.app_factory import create_base_app
    from routes.api_v1.mcp import api_v1_mcp_bp

    app = create_base_app(__name__, {'TESTING': True})

    @app.before_request
    async def _grant():
        g.auth_ctx = local_admin_context()
        g.rate_decision = None

    app.register_blueprint(api_v1_mcp_bp)
    return app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_updates_route_returns_check_results(monkeypatch):
    async def _fake_check():
        return {'x': {'updatable': True, 'source': 'npm', 'package': 'pkg',
                      'current': '1.0.0', 'latest': '2.0.0',
                      'pinned': True, 'update_available': True, 'error': ''}}
    monkeypatch.setattr('lib.mcp.updates.check_all_updates', _fake_check)

    async def go():
        resp = await _route_app().test_client().get('/api/v1/mcp/updates')
        return resp.status_code, await resp.get_json()

    status, body = _run(go())
    assert status == 200
    assert body['ok'] is True
    assert body['updates']['x']['update_available'] is True
    assert body['checked_at'] > 0


def test_apply_route_requires_id():
    async def go():
        resp = await _route_app().test_client().post(
            '/api/v1/mcp/updates/apply', json={})
        return resp.status_code, await resp.get_json()

    status, body = _run(go())
    assert status == 400 and body['ok'] is False


def test_apply_route_maps_key_error_to_404(monkeypatch):
    async def _raise_key(name):
        raise KeyError(name)
    monkeypatch.setattr('lib.mcp.updates.apply_update', _raise_key)

    async def go():
        resp = await _route_app().test_client().post(
            '/api/v1/mcp/updates/apply', json={'id': 'ghost'})
        return resp.status_code, await resp.get_json()

    status, body = _run(go())
    assert status == 404 and body['ok'] is False


def test_apply_route_success_envelope(monkeypatch):
    async def _fake_apply(name):
        return {'updated': True, 'version': '2.0.0', 'previous': '1.0.0',
                'package': 'pkg', 'source': 'npm', 'reconnected': True,
                'tools_count': 3, 'tool_names': ['a', 'b', 'c']}
    monkeypatch.setattr('lib.mcp.updates.apply_update', _fake_apply)

    async def go():
        resp = await _route_app().test_client().post(
            '/api/v1/mcp/updates/apply', json={'id': 'x'})
        return resp.status_code, await resp.get_json()

    status, body = _run(go())
    assert status == 200
    assert body['ok'] is True
    assert body['updated'] is True and body['version'] == '2.0.0'
    assert body['tools_count'] == 3
