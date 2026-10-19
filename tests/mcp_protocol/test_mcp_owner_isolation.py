"""D3: per-owner MCP bridges and config — no cross-owner visibility."""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_bridge_registry():
    import lib.mcp.client as client
    saved = dict(client._bridges)
    client._bridges.clear()
    try:
        yield
    finally:
        client._bridges.clear()
        client._bridges.update(saved)


def test_bridge_instances_are_per_owner_and_reused(monkeypatch):
    import lib.mcp.client as client
    from lib.mcp.client import get_bridge
    one, two = get_bridge(1), get_bridge(2)
    assert one is not two
    assert get_bridge(1) is one  # same owner, same bridge
    assert one._owner_user_id == 1 and two._owner_user_id == 2
    with pytest.raises(ValueError):
        get_bridge(None)
    with pytest.raises(ValueError):
        get_bridge(0)
    with pytest.raises(ValueError):
        get_bridge(True)


def test_registry_retains_all_owners_without_evicting_live_or_stopped_bridges(monkeypatch):
    import lib.mcp.client as client
    bridges = [client.get_bridge(uid) for uid in range(1, 129)]
    assert len(client._bridges) == 128
    for uid, bridge in enumerate(bridges, 1):
        assert client.get_bridge(uid) is bridge and not bridge._retired
    client.disconnect_all_bridges()
    assert client._bridges == {}


def test_per_owner_config_isolates_servers(tmp_path, monkeypatch):
    from lib.mcp import config as mcp_config
    monkeypatch.setattr(mcp_config, '_CONFIG_DIR', str(tmp_path))
    mcp_config.upsert_server('hop', {'enabled': True, 'command': 'hop-mcp'},
                             user_id=1)
    mcp_config.upsert_server('other', {'enabled': True, 'command': 'x'},
                             user_id=2)
    assert set(mcp_config.load_mcp_config(user_id=1)) == {'hop'}
    assert set(mcp_config.load_mcp_config(user_id=2)) == {'other'}
    assert mcp_config.list_configured_owners() == [1, 2]
    with pytest.raises(ValueError):
        mcp_config.upsert_server('bad', {}, user_id=0)


def test_legacy_config_migrates_once_into_owner_namespace(tmp_path,
                                                          monkeypatch):
    from lib.mcp import config as mcp_config
    monkeypatch.setattr(mcp_config, '_CONFIG_DIR', str(tmp_path))
    legacy = tmp_path / mcp_config.MCP_CONFIG_FILENAME
    legacy.write_text(json.dumps({'hop': {'enabled': True}}), encoding='utf-8')
    assert mcp_config.load_mcp_config(user_id=7) == {}
    assert mcp_config.migrate_legacy_config(user_id=7) is True
    assert set(mcp_config.load_mcp_config(user_id=7)) == {'hop'}
    owner_file = tmp_path / 'mcp_servers' / '7.json'
    assert owner_file.exists()
    # Second load does not clobber owner-side edits.
    mcp_config.upsert_server('extra', {'enabled': True}, user_id=7)
    assert set(mcp_config.load_mcp_config(user_id=7)) == {'hop', 'extra'}
    # Legacy file itself is never mutated by migration.
    assert json.loads(legacy.read_text()) == {'hop': {'enabled': True}}
    assert mcp_config.load_mcp_config(user_id=8) == {}
    with pytest.raises(PermissionError):
        mcp_config.migrate_legacy_config(user_id=8)


def test_mcp_build_fails_closed_without_owner():
    from lib.tools.registry._build import _build_mcp
    from lib.tools.registry._spec import ToolContext
    ctx = ToolContext(
        cfg={'mcpEnabled': True}, task_id='t', project_path='',
        project_enabled=False, search_mode='off', search_enabled=False,
        fetch_enabled=False, code_exec_enabled=False, browser_enabled=False,
        desktop_enabled=False, owner_user_id=0)
    assert _build_mcp(ctx) == []


