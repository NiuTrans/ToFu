"""tests/test_desktop_egress.py — desktop egress 路由层 + agent 执行器 + 刷新 singleflight 守卫（S2）。

Covers:
  * 域名白名单（精确 host 匹配，防 evil.com 后缀）
  * route_request 三态探测（ok/geo_blocked/network_fail）+ agent 选择
    （egress capability 过滤、user_id 租户隔离、'' 用户 legacy 回落、
    无 agent → EgressUnavailable）
  * egress_http 结果适配（status/json 与 requests.Response 同形）
  * agent 侧 cmd_egress_http（白名单再校验、proxy_mode 解析、结果形状）
  * OS 代理发现（winreg / scutil 两条路径）
  * bridge 按命令 TTL（cmd['ttl'] 覆盖全局 90s）
  * claude/codex 刷新 singleflight（同 token 并发刷新合并为一次上游调用）
  * claude_exchange_code 在直连 403 时经 egress 落库

Failure-first：全部在 S2 实现前红（lib/desktop/egress.py 不存在）。
"""

from __future__ import annotations

import base64
import json
import threading
import time
import unittest
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

from lib.desktop import egress
from lib.desktop.egress import EgressUnavailable


def _agent(agent_id='a1', user_id='', egress_cap=True, name='box'):
    return {
        'agent_id': agent_id, 'name': name, 'platform': 'win32',
        'capabilities': {'egress': egress_cap},
        'user_id': user_id, 'last_seen': time.time(),
    }


class TestWhitelist(unittest.TestCase):

    def test_allowed_hosts(self):
        for u in ('https://api.anthropic.com/v1/messages',
                  'https://console.anthropic.com/v1/oauth/token',
                  'https://auth.openai.com/oauth/token',
                  'https://chatgpt.com/backend-api/codex/responses'):
            self.assertTrue(egress.host_allowed(u), u)

    def test_suffix_attack_rejected(self):
        for u in ('https://api.anthropic.com.evil.com/x',
                  'https://chatgpt.com.attacker.io/',
                  'https://notanthropic.com/v1/messages'):
            self.assertFalse(egress.host_allowed(u), u)


class TestRouteRequest(unittest.TestCase):

    def _agents(self, agents):
        # Patch the bridge-level source so the REAL _online_egress_agents
        # filtering (egress capability + tenant scope) actually runs.
        return mock.patch('lib.desktop.online_agents', return_value=agents)

    def test_probe_ok_routes_direct(self):
        with mock.patch.object(egress, '_probe_host', return_value='ok'):
            self.assertEqual(egress.route_request(
                'https://api.anthropic.com/v1/messages', user_id='u1'), 'direct')

    def test_geo_block_without_agent_raises(self):
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([]):
            with self.assertRaises(EgressUnavailable):
                egress.route_request('https://api.anthropic.com/v1/messages',
                                     user_id='u1')

    def test_geo_block_with_agent_returns_egress_target(self):
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([_agent(user_id='u1')]):
            target = egress.route_request('https://api.anthropic.com/v1/messages',
                                          user_id='u1')
        self.assertEqual(target, 'a1')

    def test_network_fail_also_routes_to_agent(self):
        with mock.patch.object(egress, '_probe_host', return_value='network_fail'), \
             self._agents([_agent(user_id='u1')]):
            self.assertEqual(egress.route_request('https://api.anthropic.com/v1/x', user_id='u1'), 'a1')

    def test_non_egress_agent_not_selected(self):
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([_agent(egress_cap=False)]):
            with self.assertRaises(EgressUnavailable):
                egress.route_request('https://api.anthropic.com/v1/x', user_id='u1')

    def test_tenant_isolation(self):
        # u2's agent must not serve u1's egress.
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([_agent(user_id='u2')]):
            with self.assertRaises(EgressUnavailable):
                egress.route_request('https://api.anthropic.com/v1/x', user_id='u1')

    def test_legacy_empty_user_any_agent(self):
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([_agent(user_id='')]):
            self.assertEqual(egress.route_request('https://api.anthropic.com/v1/x', user_id=''), 'a1')

    def test_probe_result_cached_per_host(self):
        # Unique host per run — the module cache is process-global. The cache
        # lives INSIDE _probe_host, so count the http calls, not the wrapper.
        ok_resp = mock.Mock(status_code=401)
        egress._probe_cache.invalidate('api.anthropic.com')
        with mock.patch('lib.http_client.http_post', return_value=ok_resp) as hp:
            egress.route_request('https://api.anthropic.com/a', user_id='')
            egress.route_request('https://api.anthropic.com/b', user_id='')
        self.assertEqual(hp.call_count, 1)

    def test_multi_agent_requires_pinned_choice(self):
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([_agent('a1', 'u1'), _agent('a2', 'u1', name='box2')]), \
             mock.patch.object(egress, '_pinned_agent', return_value='a2'):
            self.assertEqual(egress.route_request('https://api.anthropic.com/v1/x', user_id='u1'), 'a2')

    def test_multi_agent_unpinned_raises_with_guidance(self):
        with mock.patch.object(egress, '_probe_host', return_value='geo_blocked'), \
             self._agents([_agent('a1', 'u1'), _agent('a2', 'u1', name='box2')]), \
             mock.patch.object(egress, '_pinned_agent', return_value=''):
            with self.assertRaises(EgressUnavailable) as ctx:
                egress.route_request('https://api.anthropic.com/v1/x', user_id='u1')
        self.assertIn('oauth_egress_agent_id', str(ctx.exception))


