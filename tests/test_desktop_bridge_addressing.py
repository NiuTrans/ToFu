"""tests/test_desktop_bridge_addressing.py — RWA P0:bridge agent 身份与寻址.

docs/REMOTE_WORKTREE_DESIGN.md §5 P0 + §3.2 硬约束②(owner 2026-07-25 拍板②A):
  * poll v2 注册帧:agent 上报 agent_id/机器名/平台/能力位,服务端注册表 + 心跳;
  * 命令按 ``target_agent_id`` 寻址投递,绝不跨 agent 错投;
  * 未寻址命令:唯一在线 agent 才收(单 agent 回退档,字节不变);
    多 agent 在线 → 入队即拒,模型收到诚实错(挂起不猜);
  * v1 旧 agent(无注册帧)行为字节不变;
  * ``TOFU_DESKTOP_ADDRESSING=0`` kill switch → 整体回退 legacy 不过滤。

桥机制(2026-07 盘上实况):``command_queue`` 是 dict,命令携 per-cmd
threading.Event;``take_pending_commands`` 返回 wire 投影
``{id, type, params}``(不弹队列,生产者超时自清理);重复投递由
「resolve 先于 drain」与单 agent 串行执行天然防住,多 agent 场景正是
本 P0 要堵的洞。

Run:  pytest tests/test_desktop_bridge_addressing.py -m unit -v
"""

from __future__ import annotations

import threading
import time

import pytest

from lib.desktop import bridge as db