def test_resolver_mcp_fallback_uses_only_the_task_owner(store=None):
    import lib.mcp.client as client
    from lib.tasks_pkg.executor._resolver import resolver_for

    class Bridge:
        def __init__(self, tools):
            self._tools = tools

        def is_mcp_tool(self, name):
            return name in self._tools

    client._bridges[1] = Bridge({'mcp__hop__submit'})
    client._bridges[2] = Bridge({'mcp__other__tool'})
    from lib.mcp.types import MCP_TOOL_PREFIX
    assert MCP_TOOL_PREFIX == 'mcp__'
    resolver = resolver_for({'_userId': 1},
                            registry=type('R', (), {'lookup': staticmethod(
                                lambda n, r=None: None)})())
    handler = resolver.lookup('mcp__hop__submit')
    assert handler is not None  # owner's own server resolves
    assert resolver.lookup('mcp__other__tool') is None  # another owner's
    foreign = resolver_for({'_userId': 0},
                           registry=type('R', (), {'lookup': staticmethod(
                               lambda n, r=None: None)})())
    assert foreign.lookup('mcp__hop__submit') is None  # no owner, closed


def test_active_owner_bridge_cannot_block_an_independent_owner(monkeypatch):
    import lib.mcp.client as client
    original = client.get_bridge(7)
    original._registry_in_use = True
    other = client.get_bridge(8)
    assert client._bridges == {7: original, 8: other}
    assert not original._retired


def test_explicitly_retired_bridge_cannot_start_transport(monkeypatch):
    import lib.mcp.client as client
    old = client.get_bridge(7)
    old._retired = True
    client.get_bridge(8)
    async def unused():
        raise AssertionError('retired bridge ran a coroutine')
    coro = unused()
    with pytest.raises(RuntimeError, match='retired'):
        old._run_async(coro)
    assert coro.cr_frame is None
    assert old._loop is None


def test_parallel_loop_start_creates_one_thread():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import lib.mcp.client as client
    bridge = client.get_bridge(7)
    gate = threading.Barrier(4)
    def start():
        gate.wait(timeout=5)
        return bridge._ensure_loop()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            loops = list(pool.map(lambda _: start(), range(4)))
        assert len({id(loop) for loop in loops}) == 1
        assert loops[0].is_running()
    finally:
        client.disconnect_all_bridges()
    assert not loops[0].is_running()
    loops[0].close()


def test_shutdown_only_disconnects_existing_owners(monkeypatch):
    import lib.mcp.client as client
    one, two = client.get_bridge(1), client.get_bridge(2)
    calls = []
    def fail():
        calls.append(1)
        raise RuntimeError('injected teardown failure')
    monkeypatch.setattr(one, 'disconnect_all', fail)
    monkeypatch.setattr(two, 'disconnect_all', lambda: calls.append(2))
    client.disconnect_all_bridges()
    client.disconnect_all_bridges()
    assert calls == [1, 2]
    assert client._bridges == {}
    assert one._retired and two._retired


def test_legacy_claim_survives_copy_failure_and_preserves_owner_edits(tmp_path, monkeypatch):
    from lib.mcp import config
    monkeypatch.setattr(config, '_CONFIG_DIR', str(tmp_path))
    (tmp_path / config.MCP_CONFIG_FILENAME).write_text('{"legacy": {}}')
    update = config.update_json_atomic
    def fail_copy(path, *args, **kwargs):
        if str(path).endswith('/7.json'):
            raise OSError('injected disk failure')
        return update(path, *args, **kwargs)
    monkeypatch.setattr(config, 'update_json_atomic', fail_copy)
    with pytest.raises(OSError, match='disk failure'):
        config.migrate_legacy_config(user_id=7)
    with pytest.raises(PermissionError):
        config.migrate_legacy_config(user_id=8)
    monkeypatch.setattr(config, 'update_json_atomic', update)
    assert config.migrate_legacy_config(user_id=7)
    config.save_mcp_config({}, user_id=7)
    assert not config.migrate_legacy_config(user_id=7)
    assert config.load_mcp_config(user_id=7) == {}


@pytest.mark.parametrize('raw', ['0', 'bad', '999999', '1'])
def test_retired_registry_setting_cannot_refuse_or_evict_owner_bridges(monkeypatch, raw):
    import lib.mcp.client as client
    monkeypatch.setenv('TOFU_MCP_OWNER_BRIDGE_CAPACITY', raw)
    bridges = [client.get_bridge(owner) for owner in range(1, 17)]
    assert len(client._bridges) == 16 and all(not bridge._retired for bridge in bridges)