class TestEgressHttp(unittest.TestCase):

    def test_whitelist_enforced(self):
        with self.assertRaises(EgressUnavailable):
            egress.egress_http('https://evil.com/x', user_id='u1')

    def test_happy_path_result_shape(self):
        payload = json.dumps({'access_token': 'tok-1', 'expires_in': 3600}).encode()
        agent_result = {
            'status': 200,
            'headers': {'content-type': 'application/json'},
            'body_b64': base64.b64encode(payload).decode(),
            'elapsed_ms': 42,
        }
        with mock.patch.object(egress, 'route_request', return_value='a1'), \
             mock.patch('lib.desktop.send_desktop_command',
                        return_value=(agent_result, None)) as send:
            resp = egress.egress_http(
                'https://api.anthropic.com/v1/oauth/token',
                method='POST', headers={'Content-Type': 'application/json'},
                body=b'{"x":1}', timeout=30, user_id='u1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['access_token'], 'tok-1')
        # 命令按设计带 TTL + 目标 agent + user 作用域
        _args, kwargs = send.call_args
        self.assertEqual(kwargs.get('target_agent_id'), 'a1')
        self.assertEqual(kwargs.get('user_id'), 'u1')
        self.assertEqual(kwargs.get('ttl'), 120)
        # body 走 base64
        params = send.call_args[0][1]
        self.assertEqual(base64.b64decode(params['body_b64']), b'{"x":1}')

    def test_agent_network_error_raises_unavailable(self):
        with mock.patch.object(egress, 'route_request', return_value='a1'), \
             mock.patch('lib.desktop.send_desktop_command',
                        return_value=({'status': 0, 'error': 'DNS fail'}, None)):
            with self.assertRaises(EgressUnavailable):
                egress.egress_http('https://api.anthropic.com/x', user_id='')

    def test_bridge_error_raises_unavailable(self):
        with mock.patch.object(egress, 'route_request', return_value='a1'), \
             mock.patch('lib.desktop.send_desktop_command',
                        return_value=(None, 'Desktop agent timeout')):
            with self.assertRaises(EgressUnavailable):
                egress.egress_http('https://api.anthropic.com/x', user_id='')


class TestAgentExecutor(unittest.TestCase):
    """agent 侧 cmd_egress_http（lib/desktop_agent/_egress.py）。"""

    def test_agent_side_whitelist(self):
        from lib.desktop_agent._egress import cmd_egress_http
        out = cmd_egress_http({'url': 'https://evil.com/x', 'method': 'GET'})
        self.assertIn('error', out)

    def test_executor_result_shape(self):
        from lib.desktop_agent import _egress as ag
        fake = mock.Mock(status_code=200,
                         headers={'content-type': 'application/json',
                                  'set-cookie': 'secret=1'},
                         content=b'{"ok":true}')
        fake.elapsed = mock.Mock(total_seconds=lambda: 0.05)
        with mock.patch('lib.desktop_agent._egress.requests.request',
                        return_value=fake):
            out = ag.cmd_egress_http({
                'url': 'https://api.anthropic.com/v1/messages',
                'method': 'POST', 'headers': {'x': 'y'},
                'body_b64': base64.b64encode(b'{}').decode(),
                'timeout_ms': 5000, 'proxy_mode': 'env'})
        self.assertEqual(out['status'], 200)
        self.assertEqual(base64.b64decode(out['body_b64']), b'{"ok":true}')
        self.assertNotIn('set-cookie', out['headers'])  # cookie 剥离

    def test_executor_network_error_status0(self):
        from lib.desktop_agent import _egress as ag
        with mock.patch('lib.desktop_agent._egress.requests.request',
                        side_effect=ConnectionError('refused')):
            out = ag.cmd_egress_http({
                'url': 'https://api.anthropic.com/x', 'method': 'GET',
                'proxy_mode': 'env'})
        self.assertEqual(out['status'], 0)
        self.assertIn('error', out)

    def test_direct_mode_bypasses_proxy(self):
        from lib.desktop_agent import _egress as ag
        captured = {}
        def _fake(method, url, **kw):
            captured.update(kw)
            m = mock.Mock(status_code=200, headers={}, content=b'')
            m.elapsed = mock.Mock(total_seconds=lambda: 0.01)
            return m
        with mock.patch('lib.desktop_agent._egress.requests.request', _fake):
            ag.cmd_egress_http({'url': 'https://api.anthropic.com/x',
                                'method': 'GET', 'proxy_mode': 'direct'})
        self.assertEqual(captured.get('proxies'), {'no_proxy': '*'})


class TestOSProxyDiscovery(unittest.TestCase):

    def test_windows_registry_proxy(self):
        from lib.desktop_agent import _egress as ag
        # 真实 winreg.QueryValueEx 返回 (value, type) 二元组。
        fake_reg = {
            ('ProxyEnable',): (1, 4),
            ('ProxyServer',): ('127.0.0.1:7890', 1),
        }
        fake_winreg = mock.Mock()
        fake_winreg.HKEY_CURRENT_USER = object()
        fake_winreg.OpenKey.return_value = object()
        fake_winreg.QueryValueEx.side_effect = (
            lambda _k, name: fake_reg[(name,)])
        with mock.patch.dict('sys.modules', {'winreg': fake_winreg}), \
             mock.patch('lib.desktop_agent._egress._IS_WINDOWS', True), \
             mock.patch('lib.desktop_agent._egress._IS_MACOS', False):
            self.assertEqual(ag._os_proxy_url(), 'http://127.0.0.1:7890')

    def test_macos_scutil_proxy(self):
        from lib.desktop_agent import _egress as ag
        scutil_out = ('<dictionary> {\n  HTTPEnable : 1\n'
                      '  HTTPProxy : 127.0.0.1\n  HTTPPort : 7897\n'
                      '  HTTPSEnable : 1\n  HTTPSProxy : 127.0.0.1\n'
                      '  HTTPSPort : 7897\n}\n')
        with mock.patch('lib.desktop_agent._egress._IS_WINDOWS', False), \
             mock.patch('lib.desktop_agent._egress._IS_MACOS', True), \
             mock.patch('lib.desktop_agent._egress.subprocess.run') as run:
            run.return_value = mock.Mock(returncode=0, stdout=scutil_out)
            self.assertEqual(ag._os_proxy_url(), 'http://127.0.0.1:7897')

    def test_no_system_proxy_returns_empty(self):
        from lib.desktop_agent import _egress as ag
        with mock.patch('lib.desktop_agent._egress._IS_WINDOWS', False), \
             mock.patch('lib.desktop_agent._egress._IS_MACOS', False):
            self.assertEqual(ag._os_proxy_url(), '')


class TestBridgeTTL(unittest.TestCase):

    def test_per_command_ttl_override(self):
        from lib.desktop import bridge
        now = time.time()
        cmd = {'id': 'c1', 'type': 'egress_http', 'params': {},
               'created_at': now - 100,  # 超过全局 90s
               'event': threading.Event(), 'result': None, 'error': None,
               'ttl': 120}
        with bridge.command_queue_lock:
            bridge.command_queue['c1'] = cmd
        try:
            pending = bridge.take_pending_commands()
            self.assertEqual([c['id'] for c in pending], ['c1'])
        finally:
            with bridge.command_queue_lock:
                bridge.command_queue.pop('c1', None)

    def test_default_ttl_still_90(self):
        from lib.desktop import bridge
        now = time.time()
        cmd = {'id': 'c2', 'type': 'desktop_list_files', 'params': {},
               'created_at': now - 100,
               'event': threading.Event(), 'result': None, 'error': None}
        with bridge.command_queue_lock:
            bridge.command_queue['c2'] = cmd
        try:
            pending = bridge.take_pending_commands()
            self.assertEqual(pending, [])
            self.assertEqual(cmd['error'], 'Command expired (stale cleanup)')
        finally:
            with bridge.command_queue_lock:
                bridge.command_queue.pop('c2', None)


class TestRefreshSingleflight(unittest.TestCase):

    def test_concurrent_refresh_merges_to_one_upstream_call(self):
        from lib.oauth import token_store
        calls = {'n': 0}
        stored = {'expire': 0, 'refresh_token': 'r0', 'access_token': 'old'}

        def fake_refresh(rt):
            calls['n'] += 1
            time.sleep(0.05)  # 放大竞态窗口
            stored.update({'access_token': 'new', 'refresh_token': 'r1',
                           'expire': time.time() + 3600})
            return dict(stored)

        results = []
        def worker():
            results.append(token_store.refresh_singleflight(
                'codex', 'r0', fake_refresh,
                load=lambda: dict(stored)))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(calls['n'], 1)
        self.assertTrue(all(r and r['access_token'] == 'new' for r in results))

    def test_singleflight_passes_through_failure(self):
        from lib.oauth import token_store
        with mock.patch('lib.oauth.token_store.load_token', return_value=None):
            out = token_store.refresh_singleflight(
                'codex', 'r0', lambda rt: None, load=lambda: None)
        self.assertIsNone(out)


class TestExchangeViaEgress(unittest.TestCase):

    def test_claude_exchange_falls_back_to_egress_on_geo_block(self):
        from lib.oauth import claude
        geo = mock.Mock(status_code=403,
                        text='{"error":{"type":"forbidden","message":"Request not allowed"}}')
        geo.json.return_value = {'error': {'type': 'forbidden'}}
        egress_resp = mock.Mock(status_code=200)
        egress_resp.json.return_value = {
            'access_token': 'sk-ant-oat01-NEW', 'refresh_token': 'r1',
            'expires_in': 28800,
        }
        egress_resp.text = json.dumps(egress_resp.json.return_value)
        with mock.patch('lib.oauth.claude.http_post', return_value=geo), \
             mock.patch('lib.desktop.egress.egress_http', return_value=egress_resp) as eg, \
             mock.patch('lib.oauth.claude.save_token', return_value=True) as save, \
             mock.patch('lib.desktop.egress.route_request', return_value='a1'):
            out = claude.claude_exchange_code('code-1', 'verifier-1', state='s')
        self.assertEqual(out['access_token'], 'sk-ant-oat01-NEW')
        self.assertTrue(save.called)
        self.assertTrue(eg.called)


if __name__ == '__main__':
    unittest.main()