def _run_async(coro):
    """Drive a coroutine on a private loop (repo has no pytest-asyncio)."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _clean_bridge(monkeypatch):
    """Reset every piece of bridge module state; restore scalars after."""
    monkeypatch.setenv('TOFU_DESKTOP_ADDRESSING', '1')
    monkeypatch.setattr(db, '_last_poll', [0.0])
    monkeypatch.setattr(db, '_v1_last_poll', 0.0)
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
    with db._async_waiters_lock:
        db._async_waiters.clear()
    yield
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
    with db._async_waiters_lock:
        db._async_waiters.clear()


def _register(agent_id, name='macbook', platform='darwin', **caps):
    meta = {'name': name, 'platform': platform}
    if caps:
        meta['capabilities'] = caps
    db.register_agent(agent_id, meta)


def _plant(cmd_id='cmd-1', cmd_type='desktop_list_files', target=None):
    """直接种一条未决命令进队列(绕过生产者的阻塞等待生命周期)."""
    cmd = {
        'id': cmd_id,
        'type': cmd_type,
        'params': {'path': '~'},
        'created_at': time.time(),
        'event': threading.Event(),
        'result': None,
        'error': None,
    }
    if target:
        cmd['target_agent_id'] = target
    with db.command_queue_lock:
        db.command_queue[cmd_id] = cmd
    return cmd


def _drain(agent_id=None, v1=True, timeout=0.3):
    return _run_async(db.take_pending_commands_async(
        timeout=timeout, agent_id=agent_id, v1=v1))


# ═══════════════════════════════════════════════════════════
#  注册表与心跳
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAgentRegistry:
    def test_register_stores_meta_and_marks_online(self):
        _register('agent-A', name='macbook', write=True, exec=False)
        online = db.online_agents()
        assert [a['agent_id'] for a in online] == ['agent-A']
        a = online[0]
        assert a['name'] == 'macbook'
        assert a['platform'] == 'darwin'
        assert a['capabilities'] == {'write': True, 'exec': False}
        assert db.list_agents()[0]['online'] is True

    def test_heartbeat_refreshes_last_seen(self):
        _register('agent-A')
        with db.command_queue_lock:
            db._agents['agent-A']['last_seen'] = time.time() - 10
        _register('agent-A')  # heartbeat = re-register with fresh frame
        assert db.online_agents()[0]['last_seen'] > time.time() - 5

    def test_stale_agent_drops_out_of_online(self):
        _register('agent-A')
        with db.command_queue_lock:
            db._agents['agent-A']['last_seen'] = time.time() - 3600
        assert db.online_agents() == []
        assert db.list_agents()[0]['online'] is False

    def test_register_counts_toward_connected(self):
        assert not db.is_desktop_agent_connected()
        _register('agent-A')
        assert db.is_desktop_agent_connected()

    def test_register_stores_version_and_projects_it(self):
        """The drift signal (owner amendment ②): the frame's version must
        survive registration and reach the status projection."""
        db.register_agent('agent-A', {'name': 'mac', 'version': '0.16.0'})
        a = db.online_agents()[0]
        assert a['version'] == '0.16.0'
        assert db.list_agents()[0]['version'] == '0.16.0'

    def test_a_versionless_frame_keeps_the_previous_version(self):
        """An older agent (frame without version) must not erase what a
        newer registration recorded — heartbeat fallback, same as name."""
        db.register_agent('agent-A', {'name': 'mac', 'version': '0.16.0'})
        db.register_agent('agent-A', {'name': 'mac2'})
        assert db.online_agents()[0]['version'] == '0.16.0'


# ═══════════════════════════════════════════════════════════
#  寻址投递
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAddressedDelivery:
    def test_addressed_command_reaches_only_target(self):
        _register('agent-A', name='mac')
        _register('agent-B', name='win')
        _plant(target='agent-A')
        assert _drain('agent-B', v1=False) == []
        cmds = _drain('agent-A', v1=False)
        assert [c['type'] for c in cmds] == ['desktop_list_files']
        assert cmds[0]['target_agent_id'] == 'agent-A'
        # wire 投影仍不泄漏 event/result 等内部字段
        assert set(cmds[0].keys()) == {'id', 'type', 'params', 'target_agent_id'}

    def test_addressed_to_offline_agent_refused_at_enqueue(self):
        result, error = db.send_desktop_command(
            'desktop_list_files', {'path': '~'}, timeout=0.2,
            target_agent_id='ghost')
        assert result is None and error and 'ghost' in error
        assert db.pending_commands_count() == 0

    def test_v1_agent_never_receives_addressed(self):
        _register('agent-A')
        _plant(target='agent-A')
        assert _drain(None, v1=True) == []
        assert _drain('agent-A', v1=False)[0]['target_agent_id'] == 'agent-A'

    def test_addressed_result_roundtrip(self):
        _register('agent-A')
        outcome = {}

        def producer():
            outcome['r'], outcome['e'] = db.send_desktop_command(
                'desktop_list_files', {'path': '~'}, timeout=5,
                target_agent_id='agent-A')

        t = threading.Thread(target=producer)
        t.start()
        time.sleep(0.2)
        cmds = _drain('agent-A', v1=False, timeout=2)
        assert len(cmds) == 1
        db.resolve_results([{'id': cmds[0]['id'],
                             'result': {'entries': []}, 'error': None}])
        t.join(timeout=3)
        assert outcome['e'] is None
        assert outcome['r'] == {'entries': []}


# ═══════════════════════════════════════════════════════════
#  未寻址命令:回退档与拒发
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestUnaddressed:
    def test_multi_agent_online_refused_at_enqueue(self):
        _register('agent-A', name='mac')
        _register('agent-B', name='win')
        result, error = db.send_desktop_command(
            'desktop_list_files', {'path': '~'}, timeout=0.2)
        assert result is None
        assert error and ('mac' in error or 'agent-A' in error)
        # 挂起不猜:命令绝不在队列里等某个幸运儿
        assert db.pending_commands_count() == 0

    def test_single_agent_fallback_delivered(self):
        _register('agent-A')
        _plant()
        cmds = _drain('agent-A', v1=False)
        assert [c['type'] for c in cmds] == ['desktop_list_files']
        # 回退档字节不变:未寻址命令不携带 target 字段
        assert 'target_agent_id' not in cmds[0]

    def test_zero_online_legacy_v1_drains(self):
        _plant()
        # 无 v2 注册的 legacy 世界:v1 轮询者照常收到
        assert _drain(None, v1=True)[0]['type'] == 'desktop_list_files'

    def test_v1_legacy_world_byte_compatible(self):
        """拍板②A:v1 agent(从不注册)收到的 wire 投影与 P0 之前逐键一致."""
        _plant()
        cmds = _drain(None, v1=True)
        assert len(cmds) == 1
        assert set(cmds[0].keys()) == {'id', 'type', 'params'}

    def test_v1_blocked_once_v2_online(self):
        """v2 注册后,v1 轮询者不再收到未寻址命令(多 agent 世界不许抢)."""
        _register('agent-A')
        _plant()
        assert _drain(None, v1=True) == []
        assert _drain('agent-A', v1=False)[0]['type'] == 'desktop_list_files'

    def test_second_agent_coming_online_holds_command(self):
        """零在线时入队的未寻址命令,两 agent 先后上线后谁也不许拿."""
        _plant()
        _register('agent-A')
        _register('agent-B')
        assert _drain('agent-A', v1=False) == []
        assert _drain('agent-B', v1=False) == []
        assert db.pending_commands_count() == 1


# ═══════════════════════════════════════════════════════════
#  kill switch
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestKillSwitch:
    def test_flag_off_restores_legacy_drain_all(self, monkeypatch):
        monkeypatch.setenv('TOFU_DESKTOP_ADDRESSING', '0')
        _register('agent-A')
        _register('agent-B')
        _plant(target='agent-A')
        # 不过滤:B 也能拿到(legacy 语义,明知故犯才关开关)
        assert _drain('agent-B', v1=False)[0]['type'] == 'desktop_list_files'

    def test_flag_off_refuses_targeting(self, monkeypatch):
        monkeypatch.setenv('TOFU_DESKTOP_ADDRESSING', '0')
        result, error = db.send_desktop_command(
            'desktop_list_files', {'path': '~'}, timeout=0.2,
            target_agent_id='agent-A')
        assert result is None and error
        assert db.pending_commands_count() == 0


# ═══════════════════════════════════════════════════════════
#  NEUTER — 证明两道闸承重
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNeuters:
    def test_neuter_deliverable_filter_steals_addressed(self, monkeypatch):
        """剥掉投递谓词(恒真)→ B 立刻偷走 A 的地址命令 = 过滤闸承重."""
        monkeypatch.setattr(db, '_deliverable', lambda *a: True)
        _register('agent-A')
        _register('agent-B')
        _plant(target='agent-A')
        stolen = _drain('agent-B', v1=False)
        assert [c['type'] for c in stolen] == ['desktop_list_files']

    def test_neuter_enqueue_guard_queues_unaddressed_multi(self, monkeypatch):
        """剥掉入队检查 → 多在线未寻址命令进队列等幸运儿 = 入队闸承重."""
        monkeypatch.setattr(db, '_addressing_enqueue_error',
                            lambda *a, **k: None)
        _register('agent-A')
        _register('agent-B')
        # 真实 send 会阻塞等待结果;趁它阻塞时断言命令已入队,再解决掉释放线程
        t = threading.Thread(target=db.send_desktop_command,
                             args=('desktop_list_files', {'path': '~'}),
                             kwargs={'timeout': 2})
        t.start()
        try:
            # 轮询等待入队(高负载机上固定 sleep 是竞态)
            deadline = time.time() + 2
            while db.pending_commands_count() == 0 and time.time() < deadline:
                time.sleep(0.02)
            assert db.pending_commands_count() == 1
        finally:
            with db.command_queue_lock:
                ids = list(db.command_queue.keys())
            db.resolve_results(
                [{'id': i, 'result': {}, 'error': None} for i in ids])
            t.join(timeout=3)


# ═══════════════════════════════════════════════════════════
#  poll 路由 e2e(v2 注册帧)
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestPollRouteV2:
    """poll 路由 e2e —— 测的是注册帧/寻址/wire 形状,不是认证。

    ⚠️ 每个请求都显式带 **凭据** 与 **scope_base**(B0,
    docs/UNIFIED_DEVICE_BRIDGE_DESIGN.md §3.2c):
      * 桥端点现在一律要求凭据,不看对端地址(同机反代下回环 == 全网);
      * Quart 进程内客户端缺省上报 ``'<local>'``,会被判成回环拿到
        open-mode 豁免 —— 不传 scope_base 就是在测豁免路径,断言无意义。
    这里用**进程内 agent token**,顺带实证那条通道真的可用。
    """

    @pytest.fixture(autouse=True)
    def _fast_long_poll(self, monkeypatch):
        monkeypatch.setattr(db, 'POLL_WAIT_TIMEOUT', 0.2)

    @staticmethod
    def _auth():
        from routes.api_v1.auth import loopback_agent_token
        return {'X-Bridge-Secret': loopback_agent_token()}

    _PEER = {'client': ('127.0.0.1', 5555)}

    def _poll(self, flask_client, body):
        return flask_client.post('/api/desktop/poll', json=body,
                                 headers=self._auth(), scope_base=self._PEER)

    def test_v2_poll_registers_agent(self, flask_client):
        r = self._poll(flask_client,
                       {'results': [],
                        'agent': {'agent_id': 'agent-C', 'name': 'pi',
                                  'platform': 'linux'}})
        assert r.status_code == 200
        assert r.get_json() == {'commands': []}
        online = db.online_agents()
        assert [a['agent_id'] for a in online] == ['agent-C']
        assert online[0]['name'] == 'pi'
        assert online[0]['platform'] == 'linux'

    def test_v2_poll_delivers_only_own_commands(self, flask_client):
        _register('agent-A', name='mac')
        _register('agent-B', name='win')
        _plant('cmd-a', 'desktop_list_files', target='agent-A')
        _plant('cmd-b', 'desktop_run_command', target='agent-B')
        rb = self._poll(flask_client,
                        {'results': [], 'agent': {'agent_id': 'agent-B'}})
        assert [c['type'] for c in rb.get_json()['commands']] == [
            'desktop_run_command']
        ra = self._poll(flask_client,
                        {'results': [], 'agent': {'agent_id': 'agent-A'}})
        assert [c['type'] for c in ra.get_json()['commands']] == [
            'desktop_list_files']

    def test_v1_poll_shape_unchanged(self, flask_client):
        _plant()
        r = self._poll(flask_client, {'results': []})
        assert r.status_code == 200
        cmds = r.get_json()['commands']
        assert len(cmds) == 1
        assert set(cmds[0].keys()) == {'id', 'type', 'params'}

    def test_poll_without_credential_is_rejected(self, flask_client):
        """B0: 桥端点凭据强制 —— 无凭据即 401,回环长相也不行。"""
        r = flask_client.post('/api/desktop/poll', json={'results': []},
                              scope_base=self._PEER)
        assert r.status_code == 401


# ═══════════════════════════════════════════════════════════
#  agent 侧:稳定身份 + 注册帧
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAgentSide:
    def _run_once(self, monkeypatch, tmp_path, captured):
        import lib.desktop_agent._run as ar

        class _Resp:
            status_code = 200

            def json(self):
                return {'commands': []}

        stop = threading.Event()

        def fake_post(url, json=None, headers=None, timeout=None, proxies=None):
            captured['body'] = json
            captured['headers'] = headers
            stop.set()
            return _Resp()

        monkeypatch.setattr(ar.requests, 'post', fake_post)
        monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'cfg.json'))
        ar.run_agent('http://server.example',
                     {'allow_write': True, 'allow_exec': False,
                      'allow_gui': False, 'allow_notification': True},
                     poll_interval=0.01, stop_event=stop)

    def test_agent_posts_registration_frame(self, monkeypatch, tmp_path):
        captured = {}
        self._run_once(monkeypatch, tmp_path, captured)
        frame = captured['body']['agent']
        assert frame['agent_id']
        assert frame['name']
        assert frame['platform']
        assert frame['capabilities'] == {
            'write': True, 'exec': False, 'gui': False, 'notification': True,
            'egress': False}

    def test_agent_id_stable_across_restarts(self, monkeypatch, tmp_path):
        first, second = {}, {}
        self._run_once(monkeypatch, tmp_path, first)
        self._run_once(monkeypatch, tmp_path, second)
        assert first['body']['agent']['agent_id']
        assert (first['body']['agent']['agent_id']
                == second['body']['agent']['agent_id'])


# ═══════════════════════════════════════════════════════════
#  status 端点带 agents
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestStatusEndpoint:
    def test_status_lists_agents(self, flask_client):
        _register('agent-A', name='mac')
        r = flask_client.get('/api/v1/desktop/status')
        assert r.status_code == 200
        body = r.get_json()
        assert 'agents' in body
        assert body['agents'][0]['agent_id'] == 'agent-A'
