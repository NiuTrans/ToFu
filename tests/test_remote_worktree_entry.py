"""tests/test_remote_worktree_entry.py — RWA P4a:每用户 bridge token + agent_run remote 绑定.

docs/modules/remote_execution.md:
  * bridge token 复用 api_keys 生命周期,scope ``agents:bridge``;
    token 解析出的 owner_user_id 打进 agent 注册表;
  * 命令按用户作用域投递:注册表 user_id 与命令 user_id 不一致绝不交付
    (fail-closed;relay 场景 A 用户的 agent 领不走 B 用户的命令);
  * ``agent_run`` config 别名 ``remote: '<agent_id>:<root>'`` → 校验
    (在线 / root 已声明 / 用户匹配)→ ``cfg['project_remote']``;
  * 远程绑定隐含 project_enabled(服务器无 projectPath 也投影项目工具);

Run:  pytest tests/test_remote_worktree_entry.py -m unit -v
"""

from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import threading
import time

import pytest

from lib.desktop import bridge as db


def _run_async(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _clean_bridge(monkeypatch):
    monkeypatch.setattr(db, '_last_poll', [0.0])
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
        db._streams.clear()
    with db._async_waiters_lock:
        db._async_waiters.clear()
    yield
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
        db._streams.clear()
    with db._async_waiters_lock:
        db._async_waiters.clear()


def _register(agent_id, user_id='', name='box', roots=None):
    meta = {'name': name, 'platform': 'linux'}
    if roots is not None:
        meta['share_roots'] = roots
    db.register_agent(agent_id, meta, user_id=user_id)


def _plant(cmd_id='cmd-1', cmd_type='desktop_list_files', target=None,
           user_id=''):
    cmd = {
        'id': cmd_id, 'type': cmd_type, 'params': {},
        'created_at': time.time(), 'event': threading.Event(),
        'result': None, 'error': None,
    }
    if target:
        cmd['target_agent_id'] = target
    cmd['user_id'] = user_id
    with db.command_queue_lock:
        db.command_queue[cmd_id] = cmd
    return cmd


def _drain(agent_id, user_id='', timeout=0.3):
    return _run_async(db.take_pending_commands_async(
        timeout=timeout, agent_id=agent_id, user_id=user_id))


# ═══════════════════════════════════════════════════════════
#  注册表用户标记 + 列表过滤
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRegistryUserTag:
    def test_register_stores_user_id(self):
        _register('agent-A', user_id='u-alice')
        assert db.online_agents()[0]['user_id'] == 'u-alice'

    def test_list_agents_filters_by_user(self):
        _register('agent-A', user_id='u-alice')
        _register('agent-B', user_id='u-bob')
        mine = db.list_agents(user_id='u-alice')
        assert [a['agent_id'] for a in mine] == ['agent-A']
        assert len(db.list_agents()) == 2  # 不传 = 全量(管理面)

    def test_legacy_agent_registers_unscoped(self):
        _register('agent-old')
        assert db.online_agents()[0]['user_id'] == ''


# ═══════════════════════════════════════════════════════════
#  命令用户作用域(约束②第三条:防跨用户投递)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCommandUserScope:
    def test_user_command_only_reaches_same_user_agent(self):
        _register('agent-A', user_id='u-alice')
        _register('agent-B', user_id='u-bob')
        _plant(target='agent-A', user_id='u-alice')
        # bob 的 agent 轮询:拿不到 alice 的命令
        assert _drain('agent-B', user_id='u-bob') == []
        assert _drain('agent-A', user_id='u-alice')[0]['id'] == 'cmd-1'

    def test_unaddressed_counts_only_own_user_online(self):
        _register('agent-A', user_id='u-alice')
        _register('agent-B', user_id='u-bob')
        # alice 视角只有一个在线 agent → 回退档放行,且只到 alice 的 agent
        _plant(user_id='u-alice')
        assert _drain('agent-B', user_id='u-bob') == []
        assert _drain(
            'agent-A', user_id='u-alice')[0]['type'] == 'desktop_list_files'

    def test_addressed_cross_user_refused_at_enqueue(self):
        _register('agent-B', user_id='u-bob')
        result, error = db.send_desktop_command(
            'desktop_list_files', {}, timeout=0.2,
            target_agent_id='agent-B', user_id='u-alice')
        assert result is None and error
        assert db.pending_commands_count() == 0

    def test_unaddressed_foreign_agents_invisible_to_gate(self):
        # bob 在线,alice 无 agent:alice 入队未寻址命令 → 队列等 alice 的
        # agent(bob 的在线不算数)
        _register('agent-B', user_id='u-bob')
        result, error = db.send_desktop_command(
            'desktop_list_files', {}, timeout=0.01, user_id='u-alice')
        # 入队本身放行(0 个 alice agent 在线),bob 拿不到。
        assert result is None and error
        assert _drain('agent-B', user_id='u-bob') == []

    def test_legacy_world_byte_identical(self):
        """全 '' user_id 的单用户世界:wire 投影键逐字节不变."""
        _register('agent-old')
        _plant()
        cmds = _drain('agent-old')
        assert len(cmds) == 1
        assert set(cmds[0].keys()) == {'id', 'type', 'params'}

    def test_scoped_command_never_leaks_user_id_on_wire(self):
        _register('agent-A', user_id='u-alice')
        _plant(target='agent-A', user_id='u-alice')
        cmds = _drain('agent-A', user_id='u-alice')
        assert 'user_id' not in cmds[0]

    def test_unscoped_agent_cannot_receive_user_command(self):
        # 全局 secret 注册的 legacy agent(user_id='')不许领用户命令
        _register('agent-old')
        _plant(user_id='u-alice')
        assert _drain('agent-old') == []


# ═══════════════════════════════════════════════════════════
#  poll 认证:per-user token(复用 api_keys,scope agents:bridge)
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestPollPerUserToken:
    @pytest.fixture(autouse=True)
    def _fast_long_poll(self, monkeypatch):
        monkeypatch.setattr(db, 'POLL_WAIT_TIMEOUT', 0.2)

    def _make_token(self, scopes=('agents:bridge',), user_id='101'):
        from lib.api_keys import create_key
        _row, token = create_key(
            owner_user_id=int(user_id), name='bridge-test',
            scopes=list(scopes))
        return token

    def test_bridge_scope_in_all_scopes(self):
        from lib.api_keys import ALL_SCOPES
        assert 'agents:bridge' in ALL_SCOPES

    def test_user_token_registers_with_owner_id(self, flask_client):
        token = self._make_token()
        r = flask_client.post('/api/desktop/poll',
                              json={'results': [], 'agent': {
                                  'agent_id': 'agent-A', 'name': 'mac'}},
                              headers={'X-Bridge-Secret': token})
        assert r.status_code == 200
        assert db.online_agents()[0]['user_id'] == '101'

    def test_token_without_bridge_scope_rejected(self, flask_client):
        token = self._make_token(scopes=('chat',))
        r = flask_client.post('/api/desktop/poll',
                              json={'results': []},
                              headers={'X-Bridge-Secret': token})
        assert r.status_code == 401

    def test_garbage_token_rejected(self, flask_client):
        r = flask_client.post('/api/desktop/poll',
                              json={'results': []},
                              headers={'X-Bridge-Secret': 'tofu-nope'})
        assert r.status_code == 401

    def test_missing_credential_is_rejected(self, flask_client):
        r = flask_client.post('/api/desktop/poll', json={'results': []},
                              scope_base={'client': ('127.0.0.1', 5555)})
        assert r.status_code == 401

    def test_in_process_agent_token_accepted(self, flask_client):
        """打包托盘 app 的同进程 agent 走进程内 token(不落盘、不进 env)。"""
        from lib.bridge_auth import process_agent_token
        r = flask_client.post(
            '/api/desktop/poll', json={
                'results': [], 'agent': {'agent_id': 'process-agent'}},
            headers={'X-Bridge-Secret': process_agent_token()},
            scope_base={'client': ('127.0.0.1', 5555)})
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════
#  remote 绑定校验 + agent_run 别名
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRemoteBindingValidation:
    @pytest.fixture(autouse=True)
    def _master_on(self, monkeypatch):
        monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')

    def _bind(self, agent_id='agent-A', root='myapp', user_id=''):
        from lib.desktop.remote import validate_remote_binding
        return validate_remote_binding(agent_id, root, user_id=user_id)

    def test_ok(self):
        _register('agent-A', roots=[{'name': 'myapp', 'path': '/code/myapp'}])
        binding, error = self._bind()
        assert error is None
        assert binding == {'agent_id': 'agent-A', 'root': 'myapp'}

    def test_agent_offline(self):
        binding, error = self._bind(agent_id='ghost')
        assert binding is None and error and 'online' in error

    def test_root_not_declared(self):
        _register('agent-A', roots=[{'name': 'other', 'path': '/x'}])
        binding, error = self._bind(root='myapp')
        assert binding is None and error and 'myapp' in error

    def test_user_mismatch_refused(self):
        _register('agent-A', user_id='u-bob',
                  roots=[{'name': 'myapp', 'path': '/code/myapp'}])
        binding, error = self._bind(user_id='u-alice')
        assert binding is None and error

    def test_master_switch_off_refuses(self, monkeypatch):
        monkeypatch.delenv('TOFU_REMOTE_WORKTREE', raising=False)
        _register('agent-A', roots=[{'name': 'myapp', 'path': '/code/myapp'}])
        binding, error = self._bind()
        assert binding is None and error


@pytest.mark.unit
class TestAgentRunRemoteAlias:
    def test_alias_sets_binding(self, monkeypatch):
        monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
        _register('agent-A', roots=[{'name': 'myapp', 'path': '/code/myapp'}])
        from routes.api_v1.agent_run import _apply_remote_alias
        cfg, error = _apply_remote_alias({}, 'agent-A:myapp', user_id='')
        assert error is None
        assert cfg['project_remote'] == {'agent_id': 'agent-A', 'root': 'myapp'}

    def test_alias_bad_format(self):
        from routes.api_v1.agent_run import _apply_remote_alias
        cfg, error = _apply_remote_alias({}, 'no-colon', user_id='')
        assert cfg.get('project_remote') is None and error

    def test_alias_offline_agent_honest_error(self, monkeypatch):
        monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
        from routes.api_v1.agent_run import _apply_remote_alias
        cfg, error = _apply_remote_alias({}, 'ghost:myapp', user_id='')
        assert cfg.get('project_remote') is None
        assert error and 'online' in error


@pytest.mark.unit
class TestProjectEnabledDerivation:
    def test_remote_binding_implies_project_enabled(self, monkeypatch):
        monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
        from lib.tasks_pkg.model_config import _resolve_model_config
        out = _resolve_model_config(
            {'model': 'm',
             'project_remote': {'agent_id': 'a', 'root': 'r'}}, 'task-x')
        assert out['project_enabled'] is True
        assert out['project_path'] == ''

    def test_no_remote_no_project_byte_identical(self, monkeypatch):
        monkeypatch.delenv('TOFU_REMOTE_WORKTREE', raising=False)
        from lib.tasks_pkg.model_config import _resolve_model_config
        out = _resolve_model_config({'model': 'm'}, 'task-x')
        assert out['project_enabled'] is False


# ═══════════════════════════════════════════════════════════
#  执行链路 user 传递
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestUserThreading:
    def test_desktop_tool_passes_task_user(self, monkeypatch):
        import lib.desktop
        from lib.tasks_pkg.handlers.misc import _agents
        box = {}

        def fake_send(cmd_type, params=None, timeout=30,
                      target_agent_id=None, user_id=''):
            box['user_id'] = user_id
            return {'ok': True}, None

        monkeypatch.setattr(lib.desktop, 'send_desktop_command', fake_send)
        # finalize 链需要完整 task dict(events_lock 等)——与路由套件同范式,
        # 断言目标只是 user_id 抵达桥,patch 掉 finalize。
        import lib.tasks_pkg.handlers._adapter as adapter
        monkeypatch.setattr(adapter, '_finalize_tool_round', lambda *a: None)
        task = {'id': 't1', '_userId': 'u-alice'}
        _agents._handle_desktop_tool(task, {}, 'desktop_list_files',
                                     'tc-1', {'path': '~'}, 1,
                                     {'query': 'list my home'}, {}, '', False)
        assert box['user_id'] == 'u-alice'

    def test_remote_project_tool_passes_task_user(self, monkeypatch):
        import lib.desktop
        import lib.tasks_pkg.handlers.project as hp
        monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
        box = {}

        def fake_send(cmd_type, params=None, timeout=30,
                      target_agent_id=None, user_id=''):
            box['user_id'] = user_id
            return {'content': 'x'}, None

        monkeypatch.setattr(lib.desktop, 'send_desktop_command', fake_send)
        monkeypatch.setattr(hp, '_finalize_tool_round',
                            lambda *a: None)
        task = {'id': 't2', '_userId': 'u-alice', 'convId': 'c1'}
        hp._handle_project_tool(
            task, {}, 'list_dir', 'tc-1', {'path': '.'}, 1, {},
            {'project_remote': {'agent_id': 'agent-A', 'root': 'myapp'}},
            project_path='', project_enabled=False)
        assert box['user_id'] == 'u-alice'


# ═══════════════════════════════════════════════════════════
#  status 端点按用户过滤
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestStatusUserFilter:
    def test_status_scopes_agents_to_caller(self, flask_client):
        from lib.api_keys import create_key
        _row, token = create_key(
            owner_user_id=101, name='status-viewer', scopes=['chat'])
        _register('agent-A', user_id='101', name='mac')
        _register('agent-B', user_id='202', name='win')
        r = flask_client.get('/api/v1/desktop/status',
                             headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 200
        agents = r.get_json()['agents']
        assert [a['agent_id'] for a in agents] == ['agent-A']