def test_http_crud_uses_authenticated_owner_and_redacts_credentials(tmp_path, monkeypatch):
    import asyncio
    from quart import g, request
    from lib.api_keys import AuthContext
    from lib.app.app_factory import create_base_app
    from lib.mcp import config
    import lib.mcp.client as client
    from routes.api_v1.mcp import api_v1_mcp_bp
    monkeypatch.setattr(config, '_CONFIG_DIR', str(tmp_path))
    app = create_base_app(__name__, {'TESTING': True})
    @app.before_request
    async def authenticated_fixture():
        g.auth_ctx = AuthContext(key_id='test', scopes=frozenset({'admin'}),
                                 owner_user_id=int(request.headers['X-Test-Owner']))
        g.rate_decision = None
    app.register_blueprint(api_v1_mcp_bp)
    async def exercise():
        http = app.test_client()
        for owner in (7, 8):
            response = await http.post('/api/v1/mcp/servers',
                headers={'X-Test-Owner': str(owner)},
                json={'name': 'same-name', 'command': 'unused',
                      'description': f'owner-{owner}',
                      'env': {'TOKEN': f'private-{owner}'}})
            assert response.status_code == 200
        for owner in (7, 8):
            response = await http.get('/api/v1/mcp/servers',
                                     headers={'X-Test-Owner': str(owner)})
            assert response.status_code == 200
            body = await response.get_json()
            blob = json.dumps(body)
            assert f'owner-{owner}' in blob
            assert f'owner-{15-owner}' not in blob
            assert 'private-' not in blob
        response = await http.delete('/api/v1/mcp/servers/same-name',
                                     headers={'X-Test-Owner': '7'})
        assert response.status_code == 200
        assert config.load_mcp_config(user_id=7) == {}
        assert config.load_mcp_config(user_id=8)['same-name']['env']['TOKEN'] == 'private-8'
    asyncio.run(exercise())


@pytest.mark.parametrize('fault', ['loop_create', 'thread_start', 'never_runs'])
def test_failed_loop_start_rolls_back_without_retiring_other_owners(monkeypatch, fault):
    import asyncio
    import threading
    import lib.mcp.client as client
    import lib.mcp.client._bridge as implementation
    bridge = client.get_bridge(7)
    created = []
    original_loop = asyncio.new_event_loop
    def make_loop():
        if fault == 'loop_create':
            raise OSError('injected loop creation failure')
        loop = original_loop()
        created.append(loop)
        return loop
    class FailedThread(threading.Thread):
        def start(self):
            if fault == 'thread_start':
                raise RuntimeError('injected thread start failure')
    monkeypatch.setattr(implementation.asyncio, 'new_event_loop', make_loop)
    monkeypatch.setattr(implementation.threading, 'Thread', FailedThread)
    monkeypatch.setattr(implementation.time, 'sleep', lambda _: None)
    try:
        with pytest.raises((OSError, RuntimeError)):
            bridge._ensure_loop()
        assert not bridge._registry_in_use
        assert bridge._loop is None and bridge._loop_thread is None
        assert all(loop.is_closed() for loop in created)
        assert client.get_bridge(8)._owner_user_id == 8
        assert not bridge._retired and client.get_bridge(7) is bridge
    finally:
        for loop in created:
            if not loop.is_closed():
                loop.close()


def test_late_loop_start_keeps_ownership_without_blocking_other_owners(monkeypatch):
    import threading
    import lib.mcp.client as client
    import lib.mcp.client._bridge as implementation
    bridge = client.get_bridge(7)
    gate = threading.Event()
    class DelayedThread(threading.Thread):
        def run(self):
            gate.wait(timeout=5)
            super().run()
    monkeypatch.setattr(implementation.threading, 'Thread', DelayedThread)
    monkeypatch.setattr(implementation.time, 'sleep', lambda _: None)
    try:
        with pytest.raises(RuntimeError, match='startup budget'):
            bridge._ensure_loop()
        loop, thread = bridge._loop, bridge._loop_thread
        assert thread.is_alive() and bridge._registry_in_use
        assert client.get_bridge(8)._owner_user_id == 8
        assert client._bridges[7] is bridge
        with pytest.raises(RuntimeError, match='still starting or stopping'):
            bridge._ensure_loop()
    finally:
        gate.set()
        thread = bridge._loop_thread
        if thread is not None:
            thread.join(timeout=5)
            assert not thread.is_alive()
    assert loop.is_closed()
    assert not bridge._registry_in_use and bridge._loop is None
    assert client.get_bridge(8)._owner_user_id == 8
